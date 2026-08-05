---
name: seqforge-io
description: >-
  The network surface: resolve GEO/SRA/ENA/BioProject accessions to runs with
  `seqforge io resolve`, range-read remote FASTQ heads with `seqforge io peek`,
  and manage barcode whitelists with `seqforge io onlist`. Use when given an
  accession (GSE/GSM/PRJNA/SRP/SRR/SAMN), asked what files a dataset has, or
  asked whether a run is missing its barcode read.
---

# seqforge io

The **only** place seqforge touches the network.

```bash
seqforge io resolve ACC                 # accession -> runs + declared metadata + dropped-read check
seqforge io records ACC                 # accession -> project/sample/experiment/run, as DECLARED
seqforge io peek URI                    # first records via HTTP Range (~64 KB, never the file)
seqforge io onlist list|show|pack|write # barcode whitelists (shipped pre-packed, sha256-verified)
seqforge io attributes [NAME]           # NCBI's 960 harmonized BioSample names, with definitions
seqforge io efo                         # what EFO:0009922 is actually called
```

There is **no `--json` flag**: every verb emits JSON on stdout by default. `kb list` is the one
plain-text exception, and it is not in this skill.

## `io records` is where per-sample metadata comes from

`io resolve` answers "what runs are in this accession". `io records` answers "what does the archive
SAY about them" — and those are different questions with different answers. `strain`, `tissue`, `sex`
and `dev_stage` live on the **BioSample** record; the ENA fields `io resolve` returns are
byte-identical across every run of a study ("Model organism or animal sample from Caenorhabditis
elegans" x6 on the pilot). Until 2026-07-16 nothing fetched the BioSample record at all, which is why
the pilot's manifest said `tissue: null` on six samples under a paper that says "neurons".

It is a **transcriber**: it reports what the record declares and stops. What any of it means is
`resolve`'s job — pass the result to `manifest fill --accession` (which fetches it for you) or
`--records` (which reuses what you already fetched).

It also carries `submitted_files` — one row per file the submitter uploaded, with the provider md5,
the declared size and the `sra-pub-src-*` URI. **This is the only verb that prints that URI**, which
is why five refusals elsewhere tell a reader to run it rather than naming a bucket to go hunting in.
An empty list is the ordinary case rather than a failure: most deposits publish no originals. The md5
is an address for those hosted bytes, never something to check a local file against — nothing in
seqforge reads a FASTQ end to end.

## The most important thing this does

**`fasterq-dump` skips technical reads by default.** So a 10x barcode read routinely vanishes from
the archive-generated FASTQ while remaining inside the `.sra`. What gets published then looks like
ordinary single-end RNA-seq and is silently unprocessable as single-cell — the dataset isn't broken,
it's *mislabelled by omission*.

`io resolve --check-reads` catches it from two metadata calls, **before** downloading a byte, by
comparing SRA's own per-read table to what ENA published. Real example (SRR9170959):

- ENA: 50.0 bases/spot, **one** FASTQ file — while declaring `library_layout=PAIRED`
- SRA: `nreads=3`, per-read `[50, 50, 10]`, `readTypes=TBT` (Technical/**B**iological/Technical)
- → 60 bases/spot discarded, barcode read included. **Exit 4** — a human must re-fetch.

The remedy is `fasterq-dump --include-technical --split-files ACC`, **not** ENA's generated FASTQ. If
the read was stripped before upload, that dump cannot recover it either — then `seqforge io records
ACC` lists the submitter's own uploads, each with the `sra-pub-src-*` URI the archive preserves it at.
Reach for the SRA Data Locator only after that: it answers the same question by another route, and
originals exist for select studies only, so it often dead-ends where the record set is definite.

That NCBI and ENA disagree on `base_count` for the same run is not an error to reconcile — it is two
truths about what the file contains, and the disagreement IS the signal.

## Traps that will bite you

- **GEO accessions are rejected by ENA** (HTTP 400). Resolve GSE → SRP first; `io resolve` does.
- **A SuperSeries owns no runs.** eutils and runinfo return **zero** for one, silently. Verified:
  GSE140511 → recursing finds GSE140399 + GSE140510 → 2 studies. Without recursion you lose the whole
  dataset *and report success*.
- **`_1`/`_2` are not guaranteed**, and neither is ordering. Do not infer roles from filenames —
  seqforge assigns roles from structure precisely because filenames lie.
- **`fastq_ftp` can be empty**: ENA generates no FASTQ for cellranger/longranger BAMs or BAMs with
  CB/CR/CY/RX/QX tags — i.e. the 10x case. Empty is information, not an error.
- **eutils is rate-limited** to 3/sec keyless, by IP not process.

## peek is bounded, and the server has to agree

64 KB gets several thousand reads' worth (0.013% of a 517 MB run). It asserts **HTTP 206**, not
`Accept-Ranges` — a server can advertise ranges, ignore the header, and send the whole file. A 200 is
a refusal: bounded means bounded by the server, not by our intentions.
