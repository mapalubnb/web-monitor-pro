"""
内容提取和归一化

从 FetchResult 中提取"用于比对的稳定文本"，去除噪音。

提取策略优先级（HTML 类型）：
1. 任务指定 CSS 选择器 → 只提取匹配区域
2. 任务指定 json_path → 当页面内嵌 JSON 数据时按路径提取
3. 智能 SPA 数据提取（auto_spa_detect）
   - Next.js 的 <script id="__NEXT_DATA__">
   - Nuxt.js 的 window.__NUXT__ / __NUXT_DATA__
   - Apollo/GraphQL 的 __APOLLO_STATE__
   - window.__INITIAL_STATE__ / __INITIAL_DATA__ / __PRELOADED_STATE__
   - JSON-LD (<script type="application/ld+json">)
   - OpenGraph / Twitter Card meta 标签
4. trafilatura 抽正文
5. 兜底：整页 textContent

归一化：去多余空白、移除常见动态噪音（时间戳、nonce 等）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..db import Task
from ..logger import logger
from .engine import FetchResult


# ============================================================
# 正则：归一化时移除的噪音
# ============================================================
_NOISE_PATTERNS = [
    re.compile(r"csrf[_-]?token[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"nonce[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"build[Ii]d[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    # 32 位以上的 hash / 随机字符串（注意不误伤正常 hex 数据）
    re.compile(r"\b[a-f0-9]{40,}\b"),
    # ISO 时间戳
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"),
    # Unix 毫秒时间戳
    re.compile(r"\b1[5-9]\d{11}\b|\b2[0-1]\d{11}\b"),
]

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")

# 判定"空壳 HTML"（纯 SPA 框架模板，基本没文字）的阈值
MIN_TEXT_LENGTH_TO_BE_USEFUL = 120


# ============================================================
# 入口
# ============================================================
def extract(task: Task, result: FetchResult) -> str:
    """
    根据任务配置从抓取结果中提取用于比对的文本。

    Returns:
        归一化后的纯文本（可能为空串，表示没提取到内容）
    """
    content = result.content or ""
    if not content.strip():
        return ""

    content_type = (result.content_type or "").lower()
    task_type = (task.type or "html").lower()

    # ---- JSON 类型 ----
    if task_type == "json" or "json" in content_type:
        return _extract_json(content, task.json_path)

    # ---- Markdown（jina/firecrawl 返回）----
    if "markdown" in content_type or result.strategy_used in {"jina", "firecrawl"}:
        return _normalize_text(content)

    # ---- HTML 类型 ----
    return _extract_html(content, task)


# ============================================================
# HTML 提取（核心）
# ============================================================
def _extract_html(html: str, task: Task) -> str:
    """
    HTML 提取顺序：
    1. CSS 选择器（用户明确指定）
    2. 如果 extract_next_data=True 或 auto_spa_detect，尝试全部 SPA 数据嵌入点
    3. trafilatura 抽正文
    4. 如果上面都拿不到足够文本 → 探测并返回 JSON-LD / meta 元数据
    5. 兜底：整页 textContent
    """
    # 1) CSS selector（用户明确指定，最高优先级）
    if task.selector:
        text = _extract_by_selector(html, task.selector)
        if text and len(text) >= MIN_TEXT_LENGTH_TO_BE_USEFUL:
            logger.debug("🎯 通过 CSS 选择器 '{}' 提取到 {} 字", task.selector, len(text))
            return _normalize_text(text)
        logger.debug("⚠️  CSS 选择器 '{}' 匹配内容过少，继续尝试其他策略", task.selector)

    # 2) SPA 内嵌数据（Next/Nuxt/Apollo/INITIAL_STATE 等）
    #    任一命中，就直接返回
    if task.extract_next_data or _looks_like_spa_shell(html):
        data = _extract_spa_embedded_data(html)
        if data:
            logger.debug("🔍 SPA 内嵌数据提取成功（长度 {} 字）", len(data))
            return _normalize_text(data)

    # 3) trafilatura 正文
    text = _extract_main_content(html)
    if text and len(text) >= MIN_TEXT_LENGTH_TO_BE_USEFUL:
        logger.debug("📖 trafilatura 正文提取成功（{} 字）", len(text))
        return _normalize_text(text)

    # 4) 元数据兜底（JSON-LD / OpenGraph）
    meta_text = _extract_structured_meta(html)
    if meta_text and len(meta_text) >= MIN_TEXT_LENGTH_TO_BE_USEFUL:
        logger.debug("🏷️  结构化元数据提取（{} 字）", len(meta_text))
        return _normalize_text(meta_text)

    # 5) 最后：整页 textContent
    text = _html_to_text(html)
    normalized = _normalize_text(text)
    if len(normalized) >= MIN_TEXT_LENGTH_TO_BE_USEFUL:
        return normalized

    # 真的提不出来了
    if meta_text:
        # 宁可返回短的 meta 也比空的强
        return _normalize_text(meta_text)
    return normalized  # 可能是空串


# ============================================================
# SPA 空壳检测
# ============================================================
def _looks_like_spa_shell(html: str) -> bool:
    """
    判断 HTML 是否像 SPA 空壳（服务端只下发了壳子，内容靠 JS 渲染）。

    判断依据：
    - <body> 内的可见文本 < 阈值
    - 且存在 SPA 框架特征（如 id="root"、id="app"、__NEXT_DATA__ 等）
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        return False

    try:
        tree = HTMLParser(html)
    except Exception:
        return False

    # 文本量
    body = tree.body
    if body is None:
        return True
    # 先去掉 script/style 再算可见文本
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    visible_text = body.text(separator=" ", strip=True) if tree.body else ""

    # 特征点
    lower_html = html.lower()
    spa_markers = (
        'id="root"', "id='root'",
        'id="app"', "id='app'",
        "__next_data__", "__nuxt__", "__nuxt_data__",
        "__apollo_state__", "__initial_state__",
        "__preloaded_state__", "window.__data__",
        'data-reactroot', "data-server-rendered",
    )
    has_spa_marker = any(m in lower_html for m in spa_markers)

    return has_spa_marker and len(visible_text) < 400


