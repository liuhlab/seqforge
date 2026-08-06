"""The compose gate's other two parts: **wiring** and **e2e**.

Both depend on a toolchain seqforge does not own, so both report **``skip``** — never ``pass`` — when
that toolchain is absent. A gate that silently reports ``pass`` because it did not run is worse than
no gate at all: green CI would then be mistaken for coverage.

- **wiring** (`snakemake -n -p`): needs the `snakemake` binary, which is now a declared dependency
  (`[tool.pixi.feature.wf]`), so this gate actually runs. It works in a **throwaway copy** of the run
  directory and never writes into the run directory itself — see the incident note on `_replica`.
- **e2e** (the real count-matrix run): needs STAR + liulab-genome + network. It is a Linux/cluster
  operation, deliberately NOT run inside `compose`; `seqforge kb e2e` owns it.

**Why `-p`, and why no `--lint`.** Both were measured on 2026-07-15 rather than reasoned about:

- `-p` forces Snakemake to *format* every `shell:` block while planning. Without it a dry run never
  renders the command, so a `KeyError` on a missing param — `starsolo.smk` dereferencing
  `soloCBstart` for a `CB_UMI_Complex` chemistry that has no such key — plans clean and dies on a
  compute node. `-p` is the difference between this gate catching that and rubber-stamping it.
- `--lint` was in this gate and is now gone. It fails on *every* rule we ship, for a missing `log:`
  directive and "mixed rules and functions in same snakefile" — style opinions, not wiring facts. A
  gate that is red for a correct config teaches people to ignore it, and then it guards nothing.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ..models.resolve import GateVerdict
from ..pipeline import CompiledPipeline

if TYPE_CHECKING:  # pragma: no cover
    from .core import ComposePlan

#: How much of a refused subprocess's stderr rides back on the verdict. A **tail**, because snakemake
#: prints the plan before it prints what went wrong, so the last lines are the ones written for the
#: person who hit it. Bounded at all because a `ComposeResult` is JSON on a machine's stdout and a
#: subprocess's stderr has no ceiling.
REASON_TAIL_LINES = 40


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _tail(stream: str | None) -> list[str]:
    """The last :data:`REASON_TAIL_LINES` non-empty lines of a subprocess stream."""
    lines = [line for line in (stream or "").splitlines() if line.strip()]
    return lines[-REASON_TAIL_LINES:]


def _replica(pipeline_dir: Path, plan: ComposePlan) -> Path:
    """Copy the compiled artifacts to a scratch dir and stand in zero-byte FASTQs **there**.

    This gate needs its inputs to exist — `snakemake -n` raises `MissingInputException` otherwise —
    and `compose` is a pure function that runs with no FASTQ on disk. So the dry run is given
    zero-byte stand-ins: it validates *wiring*, not data.

    **The stand-ins go in a throwaway copy, and that is the whole point of this function.** They used
    to be touched straight into the run directory itself, at each unit's `row["path"]`, and never
    removed.
    That was invisible only because `snakemake` was in no dependency table, so this gate never ran. The
    moment it did, the run directory would contain zero-byte files named exactly like the FASTQs, STAR
    would read them, and the pipeline would emit an empty matrix and **exit 0** — a silent, plausible,
    wrong answer, which is the one failure class this project exists to prevent.

    An **absolute** unit path is skipped rather than stood in: it names a real FASTQ the caller pointed
    us at with `--fastq-dir`, and `scratch / "/abs/path"` is `/abs/path`, so touching it would create a
    zero-byte file *at the real location*. If such a file is genuinely missing the dry run fails, and
    failing is right — the caller said it was there.
    """
    scratch = Path(tempfile.mkdtemp(prefix="seqforge-wiring-"))
    for item in pipeline_dir.iterdir():
        if item.name == ".snakemake":
            continue
        dst = scratch / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    for row in plan.units:
        path = Path(row["path"])
        if path.is_absolute():
            continue
        target = scratch / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
    return scratch


def wiring_gate(pipeline_dir: Path, plan: ComposePlan) -> GateVerdict:
    """`snakemake -n -p` over a throwaway replica. ``skip`` only if snakemake is absent.

    **The refusal carries snakemake's own words.** This captured both streams and discarded both,
    returning a bare ``"fail"`` that `compose` turned into exit 3 — so a module's
    `InputFunctionException`, a sentence written to be read by whoever hit it, never reached them. From
    outside, an unexplained ``fail`` and a silent pass are hard to tell apart, and #267's own triage
    mis-read one as the other.
    """
    if not have("snakemake"):
        return GateVerdict(
            status="skip", reason=["snakemake is not on PATH, so the wiring was never planned"]
        )
    scratch = _replica(pipeline_dir, plan)
    # A copy of a pipeline directory is a pipeline directory, so the wrapper is located through the
    # module that owns that layout. This gate spelled the name itself, which made it one of five
    # places that had to agree on what the composer writes — and the only one whose job is to prove
    # the composer's output runs.
    wrapper = CompiledPipeline(scratch).snakefile
    try:
        proc = subprocess.run(
            ["snakemake", "-d", str(scratch), "-s", str(wrapper), "-n", "-p"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            # BOTH streams, stderr first, and that order is measured rather than assumed: snakemake 8
            # reports an `InputFunctionException` raised while building the DAG on **stdout**, beneath
            # the plan, and leaves stderr empty. Reading only the stream an error "should" be on is how
            # this gate would go on saying nothing while looking like it now says something.
            return GateVerdict(
                status="fail",
                reason=[
                    f"`snakemake -n -p` exited {proc.returncode}",
                    *(_tail(proc.stderr) or _tail(proc.stdout)),
                ],
            )
        # A dry run that plans NOTHING exits 0 and says "Nothing to be done", which is exactly what a
        # workflow with no reachable target does — and for most of this repo's life that is what the
        # generated wrapper produced, because an `include:`d `rule all` is not a default target. A
        # gate cannot tell "correct" from "planned nothing" by exit code, so it must look.
        if "Nothing to be done" in (proc.stdout or ""):
            # Snakemake's own output says only "Nothing to be done", which describes what it did and
            # not what is wrong. This sentence is the reason, so it is written here rather than tailed.
            return GateVerdict(
                status="fail",
                reason=[
                    "`snakemake -n -p` planned nothing and exited 0: the wrapper exposes no "
                    "reachable target. An `include:`d `rule all` is not a default target"
                ],
            )
        return GateVerdict(status="pass")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def e2e_gate() -> GateVerdict:
    """The real count-matrix run is owned by ``seqforge kb e2e`` — never implicitly run by compose.

    Both branches ``skip`` and always did; what they never said is **which** skip this is. "The
    toolchain is absent" and "the toolchain is here and running it is not compose's job" are different
    facts about a green result, and only the second means a reader can go get the coverage.
    """
    missing = [name for name, present in (("STAR", have("STAR")), ("liulab-genome", _have_genome()))
               if not present]  # fmt: skip
    if missing:
        return GateVerdict(
            status="skip", reason=[f"the end-to-end toolchain is incomplete here: {missing} absent"]
        )
    return GateVerdict(
        status="skip",
        reason=[
            "STAR and liulab-genome are both here, but compose never runs the real count matrix — "
            "`seqforge kb e2e` owns it"
        ],
    )


def _have_genome() -> bool:
    import importlib.util

    return importlib.util.find_spec("genome") is not None
