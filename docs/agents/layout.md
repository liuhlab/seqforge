# Repository layout, and the liulab contracts

Single repo, single `pyproject.toml`; do **not** split into distributions. Everything below is under
`src/seqforge/` except the last three.

```text
models/     pydantic v2 schemas; `schema export` is the single source of truth
probe/      deterministic FASTQ fingerprinting (no LLM, no network)
kb/         knowledge base, one dir per technology (spec.yaml + README.md) under kb/specs/;
            hierarchical — an abstract family node (10x-3p-gex, no backend) DESCENDS to leaf chemistries
resolve/    TWO resolvers. scoring/assign/escalate decide the library from BYTES; records.py decides
            which sample each file is from RECORDS + PROSE (handed FileIdentity, never Observation).
            group.py splits a dataset into RUNS by filename (bytes assign roles); with no record that
            grouping IS the sample identity — the normal case. Per-run and per-spec scoring runs in
            parallel across a pool
manifest/   fill/validate/hash both artifacts; policy.py owns precedence (R11)
compose/    (dataset, processing) -> Snakefile + config + units.tsv  (the Snakefile is THE product)
io/         remote peek + probe-remote (fingerprint a URL, no download), ENA/SRA/GEO/SDL resolution,
            pooch-cached onlists. archive.py TRANSCRIBES the four record levels (decides nothing).
            attributes.py = NCBI's 960 BioSample names; efo.py = EFO labels. Both ship as GENERATED
            data with a refresh verb
workspace.py the one place `seqforge/` is spelled, and the one place a readable-name-plus-hash lives.
            It names DIRECTORIES and does no I/O
pipeline.py what is INSIDE one compiled pipeline directory: the three filenames, and the reader that
            answers which module ran, the config, the contracted samples, the results dir and the
            per-sample join. Top-level because the composer writes and everyone else reads (ADR-0024)
workflows/  hand-written, versioned Snakemake modules (NOT generated). map/ only — no fetch/ yet.
            h5ad.py packages Solo.out as the deliverable (its input contract IS STARsolo's layout).
            metrics.py is the leaf metric vocabulary, stats.py the per-module reader registry, and
            each adapter lives beside the writer whose format it reads (ADR-0025).
            umite/ is the SMART-seq3 UMI extractor and counter, re-implemented rather than
            depended on; count.py is the plate-wide fan-in and writes one .h5ad directly.
            map/star-umi.smk is the module that runs them — the one pipeline that is NOT
            per-sample end to end, which it DECLARES (`fan_in_artifact`) rather than leaving
            to be discovered from its rule graph
assets/     NOT a package — the one design-token layer (`sf-tokens.css`) that BOTH report pages'
            Tailwind inputs import. A build input, never read at runtime; it ships as the source of
            record. Neutral home because neither report owns it (report/assets/VENDOR.md)
hooks/      PreToolUse/PostToolUse/Stop guards behind `seqforge hook …` — policy as mechanism
cli.py      a single typer module (root app + sub-typers). JSON by default
e2e.py      ground-truth runs behind `kb e2e` (sacCer3) / `kb e2e-introns` (ce11), which RUN THE
            COMPOSED SNAKEFILE. `kb e2e-cost` (hg38) invokes STAR directly — a memory instrument must
            reap STAR itself
evals/      ground-truth corpus + harness
─── repo root ───
skills/     SKILL.md agent skills; `skills/install.py` symlinks them into a product's discovery path
tests/      mirrors the packages above; see testing.md for the module→file table
```

Which test file covers which package is [`testing.md`](testing.md). What each module *writes* is
[`state.md`](state.md).

## Consumer of the liulab stack (R10)

- **`liulab-genome`** — import `from genome import Genome`. Assemblies are named by UCSC id
  (`sacCer3`, `ce11`, `hg38`); an annotation is a **registered GTF `name`**, because liulab-genome
  does not fetch annotations — seqforge stages the GTF and calls `register_gtf(gtf, name)`. The STAR
  index comes from `Genome(assembly).build_star_index(gtf=name)`. Never write a genome path into a
  manifest.
- **`liulab-runtime`** — reference an aligner environment by its **literal** name (`align-rna`,
  `align-dna`, `ml`, `ml-gpu`). There is no profile-indirection layer: the env name *is* the
  identifier. Do not define aligner environments here.

Both halves of R10 matter. Defining either kind of machinery here forks the lab's stack; *depending*
on them is the opposite, and `tests/test_repo_invariants.py` checks the calls we make against the real
`Genome` class.
