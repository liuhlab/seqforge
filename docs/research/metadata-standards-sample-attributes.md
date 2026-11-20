# Sample attributes and ontologies in four public metadata standards

Dated 2026-11-19, for [#596](https://github.com/liuhlab/seqforge/issues/596), under the map
[#595](https://github.com/liuhlab/seqforge/issues/595). **All four standards hold species, sex,
tissue, cell type, disease and development stage, but they agree on a required ontology term for
none of them. Species is the only attribute all four require. Sex is the only other attribute all
four write from a small fixed list. Each standard writes a missing value its own way, and
only ENCODE gives a treatment a dose and a duration with units. Only ENCODE has fields for a
ChIP-style target and for a control library's type.**

The question: which sample attributes do ENCODE, CELLxGENE, the Human Cell Atlas (HCA) and NCBI
BioSample require, which ontology does each use, how is a missing value written, and how do age,
dose and time carry units? This note collects what the schema documents say. It recommends nothing.

Evidence classes: **read** means read from the schema file named in [Sources](#sources) on
2026-11-19. **Unconfirmed** means no primary source was found or opened for it. "Required" means
required by the schema file itself. A curation rule that a portal applies on top of its schema is
out of scope, and none was read.

## Sources

Every row below cites one of these by its short name.

| Short name | Document | Version read |
| --- | --- | --- |
| ENCODE | JSON schema files under <https://github.com/ENCODE-DCC/encoded/tree/dev/src/encoded/schemas>: `biosample.json` (schema version 26), `human_donor.json` (13), `mouse_donor.json` (11), `donor.json`, `organism.json` (6), `biosample_type.json` (1), `treatment.json` (12), `genetic_modification.json` (12), `experiment.json` (37), `target.json` (14), `replicate.json` (9), `library.json` (20), `mixins.json` | Branch `dev`; last commit to that folder `86e3e93`, 2026-03-27 |
| CELLxGENE | <https://github.com/chanzuckerberg/single-cell-curation/blob/main/schema/7.1.0/schema.md> | Version 7.1.0, status "Approved"; last commit to the file `21898c7`, 2026-09-02 |
| HCA | JSON schema files under <https://github.com/HumanCellAtlas/metadata-schema/tree/master/json_schema>: `type/biomaterial/donor_organism.json` (18.7.0), `specimen_from_organism.json` (11.3.0), `cell_line.json` (16.0.0), `cell_suspension.json` (14.3.0), `organoid.json`, `core/biomaterial/biomaterial_core.json` (8.4.0), `type/protocol/biomaterial_collection/treatment_protocol.json` (1.0.0), `type/protocol/sequencing/library_preparation_protocol.json` (6.5.0), and the files under `module/biomaterial/` and `module/ontology/` | Branch `master`; last commit `8d4c60a`, 2026-08-08; `versions.json` dated 2026-04-02 |
| NCBI | Attribute list <https://www.ncbi.nlm.nih.gov/biosample/docs/attributes/?format=xml> (960 attributes); package list <https://www.ncbi.nlm.nih.gov/biosample/docs/packages/?format=xml> (229 packages); the packages `Human.1.0`, `Model.organism.animal.1.0`, `Invertebrate.1.0`, `Plant.1.0` and `Microbe.1.0` at `https://www.ncbi.nlm.nih.gov/biosample/docs/packages/<name>/?format=xml`; <https://www.ncbi.nlm.nih.gov/biosample/docs/organism/>; <https://www.ncbi.nlm.nih.gov/biosample/docs/submission/faq/> | Pages carry no version or date; retrieved 2026-11-19. Each package is "version 1.0" |

Limits of what was read:

- CELLxGENE 7.1.0 is the highest-numbered version in the repository's `schema/` folder. Whether the
  portal enforces 7.1.0 today is **unconfirmed**: the repository README still links version 3.0.0.
- NCBI has 229 packages. Five were read. The other 224 (pathogen, metagenome, MIxS and others) were
  not.
- HCA has about 90 schema files. The biomaterial, treatment-protocol, library-preparation and
  ontology-module files were read; the rest were listed by name only.
- ENCODE's fly, worm and manatee donor schemas were not read.

## How each standard is built

This shapes every row below, so it comes first (all read).

- **ENCODE** links objects. A biosample links to one donor, one organism, one biosample type, and
  lists of treatments and genetic modifications. An experiment links to a target. Attributes of the
  person or animal sit on the donor; attributes of the material sit on the biosample.
- **CELLxGENE** is one table with one row per cell. Every attribute is a column in that table, or
  a dataset-wide value. A column is filled for every cell, so "missing" has to be a written value.
- **HCA** links typed entities: donor organism, specimen from organism, cell line, organoid, cell
  suspension, plus protocols. An ontology field is an object with three parts: `text` (required,
  free text), `ontology` (the term identifier, optional) and `ontology_label` (optional). So HCA
  requires the words, not the term.
- **NCBI BioSample** is a flat list of name and value pairs. A package names which attributes are
  mandatory. Nearly every value is free text. The attribute description sometimes suggests an
  ontology, but no description read here says a term is enforced.

## Attribute by attribute

Identifier forms are given as the schema writes them. `CURIE` means the prefixed form
`Ontology:Identifier`, for example `UBERON:0002421`.

### Species

| Standard | Field | Required | Ontology and form | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `biosample.organism`, a link to an Organism object with `name`, `scientific_name`, `taxon_id` | Yes | NCBI Taxonomy number without a prefix, for example `10090` | ENCODE `biosample.json`, `organism.json` |
| CELLxGENE | `organism_ontology_term_id`, one value for the whole dataset | Yes | NCBITaxon CURIE from a closed list of organisms, for example `NCBITaxon:9606` | CELLxGENE, section `organism_ontology_term_id` |
| HCA | `biomaterial_core.ncbi_taxon_id` (array) on every biomaterial; also `genus_species` | `ncbi_taxon_id` yes; `genus_species` optional | Taxon number without a prefix, example `9606`; `genus_species` takes an NCBITaxon CURIE, example `NCBITaxon:9606` | HCA `biomaterial_core.json`, `species_ontology.json` |
| NCBI | Organism, a record field outside the attribute list | Yes: "requires each record to have a single organism, with a valid taxonomy name to the species level" | Scientific name, checked against NCBI Taxonomy | NCBI `docs/organism/` |

### Sex

| Standard | Field | Required | Values | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `human_donor.sex`; `biosample.model_organism_sex` for other species | No. `human_donor.sex` defaults to `unknown` | Plain words. Human: `female`, `male`, `mixed`, `unknown`. Model organism: the same plus `hermaphrodite` | ENCODE `human_donor.json`, `biosample.json` |
| CELLxGENE | `sex_ontology_term_id` | Yes | `PATO:0000383` female, `PATO:0000384` male, `PATO:0001340` hermaphrodite; or `unknown`; `na` for a cell line | CELLxGENE, section `sex_ontology_term_id` |
| HCA | `donor_organism.sex` | Yes | Plain words: `female`, `male`, `mixed`, `unknown` | HCA `donor_organism.json` |
| NCBI | `sex` | Mandatory in `Human.1.0` and `Model.organism.animal.1.0`; optional in `Invertebrate.1.0` and `Plant.1.0` | Plain words from a fixed list: `male`, `female`, `pooled male and female`, `neuter`, `hermaphrodite`, `intersex`, `not determined`, plus the missing-value words | NCBI attribute list and packages |

### Age

| Standard | Field | Required | How the value and the unit are written | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `human_donor.age` with `age_units`; `biosample.model_organism_age` with `model_organism_age_units`; `biosample.sample_collection_age` with its units | No. When an age other than `unknown` is given, `age_units` and `life_stage` become required | Two fields. The value is a string: a number, a range such as `45-65`, `unknown`, or `90 or above`. The unit is a plain word from a list: `day`, `week`, `month`, `year` for humans; model organisms add `minute`, `hour` and `stage`. For embryonic and fetal samples the age is gestational time | ENCODE `human_donor.json`, `biosample.json` |
| CELLxGENE | None. Age is carried inside `development_stage_ontology_term_id` | Not applicable | No number and no unit. The stage term is the age | CELLxGENE, sections `obs` and `development_stage_ontology_term_id` |
| HCA | `donor_organism.organism_age` with `organism_age_unit`; also `gestational_age` with its unit; `organoid.age` with `age_unit` | No | Two fields. The value is a string, a number or a range, examples `20` and `45-65`. The unit is an ontology object, a Units Ontology (UO) time unit, examples `UO:0000010` and `UO:0000034` | HCA `donor_organism.json`, `time_unit_ontology.json` |
| NCBI | `age` | Mandatory in `Human.1.0`. In `Model.organism.animal.1.0` and `Plant.1.0`, one of `age` or `dev_stage` is mandatory. Optional in `Invertebrate.1.0` | One field, format `{float} {unit}`: the unit is a free word inside the value | NCBI attribute list and packages |

### Tissue, cell type and cell line

| Standard | Field | Required | Ontology and form | Source |
| --- | --- | --- | --- | --- |
| ENCODE | One slot for all three: `biosample.biosample_ontology`, a link to a BiosampleType with `classification`, `term_id`, `term_name` | Yes, on the biosample and on the experiment | `classification` is one of `tissue`, `whole organisms`, `organoid`, `primary cell`, `in vitro differentiated cells`, `cell line`, `cell-free sample`, `cloning host`, `technical sample`. It fixes the ontology: UBERON for tissue, whole organisms and organoid; CL for primary cell and in vitro differentiated cells; CLO or EFO for cell line. All are CURIEs. `NTR:` marks a term ENCODE has requested but the ontology does not have yet. A biosample is a tissue or a cell type or a cell line, never two of them | ENCODE `biosample_type.json`, `biosample.json`, `experiment.json` |
| CELLxGENE, tissue | `tissue_ontology_term_id` with `tissue_type` | Yes, both | `tissue_type` is one of `tissue`, `organoid`, `primary cell culture`, `cell line`. For tissue and organoid: a UBERON CURIE, or a worm (`WBbt`), zebrafish (`ZFA`) or fly (`FBbt`) anatomy term for those species. For primary cell culture: a cell type term | CELLxGENE, sections `tissue_ontology_term_id`, `tissue_type` |
| CELLxGENE, cell type | `cell_type_ontology_term_id` | Yes, unless the dataset is marked as not yet analysed, when it must be absent | CL CURIE, or `WBbt`, `ZFA`, `FBbt` cell terms for those species; `unknown` when no term fits; three top-level CL terms are banned | CELLxGENE, section `cell_type_ontology_term_id` |
| CELLxGENE, cell line | No field of its own. When `tissue_type` is `cell line`, `tissue_ontology_term_id` holds the cell line | Then required | Cellosaurus identifier. The schema lists the prefix as `CVCL_` and says Cellosaurus is the one exception to the CURIE form | CELLxGENE, sections "Integration Metadata", `tissue_ontology_term_id` |
| HCA, tissue | `specimen_from_organism.organ`; `organ_parts` | `organ` yes; `organ_parts` optional | UBERON CURIE, example `UBERON:0000948`. Only the `text` part is required | HCA `specimen_from_organism.json`, `organ_ontology.json` |
| HCA, cell type | `cell_suspension.selected_cell_types` (the cell types selected for, not those found by analysis); `cell_line.cell_type` | No | CL CURIE, example `CL:0000746` | HCA `cell_suspension.json`, `cell_line.json`, `cell_type_ontology.json` |
| HCA, cell line | A `cell_line` entity with `type` and `model_organ` | Both required on a cell line | `type` is a plain word: `primary`, `immortalized`, `stem cell`, `stem cell-derived`, `induced pluripotent`, `synthetic`. `model_organ` is a UBERON term. The line's name goes in the free-text `biomaterial_name`. No field for a cell line identifier was found | HCA `cell_line.json` |
| NCBI | `tissue`, `cell_type`, `cell_line`, `cell_subtype` | `tissue` is mandatory in all four organism packages read. The other three are optional where a package lists them; `Invertebrate.1.0` lists none of them | Free text. No ontology is named for any of them | NCBI attribute list and packages |

### Disease

| Standard | Field | Required | Ontology and form | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `biosample.disease_term_id` (list); `health_status` on the donor is free text | No | Disease Ontology or Human Phenotype Ontology CURIE: `DOID:` or `HP:` | ENCODE `biosample.json`, `human_donor.json` |
| CELLxGENE | `disease_ontology_term_id` | Yes | `PATO:0000461` for normal or healthy, or one or more MONDO CURIEs joined by ` \|\| ` in sorted order | CELLxGENE, section `disease_ontology_term_id` |
| HCA | `diseases` on the donor and on the specimen; `disease` on a cell line | No. The guideline says to enter `normal` when no disease is known | MONDO or HP CURIE, or `PATO:0000461` for normal; examples `MONDO:0005148`, `HP:0001397` | HCA `donor_organism.json`, `specimen_from_organism.json`, `disease_ontology.json` |
| NCBI | `disease`; also `disease_stage`, `health_state` | Optional in `Human.1.0`, `Model.organism.animal.1.0`, `Plant.1.0` | Free text. The description says human terms "should be chosen from DO (Disease Ontology)" and non-human values are free text | NCBI attribute list and packages |

### Development stage

| Standard | Field | Required | Ontology and form | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `human_donor.life_stage`; `biosample.mouse_life_stage`, `fly_life_stage`, `worm_life_stage` | No, except that `life_stage` is required once a human age is given | Plain words, no ontology. Human: `embryonic`, `fetal`, `newborn`, `child`, `adult`, `unknown`. Mouse: `embryonic`, `postnatal`, `adult`, `unknown`. Fly and worm have their own stage lists | ENCODE `human_donor.json`, `biosample.json` |
| CELLxGENE | `development_stage_ontology_term_id` | Yes | By species: `HsapDv` for human, `MmusDv` for mouse, `WBls` for worm, `ZFS` for zebrafish, `FBdv` for fly, and a UBERON life cycle stage for the rest. `unknown` when unavailable; `na` for a cell line | CELLxGENE, section `development_stage_ontology_term_id` |
| HCA | `donor_organism.development_stage` | Yes | A term under `EFO:0000399`, `HsapDv:0000000` or `UBERON:0000105`; examples `HsapDv:0000087`, `EFO:0002588` | HCA `donor_organism.json`, `development_stage_ontology.json` |
| NCBI | `dev_stage` | Optional in `Human.1.0` and `Invertebrate.1.0`; one of `age` or `dev_stage` is mandatory in `Model.organism.animal.1.0` and `Plant.1.0` | Free text | NCBI attribute list and packages |

### Genotype and genetic change

| Standard | Field | Required | Form | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `genotype`, `strain_name`, `strain_background` on a non-human donor; `genetic_modifications`, a list of linked objects, on a donor or a biosample | No | `genotype` is free text "according to accepted nomenclature conventions". A genetic modification has plain-word fields: `category` (for example `deletion`, `insertion`, `knockout`, `CRISPRi`), `purpose`, `method`, `zygosity`, and the modified site given by gene, target, coordinates or sequence | ENCODE `mixins.json`, `mouse_donor.json`, `genetic_modification.json` |
| CELLxGENE | No genotype field. `genetic_perturbation_id` and `genetic_perturbation_strategy`, added in 7.1.0 | Only when the dataset declares genetic perturbations | The strategy is a plain word: `control`, `CRISPR activation screen`, `CRISPR interference screen`, `CRISPR knockout mutant`, `CRISPR knockout screen`, or `no perturbations`. Human, mouse and zebrafish only | CELLxGENE, sections `genetic_perturbation_id`, `genetic_perturbation_strategy` |
| HCA | `biomaterial_core.genotype`; `mouse_specific.strain` | No | `genotype` is free text, example `DRB1 0401 protective allele`. `strain` is an EFO term, example `EFO:0004472` | HCA `biomaterial_core.json`, `strain_ontology.json` |
| NCBI | `genotype`; `strain`, `breed`, `cultivar`, `ecotype`, `isolate` | `genotype` is optional in the model organism, plant and microbe packages and absent from `Human.1.0`. One of the strain-like names is mandatory in each non-human package | Free text | NCBI attribute list and packages |

### Treatment, dose and time

| Standard | Field | Required | Ontology, dose and time | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `biosample.treatments`, a list of Treatment objects; `post_treatment_time` on the biosample | The list is optional. On a Treatment, `treatment_term_name` and `treatment_type` are required and `treatment_term_id` is optional | `treatment_term_id` is a CURIE from ChEBI (`CHEBI:`), UniProt (`UniProtKB:`), NCBI Taxonomy (`Taxon:`), CL, the Experimental Conditions Ontology (`XCO:`) or `NTR:`. `treatment_type` is a plain word: `antibody`, `chemical`, `exposure`, `infection`, `injection`, `irradiation`, `protein`, `transplantation`. Dose is `amount` (a number) with `amount_units` from a list (`M`, `mM`, `μM`, `nM`, `pM`, `mg`, `μg`, `mg/mL`, `μg/mL`, `ng/mL`, `μg/kg`, `U/mL`, `percent`, `pfu`, `rad`, `units`). Time is `duration` with `duration_units` (`second`, `minute`, `hour`, `day`). Temperature has its own pair. Each number requires its unit and each unit its number. `post_treatment_time` has its own unit field and requires a non-empty treatment list | ENCODE `treatment.json`, `mixins.json`, `biosample.json` |
| CELLxGENE | `experimental_condition_ontology_term_id`, added in 7.1.0 | Present only when at least one cell has a condition; then every cell has a value | One or more identifiers joined by ` \|\| `. A chemical is a ChEBI CURIE; a protein is `uniprot:P05112`; an antibody is the protein's identifier with `anti-` in front, `anti-uniprot:Q99467`; diet is `EFO:0002755` or a term under it, or `EFO:0002756` for fasting; temperature is `EFO:0001702`. `na` means no condition. **No dose and no time**: the words "dose", "concentration" and "duration" do not occur in the document | CELLxGENE, section `experimental_condition_ontology_term_id` |
| HCA | A `treatment_protocol` entity; `growth_conditions.drug_treatment`; `medical_history.medication`; `biomaterial_core.timecourse` | No. On a treatment protocol, `method` is required | `method` is an OBI or EFO term under `OBI:0000070`, example `EFO:0030037`. `target_pathway` is a GO term. `drug_treatment` is free text holding "the drug name and concentration with units", example `100 ug/mL ampicillin`. `medication` is an NCI Thesaurus term, example `NCIT:C72809`. Time is `timecourse`: a `value` string (number or range), a `unit` that is a UO term, and a free-text `relevance` saying what the time is measured from | HCA `treatment_protocol.json`, `growth_conditions.json`, `medical_history.json`, `timecourse.json` |
| NCBI | `treatment`; also `dose`, `timepoint`, `growth_protocol`, `perturbation`, `chem_administration` | `treatment` is optional in the packages read | Free text. `treatment`, `dose` and `growth_protocol` have no description and no format. `perturbation` has the format `{text};{interval}` and `chem_administration` `{term}; {timestamp}`, with ChEBI suggested; both come from the environmental (MIxS) attributes | NCBI attribute list and packages |

### Donor identity

| Standard | Field | Required | Form | Source |
| --- | --- | --- | --- | --- |
| ENCODE | `biosample.donor`, a link to a Donor object | No, by the schema's required list | ENCODE accession of the form `ENCDO` plus three digits and three letters; `external_ids` holds identifiers from other sources | ENCODE `biosample.json`, `donor.json` |
| CELLxGENE | `donor_id` | Yes | Free text naming one individual. `pooled` is recommended for a sample of several individuals; `unknown` only when it is not known which cells share an individual; `na` for a cell line | CELLxGENE, section `donor_id` |
| HCA | `biomaterial_core.biomaterial_id` on the donor organism entity | Yes | Free text | HCA `biomaterial_core.json`, `donor_organism.json` |
| NCBI | `isolate`: "identification or description of the specific individual from which this sample was obtained" | Mandatory in `Human.1.0`; in the other packages it is one of several names of which one is mandatory | Free text | NCBI attribute list and packages |

### Others that at least two share

| Attribute | ENCODE | CELLxGENE | HCA | NCBI |
| --- | --- | --- | --- | --- |
| Ethnicity or ancestry | `human_donor.ethnicity`, optional, plain words from a list that the schema says uses the Human Ancestry Ontology (HANCESTRO) "as a template" | `self_reported_ethnicity_ontology_term_id`, required; HANCESTRO or AfPO terms joined by ` \|\| `; `unknown`; `na` for non-human and for a cell line | `human_specific.ethnicity`, optional, HANCESTRO CURIE, example `HANCESTRO:0005` | `ethnicity` and `race`, optional in `Human.1.0`, free text |
| Assay or library method | `experiment.assay_term_name`, required, a plain name from a list the schema says comes from the Ontology for Biomedical Investigations (OBI) | `assay_ontology_term_id`, required, EFO (requirements not read in detail) | `library_preparation_protocol.library_construction_method`, required, EFO term, example `EFO:0008931` | None in the attribute list; it lives in the SRA record (**unconfirmed**, SRA documents not read) |
| What kind of material | `classification` on the biosample type, required | `tissue_type`, required | The entity type itself (specimen, cell line, organoid) | `sample_type`, optional except in `Microbe.1.0`, free text |
| Collection date | `date_obtained`, optional, `YYYY-MM-DD` | None | `collection_time`, optional, date-time, partial dates allowed | `collection_date`, mandatory in all five packages read |
| Culture passage | `passage_number`, optional integer | None | `growth_conditions.passage_number`, optional integer | `passage_history` in `Microbe.1.0` only |
| Strain | `strain_name`, `strain_background`, optional free text | None | `mouse_specific.strain`, optional, EFO term | `strain`, free text |
| Time since death | `PMI` with `PMI_units`, optional | None | The `death` module has time of death and other fields, optional | None read |

The four standards' sources for this table are the same files cited in the tables above; the HCA
`death` module is `module/biomaterial/death.json` and the ENCODE fields are in `biosample.json`.

## How a missing, unknown or not-applicable value is written

| Standard | Rule | Source |
| --- | --- | --- |
| ENCODE | An optional field is left out. Where "not known" must be said, the word `unknown` is one of the allowed values: in `sex`, `life_stage`, `mouse_life_stage`, and in the age and time patterns. There is no "not applicable" value | ENCODE `human_donor.json`, `biosample.json` |
| CELLxGENE | Every required column is filled for every cell, with two reserved words. `unknown` means the value is not available. `na` means the attribute does not apply: sex, development stage, donor and ethnicity are `na` for a cell line, ethnicity is `na` for a non-human, and the experimental condition is `na` for an untreated cell. Healthy is a real term, `PATO:0000461`, not a missing value | CELLxGENE, the `obs` sections named above |
| HCA | An optional field is left out. `unknown` is an allowed word in `sex` and `is_living`; `is_living` also allows `not applicable`. No disease is the word `normal`. There is no schema-wide missing value | HCA `donor_organism.json` |
| NCBI | A mandatory field must be filled: "If information is unavailable for any mandatory field, please enter 'not collected', 'not applicable' or 'missing' as appropriate." An optional field is left empty. The fixed list for `sex` also holds `not provided`, `restricted access` and `not determined` | NCBI package `Human.1.0` template header; submission FAQ; attribute list |

## ChIP-style target and control library type

| Standard | Target | Control type | Source |
| --- | --- | --- | --- |
| ENCODE | `experiment.target`, a link to a Target object; optional in the schema. A Target requires `label` and `investigated_as`. `label` is the common name with any modification, for example `H3K4me3`, `eGFP-E2F1`, `POLR2AphosphoS2`. `investigated_as` is a list of plain words: `transcription factor`, `histone`, `broad histone mark`, `narrow histone mark`, `chromatin remodeler`, `cofactor`, `cohesin`, `RNA binding protein`, `RNA polymerase complex`, `DNA repair`, `DNA replication`, `tag`, `synthetic tag`, `other context`. A Target has either `genes` (links to Gene objects) or a `target_organism`, never both; histone marks add a `modifications` list (kind and amino-acid position); `dbxrefs` takes `UniProtKB:` identifiers. The antibody is a separate link on the replicate | `experiment.control_type`, optional, one plain word: `control`, `IgG`, `input library`, `mock input`, `no protein target`, `non-specific target`, `wild type`. The description says it is "the specific type of control if the experiment is a control". The experiment that uses a control names it in `possible_controls`, a list of links | ENCODE `experiment.json`, `target.json`, `replicate.json` |
| CELLxGENE | None. It holds single-cell datasets, and the only place an antibody appears is as an experimental condition (`anti-uniprot:`) | None. `control` appears only as a genetic perturbation strategy | CELLxGENE, all `obs` and `uns` section titles read |
| HCA | None found. `library_preparation_protocol` has no target, antibody or control field | None found | HCA `library_preparation_protocol.json`; the other protocol files were listed by name, not opened |
| NCBI | None found among the 960 attributes by name. `neg_cont_type` and `pos_cont_type` exist and come from the environmental attributes; they were not read in detail | None found | NCBI attribute list, searched by name for target, antibody, control and ChIP |

Where GEO or SRA records hold a ChIP target or a control type is **unconfirmed**: neither GEO's nor
SRA's submission documents were in scope.

## What all four agree on

"Present" means the standard has a field for it. "Required" is by the schema.

| Attribute | Present in all four | Required by | Common ground in how it is written |
| --- | --- | --- | --- |
| Species | Yes | All four | An NCBI Taxonomy entry. ENCODE and HCA write the bare number, CELLxGENE the CURIE `NCBITaxon:9606`, NCBI the scientific name |
| Sex | Yes | CELLxGENE, HCA, NCBI (human and model organism packages). Optional in ENCODE | A small fixed list in all four. `female` and `male` are in every list; only CELLxGENE writes them as terms (PATO) |
| Tissue | Yes | All four, with a condition: ENCODE and CELLxGENE require one slot that holds a tissue or a cell line; HCA requires `organ` on a specimen; NCBI requires `tissue` in the four organism packages read | UBERON CURIEs in ENCODE, CELLxGENE and HCA. Free text in NCBI |
| Cell type | Yes | CELLxGENE. In ENCODE only when the biosample is a primary cell or differentiated cells. Optional in HCA and NCBI | Cell Ontology (CL) CURIEs in ENCODE, CELLxGENE and HCA. Free text in NCBI |
| Cell line | Yes | None as a field of its own | Nothing shared: CLO or EFO in ENCODE, Cellosaurus in CELLxGENE, a name in HCA and NCBI |
| Disease | Yes | CELLxGENE only | Nothing shared across all four. CELLxGENE and HCA share MONDO and `PATO:0000461` for healthy; ENCODE and HCA share HP |
| Development stage | Yes | CELLxGENE and HCA. Conditional in ENCODE and NCBI | Nothing shared across all four. CELLxGENE and HCA share HsapDv for human |
| Donor identity | Yes | CELLxGENE, HCA, NCBI `Human.1.0`. Optional in ENCODE | A free-text identifier unique within the submission (an accession in ENCODE) |
| Treatment | Yes | None | ChEBI for a chemical in ENCODE and CELLxGENE; UniProt for a protein in both, with different prefixes (`UniProtKB:` and `uniprot:`) |
| Age | Three of four; CELLxGENE folds it into development stage | NCBI `Human.1.0`; conditional in NCBI model organism and plant packages | A number or a range, with a unit. ENCODE and HCA use a separate unit field; NCBI puts the unit in the value |
| Genotype | Three of four as free text (ENCODE, HCA, NCBI); CELLxGENE has none | None | Free text |
| Ethnicity | Yes, for human | CELLxGENE only | HANCESTRO terms in CELLxGENE and HCA; ENCODE's word list is modelled on it |

## Where they disagree

- **Term or free text.** CELLxGENE requires a term identifier for every attribute it holds. ENCODE
  requires one only for the biosample type. HCA requires only the free-text half of each ontology
  field. NCBI requires no term anywhere in the files read.
- **Disease ontology.** ENCODE uses DOID or HP; CELLxGENE uses MONDO only; HCA uses MONDO or HP; the
  NCBI description points to DO. Healthy is `PATO:0000461` in CELLxGENE and HCA, the absence of a
  value in ENCODE, and free text in NCBI.
- **Cell line.** ENCODE uses CLO or EFO terms and makes a cell line exclusive with tissue and cell
  type. CELLxGENE uses Cellosaurus and puts it in the tissue column. HCA and NCBI use a name.
- **Tissue, cell type and cell line as one slot or three.** ENCODE has one slot with a
  classification. CELLxGENE has a tissue slot with a type, plus a separate cell type. HCA and NCBI
  have separate fields.
- **Age.** ENCODE and HCA use value plus unit in two fields; NCBI uses one string; CELLxGENE has no
  age and uses a stage term. ENCODE writes the unit as a word, HCA as a UO term. ENCODE caps
  human age at `90 or above`.
- **Development stage.** Plain words per species in ENCODE; species-specific stage ontologies in
  CELLxGENE; EFO, HsapDv or UBERON in HCA; free text in NCBI.
- **Sex.** The lists differ past `female` and `male`: `mixed` (ENCODE, HCA), `hermaphrodite` (ENCODE
  model organisms, CELLxGENE, NCBI), `pooled male and female`, `neuter`, `intersex` (NCBI only).
- **Missing values.** `unknown` (ENCODE, HCA, CELLxGENE); `na` for not applicable (CELLxGENE only);
  `not collected`, `not applicable`, `missing` (NCBI). ENCODE and HCA leave optional fields out;
  CELLxGENE cannot.
- **Dose and time of a treatment.** Structured numbers with unit lists in ENCODE; a UO-unit time
  course and a free-text drug string in HCA; absent in CELLxGENE; free text in NCBI.
- **Species identifier form.** Bare number, CURIE or scientific name, as in the table above.
- **Several values in one field.** CELLxGENE joins terms with ` || ` in sorted order; ENCODE and HCA
  use lists; NCBI has no rule in the files read.
- **Target and control.** Only ENCODE holds them.

## Unconfirmed

- Whether the CELLxGENE portal enforces schema 7.1.0 today.
- Whether ENCODE's portal demands more than its schema files do, for example a donor on every
  biosample or a target on every ChIP-seq experiment. The schema files make both optional.
- The 224 NCBI packages not read, and the meaning of `neg_cont_type` and `pos_cont_type`.
- Whether any HCA schema file not opened holds a target, antibody or control field. None of the
  roughly 90 file names suggests one.
- The requirements of CELLxGENE's `assay_ontology_term_id`, read only as far as its name and that
  it is required.
- How GEO and SRA records themselves hold a target or a control type.
- One likely misprint in the CELLxGENE document was left as found: the organism table gives
  *Caenorhabditis elegans* as `NCBITaxon:6293`, while every other section writes `NCBITaxon:6239`.
