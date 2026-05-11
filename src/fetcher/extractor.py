"""
内容提取和归一化

HTML 提取顺序：
1. 用户指定 CSS 选择器
2. SPA 嵌入数据（Next.js / Nuxt.js / Apollo / window.__X__ / JSON-LD）
3. trafilatura 正文
4. meta 结构化数据兜底
5. 整页纯文本

归一化：去空白、去噪音（时间戳、nonce、hash）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..db import Task
from ..logger import logger
from .engine import FetchResult

# 归一化时移除的噪音（时间戳 / csrf / nonce / 长 hash）
_NOISE_PATTERNS = [
    re.compile(r"csrf[_-]?token[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"nonce[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"build[Ii]d[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"\b[a-f0-9]{40,}\b"),                       # 长 hex hash
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\S*"),  # ISO 时间戳
    re.compile(r"\b1[5-9]\d{11}\b|\b2[0-1]\d{11}\b"),       # Unix ms 时间戳
]
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")

MIN_USEFUL_LENGTH = 120


# ============================================================
# 入口
# ============================================================
def extract(task: Task, result: FetchResult) -> str:
    """从 FetchResult 提取用于比对的文本（已归一化）。"""
    content = result.content or ""
    if not content.strip():
        return ""

    content_type = (result.content_type or "").lower()
    task_type = (task.type or "html").lower()

    # JSON
    if task_type == "json" or "json" in content_type:
        return _extract_json(content, task.json_path)

    # Markdown（Jina / Firecrawl）
    if "markdown" in content_type or result.strategy_used in ("jina", "firecrawl"):
        return _normalize(_strip_jina_warnings(content))

    # HTML
    return _extract_html(content, task)


# ============================================================
# HTML
# ============================================================
def _extract_html(html: str, task: Task) -> str:
    # 1) CSS 选择器
    if task.selector:
        text = _by_selector(html, task.selector)
        if text and len(text) >= MIN_USEFUL_LENGTH:
            return _normalize(text)

    # 2) SPA 嵌入数据
    if task.extract_next_data or _looks_like_spa_shell(html):
        data = _extract_spa_data(html)
        if data:
            return _normalize(data)

    # 3) trafilatura 正文
    text = _main_content(html)
    if text and len(text) >= MIN_USEFUL_LENGTH:
        return _normalize(text)

    # 4) meta 元数据
    meta = _extract_meta(html)
    if meta and len(meta) >= MIN_USEFUL_LENGTH:
        return _normalize(meta)

    # 5) 兜底：整页纯文本
    return _normalize(_html_to_text(html)) or _normalize(meta)


def _looks_like_spa_shell(html: str) -> bool:
    """body 文本少 + 有 SPA 特征 = 空壳，应尝试提取嵌入数据。"""
    lower = html.lower()
    spa_markers = (
        'id="root"', 'id="app"', 'id="__next"',
        "__next_data__", "__nuxt__", "__nuxt_data__",
        "__apollo_state__", "__initial_state__", "__preloaded_state__",
        "__redux_state__", "__gatsby_initial_state__", "__remixcontext",
        "__sveltekit_data__", "__app_data__", "__store__",
        "data-reactroot", "data-server-rendered",
    )
    has_spa_markers = any(m in lower for m in spa_markers)

    # 有嵌入数据标记 → 直接走 SPA 提取（不管可见文本多少）
    embed_markers = (
        "__next_data__", "__nuxt_data__", "__nuxt__",
        "__initial_state__", "__apollo_state__", "__redux_state__",
        "__gatsby_data__", "__remixcontext", "__sveltekit_data__",
        "application/ld+json",
    )
    if any(m in lower for m in embed_markers):
        return True

    # body 可见文本少 + SPA 壳特征
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        return has_spa_markers
    try:
        tree = HTMLParser(html)
    except Exception:
        return has_spa_markers
    if tree.body is None:
        return True
    for tag in tree.css("script, style, noscript"):
        tag.decompose()
    visible = tree.body.text(separator=" ", strip=True) if tree.body else ""

    return has_spa_markers and len(visible) < 400


# SPA 数据嵌入点
_SPA_PATTERNS = (
    ("next_data", re.compile(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("nuxt_data", re.compile(
        r'<script[^>]*id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("remix_context", re.compile(
        r'<script[^>]*id=["\']__remixContext["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("sveltekit_data", re.compile(
        r'<script[^>]*id=["\']__SVELTEKIT_DATA__["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("gatsby_data", re.compile(
        r'<script[^>]*id=["\']__GATSBY_DATA__["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )),
)
_INLINE_ASSIGNMENTS = (
    "__NUXT__", "__INITIAL_STATE__", "__INITIAL_DATA__",
    "__PRELOADED_STATE__", "__APOLLO_STATE__", "__REDUX_STATE__",
    "__GATSBY_INITIAL_STATE__", "__remixContext", "__APP_DATA__",
    "__STORE__", "_sharedData",
)


def _extract_spa_data(html: str) -> str | None:
    """提取 SPA 各种嵌入数据点，合并成 JSON 文本。"""
    collected: dict[str, Any] = {}

    # script id=xxx 的 JSON
    for name, pat in _SPA_PATTERNS:
        m = pat.search(html)
        if m:
            parsed = _try_json(m.group(1))
            if parsed is not None:
                collected[name] = parsed

    # window.__X__ = {...}
    for var in _INLINE_ASSIGNMENTS:
        parsed = _extract_js_assignment(html, var)
        if parsed is not None:
            collected[var] = parsed

    # JSON-LD
    ld = [
        _try_json(m.group(1))
        for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
    ]
    ld = [x for x in ld if x is not None]
    if ld:
        collected["json_ld"] = ld

    if not collected:
        return None
    try:
        return json.dumps(collected, ensure_ascii=False, sort_keys=True,
                          indent=2, default=str)
    except Exception:
        return None


def _try_json(raw: str) -> Any | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_js_assignment(html: str, var: str) -> Any | None:
    """从 window.VAR = {...} 提取 JSON 值。"""
    pat = re.compile(r"(?:window\.)?" + re.escape(var) + r"\s*=\s*(\{)")
    m = pat.search(html)
    if not m:
        return None
    start = m.end() - 1
    end = _find_matching_brace(html, start)
    if end is None:
        return None
    return _try_json(html[start:end + 1])


def _find_matching_brace(text: str, start: int) -> int | None:
    """从 '{' 位置起找匹配的 '}'（处理字符串与转义）。"""
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
            elif ch == str_char:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            str_char = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _by_selector(html: str, selector: str) -> str:
    """按 CSS 选择器提取文本。"""
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        return ""
    try:
        tree = HTMLParser(html)
        nodes = tree.css(selector)
        return "\n\n".join(
            t for t in (n.text(separator="\n", strip=True) for n in nodes) if t
        )
    except Exception as e:
        logger.debug("selectolax 解析失败: {}", e)
        return ""


def _main_content(html: str) -> str:
    """trafilatura 正文提取。"""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        return trafilatura.extract(
            html, include_comments=False, include_tables=True, favor_recall=True
        ) or ""
    except Exception:
        return ""


def _extract_meta(html: str) -> str:
    """提取 <title> + meta + h1/h2 作为元数据兜底。"""
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        return ""
    try:
        tree = HTMLParser(html)
    except Exception:
        return ""
    lines: list[str] = []
    if tree.head is not None:
        title = tree.css_first("head title")
        if title and (t := title.text(strip=True)):
            lines.append(f"[title] {t}")
    for meta in tree.css("head meta"):
        name = (meta.attributes.get("name") or meta.attributes.get("property") or "").strip()
        content = (meta.attributes.get("content") or "").strip()
        if name and content:
            lines.append(f"[meta:{name}] {content}")
    for tag in ("h1", "h2"):
        for node in tree.css(tag):
            if t := node.text(strip=True):
                lines.append(f"[{tag}] {t}")
    return "\n".join(lines)


def _html_to_text(html: str) -> str:
    """整页纯文本兜底。"""
    try:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        return tree.body.text(separator="\n", strip=True) if tree.body else ""
    except Exception:
        t = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        t = re.sub(r"<style.*?</style>", "", t, flags=re.DOTALL | re.IGNORECASE)
        return re.sub(r"<[^>]+>", "", t)


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
        if path:
            try:
                collected.append({path: _eval_path(obj, path)})
            except Exception as e:
                logger.debug("json_path '{}' 提取失败: {}", path, e)
    if not collected:
        return ""
    return json.dumps(collected, ensure_ascii=False, sort_keys=True, indent=2)


def _eval_path(obj: Any, path: str) -> Any:
    """简易 JSON path 求值：a.b[0].c / a.b[*].c"""
    tokens: list[str | int] = []
    for part in path.split("."):
        m = re.match(r"([^\[]+)((?:\[[^\]]+\])*)", part)
        if not m:
            continue
        key, indexes = m.group(1), m.group(2)
        if key:
            tokens.append(key)
        for idx_m in re.finditer(r"\[([^\]]+)\]", indexes):
            idx = idx_m.group(1)
            if idx == "*":
                tokens.append("*")
            else:
                try:
                    tokens.append(int(idx))
                except ValueError:
                    tokens.append(idx)

    def walk(o: Any, toks: list[str | int]) -> Any:
        if not toks:
            return o
        head, *rest = toks
        if head == "*":
            return [walk(x, rest) for x in o] if isinstance(o, list) else None
        if isinstance(head, int):
            return walk(o[head], rest) if isinstance(o, list) and 0 <= head < len(o) else None
        if isinstance(head, str) and isinstance(o, dict):
            return walk(o.get(head), rest)
        return None

    return walk(obj, tokens)


# ============================================================
# Jina Reader 元信息过滤
# ============================================================
# Jina Reader 会在正文前插入一些元信息行，常见形式：
#   "Warning: This is a cached snapshot of the original page, consider retry with caching opt-out."
#   "Warning: This page maybe not yet fully loaded, consider explicitly specify a timeout."
#   "Warning: This page contains shadow DOM that are currently hidden..."
#   "Note: ..."
# 它们不是页面真实内容，但频繁变化（具体文案随 Jina 内部状态切换），
# 会造成 diff 噪音，在比对前统一过滤掉。
_JINA_META_RE = re.compile(
    r"^(?:Warning|Note)\s*:.*$",
    re.MULTILINE | re.IGNORECASE,
)


def _strip_jina_warnings(text: str) -> str:
    """移除 Jina Reader 返回的 Warning/Note 元信息行（不属于页面正文内容）。"""
    return _JINA_META_RE.sub("", text)


# ============================================================
# 归一化
# ============================================================
def _normalize(text: str) -> str:
    """压缩空白 + 移除动态噪音。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pat in _NOISE_PATTERNS:
        text = pat.sub("[DYNAMIC]", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


def try_deep_extract(html: str) -> str:
    """
    对已拿到的 HTML 做深度提取尝试（供 engine 在判定空壳后调用）。
    与 extract() 不同：这里不依赖 Task/FetchResult，直接从原始 HTML 中
    尝试提取 SPA 嵌入数据、trafilatura 正文、meta 元数据。
    返回空字符串表示提取失败。
    """
    if not html or not html.strip():
        return ""

    # 1. SPA 嵌入数据（优先级最高 — 结构化数据最完整）
    spa = _extract_spa_data(html)
    if spa and len(spa) >= MIN_USEFUL_LENGTH:
        return _normalize(spa)

    # 2. trafilatura 正文
    text = _main_content(html)
    if text and len(text) >= MIN_USEFUL_LENGTH:
        return _normalize(text)

    # 3. meta 元数据兜底
    meta = _extract_meta(html)
    if meta and len(meta) >= MIN_USEFUL_LENGTH:
        return _normalize(meta)

    # 4. 整页纯文本（最终兜底）
    full = _html_to_text(html)
    if full and len(full) >= MIN_USEFUL_LENGTH:
        return _normalize(full)

    return ""


def content_hash(text: str) -> str:
    """SHA-256，用于快速判断是否变化。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# 诊断（给 /debug 命令用）
# ============================================================
def diagnose_html(html: str) -> dict[str, Any]:
    """分析 HTML：识别框架 / 嵌入数据 / 给建议。"""
    lower = html.lower()

    frameworks: list[str] = []
    if "__next_data__" in lower or 'id="__next"' in lower:
        frameworks.append("Next.js")
    if "__nuxt__" in lower or "__nuxt_data__" in lower:
        frameworks.append("Nuxt.js / Vue SSR")
    if "data-reactroot" in lower:
        frameworks.append("React SSR")
    if any(m in lower for m in ("cloudflare", "cf-ray", "cdn-cgi")):
        frameworks.append("Cloudflare 保护")
    if not frameworks and ('id="root"' in lower or 'id="app"' in lower):
        frameworks.append("纯客户端 SPA 壳")

    data_points: list[str] = []
    for name, pat in _SPA_PATTERNS:
        if pat.search(html):
            data_points.append(f"✅ {name}")
    for var in _INLINE_ASSIGNMENTS:
        if _extract_js_assignment(html, var) is not None:
            data_points.append(f"✅ window.{var}")
    if re.search(r'type=["\']application/ld\+json["\']', html, re.IGNORECASE):
        data_points.append("✅ JSON-LD")

    visible_len = len(_html_to_text(html))

    # 建议
    suggestions: list[str] = []
    if data_points:
        suggestions.append("页面内嵌结构化数据，当前提取策略应能拿到")
    elif "Cloudflare 保护" in frameworks and visible_len < 400:
        suggestions.append(
            "疑似 Cloudflare 挑战，建议 `--impersonate chrome124/firefox133` 换指纹，"
            "或 `--strategy jina` 用外部渲染"
        )
    elif visible_len < 400 and any("SPA" in f for f in frameworks):
        suggestions.append(
            "纯客户端 SPA。建议 `/sniff <url>` 找内部 API 用 `--type json` 监控，"
            "或 `--strategy jina` 让外部服务渲染"
        )
    else:
        suggestions.append(f"抓取正常，可见文本 {visible_len} 字")

    return {
        "html_size": len(html),
        "frameworks": frameworks,
        "data_points": data_points,
        "visible_text_length": visible_len,
        "suggestions": suggestions,
    }
