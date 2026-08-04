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

Ranking is by **specificity** (ADR-0028): a form that NAMES a node beats one that only DESCRIBES it,
then most alias tokens matched, then a form that entails a tied rival. The first component keeps
"SPLiT-seq paired-end RNA-seq" on `splitseq` — a token count measures an alias's verbosity, and the
generic bulk entry carries the wordier phrase (#266). The second picks a leaf over the family node
whose spelling it contains ("10x 3' v3" carries "10x 3'"). The third reaches a node whose name has a
rival's name inside it ("Smart-seq3xpress" carries "Smart-seq3" and is not carried back), where the
tokens tie and *length* would be no evidence at all — "bulk RNA-seq" is longer than "10x 3' v3" and
says far less. Every component reads only the two strings, and the lowest id settles what remains —
so the answer never depends on the order the KB happened to load in. That matters beyond tidiness:
the resolved chemistry is folded into `run_id`, so a first-match-wins rule lets adding an unrelated
spec silently re-point an existing dataset.
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


def naming_forms(tech_id: str, spec: Spec) -> list[str]:
    """The spellings that NAME this node: its directory id, its declared id, name and aliases.

    Every one of them is a claim that a text carrying it IS this chemistry, which is what entitles
    them to outrank a `descriptive_alias` on any other node.
    """
    return [tech_id, spec.identity.id, spec.identity.name, *spec.identity.aliases]


def curated_forms(tech_id: str, spec: Spec) -> list[str]:
    """Every spelling of a node the KB curates — the naming forms, then the descriptive ones.

    One list, because the two questions asked of it must not drift apart: which forms a value has to
    carry to NAME this node (here), and which forms a quote may carry to entail a value already known
    to be it (:func:`seqforge.harvest.verify.surface_forms`). The descriptive forms belong in it for
    the second question's sake — a quote saying "paired-end RNA-seq" does support a value already
    established to be `bulk-rnaseq-pe`. Ranking is where the two differ, and it reads them apart.
    """
    return [*naming_forms(tech_id, spec), *spec.identity.descriptive_aliases]


def _best_match(value: str, tech_id: str, spec: Spec) -> tuple[tuple[int, int], str] | None:
    """How specifically ``value`` names this node, and the form that got it there.

    ``None`` when no form is carried at all. Bigger is more specific, in two components:

    1. **Does the value NAME this node, or only describe it?** A `descriptive_alias` is a phrase a
       different chemistry's record carries just as truthfully, so it can never outrank a name.
       Without this, ranking measures the matched phrase's *verbosity*: "paired-end RNA-seq" is four
       significant tokens against `SPLiT-seq`'s two, so "SPLiT-seq paired-end RNA-seq" resolved to
       the bulk entry (#266). Node generality cannot supply it — `bulk-rnaseq-pe` and `splitseq` are
       both root leaves, neither an ancestor of the other — so the entry declares it instead.
    2. **Significant tokens of the best form matched in that class.** A form matched as a bare
       substring still scores by its tokens, so "BD Rhapsody" (2) outranks "Rhapsody" (1) on the same
       string and a leaf outranks the family node whose alias it contains.

    The form travels with the score because what settles a remaining tie is a question about the two
    forms themselves, and only :func:`resolve_chemistry_id` can see both — see :func:`_says_more`.
    """
    scored = [
        ((names, len([tok for tok in tokens(form) if tok not in STOPWORDS])), form)
        for names, forms in (
            (1, naming_forms(tech_id, spec)),
            (0, spec.identity.descriptive_aliases),
        )
        for form in forms
        if carries(value, form)
    ]
    # ...and among this node's own equally-specific forms, the longest, so the one that goes on to
    # face the other candidates is the fullest spelling of it. Cannot change WHICH node wins.
    return max(scored, key=lambda s: (s[0], len(squash(s[1])), s[1])) if scored else None


def _says_more(form: str, other: str) -> bool:
    """Does ``form`` say strictly more than ``other`` — the same entailment, one level up.

    `tokens()` reads "Smart-seq3xpress" as `['smart', 'seq3xpress']`, exactly as many significant
    tokens as the `Smart-seq3` sitting inside it, so two nodes tie and the lower id used to take it —
    leaving a node unreachable by its own name (#266). The tie-break cannot be *length*: "bulk
    RNA-seq" (12 characters) is longer than "10x 3' v3" (9) and says far less, so on "10x 3' v3, bulk
    RNA-seq" length hands the answer to the generic entry — this issue's own defect, one class up.

    Containment is the evidence length was standing in for, and this module already has the predicate
    for it. "Smart-seq3xpress" carries "Smart-seq3" and is not carried back, so it says strictly more.
    Neither of "bulk RNA-seq" / "10x 3' v3" carries the other, so neither says more and the id
    settles it — the same answer as before, and still a property of the strings alone.
    """
    return carries(form, other) and not carries(other, form)


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
    matched = [
        (best, tech_id)
        for tech_id, spec in pool.items()
        if (best := _best_match(value, tech_id, spec)) is not None
    ]
    if not matched:
        return None
    # The most specific score, then — among the nodes holding it — one whose form says strictly more
    # than a rival's, and failing that the lowest id. Each step reads only the strings, so the answer
    # is the same whichever order the pool was built in.
    top = max(score for (score, _), _ in matched)
    tied = [(form, tech_id) for (score, form), tech_id in matched if score == top]
    outranked = {form: sum(_says_more(form, other) for other, _ in tied) for form, _ in tied}
    return min(tied, key=lambda t: (-outranked[t[0]], t[1]))[1]


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
