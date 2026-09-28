"""Fail unless every module of the integration has at least 95 % line coverage.

Reads coverage.json from: pytest --cov=custom_components.halfhour --cov-report=json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MINIMUM = 95.0


def main(path: str = "coverage.json") -> int:
    files = json.loads(Path(path).read_text())["files"]
    if not files:
        print(f"{path} lists no files; did pytest run with --cov?")
        return 1
    low = {name: f["summary"]["percent_covered"] for name, f in files.items() if f["summary"]["percent_covered"] < MINIMUM}
    for name, f in sorted(files.items()):
        print(f"{f['summary']['percent_covered']:6.1f} %  {name}")
    if low:
        print(f"\nUnder {MINIMUM:.0f} %:")
        for name, pct in sorted(low.items()):
            print(f"  {name}: {pct:.1f} %")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
