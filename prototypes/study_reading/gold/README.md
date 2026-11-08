# Hand readings (gold) for the study-reading prototype

Three GEO studies, read by hand, in the shape of `../SCHEMA.md`. They are the reference the model
readings are scored against, so they were written without calling DeepSeek or Jev and without
opening `../harness/` or `../runs/`.

| Study | Kind | GEO samples | Factors (baseline) | Unsure items |
|---|---|---|---|---|
| GSE190102 | ChIP-seq with input controls | 43 | age: 3, 6, 12, 24 months (3 months), ordered | 16 |
| GSE164798 | Perturbation, 2 x 3 crossed | 24 | genotype (WT) x running (sedentary) | 0 |
| GSE206603 | Co-measured assays, SuperSeries | 24 (12 RNA-seq + 12 ATAC-seq) | genotype: 4 iPSC lines (WT) | 24 |

## How the files were made

- I decided every label by reading the GEO sample records and the paper. Two steps were scripted:
  laying the GEO records out one sample per line, and writing the YAML from my tables. The scripts
  are in `build/`. Each one holds its study's hand tables (for example the animal-to-donor list in
  `gse190102.py`) and asserts the counts. `build/check.py` loads each YAML and checks the
  structure: every factor value is a declared level, each contrast differs in one factor with one
  side at baseline, co-measured libraries share a biosample, and each control pair shares a
  biosample or the same factor values.
- `raw/` caches every fetch: GEO text records (`<GSE>_self.txt`, `<GSE>_gsm.txt`), the papers' PMC
  XML and flattened text, and one BioSample lookup. The three other GSEs in `raw/` were candidates
  I rejected.
- Times below are this session's wall clock, with the GEO records tabulated by script and the
  papers searched with grep. They are not a person's reading time. Choosing the three studies
  took about 3 minutes more.
- Ontology IDs appear only where I was certain of them. Where I was not, for example the brain
  hemisphere, the attribute has text only.

## Additions to the schema

- Each sample has `title`, the GEO title, to make hand checks easier. GSE206603 samples also carry
  `series`, the SubSeries they sit in.
- A contrast, replicate group or series never mixes assays, targets or roles. A contrast or series
  that needs that scope says so with a `target` or `assay` key. Contrasts use signal libraries
  only. Replicate groups with one member are left out.
- `background_control` entries carry `basis`: `same biosample` or `same factor values`, the two
  arms of the rule.
- `unsure` items are `{label, note}`.
- An attribute that has no NCBI attribute name carries `unlisted: true` (APOE genotype).

## GSE190102: ageing neurons, three histone marks, total input

Signal et al. 2024, *Cells*, PMID 39195281. Mouse forebrain NeuN+ nuclei (neuronal nuclei sorted by
FACS) at four ages, ChIP-seq for H3K4me3, H3K27ac and H3K27me3, one total input per age.

**Why picked:** more than one target, input controls, an ordered age factor, and one animal giving
several libraries, which exercises donors, biosamples, co-measurement and background controls all
at once.

**Traps:**

- **Two deposits in one Series.** The 28 samples of 2021 carry age and antibody as characteristics.
  The 15 H3K27me3 samples added in 2024 carry age and mark only in the title
  (`363 WT 3M H3K27me3`). Their characteristics are coarser (`tissue: brain`, `cell type: neurons`).
- **The overall design names two marks;** the title and the 2024 samples add a third.
- **Input wording:** `TI` in the title and `chip antibody: None`. On GEO, only the overall design
  spells out "total input".
- **Donor ids only in titles.** The leading number is the animal id and recurs across marks, so
  animal 512 has H3K4me3, H3K27ac, H3K27me3 and the 3-month input. That fixes 19 donors and
  biosamples, 14 co-measured groups, and 6 signal libraries whose input is from the same animal.
- **GEO and the paper disagree on design.** GEO has a 6-month group (6 samples) that the paper
  never mentions; the paper says 3, 12 and 24 months, n = 5 mice per age. The reading follows GEO.
- **Sex is only in the paper** ("C57/BL6 male mice").

