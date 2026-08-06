# Repository layout, and the liulab contracts

**Covers.** No single module — this is the map, one line per package, plus the `liulab-genome` and
`liulab-runtime` contracts. Where a package has a page of its own, that page is the standing
description and this one is the index entry.

Single repo, single `pyproject.toml`; do **not** split into distributions. Everything below is under
`src/seqforge/` except the last three.

```text
models/     pydantic v2 schemas; `schema export` is the single source of truth
probe/      bounded head reads -> Observation; what the bytes say, deterministically (no LLM, no
            network)
fingerprint/ a portable head-slice that reproduces the dataset's FULL identity: subsample.py cuts the
            reads and re-emits reproducible gzip, build.py packages them with a whole-file pin,
            load.py replays that pin so a slice resolves exactly as the originals would (ADR-0001)
harvest/    the compiler's ONE LLM seam, and three verbs of which only the middle one touches a model:
            normalize (deterministic) -> extract (LLM; emits {field, value, quote} and nothing else,
            no offsets and no verdicts) -> verify (greps the quote back, computes the offsets, checks
            entailment). The permitted vocabulary is fields.py, never the prompt
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
project.py  the "one study" views over a MULTI-ASSAY compile — a flat sample_metadata.tsv, one row per
            sample across every assay, and a project.yaml index. Both DERIVED from the per-assay
            manifests and deterministically ordered, so they cannot drift and regenerating is a no-op
recordset.py the record set on disk: ONE loader for both dialects — an `io records` cache, and a
            hand-written `source: user` file that declares structure and never a fact — plus the
            draft `records new` writes. `source` picks the dialect, never the extension. Top-level
            for pipeline.py's reason: the module that writes the artifact owns reading it, and three
            verbs read one (ADR-0034)
workflows/  hand-written, versioned Snakemake modules (NOT generated). map/ only — no fetch/ yet.
            h5ad.py packages Solo.out as the deliverable (its input contract IS STARsolo's layout).
            metrics.py is the leaf metric vocabulary, stats.py the per-module reader registry, and
            each adapter lives beside the writer whose format it reads (ADR-0025).
            umite/ is the SMART-seq3 UMI extractor and counter, re-implemented rather than
            depended on; count.py is the plate-wide fan-in and writes one .h5ad directly.
            map/star-umi.smk is the module that runs them — the one pipeline that is NOT
            per-sample end to end, which it DECLARES (`fan_in_artifact`) rather than leaving
            to be discovered from its rule graph
report/     a deterministic READER: one workspace -> one self-contained HTML page that says what the
            compiler decided and how. It decides nothing and writes only the report, so a missing
            artifact degrades one panel rather than failing the page (ADR-0024/0025/0026)
assets/     NOT a package — the one design-token layer (`sf-tokens.css`) that BOTH report pages'
            Tailwind inputs import. A build input, never read at runtime; it ships as the source of
            record. Neutral home because neither report owns it (report/assets/VENDOR.md)
hooks/      PreToolUse/PostToolUse/Stop guards behind `seqforge hook …` — policy as mechanism
cli/        a typer package, one module per command group; root app in root.py. JSON by default
e2e.py      ground-truth runs behind `kb e2e` (sacCer3) / `kb e2e-introns` (ce11), which RUN THE
            COMPOSED SNAKEFILE. `kb e2e-cost` (hg38) invokes STAR directly — a memory instrument must
            reap STAR itself. `kb e2e-fit` is that sweep's collector: it merges the per-task JSONs a
            job array emits and fits the line `e2e-cost` fits in-process, so an array and one
            sequential run answer alike. It needs no toolchain, which is why it is not among the
            verbs that may report `skip`
evals/      ground-truth corpus + harness
─── repo root ───
skills/     SKILL.md agent skills; `skills/install.py` symlinks them into a product's discovery path
tests/      mirrors the packages above; see testing.md for the module→file table
```

Which test file covers which package is [`testing.md`](testing.md). What each module *writes* is
[`state.md`](state.md). How the one LLM seam works is [`harvest.md`](harvest.md).

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
