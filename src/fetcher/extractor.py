"""内容提取和归一化。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..db import Task
from ..logger import logger
from .engine import FetchResult

# 归一化时移除的噪音（时间戳 / csrf / nonce / 长 hash）
# 注意：过于激进的 hex 匹配会误杀用户真正关心的哈希（如链上交易 hash），
# 因此仅在紧邻 token/nonce/hash/signature/session 这类关键字的上下文里替换。
_NOISE_PATTERNS = [
    re.compile(r"csrf[_-]?token[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"nonce[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"build[Ii]d[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    # 仅在明确的 token/hash/sig/session 上下文内替换长 hex，避免误杀链上 txhash
    re.compile(
        r"(?i)(?:token|hash|signature|session|sid|auth|secret|api[_-]?key)"
        r"[\"']?\s*[:=]\s*[\"']?[a-f0-9]{32,}",
    ),
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
               r"(?:Z|[+-]\d{2}:?\d{2})?"),  # ISO 时间戳（含时区）
    re.compile(r"\b1[5-9]\d{11}\b|\b2[0-1]\d{11}\b"),       # Unix ms 时间戳
    re.compile(r"\b1[5-9]\d{8}\b|\b2[0-1]\d{8}\b"),         # Unix 秒级时间戳
]
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")

MIN_USEFUL_LENGTH = 120


def extract(task: Task, result: FetchResult) -> str:
    """从 FetchResult 提取用于比对的文本（已归一化）。"""
    content = result.content or ""
    if not content.strip():
        return ""

    content_type = (result.content_type or "").lower()
    task_type = (task.type or "html").lower()
    strategy = result.strategy_used or ""

    if task_type == "json" or "json" in content_type:
        return _extract_json(content, task.json_path)

    if strategy.endswith("→deep"):
        return _normalize(content)

    if "playwright" in strategy:
        return _extract_rendered_html(content, task, result.inner_text)

    return _extract_html(content, task)


def _extract_rendered_html(html: str, task: Task, inner_text: str = "") -> str:
    """从 Playwright 渲染后的 HTML 提取文本，跳过 SPA/RSC 解析。"""
    # 1) CSS 选择器（用户指定）
    if task.selector:
        text = _by_selector(html, task.selector, task)
        if text and len(text) >= MIN_USEFUL_LENGTH:
            return _normalize(text)

    # 2) Playwright 直接提供的 body.innerText
    if inner_text and len(inner_text) >= MIN_USEFUL_LENGTH:
        return _normalize(inner_text)

    # 3) 整页可见文本（从 HTML 重新解析）
    text = _html_to_text(html)
    if text and len(text) >= MIN_USEFUL_LENGTH:
        return _normalize(text)

    # 4) trafilatura 正文提取
    text = _main_content(html)
    if text and len(text) >= MIN_USEFUL_LENGTH:
        return _normalize(text)

    # 5) meta 兜底
    meta = _extract_meta(html)
    return _normalize(meta) if meta else ""


def _extract_html(html: str, task: Task) -> str:
    """从原始 HTML 按优先级提取文本。"""
    # 1) CSS 选择器
    if task.selector:
        text = _by_selector(html, task.selector, task)
        if text and len(text) >= MIN_USEFUL_LENGTH:
            return _normalize(text)

    # 2) SPA 嵌入数据
    if task.extract_next_data or _looks_like_spa_shell(html):
        data = _extract_spa_data(html)
        if data:
            return _normalize(data)

    # 3) RSC Flight 数据（Next.js App Router SSR）
    rsc = _extract_rsc_flight(html)
    if rsc and len(rsc) >= MIN_USEFUL_LENGTH:
        return _normalize(rsc)

    # 4) trafilatura 正文
    text = _main_content(html)
    if text and len(text) >= MIN_USEFUL_LENGTH:
        return _normalize(text)

    # 5) meta 元数据
    meta = _extract_meta(html)
    if meta and len(meta) >= MIN_USEFUL_LENGTH:
        return _normalize(meta)

    # 6) 兜底：整页纯文本
    return _normalize(_html_to_text(html)) or _normalize(meta)


def _looks_like_spa_shell(html: str) -> bool:
    """检测 SPA 空壳：有 SPA 标记且可见文本少。"""
    lower = html.lower()

    spa_markers = (
        'id="root"', 'id="app"', 'id="__next"',
        "__next_data__", "__nuxt__", "__nuxt_data__",
        "__apollo_state__", "__initial_state__", "__preloaded_state__",
        "__redux_state__", "__gatsby_initial_state__", "__remixcontext",
        "__sveltekit_data__", "__app_data__", "__store__",
        "data-reactroot", "data-server-rendered",
        "self.__next_f",
    )
    has_spa_markers = any(m in lower for m in spa_markers)

    # 有嵌入数据标记 → 直接走 SPA 提取
    # 注意: application/ld+json 是 SEO 元数据，不代表页面有真实嵌入数据，不纳入此列表
    embed_markers = (
        "__next_data__", "__nuxt_data__", "__nuxt__",
        "__initial_state__", "__apollo_state__", "__redux_state__",
        "__gatsby_data__", "__remixcontext", "__sveltekit_data__",
    )
    if any(m in lower for m in embed_markers):
        return True
    # self.__next_f (RSC Flight) 视为嵌入数据，但 BAILOUT 时 RSC 无实际内容，跳过
    if "self.__next_f" in lower and "bailout_to_client_side_rendering" not in lower:
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

    for name, pat in _SPA_PATTERNS:
        m = pat.search(html)
        if m:
            parsed = _try_json(m.group(1))
            if parsed is not None:
                collected[name] = parsed

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


_RSC_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[\d+,(".*?")\]\)', re.DOTALL,
)

# RSC Flight payload 前缀白名单：这些内部标记不是可读内容，跳过以避免
# 把模块 id / hydration marker 当作文本误收。
# - I[...]  / J[...]       hydration 信息
# - HL[...] / M[...]       模块声明
# - $L... / $S...          lazy / symbol 引用（含 "$S... 双引号变体）
_RSC_SKIP_PREFIXES = ("I[", "J[", "HL[", "M[", '"$L', '"$S', "$L", "$S")


def _extract_rsc_flight(html: str) -> str | None:
    """解析 Next.js App Router RSC Flight 数据流，提取文本内容。"""
    if "self.__next_f" not in html:
        return None
    if "BAILOUT_TO_CLIENT_SIDE_RENDERING" in html:
        return None

    # 用 list + join 避免 O(n²) 的字符串累加，RSC 大页面下差距明显。
    chunks: list[str] = []
    for m in _RSC_PUSH_RE.finditer(html):
        try:
            chunks.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, Exception):
            continue
    if not chunks:
        return None
    stream = "".join(chunks)

    texts: list[str] = []
    for line in stream.split("\n"):
        colon = line.find(":")
        if colon == -1:
            continue
        payload = line[colon + 1:]
        if not payload or payload == "null":
            continue
        if any(payload.startswith(p) for p in _RSC_SKIP_PREFIXES):
            continue
        try:
            obj = json.loads(payload)
            _collect_rsc_text(obj, texts)
        except (json.JSONDecodeError, Exception):
            continue

    if not texts:
        return None
    return "\n".join(texts)


def _collect_rsc_text(node: Any, texts: list[str]) -> None:
    """递归遍历 RSC Flight 节点树，收集文本内容。"""
    if isinstance(node, str):
        s = node.strip()
        if s and not s.startswith("$") and len(s) > 1:
            texts.append(s)
    elif isinstance(node, list):
        for item in node:
            _collect_rsc_text(item, texts)
    elif isinstance(node, dict):
        for key, val in node.items():
            if key in ("children", "content", "title", "description",
                       "dangerouslySetInnerHTML"):
                _collect_rsc_text(val, texts)


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


def _by_selector(html: str, selector: str, task: Task | None = None) -> str:
    """按 CSS 选择器提取文本。"""
    if task is not None and getattr(task, "adaptive_selector", False):
        text = _by_scrapling_selector(html, selector, task)
        if text:
            return text

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
        logger.warning("CSS 选择器 '{}' 解析失败: {}", selector, e)
        return ""


def _by_scrapling_selector(html: str, selector: str, task: Task) -> str:
    """Use Scrapling adaptive selector when enabled for a task."""
    try:
        from scrapling import Selector
        from ..config import DATA_DIR
    except ImportError:
        logger.warning("Scrapling 未安装，无法使用自适应选择器，回退 selectolax")
        return ""

    try:
        page = Selector(
            html,
            url=task.url,
            adaptive=True,
            storage_args={
                "storage_file": str(DATA_DIR / "scrapling_adaptive.db"),
                "url": task.url,
            },
        )
        identifier = task.selector_identifier or selector
        threshold = max(1, min(int(task.adaptive_threshold or 40), 100))

        nodes = page.css(selector, identifier=identifier, auto_save=True)
        source = "direct"
        if not nodes:
            nodes = page.css(
                selector,
                identifier=identifier,
                adaptive=True,
                auto_save=True,
                percentage=threshold,
            )
            source = "adaptive"
        if not nodes:
            return ""

        lines: list[str] = []
        for node in nodes:
            try:
                text = str(node.get_all_text(separator="\n", strip=True))
            except Exception:
                text = str(getattr(node, "text", "") or "")
            if text.strip():
                lines.append(text)
        if source == "adaptive":
            logger.info("#{} [{}] 自适应选择器重定位成功 selector={} threshold={}",
                        task.id, task.name, selector, threshold)
        return "\n\n".join(lines)
    except Exception as e:
        logger.warning("Scrapling 自适应选择器 '{}' 解析失败: {}", selector, e)
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


def _extract_json(content: str, json_path: str | None) -> str:
    """提取 JSON 内容，支持 json_path 过滤。"""
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
            except Exception:
                pass
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
    """对已拿到的 HTML 做深度提取（SPA 嵌入 / RSC / trafilatura），不兜底低质量文本。"""
    if not html or not html.strip():
        return ""

    # 1. SPA 嵌入数据
    spa = _extract_spa_data(html)
    if spa and len(spa) >= MIN_USEFUL_LENGTH:
        return _normalize(spa)

    # 2. RSC Flight 数据
    rsc = _extract_rsc_flight(html)
    if rsc and len(rsc) >= MIN_USEFUL_LENGTH:
        return _normalize(rsc)

    # 3. trafilatura 正文
    text = _main_content(html)
    if text and len(text) >= MIN_USEFUL_LENGTH:
        return _normalize(text)

    return ""


def extract_meta_fallback(html: str) -> str:
    """Public wrapper: extract meta tags from an SPA shell as last resort.

    Used by FetchEngine when all other strategies (curl_cffi, httpx, deep
    extract, Playwright) fail. Gives the task at least a diffable baseline
    from title / og:* / description / h1-h2 tags.
    """
    if not html or not html.strip():
        return ""
    text = _extract_meta(html)
    return _normalize(text) if text else ""


def content_hash(text: str) -> str:
    """SHA-256，用于快速判断是否变化。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diagnose_html(html: str) -> dict[str, Any]:
    """分析 HTML：识别框架 / 嵌入数据 / 给建议。"""
    lower = html.lower()

    frameworks: list[str] = []
    if "__next_data__" in lower or 'id="__next"' in lower:
        frameworks.append("Next.js")
    if "self.__next_f" in lower:
        frameworks.append("Next.js App Router (RSC)")
    if "__nuxt__" in lower or "__nuxt_data__" in lower:
        frameworks.append("Nuxt.js / Vue SSR")
    if "data-reactroot" in lower:
        frameworks.append("React SSR")
    if any(m in lower for m in ("cloudflare", "cf-ray", "cdn-cgi")):
        frameworks.append("Cloudflare 保护")
    if "bailout_to_client_side_rendering" in lower:
        frameworks.append("CSR Bailout（纯客户端渲染）")
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
    if "self.__next_f" in html and "BAILOUT_TO_CLIENT_SIDE_RENDERING" not in html:
        data_points.append("✅ RSC Flight（有服务端数据）")

    visible_len = len(_html_to_text(html))

    suggestions: list[str] = []
    if data_points:
        suggestions.append("页面内嵌结构化数据，当前提取策略应能拿到")
    elif "CSR Bailout" in "".join(frameworks) or "纯客户端 SPA" in "".join(frameworks):
        suggestions.append(
            "纯客户端 SPA（数据由 JS 动态加载）。建议 `/sniff <url>` 找内部 API "
            "用 `--type json` 监控，或 `--strategy playwright` 让浏览器渲染"
        )
    elif "Cloudflare 保护" in frameworks and visible_len < 400:
        suggestions.append(
            "疑似 Cloudflare 挑战，建议 `--impersonate chrome124/firefox133` 换指纹，"
            "或 `--strategy playwright` 用浏览器渲染"
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
