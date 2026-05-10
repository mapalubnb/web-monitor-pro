"""
Diff 引擎

- 文本：基于 difflib 的行级 diff + 相似度计算
- JSON：基于 deepdiff 的结构化 diff
- 输出：DiffResult（含新增/删除行、统计、人类可读摘要、完整 diff 文本）
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field


@dataclass
class DiffResult:
    """Diff 结果。"""

    changed: bool = False              # 是否有变化
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    change_ratio: float = 0.0          # 变化占比（0.0 - 1.0）
    similarity: float = 1.0            # 相似度（0.0 - 1.0）
    unified_diff: str = ""             # 完整 unified diff（写文件）
    summary: str = ""                  # 人类可读摘要（飞书卡片用）
    is_json: bool = False              # 是否为 JSON 结构化 diff


# ============================================================
# 对外入口
# ============================================================
def compute_diff(
    before: str,
    after: str,
    *,
    is_json: bool = False,
    max_lines_in_summary: int = 8,
) -> DiffResult:
    """
    计算两段文本的 diff。

    Args:
        before: 上一次快照内容
        after: 本次抓取内容
        is_json: 若 True，尝试用 deepdiff 做结构化比对
        max_lines_in_summary: 摘要中每侧最多展示多少行

    Returns:
        DiffResult
    """
    if before == after:
        return DiffResult(changed=False, similarity=1.0)

    if is_json:
        return _compute_json_diff(before, after, max_lines_in_summary)
    return _compute_text_diff(before, after, max_lines_in_summary)


# ============================================================
# 文本 diff
# ============================================================
def _compute_text_diff(before: str, after: str, max_lines_in_summary: int) -> DiffResult:
    before_lines = before.splitlines()
    after_lines = after.splitlines()

    # SequenceMatcher 计算相似度 & 变化行
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    similarity = matcher.ratio()

    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            removed.extend(before_lines[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(after_lines[j1:j2])

    # 过滤空行
    added = [line for line in added if line.strip()]
    removed = [line for line in removed if line.strip()]

    total_lines = max(len(before_lines), len(after_lines), 1)
    change_ratio = (len(added) + len(removed)) / total_lines

    # unified diff 完整内容（写文件下载用）
    unified = "\n".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=3,
        )
    )

    summary = _build_text_summary(added, removed, max_lines_in_summary)

    return DiffResult(
        changed=bool(added or removed),
        added_lines=added,
        removed_lines=removed,
        change_ratio=change_ratio,
        similarity=similarity,
        unified_diff=unified,
        summary=summary,
        is_json=False,
    )


def _build_text_summary(added: list[str], removed: list[str], max_lines: int) -> str:
    """生成给飞书卡片用的人类可读摘要。"""
    parts: list[str] = []
    if added:
        parts.append(f"➕ 新增 {len(added)} 行：")
        for line in added[:max_lines]:
            parts.append(f"  + {_truncate(line, 140)}")
        if len(added) > max_lines:
            parts.append(f"  ...（省略 {len(added) - max_lines} 行）")
    if removed:
        if parts:
            parts.append("")
        parts.append(f"➖ 删除 {len(removed)} 行：")
        for line in removed[:max_lines]:
            parts.append(f"  - {_truncate(line, 140)}")
        if len(removed) > max_lines:
            parts.append(f"  ...（省略 {len(removed) - max_lines} 行）")
    return "\n".join(parts)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


# ============================================================
# JSON diff
# ============================================================
def _compute_json_diff(before: str, after: str, max_lines_in_summary: int) -> DiffResult:
    """
    JSON 结构化 diff。若解析失败则退化为文本 diff。
    """
    try:
        before_obj = json.loads(before) if before.strip() else {}
        after_obj = json.loads(after) if after.strip() else {}
    except json.JSONDecodeError:
        result = _compute_text_diff(before, after, max_lines_in_summary)
        return result

    try:
        from deepdiff import DeepDiff
    except ImportError:
        result = _compute_text_diff(before, after, max_lines_in_summary)
        return result

    dd = DeepDiff(before_obj, after_obj, ignore_order=True, verbose_level=2)
    changed = bool(dd)
    if not changed:
        return DiffResult(changed=False, similarity=1.0, is_json=True)

    # 把 deepdiff 结果转成人类可读摘要
    added_lines, removed_lines = _deepdiff_to_lines(dd)
    summary = _build_text_summary(added_lines, removed_lines, max_lines_in_summary)

    # 完整 diff 用 deepdiff 的 pretty 输出
    try:
        pretty = dd.pretty()
    except Exception:
        pretty = json.dumps(dd.to_dict(), ensure_ascii=False, indent=2, default=str)

    total = max(
        len(_flatten_json(before_obj)),
        len(_flatten_json(after_obj)),
        1,
    )
    change_count = len(added_lines) + len(removed_lines)
    change_ratio = min(change_count / total, 1.0)

    return DiffResult(
        changed=True,
        added_lines=added_lines,
        removed_lines=removed_lines,
        change_ratio=change_ratio,
        similarity=1.0 - change_ratio,
        unified_diff=pretty,
        summary=summary,
        is_json=True,
    )


def _deepdiff_to_lines(dd) -> tuple[list[str], list[str]]:
    """把 DeepDiff 结果转成 (added_lines, removed_lines)。"""
    added: list[str] = []
    removed: list[str] = []

    d = dd.to_dict() if hasattr(dd, "to_dict") else dict(dd)

    # 新增的字段/项
    for path, val in (d.get("dictionary_item_added") or {}).items() if isinstance(d.get("dictionary_item_added"), dict) else []:
        added.append(f"{path} = {_short_json(val)}")
    for path in d.get("dictionary_item_added") or []:
        if not isinstance(d.get("dictionary_item_added"), dict):
            added.append(str(path))

    for path in d.get("iterable_item_added") or {}:
        val = (d.get("iterable_item_added") or {}).get(path) if isinstance(d.get("iterable_item_added"), dict) else None
        added.append(f"{path} = {_short_json(val)}" if val is not None else str(path))

    # 删除的字段/项
    for path in d.get("dictionary_item_removed") or []:
        removed.append(str(path))
    for path in d.get("iterable_item_removed") or {}:
        val = (d.get("iterable_item_removed") or {}).get(path) if isinstance(d.get("iterable_item_removed"), dict) else None
        removed.append(f"{path} = {_short_json(val)}" if val is not None else str(path))

    # 值变化
    for path, change in (d.get("values_changed") or {}).items():
        old = _short_json(change.get("old_value") if isinstance(change, dict) else None)
        new = _short_json(change.get("new_value") if isinstance(change, dict) else None)
        removed.append(f"{path}: {old}")
        added.append(f"{path}: {new}")

    for path, change in (d.get("type_changes") or {}).items():
        old = _short_json(change.get("old_value") if isinstance(change, dict) else None)
        new = _short_json(change.get("new_value") if isinstance(change, dict) else None)
        removed.append(f"{path} (type changed): {old}")
        added.append(f"{path} (type changed): {new}")

    return added, removed


def _short_json(v) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return _truncate(s, 120)


def _flatten_json(obj, prefix: str = "") -> list[str]:
    """简单展平 JSON，统计"有多少个叶子"作为 diff 比例分母。"""
    result: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.extend(_flatten_json(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            result.extend(_flatten_json(v, f"{prefix}[{i}]"))
    else:
        result.append(f"{prefix}={obj}")
    return result
