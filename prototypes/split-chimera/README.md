# PROTOTYPE — the cheapest test that would actually catch a broken split

Throwaway. Answers [#414](https://github.com/liuhlab/seqforge/issues/414) on map
[#406](https://github.com/liuhlab/seqforge/issues/406). **Nothing here is production code, and none
of it should be merged to `main`** — the map forbids seqforge production code, and this branch is
where the prototype is kept as a primary source.

## The question

Constraint 6 on the map fixes a bar — a synthetic round-trip on the tiny fixtures including
`tinyEcDub` — and forbids making it expensive. But a bar is not a test. The only way to know which
assertions in that round-trip actually *earn their place* is to write a splitter, break it on
purpose, and watch which assertions go red.

So: a toy splitter implementing [#409](https://github.com/liuhlab/seqforge/issues/409)'s contract,
with a `Breakage` switchboard of 18 deliberate defects, run against 11 candidate assertions on 3
fixture shapes.

```
pixi run -e test python prototypes/split-chimera/run.py                 # the matrix
pixi run -e test python prototypes/split-chimera/run.py hardcoded_separator   # one row, verbose
```

One full round-trip — build the chimeric BAM, split it, run all 11 checks — costs **5.3 ms**. The
whole matrix is under a second. No aligner, no built chimera, no `liulab-genome` fixture on disk.

## The verdict

**Seven assertions, not eleven.** Each of these is the *only* thing catching at least one defect:

| Assertion | The defect only it catches |
|---|---|
| `sq_identity` — @SQ names + lengths + **order**, vs a hand-written single-assembly header | @SQ sorted instead of order-preserved |
| `hd` | `@HD` dropped, so the BAM stops declaring itself coordinate-sorted |
| `refdict` — every record's resolved `reference_name` | a tid remap **off by one but in range** |
| `read1_eq_read2` | a keep rule that tests `is_read1` and loses every second mate |
| `drop_reasons` — exact counts per reason | multimappers kept; a drop that is never counted |
| `refuses_partial` | a partial Component request accepted |
| `refuses_unsplittable` | a name that will not split skipped rather than refused |

**Four candidates are decorative and should not be written:**

- **`routing` — "every uniquely-placed read lands in the Component it came from", the map's own
  headline bar — catches nothing.** Every misrouting defect constructible dies louder and earlier:
  hardcoding `__` keeps the component *right* and corrupts only the bare name; the hand-rolled
  `RNAME.split("__")[-1]` yields a component nobody asked for and **refuses**. Given the name seam
  (decision 1), a record's Component is a pure function of its RNAME, so misrouting is close to
  unreachable. The bar is real; the assertion for it is not.
- **`accounting`** (reads in = kept + dropped) is strictly subsumed by `drop_reasons`.
- **`mate_component`** as an *output* assertion: the splitter's own runtime check refuses first.
  Its value is not catching a splitter bug — it is converting an opaque `KeyError` into a refusal
  that names the read and both Components. Keep it in the splitter; do not assert it in the test.
- **`refdict` for the *first* Component**: its tids are unchanged by construction, so a
  no-remap-at-all defect is invisible there and only detonates on the second. Any 2-Component
  fixture covers it — but a 1-Component one never would.

**Four defects need no assertion at all** — they crash or refuse before any check runs, so a bare
smoke test catches them: no refdict remap, mate tid not remapped, unmapped passed through (an
unmapped record has no `reference_name`), and a hardcoded `___` separator.

## The fixtures

- **`dub` (`tinyEcDub`-shaped: chromosome names already carry `__`, forcing `___`) is load-bearing,
  and for a narrower reason than the map assumed.** Hardcoding `__` on such a name does **not**
  raise — `split_suffixed('ctg__1___tinyEcDub', '__')` returns `('ctg__1_', 'tinyEcDub')`. The
  **Component is still correct**; only the bare chromosome name is corrupted, by one trailing
  underscore. So `tinyEcDub` is caught by the *header* assertion and never by the routing one.
- **`plain` is load-bearing too, and asymmetrically**: a splitter hardcoding `___` (written by
  someone who tested only on `tinyEcDub`) refuses on ordinary `__` names. That is a loud catch, so
  `plain` only has to be *run*, not asserted over.
- **`triple` caught nothing the other two did not.** Three Components is fog, as the map has it —
  the rule graph is N-agnostic by construction and this adds no evidence against that.
- Contig order is deliberately **not** alphabetical (`chrI, chrII, chrX, chrM` — real ce11 order).
  An @SQ block that happens to be sorted makes the order half of `sq_identity` decorative.

## What is not tested here, and why that is acceptable

- **That STAR produces the BAM shape assumed.** The input is hand-written; the aligner is not under
  test, which is the whole cheapness claim. #408 read the two load-bearing facts (both mates share
  `NH`; one `Transcript` has one `Chr`) off STAR's source, and #409 turned them into the splitter's
  two runtime checks. The `[fixture] cross_component_mate` row proves those checks fire when the
  assumption is violated — it cannot prove STAR never violates it.
- **That `liulab-genome` writes @SQ in per-Component declared order.** Guaranteed and verified
  upstream; the round-trip proves only that the splitter *preserves* it.
- **The counting fan-out and the `.h5ad`.** A different artifact and a different ticket (#412).
