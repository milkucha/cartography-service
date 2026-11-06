"""Fail when the Java ColumnSurface terrain whitelist differs from terrain_rules.py.

Usage: python tools/check_terrain_rules_sync.py [path/to/ColumnSurface.java]
Default Java path: ../plugin/src/main/java/com/topography/surface/ColumnSurface.java
(the sibling plugin worktree/checkout).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import terrain_rules  # noqa: E402

DEFAULT_JAVA = ROOT.parent / "plugin" / "src" / "main" / "java" / "com" / "topography" / "surface" / "ColumnSurface.java"


def _strings(block: str) -> list[str]:
    return re.findall(r'"([^"]*)"', block)


def java_rules(text: str) -> dict:
    def grab(pattern):
        m = re.search(pattern, text, re.S)
        if not m:
            raise SystemExit(f"Could not find {pattern!r} in Java source")
        return _strings(m.group(1))

    return {
        "exact": grab(r"GROUND_EXACT\s*=\s*prefixed\((.*?)\);"),
        "suffixes": grab(r"GROUND_SUFFIXES\s*=\s*\{(.*?)\};"),
        "excluded": grab(r"GROUND_SUFFIX_EXCLUDED\s*=\s*\{(.*?)\};"),
    }


def compare(java_text: str) -> list[str]:
    j = java_rules(java_text)
    py = {
        "exact": list(terrain_rules.GROUND_EXACT_NAMES),
        "suffixes": list(terrain_rules.GROUND_SUFFIXES),
        "excluded": list(terrain_rules.GROUND_SUFFIX_EXCLUDED),
    }
    problems = []
    for key in py:
        if set(py[key]) != set(j[key]):
            problems.append(
                f"{key}: python-only={sorted(set(py[key]) - set(j[key]))} "
                f"java-only={sorted(set(j[key]) - set(py[key]))}"
            )
    return problems


def main(argv) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_JAVA
    problems = compare(path.read_text(encoding="utf-8"))
    if problems:
        print("terrain rules out of sync with", path)
        for p in problems:
            print("  ", p)
        return 1
    print("terrain rules in sync with", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
