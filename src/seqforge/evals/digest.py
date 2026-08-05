"""The frozen-18 grade digest — the tier's before/after instrument, as code rather than as a comment.

**What it is for.** #225 constraint 3 asks that a change to the shipped path move *no per-case grade*
on the benchmark tier. Diffing an eighteen-row table by eye is worse than hashing it, so #231 recorded
a **timing-free grade digest**: the case count, the four tier-wide rates, and the whole per-case list
with every timing field stripped. Equal digest ⇒ no case moved **and** no graded value moved. Unequal
⇒ diff the tables.

**Why it is here now.** It lived as a copy-pasteable Python snippet in #231's resolution comment, and
prose in an issue cannot be run.

**The live baseline is ``aeff9af9ce5f626838d26c9c4f9860f51fd297dc25fe94c63495df0fa146807b`` at
``main`` @ ``3ab99ff``** (2026-08-04, ``--no-llm``): 18/18 ``correct``, ``field_accuracy`` 1.0, both
rates 0.0. It is reproduced twice on an unchanged tree **and independently from six branches** — two
based on ``5624f8e``, two on ``4adc182``, plus ``main`` itself at both of those and at ``3ab99ff``
with all six merged. So the removal of ``read_count`` from the signature vocabulary (#299) moved it
**not at all**, and neither did any of those six merges. The trees that agree are what make a
disagreement mean something.

**#267 took it before and after, and it did not move** (2026-08-05, ``--no-llm``, ``--trials 1``):
the same hex on both sides — at the merge-base ``7e2488f`` in a detached worktree, and on
``fix/267-single-end-plate`` with `smartseq3` declaring a single-end read set and ``KB_VERSION``
bumped to ``2026.8.6``. 18/18, ``field_accuracy`` 1.0, both rates 0.0, on both trees. The before was
taken in a *separate checkout of the same machine* rather than by stashing, because the branch was
being written while it ran; that is the shape to copy when a tree is not clean. **A zero here was
predictable and is recorded anyway**: the tier carries no single-end deposit and no Smart-seq3 one at
all (``GSE207085-nasal-prox1-96cells`` is outside :data:`FROZEN_18`, by #258), and `smartseq3`'s
``requires`` gate measures its motif at 0.00–0.15% on every non-SS3 read set, so no new read set can
be seated on any of the eighteen. That is the argument for *expecting* zero, and this paragraph
exists because this module's own rule is that an expected zero is not a measured one.

**It is not #231's ``247a9354eecd11773e3dc482f83cfb916c1b1a3edd3e47f1202d57298007f426``
(``27ffd05``), and nothing regressed — the report's SHAPE moved under it.** ``questions_asked`` is now
a dict (``total`` / ``per_case`` / ``missed``) where the 2026-07 baseline hashed a scalar. The
constant went stale silently while every grade it was protecting stayed put: a comparison instrument
reporting a difference that is not about the thing being compared. That is why the recipe ships as
code, why this module pins the **case list** and the **recipe** and not a value, and why **no test
asserts a digest** — a pinned constant is precisely what rotted, and what the tests hold instead is
the property it was for.

**A digest is quoted with the tree it was taken on, and what was not measured is not a finding.**
Every run above is already **post**-#297, the rename of the generic bulk entry. That rename does
change a graded ``library.chemistry`` string on ``GSE283483-bulk`` — but no pre-#297 number was ever
taken, so its effect on this digest is **unmeasured**, and an unmeasured effect is not an expected
move, a no-op, or anything else. Re-take the number on the tree you are about to change and diff it
against that same tree. A hex carried over from a neighbouring tree is exactly the mistake this
paragraph exists to stop, and the baseline above was itself misattributed to a neighbouring commit
once — caught only because the report it came from grades ``GSE283483-bulk`` at the post-#297 id.

**The exclusion, and why it is a refusal rather than a filter.** The recipe hashes ``n_cases`` and the
whole ``per_case`` list, so **equal-digest and add-a-case are incompatible instruments** (#258): a
nineteenth case moves ``n_cases``, adds a row, and — being published red — moves ``false_accept_rate``
off 0.0 by construction. The digest is therefore taken over a run of **exactly** the frozen eighteen,
which is one command::

    seqforge eval run --no-llm --cases evals/benchmark $(printf -- '--case %s ' <the 18 ids>)

and :func:`grade_digest` refuses a report holding anything else. Silently dropping the extra rows was
the alternative and it is the worse one twice over: the four tier-wide rates in the hash are computed
by ``build_report`` over *every* scored case, so a filtered nineteen-case report would hash rates that
are not the frozen tier's — and recomputing them here would be a second copy of ``build_report``'s
arithmetic, which is the shape this tree has had to fix three times. A refusal cannot be wrong quietly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

#: The eighteen cases the bar is scoped to: `evals/benchmark` as it stood at #231's baseline commit
#: `27ffd05`, re-confirmed against a whole-tier run on 2026-08-04 (`main` @ `3ab99ff`). Dated **data**,
#: not a derivation — reading the directory instead would defeat the point, since the whole purpose is
#: to name a set that does not grow when the corpus does. A case added after this line is outside the
#: bar by construction, which is the decision in #258 rather than an omission.
FROZEN_18: tuple[str, ...] = (
    "GSE110823",
    "GSE126954",
    "GSE208154",
    "GSE229022",
    "GSE234962",
    "GSE256266",
    "GSE266161-unmod-first-mixing",
    "GSE274290",
    "GSE282765-colon-crod-wta",
    "GSE283483-bulk",
    "GSE283483-multiome-atac",
    "GSE283483-multiome-gex",
    "GSE305031",
    "GSE310378-provsv-gfp-til",
    "GSE317744-ccr9ko-thymic-dc",
    "PRJNA1027859",
    "PRJNA1195922",
    "PRJNA658829",
)

#: The tier-wide keys #231 hashed, verbatim. Everything a clock touches is absent on purpose: `cost`
#: carries wall time and `per_case` rows carry `seconds`, and a digest that moved because a runner was
#: busy would be an instrument nobody trusted twice.
_TIER_KEYS = (
    "n_cases",
    "field_accuracy",
    "false_accept_rate",
    "false_refuse_rate",
    "questions_asked",
)

#: The per-case keys #231 hashed, verbatim. `fields` carries every graded (path, expected, actual, ok)
#: quadruple, which is why an equal digest means no graded *value* moved and not merely that the
#: outcome classes agree.
_CASE_KEYS = ("case", "expected", "actual", "grade", "fields", "notes", "missed_question")


class NotTheFrozenTier(ValueError):
    """The report does not hold exactly the frozen case set, so no frozen-tier digest exists for it."""


def grade_digest(report: Mapping[str, Any], *, cases: Sequence[str] = FROZEN_18) -> str:
    """#231's timing-free grade digest over ``report``, which must hold exactly ``cases``.

    ``report`` is a ``report.json`` as ``eval run`` writes it (or ``EvalReport.model_dump(mode="json")``
    — the same object). Raises :class:`NotTheFrozenTier` naming both directions of the mismatch when
    the run held any other case set; see the module docstring for why that is a refusal rather than a
    filter.

    Rows are hashed in the report's own order, which ``run_cases`` documents as the *input* order and
    not completion order — so the value does not depend on which case finished first, and it does not
    depend on this function sorting anything either.
    """
    rows = [row for row in report["per_case"] if isinstance(row, Mapping)]
    present = [str(row.get("case", "")) for row in rows]
    wanted = list(cases)
    if sorted(present) != sorted(wanted):
        extra = sorted(set(present) - set(wanted))
        missing = sorted(set(wanted) - set(present))
        raise NotTheFrozenTier(
            f"this report holds {len(present)} cases, not the frozen {len(wanted)}: "
            f"extra {extra}, missing {missing}. The digest hashes the case count and the whole "
            f"per-case list, so it is only defined over a run of exactly the frozen set — re-run "
            f"with `--case <id>` for each of them rather than hashing a subset of this report."
        )
    core: dict[str, Any] = {key: report[key] for key in _TIER_KEYS}
    # `.get`, not `[]`: a SKIPPED row carries none of these keys, and hashing it as nulls is the
    # honest reading. A skip is excluded from every rate but it is not the same report as a run where
    # the case was measured, and a digest that could not tell those apart would claim a measurement
    # the corpus does not hold.
    core["per_case"] = [{key: row.get(key) for key in _CASE_KEYS} for row in rows]
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = ["FROZEN_18", "NotTheFrozenTier", "grade_digest"]
