# 46. A binary a test execs is not an environment a rule names

Consumer-not-parallel-universe forbids seqforge defining an alignment environment, and that was read
for a year as forbidding `star` in any dependency table at all — so the `external` tests borrowed
liulab-runtime's `align-rna` by cloning it, unpinned, into CI. The rule is now read narrowly: what it
protects is that **no Snakemake rule and no wheel resolves an aligner from our tables**, which a
test-only pixi environment cannot do. `test-star` carries `star`/`samtools`/`htslib` and nothing
else — `no-default-feature`, its own solve group, no `seqforge` — and reaches the suite only by
prepending its `bin` to the `test` feature's `PATH`, because nothing imports an aligner. The
first attempt at owning them (#336) failed for a reason those two absences remove: solved alongside
the rest, STAR's `libdeflate 1.22` and mupdf's `libdeflate >=1.25` have no common solution on
`osx-arm64`, so it had to be pinned to `linux-64` and the maintainer could not build it. What is lost
is that CI no longer tracks whatever STAR the lab currently ships; what is bought is that an upstream
commit can no longer turn this repo red, and that `pixi run -e test test-external` needs no
incantation.

**Status.** Reverses #338, which reverted #336. Narrows R10.
