# Study design and inter-sample relationships: how others represent them, who holds gold labels

Research, not a decision. Sources were read on **2026-11-08**, mostly specs, schema files, and
abstracts or search excerpts of papers. Each claim is marked **read** (seen in the source; "excerpt"
means only a search-result excerpt) or **inferred** (mine). A number without a gold-set size is the
authors' claim. Nothing was re-run. Per-sample attribute curation is out of scope: see the earlier
prior-art note.

## What carries over to seqforge

1. **No standard makes "control" a property of a sample.** It appears in one of three places: a
   baseline flag on one value of a factor (Gemma); an explicit pair of sample groups, reference vs
   test (Expression Atlas "contrast"); or a typed edge between records (ENCODE `possible_controls`
   between experiments, `controlled_by` between files, plus a closed `control_type` list). seqforge
   needs all three: a samples-by-factor-values table, a contrast object over sample sets, and typed
   edges for ChIP-to-input pairing (read, rows below; the three-part split is inferred).
2. **"Same biosample, several assays" is a shared node, not an edge between assays.** ISA and
   MAGE-TAB reuse a Sample or Source name across assay tables. ENCODE gives each library a
   `biosample` link. HCA hangs every file off a chain of processes from one donor and specimen. So
   the join key is a biosample identifier minted by seqforge and attached to each library. An
   "ATAC is paired with RNA" edge is not needed (read for the models; the conclusion is inferred).
3. **Expression Atlas's configuration XML is close to a ready contrast schema.** It holds assay
   groups (id, label, run accessions such as SRR054659) and contrasts (id, name, reference group,
   test group). Gemma adds what that format lacks: a baseline flagged per factor, and batch stored
   as one more factor. ENCODE's `control_type` list covers ChIP/CUT&RUN controls: control, IgG,
   input library, mock input, no protein target, non-specific target, wild type (read).
4. **The largest human-curated GEO design labels are Gemma (>18,000 datasets, 2024) and Expression
   Atlas (4,562 studies across baseline and differential).** Smaller sets: DiSignAtlas (10,306
   disease-vs-control comparisons) and CREEDS (about 4,200 crowd signatures). Only Gemma's
   500-study benchmark (licence CC BY-NC) comes with a published LLM score for design extraction.
   No large public gold of GEO ChIP-to-input sample pairs was found outside ENCODE's own records
   (read; the "largest" ranking is inferred from what was found).
5. **Design extraction is unsolved at the study level, and pairing is the weakest step.** GEOracle
   (rules plus a support-vector machine) reports, per GEO Series: 93.2% sample grouping, 94.6%
   control-vs-perturbation labelling, but only 83.1% control-to-treatment pairing, on 73 Series.
   Gemma's 2026 agent (a preprint) finds that a factor exists 98% of the time but scores F1 0.62 on
   factor values, and reproduces the full curation in 7.5% of studies. Curators agree with each
   other only 60–90% on these tasks (read).
6. **Score per edge, with curator agreement as the ceiling.** A study-level "exactly right" score
   punishes one wrong sample as heavily as a wrong design (7.5% exact vs 0.98 factor recall). Grade
   sample-group membership, the control flag and the pairing separately (inferred).
7. **Pairing is where code can check the LLM.** A valid contrast should differ from its reference on
   one factor and match it on all the others. That rule is checkable against the factor table and
   needs no model judgement. It fits seqforge's split: the LLM proposes groups backed by quotes,
   code checks the pairs (inferred; no system found states this check).

## Q1. How standards and databases represent design and relationships