**Unsure:** the 15 H3K27me3 samples (same sort as the animal's other marks? assumed yes), and the
6-month input from animal 163734, which has no ChIP on GEO and might be a typo for 163736.

**Sources that settled labels:** GSM titles and characteristics; the Series overall design; the
paper's methods and introduction (PMC11353134). **Time:** about 4 minutes.

## GSE164798: 5xFAD by voluntary running

Wierczeiko et al. 2021, *Cells*, PMID 33804749. Male 5xFAD mice (a transgenic amyloid model) and
wild-type littermates, each sedentary, running for one day, or running for 30 days; RNA-seq of the
right brain hemisphere; 4 mice per group.

**Why picked:** two crossed factors, one with three unordered levels, so the baseline is not
obvious and the contrast rule has to drop a comparison the paper makes.

**Traps:**

- **Baseline wording:** `treatment: untreated` beside `treatment synonym: sedentary`.
- **Acute vs chronic** is in the paper but is not a contrast under the rule (neither side is the
  baseline). The reading lists 7 contrasts: genotype at each running level, and each running level
  against sedentary within each genotype.
- **Not a series.** Running duration (0, 1, 30 days) looks numeric, but GEO gives regimens and
  the paper compares regimens. `ordered: false`.
- **Genotype in the strain field:** `strain: 5xFAD (C57BL/6)`.
- **Age wording:** GEO says 3 months for every sample; the summary calls the chronic group
  "2-month-old", which is its age when running started. GEO's "one day" is "18 h before sacrifice"
  in the paper.

**Unsure:** nothing.

**Sources that settled labels:** GSM characteristics; the Series summary; the paper's methods
(PMC8004053) for littermates, hemisphere and one mouse per sample. **Time:** about 2 minutes.

## GSE206603: familial AD iPSC neurons, RNA-seq and ATAC-seq

Valdes et al. 2025, *Alzheimer's Research & Therapy*, PMID 39754192. A SuperSeries with an
RNA-seq SubSeries (GSE206602) and an ATAC-seq SubSeries (GSE206588). Neurons were differentiated
from four patient iPSC lines (a non-demented control, APP V717I, PSEN1 A79V, PSEN2 N141I), three
replicates per line per assay.

**Why picked:** the same lines are measured by two assays filed in two Series, joined only by the
SuperSeries and the paper.

**Traps:**

- **A field named `treatment` holds sex and APOE genotype** (`treatment: M, APOE2/3`). There is no
  treatment factor.
- **One donor per genotype.** The three replicates are parallel differentiations of one line, so
  4 donors, and genotype is confounded with donor, sex and APOE.
- **`disease state` duplicates genotype** (control vs AD). It is an attribute, not a second
  factor; as a factor it would make every contrast fail the one-factor test.
- **Matching titles are not proof of a shared biosample.** `NDC replicate 1 [RNA-seq]` and
  `NDC replicate 1 [ATAC-seq]` invite pairing, but neither GEO nor the paper says the two came from
  one differentiation. The same lab's 2020 study of the same design (Caldwell et al.,
  *Science Advances*, PMC7673760) had unequal replicate counts per assay (control: RNA n = 8,
  ATAC n = 5), so replicate numbers there could not have paired. The reading mints 24
  biosamples and lists no co-measurement. The study is co-measured at the donor level only.

**Unsure:** the biosample of all 24 samples, each with the candidate partner named.

**Sources that settled labels:** GSM characteristics and titles; the paper's methods
(PMC11699654); the 2020 paper's methods for the replicate-count argument; BioSample records, which
add nothing. **Time:** about 4 minutes, most of it on the pairing question.

## For the comparison

- Score `unsure` labels apart from settled ones. The GSE206603 biosample pairing is open, and a
  model that pairs by replicate number is not wrong by any source I found, only unsupported.
- `sample_type` for sorted nuclei (GSE190102) is filed as `tissue`, the source the nuclei came
  from. That is a convention, not a finding.
- Factor level spellings are mine (`WT`, `sedentary`, `3 months`). Match levels by meaning, not
  string.
