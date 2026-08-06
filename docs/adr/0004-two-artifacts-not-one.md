# 4. Two artifacts: the immutable dataset and the plural recipe, and the hash covers only the bytes

One manifest with a processing section loses because a fact and a choice have different lifetimes:
folding intent into the hash turns uniform reprocessing across 10⁴ datasets into a re-derivation and
moves a dataset's identity whenever someone changes their mind. The same test places a
*measurement*: ask what can move the value with the bytes held constant. A read estimate moves with
the probe budget where `size_bytes` does not, so it lives in `provenance`, outside the hash and
written unconditionally so a manifest never depends on the KB shipped the day it was written, while
the threshold over it is applied at compose under the live KB rather than frozen as a verdict.

**Status.** Supersedes the single-manifest, three-section shape. Absorbs ADR-0030 — where a
measurement the identity must exclude lives.
