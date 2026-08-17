"""PROTOTYPE — throwaway. Run every candidate assertion against every deliberate breakage.

    pixi run -e test python prototypes/split-chimera/run.py            # the matrix
    pixi run -e test python prototypes/split-chimera/run.py <breakage> # one column, verbose

**The question** (#414): which assertions actually catch a broken split, and which fixtures they
need to do it? A row that no assertion reddens is a defect the real test would ship. A column that
reddens on nothing is an assertion the real test should not carry.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent))

import pysam  # noqa: E402

from fixtures import SHAPES, Fragment, Shape, chimeric_bam, plate, single_assembly_bam  # noqa: E402
from genome.chimera import split_suffixed  # noqa: E402
from splitter import Breakage, SplitRefusal, Summary, split  # noqa: E402

BOLD, DIM, RED, GREEN, RESET = "\x1b[1m", "\x1b[2m", "\x1b[31m", "\x1b[32m", "\x1b[0m"


def _outputs(shape: Shape, work: Path) -> dict[str, Path]:
    return {c.name: work / f"{c.name}.bam" for c in shape.components}


def _header(path: Path) -> dict[str, Any]:
    with pysam.AlignmentFile(str(path), "rb", check_sq=False) as fh:
        return fh.header.to_dict()


def _records(path: Path) -> list[pysam.AlignedSegment]:
    with pysam.AlignmentFile(str(path), "rb", check_sq=False) as fh:
        return list(fh.fetch(until_eof=True))


# ---- the candidate assertions --------------------------------------------------------------------
# Each takes the finished split and returns a failure reason, or None when it is satisfied.

Check = Callable[[Shape, dict[str, Path], Summary, tuple[Fragment, ...]], str | None]


def a1_routing(shape, outputs, summary, frags) -> str | None:
    """Every uniquely-placed read lands in the Component it came from — the map's bar."""
    for comp, path in outputs.items():
        landed = {r.query_name for r in _records(path)}
        want = {f.name for f in frags if f.kind == "unique" and f.component == comp}
        stray = {f.name for f in frags if f.kind == "unique" and f.component != comp} & landed
        if not want <= landed:
            return f"{comp} lost {sorted(want - landed)}"
        if stray:
            return f"{comp} took {sorted(stray)}"
    return None


def a2_sq_identity(shape, outputs, summary, frags) -> str | None:
    """@SQ is byte-equal to a single-assembly run's — names, lengths AND order."""
    for comp, path in outputs.items():
        want = shape.single_assembly_sq(comp)
        got = [{"SN": e["SN"], "LN": e["LN"]} for e in _header(path).get("SQ", [])]
        if got != want:
            return f"{comp}: {[e['SN'] for e in got]} != {[e['SN'] for e in want]}"
    return None


def a3_hd(shape, outputs, summary, frags) -> str | None:
    """@HD survives, so the output still declares itself coordinate-sorted."""
    for comp, path in outputs.items():
        if _header(path).get("HD", {}).get("SO") != "coordinate":
            return f"{comp} lost HD/SO"
    return None


def a4_refdict(shape, outputs, summary, frags) -> str | None:
    """The BINARY reference dictionary was remapped, not just the text header."""
    want = {
        f.name: split_suffixed(f.contig, shape.separator)[0]
        for f in frags
        if f.kind == "unique"
    }
    for comp, path in outputs.items():
        for rec in _records(path):
            if rec.query_name in want and rec.reference_name != want[rec.query_name]:
                return f"{comp}: {rec.query_name} on {rec.reference_name}, want {want[rec.query_name]}"
    return None


def a5_mate_component(shape, outputs, summary, frags) -> str | None:
    """A record's mate is on the same contig it is — #409's free per-record check."""
    for comp, path in outputs.items():
        for rec in _records(path):
            if rec.next_reference_id >= 0 and rec.next_reference_id != rec.reference_id:
                return f"{comp}: {rec.query_name} mate on tid {rec.next_reference_id}"
    return None


def a6_read1_equals_read2(shape, outputs, summary, frags) -> str | None:
    """Both mates made the same keep decision — the cheap stand-in a stateless filter allows."""
    for comp in outputs:
        if summary.read1[comp] != summary.read2[comp]:
            return f"{comp}: {summary.read1[comp]} read1 vs {summary.read2[comp]} read2"
    return None


