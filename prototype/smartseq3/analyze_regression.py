"""What exactly moved? Winner / exit-code / score deltas per case, per variant.

Throwaway prototype for wayfinder #234.
"""

from __future__ import annotations

import json
from pathlib import Path

R = json.loads((Path(__file__).parent / "results" / "regression.json").read_text())


def winners(per_variant) -> list[str | None]:
    return [r["candidates"][0]["tech"] if r["candidates"] else None for r in per_variant["runs"]]


def scores(per_variant) -> list[dict[str, float | None]]:
    return [{c["tech"]: c["value"] for c in r["candidates"]} for r in per_variant["runs"]]


for variant in ("draft-R", "draft-S"):
    print(f"\n################ {variant} ################")
    winner_changed, exit_changed, score_changed, only_new_cand = [], [], [], []
    for cid, per in sorted(R.items()):
        if "skipped" in per:
            continue
        a, b = per["stock"], per[variant]
        if json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True):
            continue
        wa, wb = winners(a), winners(b)
        if wa != wb:
            winner_changed.append((cid, wa, wb))
        if a["exit_code"] != b["exit_code"]:
            exit_changed.append((cid, a["exit_code"], b["exit_code"]))
        sa, sb = scores(a), scores(b)
        moved_existing = []
        for ra, rb in zip(sa, sb):
            for tech, v in ra.items():
                if tech in rb and rb[tech] != v:
                    moved_existing.append((tech, v, rb[tech]))
        if moved_existing:
            score_changed.append((cid, moved_existing[:3]))
        else:
            newc = sorted({t for rb in sb for t in rb} - {t for ra in sa for t in ra})
            rank = [
                (rb_i, [c["tech"] for c in b["runs"][rb_i]["candidates"]].index("smartseq3"))
                for rb_i in range(len(b["runs"]))
                if "smartseq3" in [c["tech"] for c in b["runs"][rb_i]["candidates"]]
            ]
            sc = [
                round(b["runs"][i]["candidates"][j]["value"], 4)
                for i, j in rank
                if b["runs"][i]["candidates"][j]["value"] is not None
            ]
            win = [round(b["runs"][i]["candidates"][0]["value"], 4) for i, _ in rank]
            only_new_cand.append((cid, newc, [f"rank{j+1}" for _, j in rank][:4], sc[:4], win[:4]))

    print(f"WINNER changed on:    {len(winner_changed)} {winner_changed}")
    print(f"EXIT CODE changed on: {len(exit_changed)} {exit_changed}")
    print(f"EXISTING SCORE moved: {len(score_changed)} {score_changed}")
    print(f"ONLY a new losing candidate appended: {len(only_new_cand)}")
    for cid, newc, ranks, sc, win in only_new_cand:
        print(f"   {cid:<40} new={newc} smartseq3 at {ranks} score={sc} vs winner {win}")
