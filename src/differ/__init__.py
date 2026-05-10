"""Diff 引擎包。"""

from .text_diff import DiffResult, compute_diff, filter_by_keywords

__all__ = ["DiffResult", "compute_diff", "filter_by_keywords"]