# ============================================================
# SPA 内嵌数据提取（Next/Nuxt/Apollo/INITIAL_STATE 全覆盖）
# ============================================================
# 各种常见的"数据塞在 script 里"模式
_SPA_DATA_PATTERNS = [
    # Next.js
    (
        "next_data",
        re.compile(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        ),
    ),
    # Nuxt.js 3（JSON 形式）
    (
        "nuxt_data",
        re.compile(
            r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        ),
    ),
    # SvelteKit
    (
        "sveltekit_data",
        re.compile(
            r'<script[^>]*data-sveltekit-fetched[^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        ),
    ),
    # Remix
    (
        "remix_context",
        re.compile(
            r'window\.__remixContext\s*=\s*(\{.*?\});',
            re.DOTALL,
        ),
    ),
]

# "window.XXX = JSON" 这种 JS 赋值模式
_INLINE_JS_ASSIGNMENTS = [
    "__NUXT__",
    "__INITIAL_STATE__",
    "__INITIAL_DATA__",
    "__PRELOADED_STATE__",
    "__APOLLO_STATE__",
    "__REDUX_STATE__",
    "__DATA__",
    "INITIAL_STATE",
]


def _extract_spa_embedded_data(html: str) -> str | None:
    """
    依次尝试各种 SPA 框架的内嵌数据点。返回已 JSON 美化的文本。
    """
    collected: dict[str, Any] = {}

    # 1) id=xxx 的 script 标签
    for name, pat in _SPA_DATA_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        raw = m.group(1).strip()
        parsed = _try_parse_json(raw)
        if parsed is not None:
            collected[name] = _slim_data(parsed)

    # 2) window.__XX__ = {...} 这种赋值
    for var in _INLINE_JS_ASSIGNMENTS:
        parsed = _extract_js_assignment(html, var)
        if parsed is not None:
            collected[var] = _slim_data(parsed)

    # 3) JSON-LD（结构化数据，价格/产品/文章很常用）
    json_ld_blocks = _extract_all_json_ld(html)
    if json_ld_blocks:
        collected["json_ld"] = json_ld_blocks

    if not collected:
        return None

    try:
        return json.dumps(collected, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    except Exception as e:
        logger.debug("SPA 数据序列化失败: {}", e)
        return None


def _try_parse_json(raw: str) -> Any | None:
    """尝试把字符串当 JSON 解析（容忍前后空白）。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_js_assignment(html: str, var_name: str) -> Any | None:
    """
    从 HTML 里找 window.X = {...} 或 X = {...} 这种赋值的 JSON 值。

    使用花括号配对的启发式：找到 {，然后配对直到平衡。
    """
    # 匹配: window.VAR = { 或 VAR = { 或 var VAR = {
    pattern = re.compile(
        r"(?:window\.)?" + re.escape(var_name) + r"\s*=\s*(\{)",
    )
    m = pattern.search(html)
    if not m:
        return None

    start = m.end() - 1  # '{' 的位置
    end = _find_matching_brace(html, start)
    if end is None:
        return None

    json_str = html[start:end + 1]
    return _try_parse_json(json_str)


def _find_matching_brace(text: str, start: int) -> int | None:
    """从 start 位置（必须是 '{'）开始，找匹配的 '}'；考虑字符串和转义。"""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    str_char = ""
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
                continue
            if ch == str_char:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            str_char = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _extract_all_json_ld(html: str) -> list[Any]:
    """提取所有 <script type="application/ld+json"> 块。"""
    results: list[Any] = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        parsed = _try_parse_json(m.group(1))
        if parsed is not None:
            results.append(parsed)
    return results


def _slim_data(data: Any, max_depth: int = 6) -> Any:
    """
    精简数据：移除明显的噪音字段（构建 id、hash、session token 等）。
    """
    if max_depth <= 0:
        return "[TOO_DEEP]"

    if isinstance(data, dict):
        noisy_keys = {
            "buildId", "build_id", "sessionId", "session_id",
            "requestId", "request_id", "trace_id", "traceId",
            "__SSR__", "timestamp", "ts", "time", "_t",
            "nonce", "csrf_token", "csrfToken",
        }
        return {
            k: _slim_data(v, max_depth - 1)
            for k, v in data.items()
            if k not in noisy_keys
        }
    if isinstance(data, list):
        return [_slim_data(item, max_depth - 1) for item in data]
    return data


# ============================================================
# OpenGraph / Twitter Card / title / meta description 兜底
# ============================================================
def _extract_structured_meta(html: str) -> str:
    """
    提取页面的结构化元数据，包括：
    - <title>
    - <meta name="description" content="...">
    - <meta property="og:*" content="...">
    - <meta name="twitter:*" content="...">
    - <h1>, <h2>（保留结构感）

    这些通常是服务端渲染的，即使 SPA 壳也会有。
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        return ""

    try:
        tree = HTMLParser(html)
    except Exception:
        return ""

    lines: list[str] = []

    # title
    if tree.head is not None:
        title_node = tree.css_first("head title")
        if title_node:
            t = title_node.text(strip=True)
            if t:
                lines.append(f"[title] {t}")

    # meta
    for meta in tree.css("head meta"):
        name = (meta.attributes.get("name") or meta.attributes.get("property") or "").strip()
        content = (meta.attributes.get("content") or "").strip()
        if name and content:
            lines.append(f"[meta:{name}] {content}")

    # h1 / h2
    for tag in ("h1", "h2"):
        for node in tree.css(tag):
            t = node.text(strip=True)
            if t:
                lines.append(f"[{tag}] {t}")

    return "\n".join(lines)


# ============================================================
# 用户显式指定的 CSS 选择器
# ============================================================
def _extract_by_selector(html: str, selector: str) -> str:
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        logger.warning("selectolax 未安装，跳过选择器提取")
        return ""
    try:
        tree = HTMLParser(html)
        nodes = tree.css(selector)
        texts = [n.text(separator="\n", strip=True) for n in nodes]
        return "\n\n".join(t for t in texts if t)
    except Exception as e:
        logger.warning("selectolax 解析失败: {}", e)
        return ""


# ============================================================
# trafilatura 正文提取
# ============================================================
def _extract_main_content(html: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
        return text or ""
    except Exception as e:
        logger.debug("trafilatura 提取失败: {}", e)
        return ""


# ============================================================
# 兜底：整页 textContent
# ============================================================
def _html_to_text(html: str) -> str:
    try:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        body = tree.body
        if body is None:
            return ""
        return body.text(separator="\n", strip=True)
    except Exception:
        text = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return text


# ============================================================
# JSON 提取
# ============================================================
def _extract_json(content: str, json_path: str | None) -> str:
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return content.strip()

    if not json_path:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)

    collected: list[Any] = []
    for raw in json_path.split(","):
        path = raw.strip()
        if not path:
            continue
        try:
            collected.append({path: _eval_json_path(obj, path)})
        except Exception as e:
            logger.debug("json_path '{}' 提取失败: {}", path, e)

    if not collected:
        return ""
    return json.dumps(collected, ensure_ascii=False, sort_keys=True, indent=2)


def _eval_json_path(obj: Any, path: str) -> Any:
    tokens = _tokenize_path(path)
    return _walk(obj, tokens)


def _tokenize_path(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for part in path.split("."):
        m = re.match(r"([^\[]+)((?:\[[^\]]+\])*)", part)
        if not m:
            continue
        key, indexes = m.group(1), m.group(2)
        if key:
            tokens.append(key)
        for idx_match in re.finditer(r"\[([^\]]+)\]", indexes):
            idx = idx_match.group(1)
            if idx == "*":
                tokens.append("*")
            else:
                try:
                    tokens.append(int(idx))
                except ValueError:
                    tokens.append(idx)
    return tokens


def _walk(obj: Any, tokens: list[str | int]) -> Any:
    if not tokens:
        return obj
    head, *rest = tokens
    if head == "*":
        if isinstance(obj, list):
            return [_walk(item, rest) for item in obj]
        return None
    if isinstance(head, int):
        if isinstance(obj, list) and 0 <= head < len(obj):
            return _walk(obj[head], rest)
        return None
    if isinstance(head, str) and isinstance(obj, dict):
        return _walk(obj.get(head), rest)
    return None


# ============================================================
# 归一化
# ============================================================
def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    for pattern in _NOISE_PATTERNS:
        text = pattern.sub("[DYNAMIC]", text)

    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)

    return text.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# Debug 诊断（给 /debug 命令用）
# ============================================================
def diagnose_html(html: str) -> dict[str, Any]:
    """
    分析一段 HTML，告诉用户：
    - 页面是哪种框架（Next/Nuxt/SPA 壳/SSR 等）
    - 命中了哪些数据嵌入点
    - 可见文本长度
    - 建议策略
    """
    lower = html.lower()
    findings: dict[str, Any] = {
        "html_size": len(html),
        "frameworks": [],
        "data_points": [],
        "visible_text_length": 0,
        "suggestions": [],
    }

    # 框架识别
    if "__next_data__" in lower or 'id="__next"' in lower:
        findings["frameworks"].append("Next.js")
    if "__nuxt__" in lower or "__nuxt_data__" in lower or "data-server-rendered" in lower:
        findings["frameworks"].append("Nuxt.js / Vue SSR")
    if "data-reactroot" in lower:
        findings["frameworks"].append("React SSR")
    if "__svelte" in lower or "data-sveltekit" in lower:
        findings["frameworks"].append("SvelteKit")
    if "__remix" in lower:
        findings["frameworks"].append("Remix")
    if "cloudflare" in lower or "cf-ray" in lower or "cdn-cgi" in lower:
        findings["frameworks"].append("Cloudflare 保护")
    if not findings["frameworks"] and ('id="root"' in lower or 'id="app"' in lower):
        findings["frameworks"].append("纯客户端 SPA 壳（数据靠 JS 异步加载）")

    # 数据嵌入点
    for name, pat in _SPA_DATA_PATTERNS:
        if pat.search(html):
            findings["data_points"].append(f"✅ {name}")
    for var in _INLINE_JS_ASSIGNMENTS:
        if _extract_js_assignment(html, var) is not None:
            findings["data_points"].append(f"✅ window.{var}")
    if _extract_all_json_ld(html):
        findings["data_points"].append("✅ JSON-LD")

    # 可见文本
    visible = _html_to_text(html)
    findings["visible_text_length"] = len(visible)

    # 建议
    if findings["data_points"]:
        findings["suggestions"].append(
            "该页面内嵌了结构化数据，当前提取策略应该能拿到（请检查 `/check <id>` 后的快照）"
        )
    elif "Cloudflare 保护" in findings["frameworks"] and findings["visible_text_length"] < 400:
        findings["suggestions"].append(
            "疑似 Cloudflare 挑战页，建议：① `--impersonate chrome124/firefox133` 换指纹 "
            "② 尝试 `--strategy jina` 用外部渲染兜底"
        )
    elif findings["visible_text_length"] < 400 and any("SPA" in f for f in findings["frameworks"]):
        findings["suggestions"].append(
            "纯客户端 SPA，HTML 里没数据。强烈建议：① `/sniff <url>` 找内部 API 用 `--type json` 监控 "
            "② 或 `--strategy jina` 让外部服务帮你渲染"
        )
    else:
        findings["suggestions"].append(
            f"抓取正常，可见文本 {findings['visible_text_length']} 字"
        )

    return findings
