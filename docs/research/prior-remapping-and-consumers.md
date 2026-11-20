# Study design in public-data remapping: prior resources, whole-study LLM reading, and what models consume

Research, not a decision. Written 2026-11-08. Each claim is marked **read** (seen in the source, often
only the abstract or a search excerpt), **measured** (I ran it), or **inferred** (mine). A number with
no gold-set size is the authors' claim. Per-sample LLM curation (MetaSRA, GEOMeta, SRAgent, Elucidata
and others) is covered by the earlier prior-art note and not repeated. Design standards (ISA,
MAGE-TAB, ENCODE) are left to the parallel note.

## What carries over to seqforge

1. **Verdict on the thesis: it holds for the big uniform remaps and is overstated for curated
   resources.** ARCHS4, recount3, refine.bio, DEE2, NCBI's GEO counts, ChIP-Atlas, Cistrome DB and ReMap
   process each sample or run alone and keep per-sample attributes only, with no structured
   control-to-perturbed link. But Expression Atlas (4,187 differential experiments with curated
   factors), Gemma (over 10,000 curated studies) and GTRD (a control link for about 79% of ChIP-seq)
   do keep design. And most remaps keep the series ID and raw characteristics, so the relationships are
   recoverable rather than lost. Nothing found keeps design at remap scale with provenance, and
   nothing links different assays on the same biosample (inferred from the table below).
2. **LLM reconstruction of control and perturbed groups inside a GEO series has now been measured.**
   Soul & Young (preprint, 2026): precision 0.925, recall 0.836 on a 300-experiment test set held out by
   date, against a released 3,300-experiment gold set. It covers single-gene perturbations only. That
   gold set is the one to pull (read).
3. **The cross-sample step is the hard one.** Per-sample fields reach 95% or more. Trait extraction
   across samples in GenoTEX scores 32% and is called "the weakest link". Donor-to-sample mapping is
   Elucidata's worst field at 0.847 (read). So the model proposes a grouping and code checks it.
4. **Using the study's context helps, but the guard has to be in code.** Szczepaniak & Wren fill sample
   labels from the series text only when the sample's own evidence agrees. They state that this rule
   lives in the prompt, "not enforced by the software" (read). In seqforge, a grouping should be an
   `Assertion` with a quote that greps back for each sample, followed by code checks: every sample
   belongs to exactly one group, and the two groups differ only in the declared factor (inferred).
5. **What models consume is narrower than "rich metadata".** A seq2func track is described by a
   biosample ontology term, an assay and a target, with no condition field even in AlphaGenome. Models
   that condition on perturbation need four more things: control sets matched on cell line and batch
   (STATE), dose as a number with a unit (CPA), one biosample or donor identity shared across assays
   (Avocado, eDICE), and modalities paired within the same cell (scooby) (read).
6. **The data contract this implies** (inferred):
   - **Entities:** Biosample, Donor, Condition (agent, dose, time, vehicle), Assay with Target, Batch.
   - **Edges:** `control_of` (with the matched covariates and the one factor that differs),
     `same_biosample`, `same_donor`, `replicate_of` (biological or technical), and an ordered time or
     dose axis.
   - **Export:** a sparse biosample × assay × target × condition table.
7. **Verification is the open gap.** No system found requires a verbatim quote for a grouping.
   General data-analysis agents score about 17-21% on BixBench. GEOAgent claims 100% on resolving
   sample relationships with no gold size given (read). On long context against retrieval, neither
   wins in general (LaRA). A GEO series fits in a model's context; retrieval matters for the paper and
   its supplementary tables (inferred).

## 1. Testing the thesis: what uniform reprocessing resources keep

"Unit" is what one pipeline run consumes. In the Design column, **attr** = per-sample attributes,
**contrast** = curated comparisons, **ctrl** = a ChIP-to-input or treated-to-control link, and
**multi** = a link between assays on the same biosample.

