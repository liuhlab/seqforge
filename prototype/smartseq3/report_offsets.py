import json
import os

SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
d = json.load(open(os.path.join(SP, "offsets.json")))


def row(s):
    e = s
    return (
        f"{e['n_reads']:>6} {e['hits_at_0']:>6} {e['rate_at_0']*100:>7.2f}% "
        f"{e['best_nonzero_hits']:>6}@{str(e['best_nonzero_offset']):>4} "
        f"{e['best_nonzero_hits_first40']:>5} "
        f"{('%.1f' % e['enrich_vs_best_other']) if e['enrich_vs_best_other'] else 'inf':>7} "
        f"{('%.1f' % e['enrich_vs_best_other_first40']) if e['enrich_vs_best_other_first40'] else 'inf':>7} "
        f"{('%.4f' % e['purity']) if e['purity'] is not None else '  n/a':>7}"
    )


hdr = "  cell mate mm |  reads  hit@0   rate@0  bestOther@off  b40  enrAll  enr40  purity"
print("=== SMART-seq3 tag ATTGCGCAATG ===")
print(hdr)
for c in sorted(d["cells"], key=lambda c: int(c["cell"])):
    for mate in ("R1", "R2"):
        for mm, key in ((0, f"{mate}_exact"), (2, f"{mate}_mm2")):
            print(f"{c['cell']:>6} {mate:>4} {mm:>2} |", row(c[key]))
print()
print("=== bulk control GSE283483-bulk (SRR31555437) ===")
print(hdr)
for b in d["bulk"]:
    for mm, key in ((0, "exact"), (2, "mm2")):
        print(f"{'bulk':>6} {b['mate']:>4} {mm:>2} |", row(b[key]))
    print(f"       mode_len={b['mode_len']}")
print()
print("=== Tn5 mosaic end CTGTCTCTTATACACATCT (mm<=1) — a NON-positional motif control ===")
print(hdr)
for c in sorted(d["cells"], key=lambda c: int(c["cell"])):
    for mate in ("R1", "R2"):
        print(f"{c['cell']:>6} {mate:>4}  1 |", row(c[f"{mate}_ME"]))
for b in d["bulk"]:
    print(f"{'bulk':>6} {b['mate']:>4}  1 |", row(b["ME"]))
print()
print("=== random 11-mer controls (mm<=2) — the empirical chance floor ===")
print(hdr)
for c in d["controls"]:
    print(f"{c['label']:>28} |", row(c))
print()
print("=== read lengths ===")
for c in sorted(d["cells"], key=lambda c: int(c["cell"])):
    print(c["cell"], c.get("R1_len"), c.get("R2_len"))
