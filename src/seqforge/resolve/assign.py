"""Joint role->file assignment: the injective, cardinality-normalized optimization (§3.3).

An assignment ``A: R_t -> F`` is **injective** (each role a distinct file). ``valid(A)`` selects no
forbidden cell and fills every role. We maximize ``Σ (cell + β·prior)`` over valid ``A``. The common
single-cell case (``|F| <= 4``) is solved by brute force over all injective maps; larger ``|F|`` uses
an O(n^3) Hungarian on ``-(cell + β·prior)`` (forbidden as a large finite cost) with a post-check
that no selected edge is a forbidden edge (an all-forbidden role => unfillable => not a padded win).

**Coverage precedes score.** A file eligible for exactly one role (forbidden for every other) can be
placed nowhere else, so an assignment that leaves it orphaned while a multi-role file takes its role
is coverage-wrong even when it scores higher. We therefore optimize ``(coverage, Σ(cell + β·prior))``
lexicographically: first the count of single-role-eligible files that claim their sole role, then the
score. This is a no-op wherever injectivity already forces the map — the common one-file-per-role case
— and only bites a multi-file-per-role run (many lanes/flowcells under one accession), where a short
barcode read may out-score the real long cDNA read for the cDNA role yet the cDNA-length reads are the
only files that role can take. The coverage term orders the search; it is excluded from the reported
score, which stays the honest ``Σ(cell + β·prior)`` of the chosen map.

The filename prior is a *sub-threshold* nudge (``β << min(weight)``): it can only break an exact
byte-tie, never override a gate or flip validity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
from math import factorial, inf

#: Brute-force whenever the number of injective maps P(F, R) is at most this; else Hungarian.
_BRUTE_CAP = 40_320  # 8!
_BIG = 1e9  # a finite forbidden-edge cost for the Hungarian path (never selected if avoidable)
#: Per-cell selection bonus for a single-role-eligible file claiming its sole role. Large enough that
#: one more such placement outranks any score/prior configuration, small enough to sit under ``_BIG``;
#: excluded from the reported score, so it orders coverage above score and nothing more.
_COVERAGE_BONUS = 1e3


@dataclass(frozen=True)
class AssignmentResult:
    """The best valid injective assignment for one technology (or an invalid verdict + reason)."""

    valid: bool
    #: role index -> file index (only for a valid assignment)
    mapping: dict[int, int] = field(default_factory=dict)
    #: file indices left over (index reads / ignored), penalized at rate lambda
    unassigned_files: list[int] = field(default_factory=list)
    raw: float = -inf
    #: roles forbidden on EVERY file (structurally unfillable) — drives MISSING_TECHNICAL_READ
    unfillable_roles: list[int] = field(default_factory=list)


def _n_injective(n_files: int, n_roles: int) -> int:
    if n_files < n_roles:
        return 0
    return factorial(n_files) // factorial(n_files - n_roles)


def best_assignment(
    n_roles: int,
    n_files: int,
    score: list[list[float]],
    forbidden: list[list[bool]],
    prior: list[list[float]],
) -> AssignmentResult:
    """Return the maximum-weight valid injective role->file assignment (or an invalid verdict).

    ``score``/``forbidden``/``prior`` are ``n_roles x n_files``. ``score`` is the finite support
    value in ``[0, 1]``; ``prior`` is the sub-threshold filename nudge already scaled by ``β``.
    """
    unfillable = [
        r for r in range(n_roles) if n_files == 0 or all(forbidden[r][f] for f in range(n_files))
    ]
    if n_files < n_roles or unfillable:
        return AssignmentResult(
            valid=False,
            unassigned_files=list(range(n_files)),
            unfillable_roles=unfillable,
        )

    # ``exclusive[r][f]``: file f is eligible for role r and for no other role — it can be placed
    # nowhere else, so coverage demands r be represented by one of its exclusive files when it has any.
    n_eligible = [sum(not forbidden[r][f] for r in range(n_roles)) for f in range(n_files)]
    exclusive = [
        [(not forbidden[r][f]) and n_eligible[f] == 1 for f in range(n_files)]
        for r in range(n_roles)
    ]

    if _n_injective(n_files, n_roles) <= _BRUTE_CAP:
        chosen = _brute(n_roles, n_files, score, forbidden, prior, exclusive)
    else:
        chosen = _hungarian_assign(n_roles, n_files, score, forbidden, prior, exclusive)

    if chosen is None:  # a forbidden pattern blocks every full injective map
        return AssignmentResult(valid=False, unassigned_files=list(range(n_files)))

    mapping, raw = chosen
    assigned = set(mapping.values())
    unassigned = [f for f in range(n_files) if f not in assigned]
    return AssignmentResult(valid=True, mapping=mapping, unassigned_files=unassigned, raw=raw)


def _brute(
    n_roles: int,
    n_files: int,
    score: list[list[float]],
    forbidden: list[list[bool]],
    prior: list[list[float]],
    exclusive: list[list[bool]],
) -> tuple[dict[int, int], float] | None:
    best_key: tuple[int, float] | None = None  # (coverage, raw), lexicographic
    best: tuple[int, ...] | None = None
    for perm in permutations(range(n_files), n_roles):
        if any(forbidden[r][perm[r]] for r in range(n_roles)):
            continue
        coverage = sum(exclusive[r][perm[r]] for r in range(n_roles))
        raw = sum(score[r][perm[r]] + prior[r][perm[r]] for r in range(n_roles))
        key = (coverage, raw)
        if best_key is None or key > best_key:
            best_key = key
            best = perm
    if best is None or best_key is None:
        return None
    return {r: best[r] for r in range(n_roles)}, best_key[1]


def _hungarian_assign(
    n_roles: int,
    n_files: int,
    score: list[list[float]],
    forbidden: list[list[bool]],
    prior: list[list[float]],
    exclusive: list[list[bool]],
) -> tuple[dict[int, int], float] | None:
    n = max(n_roles, n_files)
    # square cost: minimize -(coverage_bonus + score + prior); forbidden -> _BIG; dummy cols -> 0. The
    # coverage bonus (>> any score sum) makes a single-role-eligible file claim its sole role first; it
    # is dropped from the reported ``raw``, which stays the honest Σ(score + prior) of the chosen map.
    cost = [[0.0] * n for _ in range(n)]
    for r in range(n_roles):
        for f in range(n_files):
            if forbidden[r][f]:
                cost[r][f] = _BIG
            else:
                bonus = _COVERAGE_BONUS if exclusive[r][f] else 0.0
                cost[r][f] = -(bonus + score[r][f] + prior[r][f])
    col_for_row = _hungarian(cost)
    mapping: dict[int, int] = {}
    raw = 0.0
    for r in range(n_roles):
        f = col_for_row[r]
        if f >= n_files or forbidden[r][f]:  # forced onto a dummy or forbidden edge -> no valid A
            return None
        mapping[r] = f
        raw += score[r][f] + prior[r][f]
    return mapping, raw


def _hungarian(cost: list[list[float]]) -> list[int]:
    """O(n^3) min-cost perfect assignment on a square matrix; returns ``col_for_row[i]``."""
    n = len(cost)
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = row matched to column j (1-indexed; 0 = unmatched)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = -1
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    col_for_row = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            col_for_row[p[j] - 1] = j - 1
    return col_for_row