| Resource | Assays | Scale | Unit | Design / relationships kept | How obtained |
|---|---|---|---|---|---|
| **ARCHS4** (Lachmann et al., *Nat Commun* 2018, [doi:10.1038/s41467-018-03751-6](https://doi.org/10.1038/s41467-018-03751-6)) | bulk RNA-seq, human and mouse | >1.5 M samples (archs4py docs, dated 6/2023; read) | GSM | **attr**: GEO title, characteristics, series ID; a lookup from series to samples (read, [archs4py](https://github.com/MaayanLab/archs4py)). No contrasts | copied from GEO |
| ↳ **RummaGEO** (Marino et al., *Patterns* 2024; preprint [10.1101/2024.04.09.588712](https://doi.org/10.1101/2024.04.09.588712)) | built on ARCHS4 | 135,264 human + 158,062 mouse gene sets from 23,395 studies (read) | GSE | **contrast**, automatic: k-means on embeddings of title + characteristics + source; controls found by keyword list (read). Grouping accuracy not read | rules, embeddings; no LLM |
| **recount3** (Wilks et al., *Genome Biol* 22:323, 2021, [doi:10.1186/s13059-021-02533-6](https://doi.org/10.1186/s13059-021-02533-6)) | bulk RNA-seq, human and mouse; SRA, GTEx, TCGA | >750,000 samples (read) | run, summarised per study | **attr** (SRA attributes; inferred, full text not reachable) | copied |
| ↳ **recount-brain** (Razmara et al., preprint [10.1101/618025](https://doi.org/10.1101/618025)) | brain RNA-seq | 6,547 samples, 62 SRA studies + GTEx + TCGA (read, poster excerpt) | sample | **attr**: clinical fields curated from each paper (read) | manual |
| **refine.bio** ([docs](https://refinebio-docs.readthedocs.io)) | microarray + RNA-seq, many organisms | not read | experiment = "samples processed together" (read) | **attr**, "lightly harmonized": similar keys merged, lowercased; submitter metadata kept (read). Compendia pool all of a species' samples | rules |
| **GREIN** (Al Mahi et al., *Sci Rep* 9:7580, 2019, [doi:10.1038/s41598-019-43935-8](https://doi.org/10.1038/s41598-019-43935-8)) | bulk RNA-seq, human, mouse, rat | >6,500 GEO datasets (read) | GSE | none stored; **the user defines groups** in the browser for on-the-fly differential expression (read) | user |
| **DEE2** (Ziemann et al., *GigaScience* 8:giz022, 2019, [doi:10.1093/gigascience/giz022](https://doi.org/10.1093/gigascience/giz022)) | RNA-seq, 9 species | >470,000 runs (11/2018); >1 M (4/2020, getDEE2) (read) | SRA run | **attr** (SRA) + QC (read) | copied |
| **NCBI GEO RNA-seq counts** ([info page](https://www.ncbi.nlm.nih.gov/geo/info/rnaseqcounts.html)) | bulk RNA-seq; human, with mouse announced for 2024 then 2025 (status not confirmed) | all historical human runs (read) | run, assembled into a per-series matrix | none: counts "may not match results in the accompanying publication"; minimal suitability checks (read) | automatic |
| **Expression Atlas** (Madrigal et al., *NAR* 54:D147, 2026, [doi:10.1093/nar/gkaf1238](https://doi.org/10.1093/nar/gkaf1238)) | microarray, RNA-seq, proteomics; 67 species | **4,562 bulk experiments: 4,187 differential, 375 baseline; 161,199 assays** (measured 2026-11-08 from `ebi.ac.uk/gxa/json/experiments`) | experiment | **contrast**: each contrast is a pairwise comparison between two groups of biological replicates, curated by hand (read, 2014 update). Differential experiments have 1-6 factors (2,278 one, 1,474 two, 368 three). Top factors: genotype 1,581, compound 859, time 584, disease 462 (measured). **Total contrast count not measured** | manual expert curation |
| **Gemma** (Lim et al., *Database* 2021; [curation docs](https://pavlidislab.github.io/Gemma/curation.html)) | microarray + RNA-seq; human, mouse, rat | 10,811 datasets, >395,000 samples (June 2020); ">15,000 studies" (gemma.R, 2022) (read) | experiment | **contrast**: experimental factors (most studies have 1-3), each sample labelled per factor by a curator; differential expression per factor (read) | manual |
| **ChIP-Atlas 3.0** (Zou et al., *NAR* 2024, [doi:10.1093/nar/gkae358](https://doi.org/10.1093/nar/gkae358); [wiki](https://github.com/inutano/chip-atlas/wiki)) | ChIP, ATAC, DNase, bisulfite sequencing | >376,000 experiments (read) | SRX | **attr**: antigen and cell type with class and subclass, "manually annotated by curators". Input controls are an antigen class, but **peak calling names no control**, no ChIP-to-input link is documented, and there is no treatment field (read, wiki) | manual |
| **Cistrome DB 3.0** (Taing et al., *NAR* 52:D61, 2024; [PMC10767960](https://pmc.ncbi.nlm.nih.gov/articles/PMC10767960)) | ChIP-seq, chromatin accessibility | ~45,000 human + 44,000 mouse (read) | sample | **attr**: several annotations per sample, each with a confidence score (read). Control handling not read | curated |
| **GTRD** (Kolmykov et al., *NAR* 49:D104, 2021, [doi:10.1093/nar/gkaa1057](https://doi.org/10.1093/nar/gkaa1057)) | ChIP-seq, DNase, others | not read | experiment | **attr + ctrl**: cell source, conditions, control experiment; a `control_id` field; 79.2% of ChIP-seq have a control (read, wiki excerpt). GEO metadata mapped to vocabularies by "geominer" software (read) | software + curation |
| **ReMap 2022** (Hammal et al., *NAR* 2022, [doi:10.1093/nar/gkab996](https://doi.org/10.1093/nar/gkab996)) | ChIP-seq, ChIP-exo | 8,103 human datasets (read) | dataset = series × regulator × biotype | **attr**: target and biotype checked by hand, papers read where metadata is thin; input used "when available" (read for Arabidopsis and 2015; human 2022 not confirmed) | manual |
| **scBaseCount** (Arc; preprint [10.1101/2025.02.27.640494](https://doi.org/10.1101/2025.02.27.640494)) | scRNA-seq (10x) | >500 M cells, 21 organisms, 72 tissues ([Arc atlas page](https://arcinstitute.org/tools/virtualcellatlas), read) | SRX | **attr** per SRX, including perturbation and cell line (earlier note) | LLM agent |
| **CELLxGENE Census** ([release info](https://chanzuckerberg.github.io/cellxgene-census/cellxgene_census_docsite_data_release_info.html)) | single-cell | LTS 2025-11-08: 1,845 datasets; human 162.0 M cells, 99.6 M unique (read) | dataset, as submitted | **attr** per cell; `donor_id` groups cells within a dataset; schema 5.x+ adds `experimental_condition_ontology_term_id` and `genetic_perturbation_id` (earlier note). No contrasts | submitter curation to the schema |
| **Tahoe-100M** (Zhang et al., preprint [10.1101/2025.02.20.639398](https://doi.org/10.1101/2025.02.20.639398)) | scRNA-seq perturbation | 100 M cells, 50 cancer lines, 379 drugs × 3 doses (">1,100 treatments"), ~60,000 conditions (read) | experiment generated by one lab | **ctrl** native: per-plate, per-dose vehicle (DMSO) comparison (read, secondary description) | designed in |
| **scPerturb** (Peidli et al., *Nat Methods* 21:531, 2024, [doi:10.1038/s41592-023-02144-y](https://doi.org/10.1038/s41592-023-02144-y)) | scRNA, ATAC, CITE-seq | 44 datasets (32 CRISPR, 9 drug) (read) | dataset | per-cell perturbation label and QC (read); control labelling details not read | manual harmonisation |

**Where the thesis holds** (inferred): the uniform bulk remaps process one sample or run at a time and
expose attributes only; peak atlases annotate target and cell type but do not structure the
ChIP-to-input link (GTRD excepted) or the treatment; single-cell aggregators keep `donor_id` and no
comparisons; and no resource links assays on the same biosample. **Where it is overstated:** curated
design exists, for expression only, at 10³-10⁴ studies (Expression Atlas, Gemma, CREEDS); one-lab
perturbation atlases carry design natively; and "lost" is usually "never structured", since series
membership and raw characteristics survive and RummaGEO and Soul & Young rebuilt contrasts from them.

The defensible claim: no remapping resource exposes a **cross-assay, design-aware, provenance-backed**
graph at archive scale (inferred).

## 2. LLMs and agents that read a whole study

| Work | Reads | Recovers | Gold size → result | Status |
|---|---|---|---|---|
| **Soul & Young**, "Automated generation of a gene perturbation transcriptomic atlas using large language models", [10.64898/2026.08.08.743502](https://doi.org/10.64898/2026.08.08.743502) | GEO experiments (input detail not stated on abstract page) | single-gene perturbation series; **case-control group per sample**; perturbed gene, perturbation type, cell line, normalised to ontologies | 3,300 curated experiments (2,400 train / 600 validation / 300 test held out by date): **P 0.925, R 0.836**. Reasoning models and fine-tuning "substantially improved" results. Atlas: 6,802 signatures, 4,453 experiments, 2,907 genes; gold released (read) | preprint, 2026-08 |
| **GEOAgent**, Zhao et al., [10.64898/2026.06.06.730646](https://doi.org/10.64898/2026.06.06.730646) | 181,760 series + 84,756 PubMed records in a relational database and semantic index | assay type, "experimental design pairings", standardised sample names | "100% accuracy in assay classification and sample relationship resolution", 96% retrieval precision; gold size not stated (read). Treat as unverified | preprint, 2026-06 |
| **Szczepaniak & Wren**, [rs-10844781](https://doi.org/10.21203/rs.3.rs-10844781/v1) | GSM fields + parent GSE title, summary, design + the label distribution of sibling samples | per-sample Tissue, Condition, Treatment (control, vehicle and sham kept explicit), with study-level fill-in | 1,500 per field: Condition F1 0.957, Treatment F1 0.907 (P 0.976, R 0.847); 804,427 samples; local Gemma-4-12B. Raw span kept beside the normalised value (read) | preprint, 2026-08 |
| **GenoTEX**, Liu et al., [arXiv:2406.15341](https://arxiv.org/abs/2406.15341) v3 | GEO/TCGA series matrices | dataset selection; which characteristic row encodes the trait, then its per-sample encoding | 911 datasets, 132 traits, 1,384 problems. Selection 80.25% accuracy; **trait extraction 32.28% (CSC, the paper's score for agreement with the curated table)**, "the weakest link". Final gene F1 41.39% against 70.51% for humans (read) | preprint |
| **Elucidata multi-agent** (earlier note) | GEO + paper + supplementary tables | per-sample fields incl. donor ID | 78 series, 4,652 samples; donor ID 0.847 worst | preprint |
| **Biomni**, Huang et al., [10.1101/2025.05.30.656746](https://doi.org/10.1101/2025.05.30.656746) | general agent: 150 tools, 59 databases (read) | not evaluated on study design (inferred from the evaluations described) | reported Science publication not confirmed | preprint |
| **BixBench** (FutureHouse), [arXiv:2503.00096](https://arxiv.org/abs/2503.00096) | data capsules + open questions | end-to-end analysis | 53 scenarios / 296 questions (v1): frontier agents 17% (v1) to ~21% (v2/v3) on open answers (read) | preprint |
| **PaperQA2**, Skarlinski et al., [arXiv:2409.13740](https://arxiv.org/abs/2409.13740) | full-text papers, retrieval with citations | literature answers, contradictions | LitQA2, 248 questions: precision 85.2% vs 73.8% for human experts; accuracy 66.0% vs 67.7% (read). The gain comes from abstaining | preprint |

**Long context against retrieval.** LaRA ([arXiv:2502.09977](https://arxiv.org/abs/2502.09977), ICML 2025) ran 2,326
cases and found "no silver bullet". Long context did better on reasoning and comparison tasks, and
retrieval on some others (read). That set is not science-specific. On supplementary tables, one
systematic-review study ([arXiv:2609.27418](https://arxiv.org/abs/2609.27418)) reports that naive
LLM extraction truncates tables and invents columns (read, excerpt). I found no benchmark on reading
GEO sample sheets or supplementary sample tables.

**How outputs were verified:** a gold set split by date (Soul & Young), the raw span kept beside the
normalised value and a study-context guard stated in the prompt (Szczepaniak & Wren), a reviewer agent
per field (Elucidata), citation recall (PaperQA2). No one requires a verbatim quote for each group
membership or checks the partition structurally (inferred). Both are cheap: a group label is usually a
literal token in a GSM title or characteristics line.

## 3. What condition-aware models consume

| Model family | Example | Metadata the training data must carry |
|---|---|---|
| Multi-task seq2func | **AlphaGenome** ([output metadata](https://google-deepmind-alphagenome.readthedocs-hosted.com/exploring_model_metadata.html)) | Per track: `name`, `strand`, `ontology_curie` (UBERON/CL/EFO-style), `biosample_name`, plus `gtex_tissue` for RNA. The notebook adds `biosample_type`, `assay_title`, `histone_mark`, `transcription_factor`, `biosample_life_stage`, `genetically_modified`. Tracks: RNA 667, CAGE 546, DNase 305, ATAC 167, histone ChIP 1,116, TF ChIP 1,617, contact maps 28. **No treatment field** (read). Enformer and Borzoi use similar ENCODE/FANTOM/GTEx descriptions (inferred, not re-read) |
| Cross-assay imputation | **ChromImpute** (Ernst & Kellis, *Nat Biotechnol* 2015, [doi:10.1038/nbt.3157](https://doi.org/10.1038/nbt.3157); DOI resolves, text not re-read); **Avocado** (*Genome Biol* 21:81, 2020, [doi:10.1186/s13059-020-01977-6](https://doi.org/10.1186/s13059-020-01977-6)); **eDICE** (*Nat Commun* 2023, [doi:10.1038/s41467-023-40211-2](https://doi.org/10.1038/s41467-023-40211-2)) | A tensor of position × cell type × assay (Avocado). eDICE conditions on the observed tracks of a biosample and transfers across ENTEx donors (read). It needs **one biosample identity shared across assays**, and donor × tissue for personal imputation (inferred) |
| Perturbation response | **CPA** (*Mol Syst Biol* 2023, [doi:10.15252/msb.202211517](https://doi.org/10.15252/msb.202211517)) | Separate embeddings for cell type, perturbation and covariates. **Dose is continuous**, other covariates categorical; predicts unseen doses, cell types and time points (read) |
| | **GEARS** (Roohani et al., *Nat Biotechnol* 42:927, 2024) | Unperturbed + perturbed cells from the same screen; gene-gene graph priors (read) |
| | **STATE** (Adduri et al., preprint [10.1101/2025.06.26.661135](https://doi.org/10.1101/2025.06.26.661135)) | Perturbed cell sets "paired with unperturbed control cell sets of equal size and matched covariates". Controls are sampled "from the same cell line", optionally the same batch. A perturbation is a drug-concentration pair; batch label optional. Trained on Tahoe-100M, Parse, Replogle (read) |
| Seq2func conditioned on cell state | **GET** (Fu et al., *Nature* 637:965, 2025) | Sequence + **chromatin accessibility of the same cell type**, 213 cell types (read) |
| | **scooby** (preprint [10.1101/2024.09.19.613754](https://doi.org/10.1101/2024.09.19.613754)) | Borzoi + a per-cell embedding from **paired scRNA + scATAC** (read) |
| | **Decima** (Genentech, preprint [10.1101/2024.10.09.617507](https://doi.org/10.1101/2024.10.09.617507)) | Pseudobulks over 201 cell types × 271 tissues × 82 diseases, >22 M cells (read) |
| | **STRAND** ([arXiv:2602.10156](https://arxiv.org/abs/2602.10156)) | **Control cell state + DNA sequence → perturbed distribution**, built on Borzoi (read) |

**Distilled: what the corpus must expose** (inferred from the rows above):

- **Nodes**
  - Biosample: organism; tissue or cell type (UBERON/CL); cell line (Cellosaurus); life stage; sex;
    genotype.
  - Donor.
  - Condition: agent (CHEBI or HGNC + method), dose + unit, time + unit, vehicle.
  - Assay (EFO) with Target (gene or histone mark).
  - Batch (plate, lane, date).
  - Library, which seqforge already has.
- **Edges**
  - Library `measures` (biosample, assay, target).
  - Biosample `from_donor` donor.
  - Biosample `under` condition.
  - `control_of`: a perturbed group to its control group, listing the matched covariates and the one
    differing factor. STATE matches on cell line and batch.
  - `same_biosample` across assays, and `same_cell` for multiome or CITE-seq barcodes.
  - `replicate_of`, typed biological or technical.
  - An ordered axis for time or dose series.
- **Export** a sparse biosample × assay × target × condition table with an observed/missing mask
  (Avocado, eDICE) and a list of control-to-perturbed pairs with covariates (CPA, STATE).

## Gaps

- **Expression Atlas:** the total contrast count was not measured, only experiments and factors.
  Gemma's 2026 study count was not found.
- **Controls in peak atlases:** how ChIP-Atlas 3.0, Cistrome DB and human ReMap 2022 use input
  controls in peak calling was not confirmed. The ChIP-Atlas wiki names no control.
- **Missing inputs and outputs:** Soul & Young's LLM inputs (series only, or the paper too?), GEOAgent's
  gold set, and scGPT's perturbation mode were not read.
- **Full texts not read:** recount3, Enformer and Borzoi. Their rows rest on abstracts or memory and
  are marked as such.
- **No benchmarks found** for LLM reading of GEO sample sheets or supplementary tables, or for
  recovering multi-assay links within a study.
