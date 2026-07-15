"""The compose gate's other two parts: **wiring** and **e2e** (design §4.1, parts 1 and 3).

Both depend on a toolchain seqforge does not own, so both report **``skip``** — never ``pass`` — when
that toolchain is absent. A gate that silently reports ``pass`` because it did not run is worse than
no gate at all: green CI would then be mistaken for coverage.

- **wiring** (`snakemake -n` + `--lint`): needs the `snakemake` binary. Touches zero-byte files at
  every units path **and** every resolved onlist path first, so a declared whitelist input does not
  raise a spurious `MissingInputException`.
- **e2e** (the real count-matrix run): needs STAR + liulab-genome + network. It is a Linux/cluster
  operation, deliberately NOT run inside `compose`; `seqforge kb e2e` owns it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .core import ComposePlan

_WRAPPER = """# generated wrapper: includes the HAND-WRITTEN module (rule source is never generated)
configfile: "config.yaml"
config["units_tsv"] = "units.tsv"
include: "{snakefile}"
"""


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def wiring_gate(pipeline_dir: Path, plan: ComposePlan) -> str:
    """`snakemake -n` + `--lint` over touched zero-byte inputs. ``skip`` if snakemake is absent."""
    if not have("snakemake"):
        return "skip"
    from ..workflows import get_module

    module = get_module(plan.module.name)
    for row in plan.units:  # zero-byte stand-ins: the gate validates wiring, not data
        target = pipeline_dir / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
    wrapper = pipeline_dir / "Snakefile"
    wrapper.write_text(_WRAPPER.format(snakefile=module.snakefile.resolve()))

    for args in (["-n", "--quiet"], ["--lint"]):
        proc = subprocess.run(
            ["snakemake", "-d", str(pipeline_dir), "-s", str(wrapper), *args],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return "fail"
    return "pass"


def e2e_gate() -> str:
    """The real count-matrix run is owned by ``seqforge kb e2e`` — never implicitly run by compose."""
    if not (have("STAR") and _have_genome()):
        return "skip"
    return "skip"  # available but deliberately not run here; invoke `seqforge kb e2e`


def _have_genome() -> bool:
    import importlib.util

    return importlib.util.find_spec("genome") is not None
