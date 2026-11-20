"""PROTOTYPE, throwaway: recompute a reading's relations from reading.yaml alone.

Run: python derive.py   (needs PyYAML; reads reading.yaml and writes relations.derived.yaml
beside this file).
"""

from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).parent
LABELS = (
    "biosample donor assay target control_type species sex age development_stage tissue "
    "cell_type cell_line disease genotype strain treatment dose duration"
).split()
MARKS = ("quoted", "inferred", "unknown", "na")


def level(claim):
    """The value two samples are compared on: the minted id, else number and unit, else text."""
    for gap in ("unknown", "na"):
        if gap in claim:
            return gap
    if "id" in claim:
        return claim["id"]
    if "value" in claim:
        return f"{claim['value']} {claim['unit']}"
    return claim["text"]


def expand_and_check(reading):
    """Apply the inheritance rule, then refuse a sample with a missing or badly backed claim."""
    passages = reading["passages"]
    rows = {}
    for gsm, own in reading["samples"].items():
        row = {**reading["shared"], **own}
        assert set(row) == set(LABELS), f"{gsm}: labels {set(row) ^ set(LABELS)}"
        for label, claim in row.items():
            assert sum(m in claim for m in MARKS) == 1, f"{gsm}.{label}: needs exactly one mark"
            assert 0 <= claim["p"] <= 1, f"{gsm}.{label}: p"
            if "quoted" in claim:
                quotes = [passages[n] for n in claim["quoted"]]  # KeyError: no such passage
                for q in quotes:  # a passage may only back the samples whose record holds it
                    held = q.get("record", "each_sample")
                    ok = "paper" in q or held == "each_sample" or str(held).startswith("GSE")
                    assert ok or gsm in held, f"{gsm}.{label}: passage is from {held}"
                said = claim.get("text", "")
                assert any(said in q["quote"] for q in quotes), f"{gsm}.{label}: text not quoted"
        rows[gsm] = {label: level(row[label]) for label in LABELS}
    return rows


def groups(rows, labels):
    """Samples that agree on every one of `labels`, keyed by those values."""
    out = defaultdict(list)
    for gsm, row in rows.items():
        out[tuple(row[x] for x in labels)].append(gsm)
    return out


def derive(reading):
    rows = expand_and_check(reading)
    factors = [f["label"] for f in reading["factors"]]
    cells = groups(rows, ["assay", "target", "control_type", *factors])
    rel = {"contrasts": [], "replicates": [], "series": [], "shared_biosample": [],
           "signal_to_control": []}
    rel["replicates"] = [g for g in cells.values() if len(g) > 1]
    for i, f in enumerate(reading["factors"]):
        for key, test in cells.items():  # the other side: same cell but this factor at baseline
            base_key = (*key[: 3 + i], f["baseline"], *key[4 + i :])
            if key != base_key and base_key in cells:
                fixed = {x: v for x, v in zip(factors, key[3:]) if x != f["label"]}
                rel["contrasts"].append({"factor": f["label"], "test_level": key[3 + i],
                                         "test": list(test), "baseline": list(cells[base_key]),
                                         "fixed": fixed})
        if f["ordered"]:  # a time or dose series: the levels in order, other factors fixed
            rest = [x for x in factors if x != f["label"]]
            for fixed, members in groups(rows, ["assay", "target", *rest]).items():
                by_level = groups({g: rows[g] for g in members}, [f["label"]])
                steps = sorted(by_level, key=lambda k: float(str(k[0]).split()[0]))
                rel["series"].append({"factor": f["label"],
                                      "steps": [{k[0]: by_level[k]} for k in steps]})
    rel["shared_biosample"] = [g for g in groups(rows, ["biosample"]).values() if len(g) > 1]
    design = ["biosample", *factors]  # a control library pairs with signal on the same material
    for key, members in groups(rows, design).items():
        controls = [g for g in members if rows[g]["control_type"] != "na"]
        for g in members:
            if rows[g]["target"] != "na" and g not in controls:
                rel["signal_to_control"] += [{"signal": g, "control": c} for c in controls]
    return rel


if __name__ == "__main__":
    reading = yaml.safe_load((HERE / "reading.yaml").read_text())
    head = "# PROTOTYPE, throwaway. Written by derive.py from reading.yaml. Never edit by hand.\n"
    body = yaml.safe_dump(derive(reading), sort_keys=False, default_flow_style=None, width=100)
    (HERE / "relations.derived.yaml").write_text(head + body)
    print(head + body)
