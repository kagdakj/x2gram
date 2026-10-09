"""Parsing of human selections like '1,3,5-8'."""

from typing import List


def parse_selection(text: str, max_val: int) -> List[int]:
    """Return sorted 1-based indices. Empty / 'all' selects everything, 'q' selects nothing."""
    text = text.strip().lower()
    if not text or text in ("all", "a", "*"):
        return list(range(1, max_val + 1))
    if text in ("q", "quit", "exit", "n", "no", "none"):
        return []

    selected = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            start, _, end = part.partition("-")
            if start.strip().isdigit() and end.strip().isdigit():
                lo, hi = sorted((int(start), int(end)))
                selected.update(i for i in range(lo, hi + 1) if 1 <= i <= max_val)
        elif part.isdigit() and 1 <= int(part) <= max_val:
            selected.add(int(part))
    return sorted(selected)
