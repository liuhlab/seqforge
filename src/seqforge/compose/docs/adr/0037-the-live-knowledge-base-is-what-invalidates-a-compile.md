# 37. The live knowledge base is what invalidates a compile

`compose` applies the live knowledge base's admission floor and backend params but keyed `run_id` on
the fill-time version, so re-composing after a bump reused the directory and silently overwrote its
config; the `kb` component is now a compile-time content hash of the deciding spec's processing
half, while `provenance.kb_version` stays the knowledge base that decided the manifest's chemistry.
Refusing on divergence would block every manifest in a 10⁴ corpus behind a re-fill that re-probes
bytes to arbitrate nothing. A signature edit no longer re-keys a compile; a `backend.params` edit
does. The hash is an exclusion, so a field `Spec` gains later is hashed by default and
over-invalidates, which costs a directory where under-invalidating costs one silently overwritten.

The **workflow** axis was left the other way round and stays that way: `run_id` folds the stamp the
recipe recorded, so a module edit re-keys nothing already on disk and a recipe is pinned to the
workflow it was written against. Applying this record's fix there too — the live constant, or a hash
of the staged module — would have re-keyed every recipe in the corpus once more to buy precision on
an axis where the cheap answer is available: the same silent overwrite is closed instead by making a
pipeline directory **write-once**. Occupied, composition refuses; it never compares stamps (a bump
editing no module must still compose) and never compares bytes (the occupant may be a hand edit
whose owner knows why it is there, and seqforge is not the judge of whose contents are stale). The
verb that *chains* the compile treats an occupied directory as this stage's cache hit and resumes
from it, because a refusal there would price re-rendering a report at the alignments underneath.

**Status.** Amended twice — narrowed to one spec's processing half, the per-spec objection withdrawn;
extended to say what the workflow axis does instead, and that the directory is written once.
