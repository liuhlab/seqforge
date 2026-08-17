"""Reading a **Chimera** out of the assembly name a recipe states — all of compose's chimera detection.

A chimera is one reference built from several **Component** assemblies, whose chromosome names carry a
component suffix so every read declares which organism it landed on. `liulab-genome` builds them; this
module answers exactly one question about one, and it answers it from the name alone.

**The user states the chimera by naming it.** ``--assembly ce11_ecHT115`` *is* the claim. Nothing here
probes bytes, reaches the network, or opens the shipped assembly table — and that last exclusion is a
decision rather than an economy. A table lookup would be a perfectly accurate detector and an unsound
*dispatch*: nothing folds the `liulab-genome` pin into ``run_id``, so the day a row is added upstream
the same recipe over the same dataset would compile to a different module at the same ``run_id``,
into the directory the first compile's alignments are already sitting in. A name the user typed moves
only when the user retypes it, and a retyped recipe re-keys.

Consumer, not parallel universe: the spelling rule belongs to `liulab-genome` and is read from there.
This module owns the *conversion of upstream's refusal into a value*, and nothing else.
"""

from __future__ import annotations

from genome.chimera import ChimeraNamingError, split_name


def components(assembly: str) -> tuple[str, ...] | None:
    """The Component names ``assembly`` spells, or ``None`` when it is not spelled like a chimera's.

    A thin wrapper over `liulab-genome`'s public name splitter, and the wrapping is the whole
    contract. Upstream *raises* for a name that does not split, because upstream is asked the
    question by a caller who already believes the answer; the composer asks it of every recipe it
    ever compiles, and almost every one names an ordinary assembly. **Not-a-chimera is an answer, not
    a failure** — so it arrives as a value a caller branches on, and a plain compile pays exactly one
    call that tells it to stop.

    **Syntactic, and only syntactic.** It says the name is *spelled* the way a chimera's name is
    spelled; it never says those components exist, because nothing is looked up and so nothing is
    confirmed. Components come back in the order the name spells them rather than sorted, which is
    what lets a caller tell a mis-ordered chimera from the canonical spelling instead of quietly
    accepting either.

    **The accepted price, so it is not rediscovered as a bug**: any underscored assembly name reads as
    a chimera — ``my_ref`` is ``('my', 'ref')``. That spelling is already off-contract on the genome
    side, and it fails loudly but *late*, at run time in genome resolution rather than at compose
    time. Guarding against it here would take the table this function exists in order not to read.
    """
    try:
        return split_name(assembly)
    except ChimeraNamingError:
        return None
