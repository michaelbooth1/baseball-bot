"""Tiny print helpers shared by every cmd_*."""
from __future__ import annotations

from typing import Any, List


def _print_header(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)


def _print_block(label: str, value: Any) -> None:
    print(f"  {label}: {value}")


def _print_checklist(items: List[str]) -> None:
    print()
    print("Next actions:")
    for item in items:
        print(f"  - {item}")
