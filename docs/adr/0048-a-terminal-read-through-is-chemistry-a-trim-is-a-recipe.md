# 48. A terminal read-through is chemistry; a trim is a recipe

Discussion #354 concluded that trimming belongs to `processing.yaml` — same data, different recipe,
and the dataset hash should not move because we decided to trim. That holds for a *choice*, and the
Tn5 mosaic end is not one: past it a read carries adapter, index and flowcell primer, so no recipe
could rationally keep it, and the entry naming the chemistry is the only thing that knows it is
there. The test is **terminality, not the presence of trimming** — a quality floor or a length cut
have real alternatives and stay recipe knobs, while a sequence past which the molecule has ended
does not, and is declared in the entry as `read_through`. The price, accepted, is that the field is
one `run_id` hashes (ADR-0037): a compiled dataset of that chemistry gets a new pipeline directory
and its CRAMs are not reused, which is correct because the reads really are processed differently.

**Status.** Reverses discussion #354's placement. #355 applies the same test to `clipAdapterType`.
