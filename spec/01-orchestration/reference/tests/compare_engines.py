"""Engine conformance: compare the side-effect activity ID sequences recorded by
`test_scenarios.py --dump` for the two engines.

    python tests/compare_engines.py pregel.json interp.json

Sequential nodes must produce identical sequences. Within a parallel fan-out (feature/api_change
`skeptic`+`impact`, hotfix `qa`+`review`) LangGraph and asyncio.gather may schedule the branch activities
in a different order inside one workflow task, so each maximal block of IDs belonging to parallel-branch
nodes is compared as a multiset.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

PARALLEL_NODES = {"skeptic", "impact", "qa", "review"}


def normalize(seq: list[str]) -> list[object]:
    out: list[object] = []
    block: list[str] = []
    for aid in seq:
        if aid.split(".", 1)[0] in PARALLEL_NODES:
            block.append(aid)
            continue
        if block:
            out.append(Counter(block))
            block = []
        out.append(aid)
    if block:
        out.append(Counter(block))
    return out


def main(a: str, b: str) -> int:
    ra, rb = json.load(open(a))["results"], json.load(open(b))["results"]
    bad = 0
    for name in sorted(set(ra) | set(rb)):
        if name not in ra or name not in rb:
            print(f"MISSING {name}")
            bad += 1
            continue
        sa, sb = ra[name]["activity_ids"], rb[name]["activity_ids"]
        same = normalize(sa) == normalize(sb)
        print(f"{'SAME' if same else 'DIFF'} {name}: {len(sa)} side-effect activities"
              + ("" if same else f"\n  A={sa}\n  B={sb}"))
        bad += 0 if same else 1
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
