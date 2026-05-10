"""
内容提取和归一化

从 FetchResult 中提取"用于比对的稳定文本"，去除噪音：
- HTML → 正文纯文本（优先 CSS selector，其次 trafilatura 抽正文，最后全文）
- HTML → 若 extract_next_data=True，尝试解析 Next.js 的 __NEXT_DATA__
- JSON → 按 json_path 提取感兴趣字段
- Markdown (来自 jina / firecrawl) → 原文略微归一化

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
# 常见动态 token / nonce / csrf 等，避免每次都触发"变化"
_NOISE_PATTERNS = [
    re.compile(r"csrf[_-]?token[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"nonce[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    re.compile(r"build[Ii]d[\"']?\s*[:=]\s*[\"']?[a-zA-Z0-9_-]+", re.IGNORECASE),
    # 18 位以上的 hash / 随机字符串
    re.compile(r"\b[a-f0-9]{32,}\b"),
    # ISO 时间戳
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"),
    # Unix 毫秒时间戳
    re.compile(r"\b1[5-9]\d{11}\b|\b2[0-1]\d{11}\b"),
]

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


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
# HTML 提取
# ============================================================
def _extract_html(html: str, task: Task) -> str:
    """
    HTML 提取顺序：
    1. 如果 extract_next_data=True，先尝试 __NEXT_DATA__
    2. 如果有 CSS selector，只取 selector 匹配的文本
    3. 否则用 trafilatura 抽正文
    4. 最后兜底：整页 textContent
    """
    # 1) Next.js __NEXT_DATA__
    if task.extract_next_data:
        data = _extract_next_data(html)
        if data:
            logger.debug("🔍 成功提取 __NEXT_DATA__（长度 {} 字）", len(data))
            return data

    # 2) CSS selector
    if task.selector:
        text = _extract_by_selector(html, task.selector)
        if text:
            return _normalize_text(text)
        logger.debug("⚠️  CSS 选择器 '{}' 未匹配到内容，回退到全文提取", task.selector)

    # 3) trafilatura 抽正文
    text = _extract_main_content(html)
    if text:
        return _normalize_text(text)

    # 4) 兜底：整页纯文本
    text = _html_to_text(html)
    return _normalize_text(text)


def _extract_next_data(html: str) -> str | None:
    """提取 Next.js 的 <script id="__NEXT_DATA__"> 内 JSON。"""
    match = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    # 取 pageProps（Next.js SSG/SSR 通常把数据塞这里）
    page_props = (
        data.get("props", {}).get("pageProps")
        if isinstance(data, dict)
        else None
    )
    if page_props is not None:
        return json.dumps(page_props, ensure_ascii=False, sort_keys=True, indent=2)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _extract_by_selector(html: str, selector: str) -> str:
    """用 selectolax 按 CSS 选择器提取文本。"""
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


def _extract_main_content(html: str) -> str:
    """用 trafilatura 抽正文。"""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,  # 尽可能多召回，监控场景宁多勿漏
        )
        return text or ""
    except Exception as e:
        logger.debug("trafilatura 提取失败: {}", e)
        return ""


def _html_to_text(html: str) -> str:
    """粗暴兜底：移除 script/style 后取纯文本。"""
    try:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)
        # 去除脚本和样式
        for tag in tree.css("script, style, noscript, nav, footer, header"):
            tag.decompose()
        body = tree.body
        if body is None:
            return ""
        return body.text(separator="\n", strip=True)
    except Exception:
        # 最后的最后：正则剥标签
        text = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return text


# ============================================================
# JSON 提取
# ============================================================
def _extract_json(content: str, json_path: str | None) -> str:
    """
    JSON 提取：如果配置了 json_path，只保留指定字段；否则格式化整个 JSON。

    json_path 语法（简化版）：
      - "data[*].name"       展开 data 数组下每项的 name
      - "data[0].price"      取 data 数组第 0 个的 price
      - 多个路径用英文逗号分隔："data[*].name, data[*].price"
    """
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
    """
    极简 JSON 路径求值器。支持 a.b[0].c 和 a.b[*].c 语法。
    """
    tokens = _tokenize_path(path)
    return _walk(obj, tokens)


def _tokenize_path(path: str) -> list[str | int]:
    """将 'data[0].name' 解析为 ['data', 0, 'name']；'data[*].name' 保留 '*' 字符串。"""
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
    """
    归一化文本，减少误报：
    - 去除 Windows 回车
    - 压缩连续空格
    - 压缩连续空行
    - 移除常见动态噪音（csrf token / nonce / build id / hash / 时间戳）
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    for pattern in _NOISE_PATTERNS:
        text = pattern.sub("[DYNAMIC]", text)

    # 每行 strip
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINES_RE.sub("\n\n", text)

    return text.strip()


def content_hash(text: str) -> str:
    """对归一化文本计算 SHA-256 hash，用于快速判断是否变化。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
