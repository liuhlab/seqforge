# GSE207085: what the archive actually holds

Research for [#228](https://github.com/liuhlab/seqforge/issues/228), under the plate-assay map
[#225](https://github.com/liuhlab/seqforge/issues/225). Measured **2026-08-04** against the live
GEO / SRA / ENA APIs and this repo's own `seqforge io` verbs. Every number below came from a command
reproduced in [Commands](#commands); nothing here is quoted from the paper.

## Verdict in one paragraph

`GSE207085` is a GEO **SubSeries** (of SuperSeries `GSE207086`) holding **1440 FACS-sorted mouse
cells**, one cell per BioSample, per SRA experiment, per run, per FASTQ pair — 2880 files, 54.34 GB,
all `2 x 151 bp` paired biological reads with **no technical/index read anywhere**. It is **one
batch by every archive signal there is**, down to a single sequencing lane. And the decisive number
for the harvest-cost ticket: across all 1440 BioSample records — not a sample, the whole population —
there is **exactly one distinct attribute block**. The records differ in accession and in a cell
index, and in nothing else. No genotype, no treatment, no plate, no well, no sex, no age.

## Accessions and counts

| thing | value |
|---|---|
| GEO SubSeries | `GSE207085` — *Three-dimensional morphologic and molecular atlases of murine nasal vasculatures [Mouse_Nasal_SmartSeq]* |
| GEO SuperSeries | `GSE207086` (siblings: `GSE207083`, `GSE207084`) |
| BioProject | `PRJNA853582` |
| SRA study | `SRP383998` |
| SRA submission | `SRA1445150` (one) |
| GEO samples (GSM) | **1440** — `GSM6276900` … `GSM6278339`, contiguous |
| BioSamples (SAMN) | **1440** — `SAMN29399743` … `SAMN29401182`, contiguous |
| SRA experiments (SRX) | **1440** — `SRX15928234` … `SRX15929673`, contiguous |
| SRA runs (SRR) | **1440** — `SRR19884905` … `SRR19886344`, contiguous |
| files per run | **2** (`_1.fastq.gz`, `_2.fastq.gz`) — 1440/1440 runs, no exceptions |
| platform | Illumina HiSeq X Ten, `GPL21273` |
| submitting centre | Institute for Basic Science (contact: Myung Jin Yang) |
| publication | Hong SP, Yang MJ, *et al.* *Nat Cardiovasc Res* 2023;2:449–466. PMID `39196043`, PMC `PMC11358012`, **DOI `10.1038/s44161-023-00257-3`** |

The mapping is strictly **1 cell : 1 GSM : 1 SAMN : 1 SRX : 1 SRR : 1 FASTQ pair**. All four
accession series are contiguous with no gaps, and `GSM_number = 6276899 + cell_index` holds for all
1440 rows — one linear axis, no block structure of any kind.

## Is it really one batch? Yes, and the evidence is bytes

The archive metadata is consistent with one batch but does not by itself prove it — every submitted
dataset shares a submission date. So the question was pushed down to the FASTQ headers, which the
ENA copies preserve verbatim (`@SRR19884905.1 ST-E00114:1220:HGL5HCCX2:7:1101:4411:1889/1`).

| survey | runs | headers read | distinct `instrument:run:flowcell:lane` |
|---|---|---|---|
| 64 KB prefix, evenly spread across the whole SRR range | 48 | 39,326 | **1** — `ST-E00114:1220:HGL5HCCX2:7` |
| **whole file**, the 10 smallest runs (100% coverage) | 10 | 15,391 | **1** — `ST-E00114:1220:HGL5HCCX2:7` |

Every one of 54,717 reads examined across 58 runs came off **one instrument (`ST-E00114`), one
sequencing run (`1220`), one flowcell (`HGL5HCCX2`), one lane (`7`)**. Corroborating arithmetic: the
whole series is 332,472,709 spots, which is about one HiSeq X lane's yield — there is no room for a
second lane.

Supporting metadata, all single-valued across 1440 runs: one BioProject, one SRA study, one
submission accession, one centre, one instrument model, one library strategy/source/selection, one
`library_construction_protocol` string (byte-identical), one load date (`2022-06-28`), one release
date (`2022-07-12`). BioSample submission timestamps span **6 minutes 48 seconds** on 2022-06-28
(09:59:05.090 → 10:05:53.510) — one upload, not a series of them.

**What is not there: any plate or well.** No record at any level — GEO SOFT, BioSample, SRA
experiment, ENA — carries a plate id, a well coordinate, or a batch field. The only per-cell
identifier is the integer `1…1440` in `nasal_prox1_<N>`, and the processed GEO matrix names its
columns `NasalProx1_<N>` with the same integer and no extra structure. So: **one sequencing batch,
proven; plate layout, unrecoverable from the archive.** (1440 = 15 x 96 is arithmetic, not evidence;
SMART-seq3 is more usually run in 384-well plates, where 1440 is not a whole number of plates. The
archive does not say, and neither should we.)

## Organism, assembly

- **Mus musculus, taxid `10090`** — single-valued across all 1440 records.
- Strain `FVB` (BioSample attribute). The Prox1-GFP transgene appears only in the study abstract
  prose; there is no genotype slot on any record.
- GEO's declared processing: `Assembly: mm10`, STAR + featureCounts + Seurat.
- **`mm10` is registered in `liulab-genome`**, at `src/genome/data/assembly_metadata.tsv`:
  `mm10  Mus musculus  mm10  GRCm38  GCF_000001635.26  10090`. `mm39` is registered too.
  Whether an `mm10` STAR index is *materialized* on any given cluster is not verifiable from a
  laptop — that is a `liulab-genome` / cluster question, not an archive one.

## Read layout

Both the SRA per-read table and the ENA file list agree, for **1439/1440** runs (one run's stats
fetch hit a transient `Response ended prematurely`; refetched by hand, it agrees — `nreads=2`,
2 x 151):

- `n_reads = 2`, `spot_length = 302`, per-mate `average_length = 151, 151`. Zero variance.
- `library_layout = PAIRED`, 2 published FASTQ files per run.
- **`seqforge io resolve SRP383998` reports `n_runs_missing_technical_read: 0`.** ENA carries no
  technical or index read, and none was dropped — SRA declares two reads and ENA publishes two
  files. Nothing is hiding inside the `.sra`.

Both mates are biological, but they are not interchangeable. A bounded 256 KB read of six runs
spread across the range shows the SMART-seq3 structure sitting in plain sight on **R1**:

| run | cell | R1 reads starting with `ATTGCGCAATG` | of those, `GGG` at offset 19 | R2 with the tag |
|---|---|---|---|---|
| SRR19884905 | nasal_prox1_1375 | 1421/3532 (40.2%) | 1179 | 2/3659 (0.05%) |
| SRR19885193 | nasal_prox1_685 | 2259/3559 (63.5%) | 1151 | 0/3818 (0%) |
| SRR19885481 | nasal_prox1_171 | 1257/3290 (38.2%) | 1134 | 0/3094 (0%) |
| SRR19885768 | nasal_prox1_1212 | 1474/3140 (46.9%) | 1331 | 3/2946 (0.10%) |
| SRR19886056 | nasal_prox1_547 | 2211/3401 (65.0%) | 1848 | 2/3249 (0.06%) |
| SRR19886344 | nasal_prox1_496 | 1540/3364 (45.8%) | 1028 | 0/3567 (0%) |

`seqforge io probe-remote` on R1 reads the same thing off the per-cycle composition without knowing
the tag: cycles 0–10 are each dominated by one base spelling `A T T G C G C A A T G`; cycles 11–18
are flat (the 8 bp UMI); cycles 19–21 are G-enriched (the `GGG`). R2 is undifferentiated cDNA.

**This matters to the map.** Constraint 2 of #225 allows for the possibility that "the bytes cannot
separate SMART-seq3 from `bulk-rnaseq-pe` at any rung". On this dataset they plainly can: 38–65% of
R1 carries a fixed 11 bp anchor at offset 0 that no bulk library has. Whether that generalises (the
UMI-read fraction is protocol- and depth-dependent, and SMART-seq3xpress may differ) is #229's
question, not this one — but the pessimistic branch of constraint 2 is not forced by this dataset.

## What one BioSample record contains — the harvest-cost number

**All 1440 BioSample records were fetched and diffed, not a sample.** (`efetch db=biosample` in 8
batches of 180; parsed and compared field by field.)

Every record is `Package: Generic.1.0`, `Model: Generic`, and carries exactly **three** typed
attributes:

| harmonized slot | value | distinct across 1440 |
|---|---|---|
| `source_name` | `Nasal Prox1+ cells` | **1** |
| `cell_type` | `Nasal Prox1+ cells` | **1** |
| `strain` | `FVB` | **1** |

Everything else on the record — owner `Institute for Basic Science`, contact `Myung Jin Yang`,
organism `Mus musculus`, taxid `10090`, status `live`, access `public`, package, model — is likewise
single-valued across all 1440.

**Distinct attribute-key sets across 1440 records: 1. Distinct records once accession, GEO/SRA
cross-ids, title and timestamps are removed: 1.** The only per-record variation is:

- `accession` / `Id:BioSample` (SAMN…), `Id:SRA` (SRS…), `Id:GEO` (GSM…) — three names for one cell
- `Title` — `nasal_prox1_<N>`, `N` = 1…1440
- the GEO link URL (contains the GSM) and sub-second timestamps

There is **no** genotype, treatment, condition, timepoint, sex, age, dev_stage, tissue, plate, well,
or replicate slot. Not empty — absent. The population is one condition.

### The same measurement through `seqforge io records`

`seqforge io records SRP383998` returns **4335 records** (3.79 MB on disk, 2.38 MB of JSON) and tells
the same story with two extra findings.

| level | n | distinct attribute-key sets | **distinct attribute blocks** | attributes carried |
|---|---|---|---|---|
| project | **15** | 1 | **1** | `center_name`, `data_type`, `submission_date` |
| sample | 1440 | 1 | **1** | `biosample_package`, `cell_type`, `center_name`, `source_name`, `strain`, `taxonomy_id` |
| experiment | 1440 | 1 | **1** | `instrument_model`, `library_selection`, `library_source`, `library_strategy` |
| run | 1440 | 1 | **1** | *(none — runs carry only an alias)* |

Collapsing every free-text string in the record set by masking digits leaves **7 templates,
420 characters (~105 tokens), from 7230 entries and 143,913 characters** — a 343x collapse:

```
x1440  sample     sample_alias      GSM#
x1440  experiment experiment_title  GSM#: nasal_prox#_#; Mus musculus; RNA-Seq
x1440  experiment experiment_alias  GSM#_r#
x1440  experiment library_name      GSM#
x1440  run        run_alias         GSM#_r#
x15    project    study_title       Three-dimensional morphologic and molecular atlases of murine
                                    nasal vasculatures [Mouse_Nasal_SmartSeq]
x15    project    study_abstract    To investigate heterogeneity of murine nasal Prox1+ …
```

Deduplicating by *identical string* saves almost nothing (7230 → 7202 entries) because the aliases
are per-cell unique. The collapse only works if it is done **structurally** — one exemplar per
(level, label, template), with the varying token recognised as an identifier. That is the shape the
harvest-cost design has to take.

### Two defects found on the way

1. **`seqforge io records` emits the project record 15 times.** `fetch_records` de-duplicates inside
   `parse_sra_package_set`, which sees one `_BATCH = 100` slice at a time; 1440 experiments = 15
   batches = 15 identical `PRJNA853582` records (`src/seqforge/io/archive.py:409`). All 15 are
   byte-identical apart from nothing at all. It also sends 15 duplicate accessions to
   `efetch db=bioproject`. Harmless today at 6 samples; at 1440 it is 15 copies of the only real
   prose in the set.

2. **The string that names the chemistry is fetched and then dropped.** The SRA experiment XML
   carries `<DESIGN_DESCRIPTION/>` **empty** and puts the prose in
   `<LIBRARY_CONSTRUCTION_PROTOCOL>`:

   > FACS sorted nasal Prox1+ cells were processed by Smart-Seq3 protocol Libraries were generated
   > following Smart-Seq3 protocol (Hagemann-Jensen M et al., Single-cell RNA counting at allele and
   > isoform resolution using Smartseq3. Nat Biotechnol 2020)

   `parse_sra_package_set` reads `DESIGN_DESCRIPTION` (the comment there calls it "the one piece of
   prose that describes the chemistry") and never reads `LIBRARY_CONSTRUCTION_PROTOCOL`. ENA exposes
   the same string as the `library_construction_protocol` field, which is not in `ENA_FIELDS`
   (`src/seqforge/io/remote.py:74`). GEO's SOFT record carries it twice more, as
   `!Sample_growth_protocol_ch1 = Smart-Seq3 single-cell RNA libraries` and
   `!Sample_extract_protocol_ch1`.

   The consequence is sharp. Grepping the whole 4335-record set for `smart` matches **15 records,
   all project-level, all the same string** — the series title `[Mouse_Nasal_SmartSeq]`. That says
   "SmartSeq" but **not which version**. As things stand seqforge cannot tell SMART-seq2 from
   SMART-seq3 from its own `io records` output, while the archive states it in words, three
   different ways, in records seqforge already fetches.

## What the near-identical collapse banked on this deposit (added 2026-08-04)

The section above says what the collapse *would* have to look like. This one is the measurement of
the shipped one, taken **2026-08-04** on the same 1440-record dump, against the tree carrying
[ADR-0031](../adr/0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md)
(`HARVEST_VERSION = 2026.8.0`). It is the note that record's Consequences point at.

| | before the collapse | after |
|---|---|---|
| document text sent | 786,906 characters | **194,038** |
| requests | 80 | **59** — 53 for the sample and run documents, 6 for the experiments |
| estimated input tokens | 375,066 | **180,035** |
| records withheld | — | **0** |
| records reduced | — | **4317 of 4320**; the three exemplars carry the prose |

**The baseline is the ask-derived batch width, not the world before it.** #282's width rule had
already taken this deposit from 540 requests to 80, so the 80 → 59 comparison is taken on top of it
rather than instead of it — quoting 540 here would credit the collapse with a saving the width rule
banked. The 53 + 6 split is #233 decision 4's arithmetic, to the request.

**Nothing is withheld because every level carries a per-cell serial name** (`nasal_prox1_270`,
`GSM6277169_r1`), so every non-exemplar member has a difference worth sending and is asked it. A
deposit whose members differed only in the accession seqforge itself wrote would withhold instead;
this one does not, and that is a property of the deposit.

**Quote residue goes to zero.** Before the reduction, 71 % of every document's four-token spans
occurred verbatim in another document of the same request, so a misrouted draft would have
span-verified against the wrong member. After it: **0 %, at every batch width from 1 to 250 and every
quote length from one token to four.** A reduced document holds exactly what distinguishes its
record, so there is nothing left for two of them to share.

**It also falsified a reading of the rule.** "Mark, never splice" applied to *every* member rather
than to the exemplar alone folds nothing on this deposit — the plan does not move at all. The
measurement is what showed that reading was wrong.

### Method

```bash
pixi run seqforge io records SRP383998 -C <workspace>           # the 4335-record set, cached
pixi run seqforge harvest extract --records seqforge/records/SRP383998.json --dry-run -C <workspace>
```

`--dry-run` renders every document and resolves no provider, so the plan is the paid run's list and
not a projection of one: `n_chars`, `n_requests` and `estimated_input_tokens` are read straight off
it. The before/after pair is the same command on either side of the collapse. `quote_residue` reaches
no CLI verb; it was called from Python over the same `ExtractionPlan`
(`seqforge.harvest.quote_residue(plan, width=…)`), swept over widths 1–250 and quote lengths 1–4.

### What this could not establish

- **Whether the reduction generalises.** One deposit, and an extreme one — 1440 records off a single
  template. The corpus-wide figure is the six benchmark cases ADR-0031 names, at 29–77 % less text.
- **Whether 0 % residue survives a deposit with genuinely repeated prose.** This deposit's members
  differ in a serial number; one whose members repeat whole sentences may not reduce to disjoint
  text. The instrument stays for that reason — 0 % here is a property of this dump, not a theorem.
- **What the reduction does to extraction quality.** Different text reaching a model can move a
  draft. That is a before/after digest pair under #225 constraint 3, and it is not measured here.

## Declared strings, and what an alias could reach

| field | value |
|---|---|
| `library_strategy` | `RNA-Seq` |
| `library_source` | `TRANSCRIPTOMIC SINGLE CELL` |
| `library_selection` | `cDNA` |
| `library_construction_protocol` (ENA / SRA) | names **Smart-Seq3** twice |
| GEO `!Sample_growth_protocol_ch1` | `Smart-Seq3 single-cell RNA libraries` |
| GEO series title | `… [Mouse_Nasal_SmartSeq]` |

The three enum fields do **not** name the chemistry — `RNA-Seq` / `cDNA` are exactly what
`bulk-rnaseq-pe` would declare. `TRANSCRIPTOMIC SINGLE CELL` says single-cell but not which. The KB
today ships 17 technologies (`10x-*`, `bd-rhapsody-*`, `bulk-rnaseq-pe`, `splitseq`) and no
`smartseq` entry, so nothing matches regardless.

## `seqforge io resolve GSE207085` fails — and it is a one-line cause

```
$ pixi run seqforge io resolve GSE207085
{"error": "GSE207085: no SRA study in the GEO record. It may be a SuperSeries with no declared
 sub-series, unreleased (status=hup), or carry no raw data."}   # exit 1
```

`geo_to_studies` finds an SRP by matching `term=([EDSR]RP\d+)` in the brief SOFT record
(`src/seqforge/io/remote.py:94`), i.e. it needs a `!Series_relation = SRA: …?term=SRP…` line. This
SubSeries has no such line — its brief SOFT declares only:

```
!Series_relation = SubSeries of: GSE207086
!Series_relation = BioProject: https://www.ncbi.nlm.nih.gov/bioproject/PRJNA853582
```

`parse_soft_superseries` only recurses **downward** (`SuperSeries of:`), so a SubSeries with a
BioProject-only relation dead-ends. `seqforge io resolve PRJNA853582` and `… SRP383998` both work
fine. Anyone driving GSE207085 today must hand seqforge the BioProject or the SRP, not the GEO
accession the paper cites.

## Data volume

| measure | value |
|---|---|
| ENA `fastq.gz`, all mates | **54.34 GB** across 2880 files |
| per-run pair (both mates) | min 0.17 MB · p25 7.67 MB · **median 17.95 MB** · p75 45.88 MB · p95 146.91 MB · max 569.53 MB · mean 37.74 MB |
| SRA `.sra` objects | 38.70 GB total, median 14 MB, max 399 MB |
| spots (read pairs) | 332,472,709 |
| bases | 100,406,758,118 (100.4 Gbp) |
| reads per cell | min 901 · p25 52,373 · **median 120,218** · p75 292,808 · max 3,113,299 |
| GEO processed matrix | `GSE207085_ss3_prox1_ct_normalized_expression_matrix.csv.gz`, 36 MB, **1001 cells** (of 1440) after the authors' QC |

The depth spread is three and a half orders of magnitude — the shallowest cell is 901 read pairs,
the deepest 3.1 million. A per-cell pipeline that assumes a uniform cost per unit will be wrong by
that factor.

## What could not be established

- **Plate and well.** Absent from every record at every level. If plate identity is wanted, it has
  to come from the authors or the paper, not the archive.
- **Sex, age, genotype.** Absent. `strain: FVB` is the only animal-level fact; "Prox1-GFP" appears
  only inside abstract prose.
- **How many mice.** Nothing in the records distinguishes animals.
- **Whether an `mm10` STAR index exists on any particular cluster.** `mm10` is a registered assembly
  in `liulab-genome`; index materialization is a cluster fact, not checkable from here.
- **Lane uniformity in the tails of the large runs.** Complete coverage was achieved only on the 10
  smallest runs; the other 48 were surveyed over a bounded 64 KB prefix. The lane-yield arithmetic
  makes a second lane implausible, but it is inference, not measurement.
- **`GSE207083` / `GSE207084`** (the SuperSeries' 10x and human siblings) were not characterized —
  out of scope for #228.

## Commands

Run from the repo root; scratch outputs went to a temp directory.

```bash
# --- GEO ---
curl -sS "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=GSE207085%5BAccession%5D&retmode=json"
curl -sS "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&id=200207085&retmode=json"
curl -sS "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE207085&targ=self&form=text&view=brief"
curl -sS "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE207086&targ=self&form=text&view=brief"
curl -sS "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSM6278339&targ=self&form=text&view=brief"
curl -sS "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE207nnn/GSE207085/suppl/"
curl -sS "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id=39196043&retmode=json"

# --- ENA: the full run inventory, including the field seqforge does not fetch ---
curl -sS "https://www.ebi.ac.uk/ena/portal/api/filereport?accession=PRJNA853582&result=read_run\
&fields=study_accession,secondary_study_accession,sample_accession,secondary_sample_accession,\
experiment_accession,run_accession,submission_accession,tax_id,scientific_name,instrument_platform,\
instrument_model,library_name,library_layout,library_strategy,library_source,library_selection,\
read_count,base_count,first_public,last_updated,experiment_title,study_title,sample_title,\
fastq_bytes,fastq_md5,fastq_ftp,submitted_ftp,sra_ftp,nominal_length,nominal_sdev,\
library_construction_protocol&format=tsv&limit=0"   # -> 1441 lines (header + 1440 runs)

# --- SRA ---
curl -sS "https://trace.ncbi.nlm.nih.gov/Traces/sra-db-be/runinfo?acc=SRP383998"      # 1441 lines
curl -sS "https://trace.ncbi.nlm.nih.gov/Traces/sra-db-be/run_new?acc=SRR19885643"    # per-read table
curl -sS "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=sra&id=SRX15928398&rettype=full&retmode=xml"

# --- all 1440 BioSamples, in 8 batches of 180 ---
curl -sS "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=biosample&rettype=full\
&retmode=xml&id=SAMN29399743,SAMN29399744,..."     # then parse Attributes/Ids/Owner and diff

# --- seqforge's own verbs ---
pixi run seqforge io resolve GSE207085                       # exit 1: no SRA study in the GEO record
pixi run seqforge io resolve SRP383998                       # exit 0, 2.5 MB, n_runs 1440
pixi run seqforge io records SRP383998 -C <workspace>        # 4335 records -> seqforge/records/SRP383998.json
pixi run seqforge io peek "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR198/005/SRR19884905/SRR19884905_1.fastq.gz"
pixi run seqforge io probe-remote \
  "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR198/005/SRR19884905/SRR19884905_1.fastq.gz" \
  --md5 c2cced79be6532140fef6345da8a7f0c --max-reads 2000
pixi run seqforge kb list                                    # 17 technologies, no smartseq
```

The header survey, the tag-fraction count and the whole-file lane check used bounded HTTP `Range`
reads at the same 64 KB / 256 KB budgets `io peek` and `io probe-remote` use; `peek` reports only one
`example_header` per file, and the batch question needs the distribution.
