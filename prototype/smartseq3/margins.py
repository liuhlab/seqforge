"""How close did the supports-only draft get to winning? The margin to the tie threshold (theta=0.02).

Throwaway prototype for wayfinder #234.
"""

from __future__ import annotations

import json
from pathlib import Path

R = json.loads((Path(__file__).parent / "results" / "regression.json").read_text())
THETA = 0.02

rows = []
for cid, per in sorted(R.items()):
    if "skipped" in per:
        continue
    for run in per["draft-S"]["runs"]:
        cands = {c["tech"]: c["value"] for c in run["candidates"] if c["value"] is not None}
        if "smartseq3" not in cands:
            continue
        top = run["candidates"][0]
        if top["tech"] == "smartseq3":
            rows.append((cid, run["run"], "SMARTSEQ3 WON", 0.0))
            continue
        rows.append((cid, run["run"], top["tech"], round(top["value"] - cands["smartseq3"], 4)))

rows.sort(key=lambda r: r[3])
print(f"{'case':<38} {'run':<14} {'winner':<26} margin_over_smartseq3")
for cid, run, w, m in rows:
    flag = "  <-- INSIDE THETA" if m <= THETA else ""
    print(f"{cid:<38} {str(run):<14} {w:<26} {m:>8}{flag}")
print(f"\nruns where smartseq3 is a candidate: {len(rows)}")
print(f"minimum margin: {min(r[3] for r in rows)}  (theta = {THETA})")