def a7_accounting(shape, outputs, summary, frags) -> str | None:
    """Reads in equals reads kept plus reads dropped — nothing left without being counted."""
    return None if summary.closes() else f"{summary.records_in} in, {dict(summary.kept)} kept, {dict(summary.dropped)} dropped"


def a8_drop_reasons(shape, outputs, summary, frags) -> str | None:
    """Every drop is attributed to the right reason, not merely absent."""
    want = {"multimapping": 0, "unmapped": 0, "secondary": 0, "supplementary": 0}
    for f in frags:
        if f.kind == "multimapper":
            want["multimapping"] += 2
        elif f.kind in want:
            want[f.kind] += 1
    got = {k: summary.dropped.get(k, 0) for k in want}
    return None if got == want else f"{got} != {want}"


def a9_pg(shape, outputs, summary, frags) -> str | None:
    """STAR's @PG/@CO kept verbatim, and the splitter recorded itself."""
    name = "_".join(sorted(c.name for c in shape.components))
    for comp, path in outputs.items():
        head = _header(path)
        pg = head.get("PG", [])
        star = [p for p in pg if p["ID"] == "STAR"]
        if not star or f"--genomeDir {name}" not in star[0].get("CL", ""):
            return f"{comp}: STAR's @PG was rewritten or lost"
        if not any(p["ID"] == "seqforge-split-chimera" for p in pg):
            return f"{comp}: no @PG for the splitter"
    return None


CHECKS: dict[str, Check] = {
    "routing": a1_routing,
    "sq_identity": a2_sq_identity,
    "hd": a3_hd,
    "refdict": a4_refdict,
    "mate_component": a5_mate_component,
    "read1_eq_read2": a6_read1_equals_read2,
    "accounting": a7_accounting,
    "drop_reasons": a8_drop_reasons,
    "pg": a9_pg,
}

# Two refusals, which need their own scenario rather than a finished split.
REFUSALS = ("refuses_partial", "refuses_unsplittable")


def _refusal_checks(shape: Shape, work: Path, breakage: Breakage) -> dict[str, str | None]:
    frags = plate(shape)
    bam = chimeric_bam(work / "partial.bam", shape, frags)
    out: dict[str, str | None] = {}

    partial = {shape.components[0].name: work / "partial_out.bam"}
    try:
        split(bam, partial, shape.separator, breakage)
        out["refuses_partial"] = "a partial request was accepted"
    except SplitRefusal:
        out["refuses_partial"] = None

    junk = Shape("junk", shape.components)
    unsplittable = work / "unsplittable.bam"
    header = pysam.AlignmentHeader.from_dict(
        {"HD": {"VN": "1.6", "SO": "coordinate"}, "SQ": [{"SN": "chrPlain", "LN": 100}]}
    )
    with pysam.AlignmentFile(str(unsplittable), "wb", header=header):
        pass
    try:
        split(unsplittable, _outputs(junk, work), shape.separator, breakage)
        out["refuses_unsplittable"] = "an unsplittable @SQ name was accepted"
    except SplitRefusal:
        out["refuses_unsplittable"] = None
    return out


def evaluate(shape: Shape, breakage: Breakage, cross_mate: bool = False) -> dict[str, str | None]:
    """Run one breakage against one fixture shape; return each check's failure reason or None."""
    work = Path(tempfile.mkdtemp(prefix="split-proto-"))
    try:
        frags = plate(shape, cross_mate=cross_mate)
        bam = chimeric_bam(work / "chimeric.bam", shape, frags)
        for comp in (c.name for c in shape.components):
            single_assembly_bam(work / f"{comp}.single.bam", shape, comp)
        outputs = _outputs(shape, work)
        try:
            summary = split(bam, outputs, shape.separator, breakage)
        except SplitRefusal as exc:
            return {name: f"refused: {exc}" for name in CHECKS} | _refusal_checks(
                shape, work, breakage
            )
        except Exception as exc:  # noqa: BLE001 — a crash is a loud catch, and worth distinguishing
            return {name: f"crashed: {type(exc).__name__}: {exc}" for name in CHECKS} | _refusal_checks(
                shape, work, breakage
            )
        results = {}
        for name, check in CHECKS.items():
            try:
                results[name] = check(shape, outputs, summary, frags)
            except Exception as exc:  # noqa: BLE001
                results[name] = f"crashed: {type(exc).__name__}: {exc}"
        return results | _refusal_checks(shape, work, breakage)
    finally:
        shutil.rmtree(work, ignore_errors=True)


