"""``compose`` — manifest -> pipeline configuration (emit data, never code: R1).

A pure function of the manifest plus two versioned inputs recorded in provenance (the KB and the
hand-written workflow modules). It selects a module and emits ``config.yaml`` + ``units.tsv``; it
never writes rule source. The three-part gate (design §4.1) runs here: the deterministic **params**
assertions always, **wiring** (`snakemake -n`/`--lint`) and **e2e** (the count-matrix run) only when
their toolchain exists — otherwise ``skip``, never a silent ``pass``.
"""

from __future__ import annotations

from .core import ComposeError, ComposePlan, compose, plan
from .gates import e2e_gate, wiring_gate
from .params import params_gate, render_param

__all__ = [
    "compose",
    "plan",
    "ComposePlan",
    "ComposeError",
    "params_gate",
    "render_param",
    "wiring_gate",
    "e2e_gate",
]
