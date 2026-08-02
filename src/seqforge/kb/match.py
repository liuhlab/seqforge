"""What a prose chemistry string NAMES in the knowledge base — one function, one direction.

A record writes `library_strategy: RNA-Seq`; a paper writes "Chromium Next GEM Single-Cell 5' Reagent
Kit v2"; an operator types `--chemistry 10x-3p-gex-v3`. All three arrive as a bare string that
something downstream will treat as a chemistry claim, and exactly one question decides whether it is
one: **does this string carry a chemistry the KB knows?**

:func:`resolve_chemistry` answers it by ENTAILMENT, in one direction only — a curated alias must sit
inside the value (`alias ⊆ needle`), never the reverse. A value carrying "bulk RNA-seq" says at least
what that alias says. A value that is merely a *fragment* of an alias says less than it: "RNA-Seq" is
inside "bulk RNA-seq", inside "paired-end RNA-seq", and inside a hundred kit names nobody curated, so
reading it as a chemistry claim manufactures a claim out of an archive's filing vocabulary. That
vacuous direction is the defect this module was written to remove (#184).

**Not strict exact-alias matching.** Requiring an exact id/name/alias would reject every realistic
prose spelling — a paper writes the kit name, never `10x-5p-gex-v2` — closing the metadata channel in
production while a benchmark steered by recipe hypotheses stayed green. Measured and rejected.

The tie-break is **most alias tokens matched**, which picks a leaf over the family node whose spelling
it contains ("10x 3' v3" carries "10x 3'") and, failing that, the lowest id — so the answer is a
property of the strings and never of the order the KB happened to load in. That matters beyond
tidiness: the resolved chemistry is folded into `run_id`, so a first-match-wins rule lets adding an
unrelated spec silently re-point an existing dataset.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .loader import load_all_specs
from .schema import Spec

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9']+")
#: Tokens too generic to carry entailment weight on their own. Filtered out of the FORM, never out of
#: the text it is matched against: dropping "kit" from an alias lets "…Analysis Kit" match "…Analysis",
#: while dropping it from the text would only ever loosen the check.
STOPWORDS = frozenset(
    {"the", "a", "an", "of", "for", "with", "and", "or", "was", "were", "used", "using", "kit"}
)


def squash(text: str) -> str:
    """Collapse runs of whitespace, so a phrase broken across a line wrap compares as one string."""
    return _WS.sub(" ", text).strip()


def tokens(text: str) -> list[str]:
    """Lowercase word tokens, hyphens read as spaces (`10x-3p-gex` and "10x 3p gex" agree)."""
    return _TOKEN.findall(text.lower().replace("-", " "))


def carries(text: str, form: str) -> bool:
    """Does ``text`` carry ``form`` — as a substring, or as all of the form's significant tokens?

    The order-independent token test is what lets a curated alias survive real prose: "Chromium Single
    Cell 3' v3" carries "Chromium 3' v3" though neither is a substring of the other. Requiring every
    significant token is what keeps that from being a licence — a quote saying only "single-cell
    RNA-seq" carries no version, so it can never carry a versioned form.

    One direction only. ``carries(text, form)`` is not ``carries(form, text)``, and the asymmetry is
    the whole point: a longer, more specific text entails a shorter form, never the other way round.
    """
    t = squash(text).lower()
    f = squash(form).lower()
    if not f:
        return False
    if f in t:
        return True
    f_tokens = [tok for tok in tokens(f) if tok not in STOPWORDS]
    return bool(f_tokens) and set(f_tokens) <= set(tokens(t))


def curated_forms(tech_id: str, spec: Spec) -> list[str]:
    """Every spelling of a node the KB curates: its directory id, its declared id, name and aliases.

    One list, because the two questions asked of it must not drift apart: which forms a value has to
    carry to NAME this node (here), and which forms a quote may carry to entail a value already known
    to be it (:func:`seqforge.harvest.verify.surface_forms`).
    """
    return [tech_id, spec.identity.id, spec.identity.name, *spec.identity.aliases]


def _specificity(value: str, tech_id: str, spec: Spec) -> int | None:
    """How specifically ``value`` names this node — significant tokens of its best matching form.

    ``None`` when no form is carried at all. A form matched as a bare substring still scores by its
    tokens, so "BD Rhapsody" (2) outranks "Rhapsody" (1) on the same string and a leaf outranks the
    family node whose alias it contains.
    """
    scores = [
        len([tok for tok in tokens(form) if tok not in STOPWORDS])
        for form in curated_forms(tech_id, spec)
        if carries(value, form)
    ]
    return max(scores) if scores else None


def resolve_chemistry_id(value: str | None, specs: Mapping[str, Spec] | None = None) -> str | None:
    """The **pool key** of the node a chemistry string names, or ``None`` when it names none.

    The key, not ``spec.identity.id``: a caller holding a pool turns this back into a spec with
    ``specs[...]``, and while the two agree on every shipped entry (the key is the spec's directory
    name), nothing checks that they must — so returning the declared id would trade a ``None`` for a
    ``KeyError`` the first time an entry disagreed.

    ``specs`` defaults to the whole KB; the byte resolver passes the pool it is scoring against, so a
    test can score a KB of its own.
    """
    if not value or not value.strip():
        return None
    pool = load_all_specs() if specs is None else specs
    ranked = sorted(
        (-score, tech_id)
        for tech_id, spec in pool.items()
        if (score := _specificity(value, tech_id, spec)) is not None
    )
    return ranked[0][1] if ranked else None


def resolve_chemistry(value: str | None, specs: Mapping[str, Spec] | None = None) -> Spec | None:
    """The KB node a chemistry string names, or ``None`` when it names none.

    The **node**, not an id: a family term legitimately resolves to a family node, and a caller that
    has to know the difference (a conflict is a disagreement, and a family term is not one — see
    ADR-0020) needs the spec to ask. Where the caller has a pool to index instead, take
    :func:`resolve_chemistry_id`, which is this same answer in the shape that pool is keyed by.
    """
    pool = load_all_specs() if specs is None else specs
    tech_id = resolve_chemistry_id(value, pool)
    return None if tech_id is None else pool[tech_id]