| Model | Core entities and edges | What a control or pairing looks like | Grain | Source (evidence) |
|---|---|---|---|---|
| **ISA** (Investigation–Study–Assay; ISA-Tab, ISA-JSON) | Investigation has Studies, each Study has Assays. Each Study and Assay holds a directed, acyclic graph: Source → (sample-collection Process) → Sample → Process → … → Data file. Processes may split one input into several outputs or pool several into one | No control construct. Study factors are declared in the investigation file (`Study Factor Name`, `Study Factor Type`); values go in `Factor Value[...]` columns as free text, a number or an ontology term. "If both Study and Assay have a Factor Value, these must be different" | Per-sample factor values on top of a process graph | [isa-specs: model](https://isa-specs.readthedocs.io/en/latest/isamodel.html), [ISA-Tab](https://isa-specs.readthedocs.io/en/latest/isatab.html) (read). Splitting and pooling reuse the same `Name` value. The spec does not say outright that one Sample Name may start several Assay tables (read); that this is the multi-assay link is inferred |
| **MAGE-TAB 1.1** (IDF investigation file + SDRF sample-and-data file) | SDRF: one row per hybridization channel, columns left to right in time order: Source → Extract → Labeled Extract → Hybridization/Assay → Scan → data files, joined by `Protocol REF` columns | IDF declares `Experimental Factor Name`/`Type`; SDRF `Factor Value[...]` columns sit after the hybridization and "usually duplicate" a Characteristics or Parameter Value column. No control edge | Per-row (one assay channel) factor values | [MAGE-TAB 1.1 PDF](https://www.ddbj.nig.ac.jp/assets/files/magetab/MAGE-TABv1.1.pdf), [tab2mage overview](https://tab2mage.sourceforge.net/docs/magetab_docs.html) (read overview; PDF not opened) |
| **SDRF-Proteomics v1.1.0** (2026-01) | "Each row = one sample linked to one data file"; `characteristics[...]`, `comment[...]`, `factor value[...]` | A factor value "typically mirrors a characteristics column value". Biological and technical replicate columns are both required; biological numbering "restarts per experimental condition (factor value group)". A source name may repeat when one sample feeds several files | Per-row; the design is implicit in factor-value groups | [bigbio/proteomics-sample-metadata](https://github.com/bigbio/proteomics-sample-metadata/blob/master/sdrf-proteomics/README.adoc) (read) |
| **OBI / EFO** (Ontology for Biomedical Investigations; Experimental Factor Ontology) | Terms only. OBI `study design` (OBI_0500000) is "a plan specification comprised of protocols", with children such as parallel group design (OBI_0500006). EFO_0000001 `experimental factor` is a root class; EFO_0001779 `time series design` | Design-type labels for a whole study, not edges | Per-study tag | Excerpts via [OntoBee/BioPortal](https://bioportal.bioontology.org/ontologies/EFO) and [NMDC slot](https://w3id.org/nmdc/experimental_factor/) (read, excerpt). No case-control design term ID was confirmed |
| **Gemma** (UBC curated GEO re-analysis) | ExperimentalDesign → Experimental Factors (usually 1–3; e.g. age, genotype, treatment) → Factor Values. "Each sample has a specific Factor Value for each of the factors" | Curators mark a **baseline condition** for factors such as genotype, treatment and timepoint, not for organism part or sex. "Any batch information obtained is stored as a factor in the experimental design" | Per-sample values plus a per-factor baseline; contrasts are derived from these | [Gemma curation docs](https://pavlidislab.github.io/Gemma/curation.html) (read) |
| **Expression Atlas** (EMBL-EBI) differential experiments | `configuration.xml`: `<assay_groups>` hold `<assay_group id label>` with run accessions; `<contrasts>` hold `<contrast id>` with `<name>`, `<reference_assay_group>`, `<test_assay_group>` | The contrast is the control edge: reference set vs test set, curated by hand from "experimental intent". At least 3 biological replicates per group | Per-contrast, over run-level sample sets | [E-GEOD-21860 configuration](https://ftp.ebi.ac.uk/pub/databases/microarray/data/atlas/experiments/E-GEOD-21860/E-GEOD-21860-configuration.xml) (read). Manual curation and the 3-replicate minimum: [Petryszak 2014, NAR](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3964963/) (read, excerpt) |
| **ENCODE** | Experiment → Replicate (biological and technical numbers) → Library → Biosample → Donor. Biological replicate: "different biological source"; technical: "same biological source". Library `biosample`: "the biosample that nucleic acid was isolated from". Series objects (reference epigenome, treatment time series, organism development series, …) group experiments | Experiment `possible_controls` → Dataset; File `controlled_by` → File, "used by the pipeline to find ChIP-seq controls"; control experiments carry `control_type`. An audit checks that a ChIP-seq experiment "lists control Experiments with matching biosample metadata". The helper script assigns `controlled_by` by biological replicate **position**, and needs the same replicate count in control and experiment | A graph: experiment-level and file-level typed edges | Schemas [experiment](https://github.com/ENCODE-DCC/encoded/blob/dev/src/encoded/schemas/experiment.json), [replicate](https://github.com/ENCODE-DCC/encoded/blob/dev/src/encoded/schemas/replicate.json), [library](https://github.com/ENCODE-DCC/encoded/blob/dev/src/encoded/schemas/library.json), [file](https://github.com/ENCODE-DCC/encoded/blob/dev/src/encoded/schemas/file.json); [data organization](https://www.encodeproject.org/help/data-organization/); [eu_add_controlled_by](https://encode-utils.readthedocs.io/en/latest/scripts/eu_add_controlled_by.html) (all read). Treatment time series: 82 datasets, 24 treatments ([arXiv 2305.00006](https://arxiv.org/abs/2305.00006), read, excerpt) |
| **HCA** (Human Cell Atlas) | Biomaterials (donor_organism → specimen_from_organism → cell_suspension), files, protocols, processes. A process with biomaterial in and biomaterial out is sample preparation; biomaterial in, file out is an assay; file in, file out is analysis. Protocols attach to processes. Edges are stored as `links.json` subgraphs | No control concept found. Donor grouping is structural: everything traces back to one donor node | A process graph | [HCA metadata structure](https://data.humancellatlas.org/metadata/structure) (read, excerpt; direct fetch returned 404), [ingest docs](https://ebi-ait.github.io/ingest-programmatic-submissions/docs/introduction/introduction.html) (read, excerpt) |
| **LINCS / CMap L1000** (drug and gene perturbation screens) | Signature = "the entire vector of differential expression values … after aggregating the data across replicates"; `distil_id` = one replicate profile; `pert_idose`, `pert_itime` = dose and time strings; `pert_type` (trt_cp compound, trt_oe overexpression, trt_xpr CRISPR, …) | The default control is the **plate population**: a robust z-score against "every other well" on the plate, not a matched vehicle well. Vehicle controls are defined but are not the reference | Per-signature; the control is implicit | [clue.io glossary](https://clue.io/connectopedia/glossary) (read). The `ctl_vehicle`/`ctl_untrt` values were not confirmed |
| **scPerturb** | One h5ad file per dataset. Columns `perturbation`, `perturbation_type`, `dose_value`, `dose_unit`, `nperts` appear in annotated tables | Control cells are a value in the perturbation column (the package example passes `control='control'`) | Per-cell label | [Peidli et al., Nat Methods 21:531, 2024](https://doi.org/10.1038/s41592-023-02144-y) (paywalled, not read); fields seen only in [pertpy's tutorial](https://pertpy.readthedocs.io/en/latest/tutorials/notebooks/metadata_annotation.html) and the [GitHub README](https://github.com/sanderlab/scPerturb) (read, excerpt). No formal schema found |
| **PROV-O** (W3C provenance ontology) | Entity, Activity, Agent | Not found in use for sample design or control relationships. Its use found here is workflow-run provenance (Workflow Run RO-Crate) | — | [Leo et al., arXiv 2312.07852](https://arxiv.org/abs/2312.07852) (read, excerpt) |
| **4DN** (4D Nucleome) | Not read: the fetch of the [data model page](https://data.4dnucleome.org/help/user-guide/data-model) failed | — | — | gap |

## Q2. Gold labels for relationships, tied to GEO

Legend: (a) control vs treatment contrast, (b) ChIP or CUT&RUN sample-to-input/IgG pairing,
(c) same-biosample multi-assay link, (d) replicate or donor grouping.

| Resource | Types | Size (source, date) | Scope | How curated | GEO IDs recoverable? Licence |
|---|---|---|---|---|---|
| **Gemma** | a (factor + baseline), d (batch, replicates via factor values) | 10,811 datasets, >395,000 samples, June 2020 ([Lim et al., Database 2021](https://pmc.ncbi.nlm.nih.gov/articles/PMC7904053), read, excerpt); ">18,000 data sets (>600,000 samples)" in an ISMB 2024 talk abstract (read, excerpt; not peer reviewed). Single-cell since May 2025 (read, excerpt) | Human, mouse, rat expression (inferred from the earlier note) | Manual, with ontologies | Yes, Gemma is built on GEO accessions (read). Access via gemma.R/gemmapy (read, excerpt); licence of the full design data not checked |
| **Gemma curation benchmark** | a, d (factors, values, tags) | 500 studies: 400 development (9,859 samples) + 100 held out (read) | Gemma's scope | Gemma curators, "polished" to current standards | Yes. Data CC BY-NC; code Apache-2.0 ([gemma-curation-benchmark-data](https://github.com/PavlidisLab/gemma-curation-benchmark-data), read via the preprint) |
| **Expression Atlas** | a (explicit contrasts), d (assay groups) | 4,562 studies, 161,199 assays, 67 species on the live page, undated ([gxa home](https://www.ebi.ac.uk/gxa/home), read 2026-11-08). The baseline/differential split was not shown. Human bulk: 1,600 experiments ([George et al., NAR 52:D107, 2024](https://doi.org/10.1093/nar/gkad1021), read, excerpt) | Microarray and RNA-seq, many species | Manual contrast curation (read, excerpt) | Yes: E-GEOD-* IDs and run accessions sit in each `configuration.xml` on the FTP site (read). Licence not checked |
| **DiSignAtlas** | a (disease vs control) | 10,306 comparisons incl. 328 single-cell; 181,434 profiles; 1,836 diseases ([Zhai et al., NAR 52:D1, 2024](https://doi.org/10.1093/nar/gkad961), read, excerpt) | Human and mouse; GEO, TCGA, ArrayExpress | Manual keyword search, then a manual check that each set has disease and control samples (read, excerpt) | Likely (GEO-sourced); download route and licence not checked |
| **CREEDS** | a (perturbation vs control) | 2,460 single-gene, 839 disease, 906 drug signatures ([Wang et al., Nat Commun 2016](https://doi.org/10.1038/ncomms12846), read, excerpt) | GEO expression | Crowd (online course), then manual validation | GSE and GSM control/case lists per signature (inferred from the design; not checked) |
| **GEOracle's annotated set** | a, d | 277 GSE labelled perturbation or not; 73 GSE, 832 GSM in 259 groups labelled perturbation or control (read) | Mouse and human expression | Manual by the authors | Yes, in principle; a release was not checked |
| **ENCODE** (incl. Roadmap, also deposited in GEO) | b (`possible_controls`, `controlled_by`, `control_type`), c (shared biosample), d (replicate numbers) | Counts not read | Human, mouse; ChIP, ATAC, DNase, RNA, … | Submitted by the consortium, then audited (read) | ENCODE accessions; the GEO cross-references were not checked. Note: the standard metadata TSV once lacked controls; use the REST API ([Biostars, about 2017](https://biostars.org/p/212548), read, excerpt) |
| **Cistrome DB** | factor and biosource; (b) not confirmed | 13,366 human + 9,953 mouse samples (2017); about 47,000 (2019; read only on a secondary page) | ChIP-seq, DNase-seq, ATAC-seq | "Automatically parsed from GEO entries, followed by manual curation of factor names and biological sources" (read, excerpt) | GSM-level (inferred). **How controls are matched was not found** |
| **ChIP-Atlas** | factor and cell type; (b) not confirmed | Count not read | ChIP, ATAC, DNase, bisulfite; many species | "Antigens and cell types were manually annotated by curators" ([wiki](https://github.com/inutano/chip-atlas/wiki), read) | SRX-keyed (inferred). Whether MACS2 peak calling uses inputs was not confirmed |
| **ReMap 2022** | (b) used, not published as pairs | Human 10,619 curated → 8,103 passing QC; mouse 7,317 → 5,503 ([about page](https://remap.univ-amu.fr/about_hsap_page), read) | Transcription-factor ChIP-seq and ChIP-exo | Manual; QC drops datasets with "less than twofold" signal over control (read). The 2018 release used inputs "when available" (read, excerpt) | GSE-keyed (read). Pair table not found |
| **scPerturb** | a (per-cell control label) | Count not read (paper paywalled) | Single-cell perturbation screens | Harmonised by hand across datasets (read, excerpt) | Per dataset; licence not checked |
| **GEO SuperSeries / SubSeries** | groups related Series (sometimes multi-assay) | — | All of GEO | Submitter-made | A SuperSeries "is composed of the SubSeries listed below"; links live in the SOFT `series relation` fields (read, secondary sources only) |
| **GTRD** | — | Not checked | — | — | gap |
| **PharmGEO** | — | **Not found** under this name in any search | — | — | It may be the name of a validation set in the GEOMeta preprint; not resolved |

## Q3. Automated extraction of design, contrasts and control pairing

| System | Method | Measured accuracy (gold size) | Status |
|---|---|---|---|
| **GEOracle**, [bioRxiv 10.1101/150896 v3](https://www.biorxiv.org/content/10.1101/150896v3.full), Oct 2018; published in Comput Biol Chem 80:152 (2019) (read, excerpt) | Six steps: SVM flags perturbation Series; multi-stage clustering groups samples (titles, then characteristics); SVM labels each group perturbation or control; text matching pairs groups; manual check; limma differential expression | Step 1: AUROC 0.89, cross-validated on 277 GSE. Grouping: 93.2% per Series (73 GSE; titles alone 86.3%, characteristics alone 64.4%). Control labelling: 94.6% per Series after cluster adjustment (73.3% raw). **Pairing: 83.1%** (gold set not stated). No specificity reported (read) | Peer-reviewed; small, self-built gold. [GEOraclePlus](https://github.com/VCCRI/GEOraclePlus) exists (read, excerpt) |
| **RummaGEO**, Marino … Ma'ayan, [bioRxiv 10.1101/2024.04.09.588712](https://www.biorxiv.org/content/10.1101/2024.04.09.588712.full); Patterns 2024 (read, excerpt) | K-means on embeddings of `sample_title` + `characteristics_ch1` + `source_ch1`; a keyword list finds controls ("wildtype", "ctrl", …); with no control found, each group is compared with all the others | **No evaluation of grouping or control detection against a gold set** (read). Scale: 135,264 human + 158,062 mouse gene sets from 23,395 studies (paper); the live site reports 30,576 studies (read, excerpt) | Peer-reviewed. Shows how large an automated, unvalidated contrast set can get |
| **Gemma curation agents v1.1**, Pavlidis, Mancarci et al., [bioRxiv 10.64898/2026.07.30.741874](https://www.biorxiv.org/content/10.64898/2026.07.30.741874v1.full), 2026-07 | Agentic workflow, about 25 LLM calls per dataset (Haiku 4.5, Sonnet 5, Opus 4.8; GLM 5.2 for comparison). Proposes factors, factor values grounded in ontology terms, and study tags | Against 500 Gemma-curated studies: factor detection recall **0.98**; factor-value ontology F1 **0.62** (exact match); tag F1 0.45 against a curator ceiling of about 0.55; exact reproduction 30/400 (7.5%). A triage model flags its own errors (AUC 0.86); the LLM's self-reported confidence does not (AUC 0.38). **Baseline/control and sample-assignment accuracy are not reported separately.** About $0.5 per dataset vs about $10 by hand (read) | **Preprint.** Code at [PavlidisLab/gemma-curation-agents-v1.1](https://github.com/PavlidisLab/gemma-curation-agents-v1.1); a full run needs a Gemma 2.0 endpoint that was not public in Aug 2026 (read) |
| **ENCODE `eu_add_controlled_by`** | Rule-based: after `possible_controls` is set, assign control files to experiment files by biological replicate position (read) | None reported | Production tool, ENCODE-internal |
| **Qwen3-4B "geo-perturbation-grouping"** ([featherless.ai model card](https://featherless.ai/models/jsoul/geo-perturbation-grouping-qwen3-4b)) | Fine-tuned small LLM that outputs knockdown/knockout/overexpression case and control groups with sample IDs, for human review (read, excerpt) | None found | No paper found |

## Gaps and not found

- **How ChIP controls are matched** in Cistrome DB, ChIP-Atlas and ReMap 2022: not found. Their
  papers' methods (Cistrome 2017 NAR [PMC5048594](https://pmc.ncbi.nlm.nih.gov/articles/PMC5048594);
  ReMap 2022 [PMC8728178](https://pmc.ncbi.nlm.nih.gov/articles/PMC8728178)) were blocked or
  unopened. No released GSM-level table of ChIP-to-input pairs was found for GEO.
- **Same-biosample multi-assay gold outside ENCODE, HCA and 4DN:** none found. GEO gives each
  assay its own BioSample record, so nothing in GEO links them by default (inferred; not checked
  against BioSample data).
- **Expression Atlas's differential-experiment and contrast counts** and its licence; **Gemma's
  current count** (the homepage showed none); **scPerturb's dataset count** and formal schema.
- **Any LLM paper scoring contrast or pairing extraction** (reference vs test groups) on GEO: none
  found. The Gemma 2026 preprint scores factors and values, not contrasts.
- Not researched for lack of time: 4DN's data model, GTRD, the MAGE-TAB 1.1 PDF's SDRF section,
  and HCA's JSON schema field names.
