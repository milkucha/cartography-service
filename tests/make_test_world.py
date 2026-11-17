"""Generate a tiny synthetic Minecraft save for Phase 4 manual scenarios.

Usage (from the repository root):
    python tests/make_test_world.py --out ./test-world
    python main.py --world ./test-world --preload

The save contains one 2x2-chunk region with a straight coastline: grass land
at x < 16 (surface y 64-66) and an ocean at x >= 16 (ground y 60, water y 63).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="test-world",
        help="Output save root (a world/region folder is created inside)",
    )
    args = parser.parse_args(argv)

    world = helpers.build_coast_world(args.out)
    print(f"Wrote synthetic world to {world}")
    print("Expected queries:")
    print("  !topography(10, 8, 22, 8)   -> land/water crossing at x=16")
    print("  !shoreline(10, 8, 8)        -> coastline polyline near x=16")
    print("  !area_composition(8, 8, 6)  -> land composition")
    print("  !recall(10, 8)              -> chunk containing (10, 8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
