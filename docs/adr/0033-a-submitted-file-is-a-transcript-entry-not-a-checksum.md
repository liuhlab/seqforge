# 33. A submitted file is a transcript entry, and its md5 is an address we never check

Date: 2026-08-05

## Status

Accepted.

## Context

An archive declares, per run, the files the submitter actually uploaded. SRA's are
`<SRAFile supertype="Original">`, and each one carries four things:

```xml
<SRAFile filename="NasalProx1_270_2.fastq.gz" size="28543057"
         md5="993e02dd8079b30a23285828a8ee9982" supertype="Original">
  <Alternatives url="s3://sra-pub-src-15/SRR19886090/NasalProx1_270_2.fastq.gz.1"
                free_egress="-" access_type="Use Cloud Data Delivery" org="AWS"/>
</SRAFile>
```

We read `@filename` and nothing else, into `ArchiveRecord.filenames: list[str]`
([#249](https://github.com/liuhlab/seqforge/issues/249)).

**The obvious reading of the md5 is that it joins a file on disk to its record, and it is wrong.**
That is the reading #249 was filed under, and anyone re-deriving from the same evidence will reach it
again: a collaborator hands you `NasalProx1_270_2.fastq.gz` with no accession in the name, the archive
declares a hash for a file by that name, so hash the file and join. Checking a local file against an
md5 means reading every byte of it, which **R3** forbids and [#37](https://github.com/liuhlab/seqforge/issues/37)
already removed once — `probe.core._content_key` replaced a whole-file sha256 precisely because
"the whole file was read, which was never the point". This record exists so that trade is not
re-opened a third time.

**What the md5 actually is.** It addresses the bytes at the `<Alternatives>` URL. That is structurally
ENA's `fastq_md5`, which `content_key_from_md5` already adopts as a content-address with no byte read
([#39](https://github.com/liuhlab/seqforge/issues/39)). The difference is whose file it names: ENA's
hash is over the FASTQ **ENA generated**, SRA's is over the **submitter's own upload** — the one copy
that was never normalized, never regenerated, and never had its technical read dropped. That makes it
the more valuable of the two identities, not a duplicate of one we hold.

**We already send people to a second API for a fact we threw away.** Five sites name the
`sra-pub-src-*` buckets as somewhere the originals "may exist via the SRA Data Locator / SDL API" —
`io.remote.technical_read_remedy`, the `PRETRIMMED_VARIABLE_LENGTH`, `MISSING_TECHNICAL_READ` and
`BARCODE_READ_ABSENT` remedies in `resolve/escalate.py`, and the unfilled-role blocker in
`manifest/validate.py`. (The first count of them said four; the fifth turned up while the other four
were being repaired, which is the argument for repairing all of one sentence at once rather than the
instances that prompted the ticket.) The
efetch package we already fetch, parse and cache names the bucket outright, per run, with the
`access_type` attached. A remedy that says *go query SDL and hope* is worse than one that says *the
record you already have lists it*.

**And it is not an SRA fact.** ENA publishes the same concept under the same word: `submitted_ftp`,
`submitted_bytes` and `submitted_format` are already in the field list `io/remote.py` requests.
`submitted_md5`, which ENA also publishes, is not. Same gap, two archives — and one of them has a
documented case where the submitted file is the *only* data, since ENA generates no FASTQ for
cellranger/longranger BAMs.

## Decision

**A submitted file is part of the transcript: `ArchiveRecord` carries what the archive declares about
each file the submitter uploaded — name, provider md5, size, and where it can be fetched — and no code
path ever hashes a local file to compare against it.**

| | rule |
| --- | --- |
| **`submitted_files`** | replaces `filenames` as the *stored* field: `SubmittedFile(filename, md5, size_bytes, uri)`. `filenames` survives as a **derived property** over it, so nothing stores the same names twice and the join keeps its shape |
| **the md5** | an **address** over the bytes at `uri`, adopted via `content_key_from_md5` if those bytes are ever fetched. Never computed, never verified, never compared against a local file |
| **the size** | **checks, never joins.** Where `_join` matched a file by the submitter's filename, a size disagreement is a `Warning`; a size never *creates* a join, because the archive supplied a fact and matching on a coincidence would be a guess over it |
| **the uri** | printed where the record set is in hand — `io records`, and the record-join blocker. The five remedies point at that verb and never carry the value |
| **a record set** | carries the stamp of the version that wrote it, so a cache predating this is distinguishable from a deposit that legitimately publishes no originals — most do |
| **ENA** | `submitted_md5` joins the requested field list and surfaces in `io resolve`. Same concept, same word, one surface earlier |

**The unit is the file, not the name.** A filename with no hash and no URI is what we had; a hash with
no URI names bytes nobody can reach. They arrive together on one element and they are modelled
together, which is why this is a new type rather than three parallel lists.

## Why not hash the local file

It is the reading the issue was filed under, so it gets the long answer. R3 admits exactly one budget
loop and every FASTQ touch goes through it; a whole-file md5 is a second read path with no budget,
justified by a join that already works. `_join` matches by run accession first and by the submitter's
declared filename second, and between them they cover every file that was not deliberately renamed to
something the archive never saw. The residual case — renamed, and no accession in the name — buys one
join for the cost of reading every byte of every FASTQ in the dataset, at 10⁴-dataset scale, against a
hash the submitter computed on a file that may since have been recompressed. `@size` catches most of
what that would have caught, from `stat()`, for nothing.

## Why not name the URI in the remedy that needs it

Not one of the five holds a record set, and three of them are inside the byte resolver where one may
never arrive. `score(Observation, KB, hypo?)` decides what a library is from bytes alone; records
enter at `resolve_metadata` and nowhere else. Threading an `ArchiveRecordSet`
into `resolve/escalate.py` or `manifest/validate.py` buys a better sentence — the exact `s3://` URI,
in the blocker that made you want it — at the price of the split the compiler is built on, and it
would be bought quietly, as a parameter with a default. The pointer costs the reader one command and
costs the architecture nothing.

## Why not model SRA's element

`supertype`, `semantic_name`, `sratoolkit`, `cluster`, `free_egress` and `<Alternatives>` are SRA's
XML, and a model carrying them would be that XML wearing a model's clothes — the shape that cannot
accept ENA's spelling of the same fact, or an in-house deposit's, which have no `supertype` at all.
The four fields kept are the four both archives publish and the four that mean something to a
compiler. `access_type="Use Cloud Data Delivery"` is dropped with the rest: it is a billing property
of one bucket at one moment, and the ticket that fetches those bytes will need it fresh rather than
transcribed.

## So in code

**When you hold a provider md5, adopt it as an address or carry it — never verify it.** There is no
code path in this tree that reads a whole FASTQ to compare a hash, and adding the first one is the
regression this record names. If you find yourself wanting an `ArchiveRecordSet` inside
`resolve/escalate.py` or `manifest/validate.py`, the answer is a pointer to `seqforge io records`, not
a parameter with a default. And when you add a field to `ArchiveRecord`, ask what a cache written
before it should do: absent-and-stale must not read as absent-and-true, which is the whole reason the
set carries a writer stamp.

**Enforced by.** R3's single budget loop is held by
`test_the_read_budget_bounds_bytes_read_however_large_the_file` (`tests/test_probe.py`) — the guard
that makes "never check the md5" true by construction rather than by discipline, since there is no
second read path to write one in. The join this record re-shapes is held by
`test_the_original_filenames_join_when_the_accession_is_gone` (`tests/test_records.py`), which must
keep passing *through the derived property*, unchanged: if it needs editing, the property is wrong.

The three gates this record was written demanding now exist. In `tests/test_archive.py`, the parse is
held by `test_every_submitted_file_carries_the_md5_size_and_uri_beside_its_name` and
`test_the_submitted_files_are_the_uploads_and_not_sras_own_normalized_products`, both driving the real
parse over committed archive bytes rather than a hand-built string; the rename is held by
`test_the_filenames_property_returns_what_the_stored_field_no_longer_duplicates`, and the stamp by
`test_a_freshly_fetched_record_set_carries_the_version_that_wrote_it` with
`test_a_record_set_written_before_submitted_files_loads_and_reads_as_unstamped`. In
`tests/test_records.py`, `test_an_unstamped_record_set_that_cannot_join_names_the_re_fetch` and
`test_a_stamped_set_that_declares_no_originals_still_blames_neither_side` are the two halves of the
distinction the stamp exists for, and the size rule is pinned from four sides:
`test_a_size_the_record_disagrees_with_warns_on_a_filename_made_join`,
`test_a_size_disagreement_is_silent_where_the_accession_made_the_join`,
`test_a_record_declaring_no_size_says_nothing_about_the_one_on_disk` and
`test_a_size_disagreement_never_blocks_and_never_unmakes_the_join`.

**The pointer-never-a-value rule is the one worth a named guard**, since the tempting version of the
mistake compiles: `test_both_remedies_for_a_missing_original_point_at_the_records_verb_not_a_second_api`
(`tests/test_resolve.py`) asserts each remedy names the verb, keeps the bucket, and carries no run
accession — a remedy that grew a URI would have to grow an accession first, and that is what fails.
`test_barcode_absent_refusal_abstains_when_a_sibling_barcoded_leaf_hits` holds the fifth site the same
way. `test_the_filereport_asks_ena_for_the_submitted_md5_beside_its_siblings` (`tests/test_remote.py`)
holds the ENA half, and `test_io_records_prints_each_submitted_files_md5_size_and_uri`
(`tests/test_cli.py`) holds the one place the concrete URI is allowed to appear.

**What nothing pins: the hash.** `dataset_hash` was measured identical across this change on the
suite's deterministic synthetic build, which is how the claim below was checked rather than assumed —
but no test compares a hash to a value from before a release, and
`test_dataset_hash_is_invariant_across_a_processing_sweep` pins invariance across *recipes*, not
across versions. A future change that did move the hash would go unnoticed here; noticing it would
take a committed expected hash per benchmark case, which the corpus deliberately does not carry.

## Consequences

- **No manifest field moves and `dataset_hash` does not move.** Nothing here reaches the write-once
  artifact: the md5 is record provenance, and the size disagreement is a `Warning`, which
  [ADR-0010](0010-two-resolvers-one-blocks-one-warns.md) keeps out of the manifest by construction.
  A dataset compiled before and after this must hash identically, and that is a checkable gate rather
  than an expectation.
- `IO_VERSION` and `RESOLVE_VERSION` bump: the parser reads more and the metadata resolver emits a
  warning it did not have.
- **The fetch is deliberately left undone.** `free_egress="-"` and `access_type="Use Cloud Data
  Delivery"` make those buckets requester-pays, so probing a run from its submitted original needs
  credentials and an egress budget — a third branch in `io.sra.sra_whole_file`'s content-address
  precedence, and its own decision. What lands here is the transcript that makes it possible.
- A **Submitted file** is now a term (`CONTEXT.md`), defined against **Whole file** — what we know
  about a FASTQ we probed — and against **Download**, the part of a **Deposit** actually handed to
  seqforge. A submitted file is neither: it is what the deposit says exists, whether or not anyone
  fetched it.
