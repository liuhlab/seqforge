# 37. The live knowledge base is what invalidates a compile

`compose` applies the live knowledge base's admission floor and backend params but keyed `run_id` on
the version the manifest recorded at fill, so re-composing one manifest after a bump reused the
directory and overwrote its config with no refusal and no warning; the `kb` component is now read
from the knowledge base installed at compile time, while `provenance.kb_version` stays what it was —
the knowledge base that decided that manifest's chemistry. A per-spec fingerprint would be more
precise but invents a hash nobody has and gives the key a fifth component, and refusing on
divergence would block every manifest in a 10⁴ corpus behind a re-fill that re-probes bytes to
arbitrate nothing. One repository-wide version over-invalidates, and that is the cheap side of the
asymmetry: over-invalidating costs a directory, under-invalidating costs a directory silently
overwritten.
