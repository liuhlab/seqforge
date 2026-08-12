# 37. The live knowledge base is what invalidates a compile

`compose` applies the live knowledge base's admission floor and backend params but keyed `run_id` on
the fill-time version, so re-composing after a bump reused the directory and silently overwrote its
config; the `kb` component is now a compile-time content hash of the deciding spec's processing
half, while `provenance.kb_version` stays the knowledge base that decided the manifest's chemistry.
Refusing on divergence would block every manifest in a 10⁴ corpus behind a re-fill that re-probes
bytes to arbitrate nothing. A signature edit no longer re-keys a compile; a `backend.params` edit
does. The hash is an exclusion, so a field `Spec` gains later is hashed by default and
over-invalidates, which costs a directory where under-invalidating costs one silently overwritten.

**Status.** Amended — narrowed to one spec's processing half; the per-spec objection is withdrawn.
