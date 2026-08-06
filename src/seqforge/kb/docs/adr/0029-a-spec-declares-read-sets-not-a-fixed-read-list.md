# 29. A spec declares read sets, not a fixed read list

**Status.** Absorbs ADR-0044 — the file a barcodeless fallback orphaned is evidence against it.

A fixed `Spec.reads` plus injective, total role assignment meant one chemistry could not cover the
several sequencing configurations its protocol publishes, and a single-end bulk FASTQ refused
outright. `Read.optional` looks like the smaller change and is the larger one — it makes assignment
partial, rewriting the core optimization to express what a complete subset already expresses — and a
second `-se` leaf is unlabellable, since `Mechanism` has no member meaning "how many files there
are". A read set names ids `reads` already declares, so two configurations cannot drift apart; and a
proper-subset set that orphans a file a barcoded candidate would seat as its barcode read does not
anchor the tie band, because the evidence against the fallback lies in the file it dropped.
