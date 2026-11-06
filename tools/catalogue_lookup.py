"""Look up catalogue products for authoring scripted calls:  python tools\\catalogue_lookup.py "equity income" [class]

Prints product ids (family, share class, display name) whose fund name contains every given word. With a second argument
the output is limited to that share class key (A, C, Advisor, R6, R, I, ETF, W).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "catalogue" / "products.json"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    words = argv[0].lower().split()
    cls = argv[1] if len(argv) > 1 else None
    catalogue = json.loads(DATA.read_text(encoding="utf-8"))
    hits = [p for p in catalogue["products"]
            if all(w in p["fund_name"].lower() for w in words) and (cls is None or p["share_class"].lower() == cls.lower())]
    for p in hits[:60]:
        print(f'{p["product_id"]}  {p["family_id"]:5} {p["share_class"]:8} {p["display_name"]}  [{p["vehicle"]}]')
    print(f"{len(hits)} match(es)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
