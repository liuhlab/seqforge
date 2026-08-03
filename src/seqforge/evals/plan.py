"""What an ``--llm`` pass over a whole TIER will ask, and cost, before any of it is paid for.

``harvest extract --dry-run`` answers that question for one dataset. It could not answer it for a
corpus, and the corpus is where the decision lives: *is this run worth its money* is asked once, over
eighteen datasets, by a maintainer who would otherwise have to spend one run to find out. So the
per-dataset planner grew a caller rather than a second implementation — :func:`plan_case` builds each
case's send list with the same :func:`~seqforge.harvest.plan.plan_extraction` the paid run uses, and
:func:`plan_cases` adds them up.

**The plan is exact where it can be and a lower bound where it cannot.** Rendering documents costs no
token and no network, so the document count is the send list itself rather than a projection of one.
Input tokens are estimated from characters. **Output tokens are not estimated at all**: the model
decides how many claims a document supports, and a number invented for that half would be the one a
reader quotes. The token Ceiling is what bounds it.

**A case still has to be materialized**, because a fingerprint package carries its prose inside
itself and the characters cannot be counted without unpacking it. That is a package pull and a probe,
not a model call — the same cost a ``--no-llm`` run already pays, and pooch caches it. A case whose
package is not reachable **skips**, carrying the same reason and the same ``absent`` / ``unavailable``
split :func:`~seqforge.evals.run.run_case` reports, because a case whose price is unknown must not
read as a case that costs nothing.
"""

from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..harvest import build_system_prompt, llm_schema, normalize_document, plan_extraction
from ..kb.loader import load_all_specs
from ..models.resolve import CasePlanRow, EvalPlanReport
from .case import Case, CaseSkipped, materialize


def system_prompt_chars() -> int:
    """The stable prefix's length — byte-identical on every request, so charged once per document.

    Built from the live KB and the live schema rather than measured once and pinned: a KB entry is
    prose in this prompt, so adding one moves this number, and a plan quoting a stale constant would
    be wrong in exactly the direction that matters (too cheap).
    """
    return len(build_system_prompt(load_all_specs(), llm_schema()))


def plan_case(case: Case, *, prompt_chars: int) -> CasePlanRow:
    """One case's send list, costed. Reaches no model and needs no credential.

    The send list is built from what ``--llm`` would really read: the prose the package carried (or
    the documents a synthetic case keeps beside itself) **plus** every archive record worth asking.
    Both halves matter — eleven of the eighteen benchmark packages carry no prose at all, and their
    whole bill is records.
    """
    with tempfile.TemporaryDirectory(prefix="seqforge-plan-") as tmp:
        try:
            built = materialize(case, Path(tmp) / "inputs")
        except CaseSkipped as exc:
            return CasePlanRow(case=case.id, skipped=str(exc), skip_kind=exc.kind)
        docs = built.metadata_docs or case.metadata_docs
        plan = plan_extraction(
            documents=[normalize_document(p) for p in docs],
            records=case.records,
            system_prompt_chars=prompt_chars,
        )
    return CasePlanRow(
        case=case.id,
        n_documents=plan.n_documents,
        n_requests=plan.n_requests,
        n_records_read=plan.n_records_read,
        n_records_collapsed=plan.n_records_collapsed,
        n_chars=plan.n_chars,
        estimated_input_tokens=plan.estimated_input_tokens,
    )


def plan_cases(
    cases: list[Case],
    *,
    trials: int = 1,
    ceiling: int | None = None,
    jobs: int | None = None,
) -> EvalPlanReport:
    """Price an ``--llm`` run over these cases without making one.

    ``trials`` multiplies every total, because ``eval run --trials N`` really does send the same list
    N times — the per-case rows stay per trial, since a trial is the unit that repeats.

    ``ceiling`` is read against the estimate to name the cases that will certainly breach. It is a
    lower bound in the only direction that can mislead: output and cache-write tokens count against a
    Ceiling and neither is knowable here, so a case *absent* from that list may still breach.

    Order follows the case list, for the same reason ``run_cases`` preserves it: a plan is read in a
    diff against the last one.
    """
    from .run import default_jobs

    prompt_chars = system_prompt_chars()
    n = jobs if jobs is not None else default_jobs()

    def one(case: Case) -> CasePlanRow:
        return plan_case(case, prompt_chars=prompt_chars)

    if n <= 1 or len(cases) <= 1:
        rows = [one(c) for c in cases]
    else:
        with ThreadPoolExecutor(max_workers=min(n, len(cases))) as pool:
            # `map` yields in submission order, so the rows match `cases` however they interleave.
            rows = list(pool.map(one, cases))

    planned = [r for r in rows if r.skipped is None]
    over = [r.case for r in planned if r.estimated_input_tokens >= ceiling] if ceiling else []
    return EvalPlanReport(
        n_cases=len(planned),
        n_reaching_a_model=sum(1 for r in planned if r.n_documents),
        n_skipped=len(rows) - len(planned),
        trials=trials,
        n_documents=sum(r.n_documents for r in planned) * trials,
        n_requests=sum(r.n_requests for r in planned) * trials,
        n_records_read=sum(r.n_records_read for r in planned) * trials,
        n_records_collapsed=sum(r.n_records_collapsed for r in planned) * trials,
        n_chars=sum(r.n_chars for r in planned) * trials,
        system_prompt_chars=prompt_chars,
        estimated_input_tokens=sum(r.estimated_input_tokens for r in planned) * trials,
        ceiling=ceiling,
        estimated_over_ceiling=over,
        per_case=rows,
    )


__all__ = ["plan_case", "plan_cases", "system_prompt_chars"]
