# Can compose tell a chimera from its name alone, offline?

Read 2026-08-14 out of the `liulab-genome` source for
[#407](https://github.com/liuhlab/seqforge/issues/407), under map
[#406](https://github.com/liuhlab/seqforge/issues/406). **Yes for the two questions the map's
constraint 5 actually asks — a chimeric name is decidable, and its component list recoverable, from
the name plus the wheel's own shipped table, with no read of the machine's reference store and no
network. But the offline answer is a different proposition from the on-machine one, and the two
disagree in both directions: `liulab-genome` itself states that "the record, never the metadata row"
decides whether a built assembly is a chimera, and it ships a test proving that `ce11_ecHT115`
registered from somebody's own FASTA is not one. Offline detection is therefore a claim about the
NAME, and it is sound only if every component is a row in the shipped
`assembly_metadata.tsv` — which today is 7 assemblies, so exactly 120 chimeras of any arity are
detectable and every other one needs a `liulab-genome` edit and release first. The map has not priced
that. Per-component `ncbi_taxid` is reachable offline; per-component annotation NAME is not —
the merged name is not injective, by that function's own docstring.**

This is a measurement, not a decision. It reports what the code on disk does today. What to do about
it — whether compose selects the module, whether the `.smk` decides at parse time, whether a
processing field declares it — belongs to the compose-selection ticket and to whatever record it
writes.

Two source trees are involved and they are not at the same version, which is itself a finding (§2).
Line citations are against the sibling checkout `/Users/hanqingliu/src/liulab-genome` at
`bd0d4d5` on branch `refactor/94-surfaces`; seqforge citations are against this worktree.

---

## 1. The four checks, in order, for `ce11_ecHT115`

The four checks ADR-0008 names are implemented in one function,
`genome/io/source.py::resolve_source`, and are enumerated in its own docstring at
`src/genome/io/source.py:198-219`. What each one does for the name `ce11_ecHT115`:

| # | check | code | fires? | reads disk? |
|---|---|---|---|---|
| 1 | a completion record here | `source.py:256-261` | **yes**, always | **yes** — `AssemblyDir.read_record()` |
| 2 | a source the caller named | never reaches this function | no | n/a |
| 3 | the name | `source.py:262-277` | **yes**, and it decides | **partly** — see below |
| 4 | today's fetch path | `source.py:260`, `265`, `267` | no, not reached | no |

**Check 1 is unconditional and it is a disk read.** `record = assembly_dir.read_record()`
(`source.py:256`). If a record is there it is believed outright and the name is never consulted:
`ChimeraDetails.from_record(record)` returns the components for a chimera's record, or `None` for
any other record, in which case the assembly is resolved as an ordinary fetch (`source.py:257-261`).

**Check 2 never arrives.** `--source` / `Genome(path_or_url=...)` builds a `SeededSource` handed
straight to the registration; the docstring says so at `source.py:205-210`. There is no
compose-time equivalent, so it is not a check compose would have to reproduce.

**Check 3 is the whole of the offline question, and it is two halves.** The syntactic half is pure:

```python
# src/genome/io/source.py:262-267
    try:
        candidates = split_name(assembly_dir.assembly)
    except ChimeraNamingError:
        return fetched_source(metadata, golden_path_url)
    if not all(_could_be_a_component(name) for name in candidates):
        return fetched_source(metadata, golden_path_url)
```

`split_name` (`src/genome/chimera.py:154-197`) is a `str.split` and a regex over the parts and
nothing else — `chimera.py:189-190`:

```python
    parts = tuple(name.split(_NAME_JOIN))
    if len(parts) < _MIN_COMPONENTS or not all(_COMPONENT_RE.fullmatch(part) for part in parts):
```

The module's own docstring states the guarantee: *"Pure means names in, names out: nothing here
opens a file, and nothing here imports from `genome.io` or `genome.genome`"* (`chimera.py:10-11`).
Confirmed by the import list at `chimera.py:51-55`: `re`, `collections`, `collections.abc`.

The semantic half is `_could_be_a_component`, and this is the one line that touches the machine —
`src/genome/io/source.py:183`:

```python
    return is_prepared(assembly) or lookup_assembly(assembly) is not None
```

`is_prepared` (`source.py:150-172`) is `read_record(assembly_data_dir(assembly)) is not None`; the
directory comes from `LIULAB_DATA` or a well-known lab root
(`src/genome/io/registration.py:79-135`). It never raises when the root is missing — it falls back
to `~/liulab_data` and simply stats a path that is not there — so a caller can be offline and still
get an answer.

**The disk read in check 3 is a disjunct, and for `ce11_ecHT115` it is the losing one.** `ce11` and
`ecHT115` are both rows of the shipped `src/genome/data/assembly_metadata.tsv`, so
`lookup_assembly` answers each of them from a file inside the installed wheel — `metadata.py:397-401`
reads it through `importlib.resources.files("genome")`, cached (`metadata.py:355-373`). Same bytes
on every machine, same answer with the reference store deleted.

Then the canonical-order check, pure again (`source.py:268-277`): `derive_name(candidates)` is
compared to the given name, and a mis-ordered spelling raises `FileNotFoundError` naming the
canonical one.

**So: for `ce11_ecHT115` on a machine holding nothing, check 1 fires and reads the disk, check 3
fires and decides, check 4 is not reached — and only check 3 is reproducible offline.** Dropping
check 1 is exactly what makes an offline answer a different proposition, which is §5.

Run against the sibling checkout's source with `LIULAB_DATA` unset and no chimera anywhere on this
laptop:

```text
name                   split_name                         all parts listed?  canonical
ce11_ecHT115           ('ce11', 'ecHT115')                True               ce11_ecHT115
ecHT115_ce11           ('ecHT115', 'ce11')                True               ce11_ecHT115   <-- MIS-ORDERED
hg38                   ChimeraNamingError                 -                  -
my_ref                 ('my', 'ref')                      False              my_ref
hg38_mm10              ('hg38', 'mm10')                   True               hg38_mm10
tinyCe_tinyEc          ('tinyCe', 'tinyEc')               False              tinyCe_tinyEc
sacCer3_hg38           ('sacCer3', 'hg38')                True               hg38_sacCer3   <-- MIS-ORDERED
test-star              ChimeraNamingError                 -                  -
hg38_mm10_sacCer3      ('hg38', 'mm10', 'sacCer3')        True               hg38_mm10_sacCer3
```

---

## 2. Is a table row required for every chimera? Not for the chimera — for every component

**The chimera's own row plays no part in detection.** Check 3 splits the name and looks up the
*parts*; the whole name is never looked up. The `metadata` argument `resolve_source` receives is
used only by `fetched_source` (`source.py:146-147`), which is check 4, and its only call site
(`src/genome/io/download.py:228-232`) passes `lookup_assembly(assembly)` for that purpose alone. Delete
the `ce11_ecHT115` row and `resolve_source` still returns `ComponentSource(("ce11", "ecHT115"))`.

`liulab-genome`'s own test says the same thing in the same words: `tests/test_metadata.py:45-55`
defines what makes a shipped row a chimera's — *"It splits into two or more parts and the table lists
every one of them"* — and `tests/test_metadata.py:237-251` asserts the `ce11_ecHT115` row is name
and nothing else, existing *"so that a machine holding neither component can still tell this name
from a free-form local key, by splitting it into components the table lists"*. Confirmed by reading
it back: `AssemblyMetadata(assembly_name='ce11_ecHT115', species=None, ucsc_name=None,
ncbi_name=None, ncbi_assembly_id=None, ncbi_taxid=None, source_url=None, sha256=None)`.

**Every COMPONENT, though, must be a row — and this is the constraint the map has not priced.**
Offline, `is_prepared` is unavailable to compose by construction (R7: no machine fact may decide a
compose-time output; the genome deliberately resolves at run time in `rule genome_index`,
`src/seqforge/workflows/map/star-umi.smk:251-253`). That leaves `lookup_assembly` alone, so the set
of chimeras compose can detect is exactly the set whose every component is one of the seven rows the
wheel ships:

```text
hg38  hg19  mm39  mm10  sacCer3  ce11  ecHT115
```

That is 21 pairs, 35 triples, … — 120 chimeras of arity ≥ 2 in total, and **no others, ever, until
`liulab-genome` gains a row and cuts a release.** Say that plainly:

- A chimera involving any organism not on that list — a PDX with a new mouse strain, a co-culture
  with a second bacterium, a spike-in genome, *any* locally seeded reference — is **undetectable by
  compose** until someone edits `src/genome/data/assembly_metadata.tsv`, releases `liulab-genome`,
  and re-pins seqforge's dependency. On-machine resolution has no such constraint: `is_prepared`
  covers a locally registered component the moment it is built.
- The fixtures the map leans on are already outside the set. `tinyCe`, `tinyEc`, `tinySc` and
  `tinyEcDub` are not table rows, so `tinyCe_tinyEc` is **not** offline-detectable — see the run in
  §1. `liulab-genome`'s own `tests/test_source.py:139-157` proves this is the intended behaviour and
  that only `is_prepared` rescues those names. **The map's cheap bar (constraint 6) is a synthetic
  round-trip on exactly those fixtures.** If compose's selection rule is "the table lists every
  part", a fixture round-trip cannot exercise it without a monkeypatched table — which is testing
  something other than what ships.
- **The pin is already behind.** This worktree's `pixi.lock` pins `liulab-genome` at
  `ab3272312eb3f9ef25e3ac8320aac28187af42f5` (2026-07-23, "add a Chromap aligner"). The installed
  package at `.pixi/envs/default/.../site-packages/genome/` has **no `chimera.py`, no
  `io/source.py`, no `io/components.py`, no `ecHT115` row and no `ce11_ecHT115` row** — its table
  stops at `ce11`. So today seqforge cannot detect a chimera offline at all, and the very first
  chimera ticket owes a dependency re-pin before a line of detection code can run. This is the
  "edit and release before composing" cost, already binding, not hypothetical.

---

## 3. The component list comes from the name, and only from the name

`split_name` returns the candidates (`chimera.py:154-197`); `derive_name` re-derives the canonical
spelling from them (`chimera.py:106-151`). Both pure. `ComponentSource(candidates)` — the value
check 3 returns (`source.py:277`) — carries nothing the name did not.

The record is **not** needed for the list. Where the record is load-bearing is everything *else* a
chimera knows about itself: `ChimeraDetails` (`src/genome/io/components.py:186-217`) carries the
**separator** its chromosome names were written with, and each component's `sha256` and contributed
annotation. `Genome.components` reads it, never the name — `src/genome/genome.py:397-418`:

> *"The single test of whether an assembly is a chimera, and the completion record is what answers
> it — never the metadata table, which lists a chimera as a cross-reference and would answer the
> same question differently on a machine where the row is stale or absent (ADR-0008)."*

That sentence is the boundary of this whole ticket. It is a direct statement, in `liulab-genome`'s
public API, that name-and-table is *not* the is-a-chimera test. An offline compose-time answer is a
second, weaker predicate, and it needs its own name so nothing confuses the two.

Note what this costs the splitter: **the separator is not offline-recoverable.** `__` is only the
default `split_suffixed` assumes when no component carries a doubled underscore
(`chimera.py:295-362`), and `tinyEcDub` is the shipped fixture that breaks it. The map's constraint 1
already puts the split on the `liulab-genome` side and reads the separator off the record at run
time, so this is consistent — but it does mean a compose-time artifact may not bake a separator in.

---

## 4. Taxid: yes, offline. Annotation name: no

**`ncbi_taxid` per component is reachable offline**, straight out of the shipped table:

| component | `ncbi_taxid` | `species` | default annotation |
|---|---|---|---|
| `ce11` | `6239` | `Caenorhabditis elegans` | `wormbase_ws298` |
| `ecHT115` | `634469` | `Escherichia coli HT115` | `refseq_rs_2025_06_26` |

The chimera's own row carries `ncbi_taxid=None` (§2), which is correct — a chimera has no single
taxid — and it is worth flagging against seqforge's current shape: `GenomeRef.ncbi_taxid`
(`src/seqforge/models/processing.py:42-44`) is a **single** optional taxid, and
`src/seqforge/manifest/policy.py:418-422` fills it from `dataset.experiment.organism.value` — the
*dataset's* asserted organism, never the assembly's. For a chimeric run that field describes one
component at best and is silently wrong at worst. Nothing today reads it into a command line, so
this is a shape note for the identity ticket, not a live bug.

**The per-component annotation NAME is not offline-recoverable in general, and this is stated by the
code rather than inferred.** `merged_annotation_name` (`src/genome/io/components.py:84-116`) joins the
contributing annotations with `+` in sorted-component order, and its docstring says at
`components.py:94-99`:

> *"It needs no parse-back: what a merged annotation is made of is recovered from the components, and
> written down in its own record besides. And it is not asked to carry *which* components
> contributed — a chimera with a component that contributes nothing spells the same name a different
> subset would."*

So `wormbase_ws298+refseq_rs_2025_06_26` splits on `+` into two names, but nothing in the string says
which component each belongs to, and a 3-component chimera where one component contributed no
annotation yields a 2-element list against a 3-element component list. Positional alignment is a
guess. Confirming the second half: `annotation_metadata.tsv` has **no row naming `ce11_ecHT115`** —
ADR-0008's *"its merged annotation gets no row at all"*, verified.

What *is* offline is each component's **default** annotation (the table above, `default: yes` for
both). For today's `ce11_ecHT115` that happens to be the truth — the built chimera's merged
annotation is `wormbase_ws298+refseq_rs_2025_06_26`, exactly the two defaults in sorted-component
order. It is a coincidence of the build, not a guarantee: a chimera built against a non-default
component annotation spells a merged name the table cannot reproduce. **Per-component annotation is
a run-time read of the record, full stop.** Map constraint 3 ("each counted against its own
annotation, which the chimera's record names per component") already says record; this confirms
compose cannot pre-compute it.

---

## 5. False positives and false negatives — both exist, both have a shipped test

A local key containing `_` splits happily; whether it then reads as a chimera turns entirely on
whether the table lists every part. `my_ref` → `('my', 'ref')` → neither listed → ordinary
assembly, which is the documented separation (`source.py:211-214`). `hg38` and `test-star` never
reach the split at all — one has no `_`, the other's parts fail `[A-Za-z0-9]+`.

**False positive — a real, non-chimeric assembly whose name reads as a chimera's.** Concrete and
already tested in `liulab-genome`: `tests/test_source.py:173-186`,
`test_a_plain_record_keeps_a_chimera_shaped_name_whatever_it_was_registered_as`, whose comment is

> *"The reason the record comes first: `ce11_ecHT115` seeded years ago from somebody's own FASTA is
> not a chimera, and no amount of the name looking like one may change what a finished registration
> already is."*

The assembly whose name the map is built on is the test's own example. `hg38_mm10` is the same case
with two very ordinary parts — a name a lab would plausibly give a hand-concatenated reference built
years before this package existed, or a liftover scratch build. Offline, compose cannot tell it from
a real chimera, because the only thing that can is check 1, the record. Cost of getting it wrong:
compose selects the chimeric `.smk`, the run maps fine (the index is whatever is there), and the
split rule finds no `__` suffixes — a failure at split time on a reference that was never chimeric.

**False negative — a real chimera that reads as an ordinary local key.** Also tested:
`tests/test_source.py:139-157`, `test_only_a_prepared_component_counts_when_the_table_lists_neither`
— *"Neither half is in the shipped table, so nothing but a record of its own can make
`tinyCe_tinySc` read as two assemblies rather than as one name somebody chose."* Every locally
registered component is in this class, and so is every one of the map's fixtures. Cost: compose
selects the plain module, the chimeric BAM is produced and nothing downstream knows — precisely the
gap the map says it exists to close, silently reinstated for exactly the references the lab builds
by hand.

**A third case, neither of the above: the mis-ordered name.** `ecHT115_ce11` and `sacCer3_hg38`
split to listed parts but are not the canonical spelling, and `resolve_source` *raises*
`FileNotFoundError` naming the right one (`source.py:268-276`). A compose-time rule that mirrors
check 3 inherits that raise, which turns a legal free-form local key into a hard compose refusal for
a user who never mentioned a chimera. Offline compose has no record to fall back on, so it cannot
resolve the ambiguity the way `resolve_source` does.

**Neither direction is closable offline**, and that is structural rather than a gap in the
implementation: check 1 exists precisely because the name is not authoritative, and check 1 is the
machine.

---

## 6. Verdict

**LEGAL-WITH-CONDITIONS.** Compose *can* decide, offline and with no network, that a name is spelled
like a chimera's and that every part is a shipped table row, and recover the ordered component list —
all of it from `chimera.split_name` plus `metadata.lookup_assembly`, both pure of the machine's
reference store. Constraint 5 is not illegal as written. The conditions are:

1. **What compose decides is a property of the NAME, not of the reference.** It needs its own term —
   *chimera-spelled*, say — kept distinct from `Genome.components`, which is the real test and is a
   run-time record read. A compose-time refusal must say which of the two it is talking about.
2. **Detection is capped by the shipped table.** Seven components today, 120 chimeras; anything else
   needs a `liulab-genome` row, a release and a seqforge re-pin *before* the dataset can be composed.
   The map should price this, and should decide what compose does when a user names a chimera it
   cannot see — silently plain, or a refusal naming the missing row.
3. **The pin is behind the feature.** The `liulab-genome` this repo installs has no `chimera.py`
   at all. Re-pinning is the first task of the compose-selection ticket, not a footnote.
4. **Both error directions survive**, and each hits a real reference class — a pre-existing
   `hg38_mm10`-shaped assembly, and every locally registered component including the map's own test
   fixtures. Whatever compose emits must be overridable by a declared processing field, so a user can
   say "this is not a chimera" or "this is one" and be believed. That override is also the only way
   the constraint-6 fixture round-trip exercises the shipped selection rule rather than a
   monkeypatched table.
5. **Nothing about the chimera's INTERNALS may be pre-computed at compose time** — not the separator
   (`tinyEcDub` forces `___`), not the per-component annotation (the merged name is not injective).
   Compose may select the module; the module reads the record. That is consistent with map
   constraints 1 and 3, and it is the line that keeps R7 intact.

---

## Method and caveats

- Everything above is read from source. Nothing was built, no chimera was constructed, no reference
  store was consulted beyond `~/liulab_data` existing on this laptop.
- Citations are against `/Users/hanqingliu/src/liulab-genome` at commit `bd0d4d5`, branch
  `refactor/94-surfaces` — **not** a tagged release and **not** what seqforge installs. Line numbers
  will move.
- The run in §1 and the tables in §2 and §4 were produced by importing that checkout's `src/` ahead
  of the installed package (`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1`), inside seqforge's `default`
  pixi env, with `LIULAB_DATA` unset. `liulab-genome` was not modified.
- "120 chimeras" is `2^7 - 7 - 1` over the seven shipped rows — every subset of size ≥ 2. It counts
  what the table *permits*, not what would build; only a prepared set builds one (ADR-0008).
- The claim that check 3 never consults the chimera's own row is by reading, not by deleting the row
  and re-running. The reading is unambiguous: `resolve_source` looks up only `candidates`, and its
  `metadata` argument reaches `fetched_source` and nothing else.
- Written up on branch `research/chimera-offline-detection` (not opened as a PR).
