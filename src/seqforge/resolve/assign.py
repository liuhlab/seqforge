"""Joint role->file assignment: the injective, cardinality-normalized optimization (§3.3).

An assignment ``A: R_t -> F`` is **injective** (each role a distinct file). ``valid(A)`` selects no
forbidden cell and fills every role. We maximize ``Σ (cell + β·prior)`` over valid ``A``. The common
single-cell case (``|F| <= 4``) is solved by brute force over all injective maps; larger ``|F|`` uses
an O(n^3) Hungarian on ``-(cell + β·prior)`` (forbidden as a large finite cost) with a post-check
that no selected edge is a forbidden edge (an all-forbidden role => unfillable => not a padded win).

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

    if _n_injective(n_files, n_roles) <= _BRUTE_CAP:
        chosen = _brute(n_roles, n_files, score, forbidden, prior)
    else:
        chosen = _hungarian_assign(n_roles, n_files, score, forbidden, prior)

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
) -> tuple[dict[int, int], float] | None:
    best_raw = -inf
    best: tuple[int, ...] | None = None
    for perm in permutations(range(n_files), n_roles):
        if any(forbidden[r][perm[r]] for r in range(n_roles)):
            continue
        raw = sum(score[r][perm[r]] + prior[r][perm[r]] for r in range(n_roles))
        if raw > best_raw:
            best_raw = raw
            best = perm
    if best is None:
        return None
    return {r: best[r] for r in range(n_roles)}, best_raw


def _hungarian_assign(
    n_roles: int,
    n_files: int,
    score: list[list[float]],
    forbidden: list[list[bool]],
    prior: list[list[float]],
) -> tuple[dict[int, int], float] | None:
    n = max(n_roles, n_files)
    # square cost: minimize -(score+prior); forbidden -> _BIG; dummy rows/cols -> 0 (an unassigned).
    cost = [[0.0] * n for _ in range(n)]
    for r in range(n_roles):
        for f in range(n_files):
            cost[r][f] = _BIG if forbidden[r][f] else -(score[r][f] + prior[r][f])
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
