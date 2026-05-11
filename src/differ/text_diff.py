"""Diff 引擎：文本行级 diff 和 JSON 结构化 diff。"""

from __future__ import annotations

import difflib
import json
from collections import Counter
from dataclasses import dataclass, field, replace


@dataclass
class DiffResult:
    """Diff 结果。"""

    changed: bool = False
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    change_ratio: float = 0.0
    similarity: float = 1.0
    unified_diff: str = ""
    summary: str = ""
    is_json: bool = False


def compute_diff(
    before: str,
    after: str,
    *,
    is_json: bool = False,
    max_lines_in_summary: int = 8,
) -> DiffResult:
    """计算两段文本的 diff，返回 DiffResult。"""
    if before == after:
        return DiffResult(changed=False, similarity=1.0)

    if is_json:
        return _compute_json_diff(before, after, max_lines_in_summary)
    return _compute_text_diff(before, after, max_lines_in_summary)


def _compute_text_diff(before: str, after: str, max_lines_in_summary: int) -> DiffResult:
    """基于 difflib 的行级文本 diff。"""
    before_lines = before.splitlines()
    after_lines = after.splitlines()

    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    similarity = matcher.ratio()

    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            removed.extend(before_lines[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(after_lines[j1:j2])

    added = [line for line in added if line.strip()]
    removed = [line for line in removed if line.strip()]

    # 消除位置移动产生的假 diff
    added, removed = _cancel_moved_lines(added, removed)

    total_lines = max(len(before_lines), len(after_lines), 1)
    change_ratio = (len(added) + len(removed)) / total_lines

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
    """生成人类可读摘要。"""
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


def _cancel_moved_lines(added: list[str], removed: list[str]) -> tuple[list[str], list[str]]:
    """消除位置移动产生的假 diff，按计数逐一抵消相同内容的行。"""
    add_counts = Counter(added)
    rem_counts = Counter(removed)
    cancel: dict[str, int] = {}
    for line in add_counts:
        if line in rem_counts:
            cancel[line] = min(add_counts[line], rem_counts[line])
    if not cancel:
        return added, removed
    new_added: list[str] = []
    cancel_add = dict(cancel)
    for ln in added:
        if cancel_add.get(ln, 0) > 0:
            cancel_add[ln] -= 1
        else:
            new_added.append(ln)
    new_removed: list[str] = []
    cancel_rem = dict(cancel)
    for ln in removed:
        if cancel_rem.get(ln, 0) > 0:
            cancel_rem[ln] -= 1
        else:
            new_removed.append(ln)
    return new_added, new_removed


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _compute_json_diff(before: str, after: str, max_lines_in_summary: int) -> DiffResult:
    """JSON 结构化 diff，解析失败则退化为文本 diff。"""
    try:
        before_obj = json.loads(before) if before.strip() else {}
        after_obj = json.loads(after) if after.strip() else {}
    except json.JSONDecodeError:
        return _compute_text_diff(before, after, max_lines_in_summary)

    try:
        from deepdiff import DeepDiff
    except ImportError:
        return _compute_text_diff(before, after, max_lines_in_summary)

    dd = DeepDiff(before_obj, after_obj, ignore_order=True, verbose_level=2)
    changed = bool(dd)
    if not changed:
        return DiffResult(changed=False, similarity=1.0, is_json=True)

    added_lines, removed_lines = _deepdiff_to_lines(dd)
    summary = _build_text_summary(added_lines, removed_lines, max_lines_in_summary)

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

    for path, val in (d.get("dictionary_item_added") or {}).items():
        added.append(f"{path} = {_short_json(val)}")
    for path, val in (d.get("iterable_item_added") or {}).items():
        added.append(f"{path} = {_short_json(val)}")

    for path, val in (d.get("dictionary_item_removed") or {}).items():
        removed.append(f"{path} = {_short_json(val)}")
    for path, val in (d.get("iterable_item_removed") or {}).items():
        removed.append(f"{path} = {_short_json(val)}")

    for path, change in (d.get("values_changed") or {}).items():
        if isinstance(change, dict):
            removed.append(f"{path}: {_short_json(change.get('old_value'))}")
            added.append(f"{path}: {_short_json(change.get('new_value'))}")

    for path, change in (d.get("type_changes") or {}).items():
        if isinstance(change, dict):
            removed.append(f"{path} (type changed): {_short_json(change.get('old_value'))}")
            added.append(f"{path} (type changed): {_short_json(change.get('new_value'))}")

    return added, removed


def _short_json(v) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return _truncate(s, 120)


def _flatten_json(obj, prefix: str = "") -> list[str]:
    """展平 JSON 计算叶子数量，用作 diff 比例分母。"""
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


def filter_by_keywords(
    diff: DiffResult,
    keywords: list[str],
    *,
    max_lines_in_summary: int = 8,
) -> DiffResult:
    """按关键词过滤 DiffResult，只保留包含任一关键词的变化行。"""
    kws = [kw.strip() for kw in (keywords or []) if kw and kw.strip()]
    if not kws:
        return diff

    lowered_kws = [kw.lower() for kw in kws]

    def _hit(line: str) -> bool:
        ll = line.lower()
        return any(kw in ll for kw in lowered_kws)

    added = [ln for ln in diff.added_lines if _hit(ln)]
    removed = [ln for ln in diff.removed_lines if _hit(ln)]

    added, removed = _cancel_moved_lines(added, removed)

    if not added and not removed:
        return replace(
            diff,
            changed=False,
            added_lines=[],
            removed_lines=[],
            change_ratio=0.0,
            similarity=1.0,
            unified_diff="",
            summary="",
        )

    summary = _build_text_summary(added, removed, max_lines_in_summary)

    unified_lines = ["--- before", "+++ after"]
    for ln in removed:
        unified_lines.append(f"-{ln}")
    for ln in added:
        unified_lines.append(f"+{ln}")
    unified = "\n".join(unified_lines)

    orig_total = max(len(diff.added_lines) + len(diff.removed_lines), 1)
    hit_total = len(added) + len(removed)
    change_ratio = hit_total / orig_total

    return replace(
        diff,
        changed=True,
        added_lines=added,
        removed_lines=removed,
        change_ratio=change_ratio,
        similarity=max(0.0, 1.0 - change_ratio),
        unified_diff=unified,
        summary=summary,
    )