#: A row is a breakage flag, except the two `[fixture]` rows: the splitter is CORRECT there and the
#: BAM is the thing that is wrong, which is the only way to make an input-guarding check fire.
BREAKAGES = (
    ["<none: the contract>"]
    + [f.name for f in fields(Breakage)]
    + ["[fixture] cross_component_mate", "[fixture] cross_mate + no_mate_check"]
)


def _row(breakage: str, shape: Shape) -> dict[str, str | None]:
    if breakage.startswith("<"):
        return evaluate(shape, Breakage())
    if breakage == "[fixture] cross_component_mate":
        return evaluate(shape, Breakage(), cross_mate=True)
    if breakage == "[fixture] cross_mate + no_mate_check":
        return evaluate(shape, Breakage(no_mate_check=True), cross_mate=True)
    return evaluate(shape, Breakage(**{breakage: True}))


def _matrix() -> None:
    names = list(CHECKS) + list(REFUSALS)
    label_w = max(len(b) for b in BREAKAGES) + 2
    print(f"\n{BOLD}Which assertion catches which broken split{RESET}")
    print(f"{DIM}cell = the fixture shapes that caught it; '.' = missed by every shape{RESET}\n")
    header = " " * label_w + "  ".join(f"{n[:14]:>14}" for n in names)
    print(BOLD + header + RESET)

    unc: list[str] = []
    loud: list[tuple[str, str]] = []
    caught_by: dict[str, set[str]] = {n: set() for n in names}
    for breakage in BREAKAGES:
        per_shape = {label: _row(breakage, shape) for label, shape in SHAPES.items()}

        # A split that refused or died never reached an assertion — every column would light up for
        # one cause. That is a LOUDER and cheaper catch than any assertion, so say so instead.
        died = {
            lbl: str(res[names[0]]).split(":")[0]
            for lbl, res in per_shape.items()
            if all(str(res[n]).startswith(("refused:", "crashed:")) for n in CHECKS)
        }
        if died:
            where = "every shape" if len(died) == len(SHAPES) else "+".join(sorted(died))
            kind = sorted(set(died.values()))[0]
            loud.append((breakage, kind))
            print(f"{breakage:<{label_w}}{GREEN}{kind} on {where}{RESET} "
                  f"{DIM}— no assertion needed{RESET}")
            continue

        cells = []
        any_caught = False
        for name in names:
            hits = sorted(lbl for lbl, res in per_shape.items() if res[name] is not None)
            if breakage.startswith("<"):
                cells.append(f"{GREEN}{'ok':>14}{RESET}" if not hits else f"{RED}{'FAILS':>14}{RESET}")
                continue
            if hits:
                any_caught = True
                caught_by[name].add(breakage)
                tag = "all" if len(hits) == len(SHAPES) else "+".join(h[:5] for h in hits)
                cells.append(f"{GREEN}{tag:>14}{RESET}")
            else:
                cells.append(f"{DIM}{'.':>14}{RESET}")
        if not breakage.startswith("<") and not any_caught:
            unc.append(breakage)
        print(f"{breakage:<{label_w}}" + "  ".join(cells))

    print()
    for name in names:
        if not caught_by[name]:
            print(f"{RED}assertion `{name}` caught nothing — it is decorative{RESET}")
    only = {n: c for n, c in caught_by.items() if c}
    for name, catches in only.items():
        unique = [b for b in catches if not any(b in c for n2, c in only.items() if n2 != name)]
        if unique:
            print(f"{BOLD}{name}{RESET} is the ONLY assertion catching: {', '.join(unique)}")
    covered = {b for b, _ in loud} | {
        flag for b, _ in loud for flag in b.replace("[fixture] ", "").split(" + ")
    }
    for b in unc:
        if b in covered:
            print(f"{DIM}`{b}` is reached only by a [fixture] row, where it is a loud catch{RESET}")
        else:
            print(f"{RED}nothing caught `{b}`{RESET}")


def _one(breakage: str) -> None:
    for label, shape in SHAPES.items():
        print(f"\n{BOLD}{breakage} on the {label} fixture{RESET} {DIM}(separator {shape.separator!r}){RESET}")
        for name, reason in _row(breakage, shape).items():
            mark = f"{GREEN}pass{RESET}" if reason is None else f"{RED}CAUGHT{RESET}"
            print(f"  {name:<20} {mark}  {DIM}{reason or ''}{RESET}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _one(sys.argv[1])
    else:
        _matrix()
