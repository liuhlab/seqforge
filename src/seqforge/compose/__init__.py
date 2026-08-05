"""``compose`` — manifest -> pipeline configuration (emit data, never code).

A pure function of the manifest plus two versioned inputs recorded in provenance (the KB and the
hand-written workflow modules). It selects a module and emits ``config.yaml`` + ``units.tsv``; it
never writes rule source. The three-part gate runs here: the deterministic **params**
assertions always, **wiring** (`snakemake -n`/`--lint`) and **e2e** (the count-matrix run) only when
their toolchain exists — otherwise ``skip``, never a silent ``pass``.
"""

from __future__ import annotations

from .admission import Admission, admit
from .core import ComposeError, ComposePlan, compose, plan
from .gates import e2e_gate, wiring_gate
from .params import RECIPE_PARAM_KEYS, param_owners, params_gate, processing_params, render_param

# `admission.py` exports two helpers this list deliberately does not re-export. `sample_reads` has
# exactly one caller — `admit`, four lines below it in the same file — and `render_record` is reached
# from one module over (`core.py`) and from one test, which imports it out of `admission` directly.
# Neither crosses the package boundary, and re-exporting a helper nobody outside the package calls
# invites the next caller to depend on it from here, where the import that has to move later is
# somebody else's.
#
# `render_record` has the sharper reason of the two, and it is the one to know before widening this
# list: THERE ARE TWO FUNCTIONS WITH THAT NAME. `compose.admission.render_record` renders an
# exclusion record; `harvest.normalize.render_record` renders an archive record into a document, and
# only that one is imported across packages, which is why only that one sits on a package surface.
# Publish compose's here and `from seqforge.compose import render_record` and `from seqforge.harvest
# import render_record` become two unrelated functions one import line apart, with nothing at either
# call site to say which was meant. Renaming either is its own decision rather than a drive-by;
# declining to collide on a package surface costs nothing and is this list's job.
__all__ = [
    "compose",
    "plan",
    "ComposePlan",
    "ComposeError",
    "Admission",
    "admit",
    "params_gate",
    "param_owners",
    "processing_params",
    "RECIPE_PARAM_KEYS",
    "render_param",
    "wiring_gate",
    "e2e_gate",
]
