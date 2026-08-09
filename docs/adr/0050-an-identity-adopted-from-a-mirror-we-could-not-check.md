# 50. An identity adopted from a mirror we could not check

`probe_sra` decides each mate's content address by asking whether ENA mirrored the run faithfully. Three
of the four conditions are refusals — ENA lists no FASTQ, its file count differs from the streamed mate
count, or it flagged a dropped technical read — and each sends the mate to a synthetic SRA-derived
address. The fourth compares the streamed read table against what the run row publishes, and it can
**abstain**: where the streamed lengths vary there is no average to compare, so `dropped_reads`
declines rather than accuse a trimmed run of losing bases.

**An abstain adopts.** The ENA md5 becomes the address even though nothing confirmed the ENA copy is
what we streamed. That was invisible while the field was a bool called `ena_verified`, which read
`True` for both "checked, clean" and "could not check" — the name claimed more than the predicate
delivered, on a value that is hashed into the manifest and never rewritten. `address_basis` now names
the arm, and `ena-loss-unknown` is the one this record is about.

The behaviour is deliberately unchanged. Refusing to adopt on an abstain is the defensible correctness
position and it was considered: it would re-address every run whose stream could not answer, so
manifests already computed would stop reproducing and a content-addressed corpus would carry two
identities for the same bytes. Against that, adopting is not baseless — the other three conditions all
held, and the address we take is the address of the file a downloader would fetch. Making the gap
visible costs nothing and closes the part that was actually wrong, which was the reporting.

**Status.** Closes #348 item 5. The tightening is a live option, not a rejected one: it needs a
migration story for existing manifests, which is why it is not bundled with a seam cleanup. If it is
ever taken, `ena-loss-unknown` is the exact set of runs it moves, which is the other thing naming the
arm buys.
