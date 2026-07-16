"""The compose gate's other two parts: **wiring** and **e2e** (design §4.1, parts 1 and 3).

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

if TYPE_CHECKING:  # pragma: no cover
    from .core import ComposePlan


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _replica(pipeline_dir: Path, plan: ComposePlan) -> Path:
    """Copy the compiled artifacts to a scratch dir and stand in zero-byte FASTQs **there**.

    This gate needs its inputs to exist — `snakemake -n` raises `MissingInputException` otherwise —
    and `compose` is a pure function that runs with no FASTQ on disk. So the dry run is given
    zero-byte stand-ins: it validates *wiring*, not data.

    **The stand-ins go in a throwaway copy, and that is the whole point of this function.** They used
    to be touched straight into the run directory, at `pipeline_dir / row["path"]`, and never removed.
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


def wiring_gate(pipeline_dir: Path, plan: ComposePlan) -> str:
    """`snakemake -n -p` over a throwaway replica. ``skip`` only if snakemake is absent."""
    if not have("snakemake"):
        return "skip"
    scratch = _replica(pipeline_dir, plan)
    try:
        proc = subprocess.run(
            ["snakemake", "-d", str(scratch), "-s", str(scratch / "Snakefile"), "-n", "-p"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return "fail"
        # A dry run that plans NOTHING exits 0 and says "Nothing to be done", which is exactly what a
        # workflow with no reachable target does — and for most of this repo's life that is what the
        # generated wrapper produced, because an `include:`d `rule all` is not a default target. A
        # gate cannot tell "correct" from "planned nothing" by exit code, so it must look.
        if "Nothing to be done" in (proc.stdout or ""):
            return "fail"
        return "pass"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def e2e_gate() -> str:
    """The real count-matrix run is owned by ``seqforge kb e2e`` — never implicitly run by compose."""
    if not (have("STAR") and _have_genome()):
        return "skip"
    return "skip"  # available but deliberately not run here; invoke `seqforge kb e2e`


def _have_genome() -> bool:
    import importlib.util

    return importlib.util.find_spec("genome") is not None
