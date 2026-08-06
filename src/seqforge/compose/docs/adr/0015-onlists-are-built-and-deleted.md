# 15. Barcode whitelists are built by a rule and `temp()`-deleted, never stored

`compose` used to expand the resolved whitelist into every run directory, and 10x's v3 list is 111
MB of text — a third of a gigabyte for one dataset compiled three ways, for a file STAR opens once.
It is now materialized by `rule onlist` and `temp()`-deleted, because the expansion is a pure
function of the 522 kB packed array we already ship, and caching it would trade cheap deterministic
CPU for duplicated bytes no cache key protects and nobody collects. The earlier `temp()` was
decorative: snakemake cannot delete a file no rule produced, so an input with no producing rule is
one it merely requires to already exist.
