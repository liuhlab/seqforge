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
seqforge io resolve ACC --json          # accession -> runs + declared metadata + dropped-read check
seqforge io peek URI --json             # first records via HTTP Range (~64 KB, never the file)
seqforge io onlist list|show|fetch|add  # barcode whitelists (pooch-cached, sha256-verified)
```

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

The remedy is `fasterq-dump --include-technical --split-files ACC`, **not** ENA's generated FASTQ,
and not the SRA Data Locator (originals exist for select studies only, so SDL usually dead-ends).

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
a refusal: bounded means bounded by the server, not by our intentions (R3).
