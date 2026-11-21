# Looking up ontology terms for the reading's vocabularies

Dated 2026-11-21, for [#627](https://github.com/liuhlab/seqforge/issues/627). The choice is
[#632](https://github.com/liuhlab/seqforge/issues/632). **Every vocabulary the reading names except
Cellosaurus and UniProt can be searched by text, synonyms included, in two maintained ways: the
EBI's Ontology Lookup Service (OLS4) on the web, and ready-made SQLite files read by the Ontology
Access Kit (OAK) on a machine with no internet. Cellosaurus and UniProt each have their own search
service and their own download files. liulab-data holds no ontology file today and has no issue
that plans one. Of the gaps, fly stages are covered (FBdv), rat stages exist only as a small
unmaintained file, assay has EFO and OBI, a target has no single vocabulary, and a vector has no
open vocabulary at all.**

The question: how can code find candidate ontology terms for a sample's label text, such as
"hippocampus", from a maintained package, in a way that also works on a compute node with no
internet? A model then picks among the candidates.

Evidence class: each fact is marked. **Measured** means a small call made on 2026-11-21: an OLS4 or
Cellosaurus query, or an HTTP header read for a file size. No file larger than 50 MB was
downloaded. **Read** means taken from the named document or source file. **Inferred** says so.

## The vocabularies and what serves each

The reading's label table names twelve vocabularies (`docs/formats/reading.md`, "Labels"). Each row
below says which way of searching covers it. "In OLS4" is measured from the OLS4 ontology list. "In
SemSQL" is measured by a HEAD request to the ready-made file. SemSQL sizes are compressed; OBO
sizes are uncompressed unless marked `.gz`.

| Label | Vocabulary | OLS4 terms | SemSQL file (`.db.gz`) | Plain OBO file |
| --- | --- | --- | --- | --- |
| species | NCBI Taxonomy (`ncbitaxon`) | 3,025,030 | 2.37 GB | 729 MB (`.obo`), 53 MB (`.obo.gz`) |
| tissue | UBERON | 26,739 | 195 MB | 22.5 MB (12.2 MB "basic") |
| tissue, worm | WBbt | 7,573 | 8.1 MB | 1.6 MB |
| cell type | Cell Ontology (`cl`) | 21,322 | 104 MB | 4.3 MB ("basic") |
| cell line | Cellosaurus | **not in OLS4** | 98.7 MB (built May 2026) | 119 MB (`cellosaurus.obo`) |
| disease | MONDO | 63,573 | 243 MB | 53 MB |
| healthy | PATO | 8,713 | 28.5 MB | 0.8 MB |
| stage, human | HsapDv | 260 | 0.4 MB | 0.14 MB |
| stage, mouse | MmusDv | 178 | 0.3 MB | 0.10 MB |
| stage, worm | WBls | 796 | 7.4 MB | 0.4 MB |
| treatment, chemical | ChEBI | 238,259 | 841 MB | 274 MB (55 MB "lite") |
| treatment, protein | UniProt | **not in OLS4** | 415 MB | Swiss-Prot flat file 700 MB gz |

Notes on the table:

- The SemSQL files are much larger than the OBO files because each holds precomputed ancestor
  tables and imported terms as well as labels (read: the semantic-sql README, "entailed edges").
- OLS4 counts include terms imported from other ontologies, so they are an upper bound on each
  vocabulary's own terms (inferred from the counts, not documented).
- NCBI's own taxonomy dump is 80 MB (`taxdump.tar.gz`, measured). Its Last-Modified date was the
  day of measurement, so it is rebuilt at least that often (measured).
- ChEBI's "lite" OBO file leaves out synonyms: no term in its first 200 KB carried a `synonym` line
  (measured, by a range request). It is no use for a synonym search.

## Way 1: the OLS4 web API

OLS4 is the EBI's search service over about 300 ontologies (294 listed, measured).

- **Coverage.** Ten of the twelve: everything but Cellosaurus and UniProt (measured). It also holds
  EFO, OBI, the Sequence Ontology (SO), the Protein Ontology (PR), the Cell Line Ontology (CLO,
  40,851 terms), FBdv for the fly, and ZFS for zebrafish (measured).
- **Search.** `GET /ols4/api/search?q=…&ontology=uberon` searches labels and synonyms. `exact=true`
  keeps only whole-string matches. `queryFields=label,synonym` limits the fields. `rows` sets the
  number of hits (read: seqforge already calls it in `src/seqforge/io/efo.py`; the parameters below
  were each measured).
- **How it behaves.** `q=hippocampus&ontology=uberon` returned 56 hits, and the first three were CA1,
  CA2 and CA3 fields, not the hippocampus. With `exact=true` it returned three: "Ammon's horn"
  (UBERON:0001954), "hippocampal formation" (UBERON:0002421) and "archicortex" (UBERON:0002961). All
  three carry "hippocampus" as a synonym. So the plain search ranks partial matches above whole
  matches, and one everyday word can be a synonym of several terms. `q=L4 larva&ontology=wbls`
  with `exact=true` gave the one term "L4 larva Ce" (WBls:0000038). `q=HeLa&ontology=efo,clo`
  gave EFO:0001185 first. `q=dexamethasone&ontology=chebi&exact=true` gave CHEBI:41879 alone (all
  measured).
- **Updates.** Each ontology shows a `loaded` time. On 2026-11-21 every one checked had been loaded
  between 2026-11-15 and 2026-11-19, and UBERON was at its 2026-11-05 release (measured). So OLS4
  reloads often, and an answer can change between two runs. The API reports the version it holds.
- **Rate limits.** No number is published. The EMBL-EBI terms of use say a user whose use
  "prevents, or is likely to prevent" others' use may be blocked, after the EBI first tries to make
  contact ([EMBL-EBI terms of use](https://www.ebi.ac.uk/about/terms-of-use/), read). A figure of 5
  requests per second circulates in third-party guides; no EBI page states it.
- **Offline.** No, as a web service. OLS4 is open source and can be run locally. Its README says
  it "is intended to be run as a containerised application": Docker containers with a PostgreSQL
  database and a separate data-load step, with a non-container route called "best effort"
  ([OLS4 README](https://github.com/EBISPOT/ols4), read). Its last release was v4.0.7 on
  2026-11-20 (read).

## Way 2: OAK with the ready-made SemSQL files

The Ontology Access Kit (OAK, Python package `oaklib`) reads ontologies through one interface. Its
SQLite back end reads files built by the semantic-sql project.

- **Coverage.** Ready-made files exist for every OBO Foundry ontology and a list of others. That list
  includes Cellosaurus, EFO, NCIt and UniProt (read:
  [`ontologies.yaml`](https://github.com/INCATools/semantic-sql/blob/main/src/semsql/builder/registry/ontologies.yaml)).
  All twelve of the reading's vocabularies have a file (measured, table above). The UniProt file is
  built from a third-party OWL conversion (`w3id.org/biopragmatics/resources/uniprot`, read), whose
  scope this note did not check.
- **Search.** OAK's default search looks at the label and every synonym type (read:
  `oaklib/datamodels/search.py`, where `DEFAULT_SEARCH_PROPERTIES` is label plus alias). A prefix
  picks the mode: `t=` exact, `t~` contains, `t^` starts with, `t/` regular expression (read:
  [OAK expression language](https://incatools.github.io/ontology-access-kit/howtos/use-oak-expression-language.html)).
  The same search runs from Python (`get_adapter("sqlite:obo:uberon")`) or the shell (`runoak`).
- **Size.** From 0.3 MB (MmusDv) to 841 MB (ChEBI) and 2.37 GB (NCBI Taxonomy), compressed
  (measured). The ten without NCBI Taxonomy and UniProt sum to about 1.5 GB compressed (measured,
  summed). The files are unpacked before use, so disk use is larger; this note did not measure by
  how much.
- **Updates.** The README says the files are "created weekly"
  ([semantic-sql README](https://github.com/INCATools/semantic-sql), read). The files served on
  2026-11-21 were last modified on 2026-09-26, and the Cellosaurus file on 2026-05-11 (measured). So
  the files can lag OLS4 by weeks to months.
- **How OAK caches.** `sqlite:obo:uberon` downloads `uberon.db.gz` into a pystow folder
  (`~/.data/oaklib` by default) and re-downloads it when the copy is older than the cache policy.
  The default policy is one month (read: `oaklib/utilities/caching.py`, `FileCache`, default
  `"1m"`). The download address can be changed with the `OAKLIB_SEMSQL_SQLITE_URL_BASE` environment
  variable (read: `oaklib/constants.py`).
- **Offline.** Yes, with one care. A node with no internet can read a copied file by its path
  (`sqlite:/path/to/uberon.db`), which bypasses the cache. Through `sqlite:obo:…` it would try to
  re-download once the copy is a month old (inferred from the cache code above).
- **Maintenance.** OAK's last release was v0.7.5 on 2026-11-17 (read).

## Way 3: a local OBO reader such as pronto

pronto is a Python library that reads OBO and OWL files into memory.

- **Coverage.** Any vocabulary that ships an OBO or OWL file: the ten in OLS4, plus Cellosaurus,
  which ships an OBO file (measured, table above). UniProt ships no OBO file.
- **Search.** None built in. A term's label and synonyms are attributes, so code must build its own
  index over them. This is new code, which the lab rule prefers to avoid.
- **Size.** The plain OBO files are the smallest form. The nine OLS4 vocabularies other than NCBI
  Taxonomy sum to about 360 MB, of which ChEBI is 274 MB (measured, summed, with the "basic" CL).
  ChEBI's "lite" file would cut that to about 140 MB, but it has no synonyms. NCBI Taxonomy's OBO
  is 729 MB unpacked, large to load whole for a species list.
- **Updates.** Each ontology's own release, fetched from its permanent `purl.obolibrary.org`
  address (measured: UBERON 2026-11-05, MONDO 2026-11-03, CL 2026-11-16).
- **Offline.** Yes, once the files are copied.
- **Maintenance.** pronto v2.7.4 was released on 2026-10-19 (read:
  [pronto releases](https://github.com/althonos/pronto/releases)).

## Way 4: Cellosaurus's own service and files

- **Search.** `GET https://api.cellosaurus.org/search/cell-line?q=…` takes a subset of the Solr query
  language and returns JSON, TSV or XML ([Cellosaurus API](https://api.cellosaurus.org/), read).
  `q=HeLa` returned CVCL_0030 first, with its synonyms "HELA", "He-La", "HeLa-CCL2" and others
  (measured).
- **Size.** `cellosaurus.obo` 119 MB, `cellosaurus.txt` 122 MB, `cellosaurus.xml` 673 MB, all
  uncompressed (measured, from the download folder).
- **Updates.** Release 57 is from 22 October 2026, and release 56 from June 2026, so about every
  four months. It holds 169,026
  cell lines and 128,560 synonyms (read: `cellosaurus_relnotes.txt`). The API reports its release at
  `/release-info` (measured).
- **Rate limits.** The API page states none (read).
- **Offline.** Yes, through the OBO or text file, read by pronto, by OAK's SemSQL Cellosaurus file,
  or by new code.
- **Note.** OLS4 holds the Cell Line Ontology (CLO) and EFO's cell lines, not Cellosaurus (measured).

## Way 5: UniProt's own service and files

- **Search.** `GET https://rest.uniprot.org/uniprotkb/search?query=…` returned TNF (P01375) for
  `gene_exact:TNF AND organism_id:9606 AND reviewed:true`, with its alternative names such as
  "TNF-alpha" (measured). The service reports its release in a header: `2026_04`, dated
  4 November 2026 (measured). No rate limit is published on the pages found; third-party guides
  disagree.
- **Size.** The reviewed set (Swiss-Prot) is 94 MB as FASTA and 700 MB as the full flat file,
  compressed. The unreviewed set is 40 GB as FASTA (measured).
- **Inferred:** a treatment protein is usually a named human or mouse protein, such as a cytokine, so
  the reviewed set covers it. The Protein Ontology (PR), which is in OLS4 and SemSQL, is another
  way to name a protein.

## Way 6: grounding packages built on these files

Two maintained packages turn a text string into an ontology id and work offline once their files are
cached.

- **Gilda** ([gyorilab/gilda](https://github.com/gyorilab/gilda), read) ships one resource file of
  about 2 million names from HGNC, UniProt, FamPlex, ChEBI, GO, DOID, EFO, HP, MeSH and Adeft. It
  does not include UBERON, CL, MONDO or the stage lists by default, but it can build a custom
  grounder from other vocabularies. It downloads into `~/.data/gilda/<version>` and has an optional
  SQLite back end to save memory.
- **PyOBO** ([biopragmatics/pyobo](https://github.com/biopragmatics/pyobo), read) offers
  `pyobo.ground("chebi", "Fusilade II")` for any OBO ontology, and has its own loaders for
  non-OBO sources, among them UniProt, NCBI resources and FlyBase. Its README says it downloads
  and caches every resource on first use. Last release v0.12.16, 2026-02-04 (read).

## Comparison

| | OLS4 web | OAK + SemSQL | pronto + OBO | Cellosaurus | UniProt |
| --- | --- | --- | --- | --- | --- |
| Of the 12 | 10 | 12 | 11 | 1 | 1 |
| Synonyms searched | yes | yes, by default | own code | yes | yes |
| Ranking of a whole match | below partial matches (measured) | exact mode `t=` | own code | Solr | Solr |
| Disk | none | about 1.5 GB gz without taxonomy and UniProt | about 360 MB without taxonomy | 119 MB | 94 MB to 700 MB gz |
| Freshness | days | weeks to months | each release | about 4 months | each release |
| Rate limit | none published, fair use | none | none | none published | none published |
| No internet | no, unless self-hosted with Docker | yes, by file path | yes | yes, by file | yes, by file |

Trade-offs, noted for the choice and not made here:

- OLS4 alone needs no files and no new code, but it does not work on a node with no internet, and
  its answer can change between two runs because it reloads often. A run of 2,000 studies at about
  18 labels per sample would make many calls (inferred), against an unpublished limit.
- Local files give a fixed snapshot that a reading can name, which the earlier decision asked for.
  OAK covers all twelve and searches synonyms with no new code, at about 1.5 GB of compressed files.
  The OBO files are smaller but need a search index, written in seqforge or taken from a package
  such as PyOBO.
- NCBI Taxonomy is the outlier in size in every local form. The species in the study set are few, so
  a species table is much smaller than the whole taxonomy (inferred).

## What liulab-data holds

liulab-data holds no ontology snapshot and no code that reads one (read: a search of its `src/`,
`docs/`, `scripts/`, `tests/`, `skills/`, README, AGENTS.md, GLOSSARY.md, pyproject.toml and
CHANGELOG for "ontolog", "uberon", "semsql", "oaklib", "cellosaurus", "mondo" and ".obo" found no
match, at commit `55b6305`, which matches its remote `main`). Its issue tracker has no issue about
ontologies or UBERON (read: GitHub search). The decision on
[#520](https://github.com/liuhlab/seqforge/issues/520) says "liulab-data keeps the ontology
releases as dated snapshots, like its other bulk sources", and nothing has been built for it yet.

## The gaps

### Rat and fly stage lists

- **Fly.** FBdv, the Drosophila development ontology from FlyBase, is maintained: 250 terms in OLS4,
  release 2026-11-02, and a 1.3 MB SemSQL file (measured). The CELLxGENE schema 7.1.0 uses it for
  fly stages ([CELLxGENE schema](https://github.com/chanzuckerberg/single-cell-curation/blob/main/schema/7.1.0/schema.md),
  read).
- **Rat.** A rat stage list, RnorDv, exists as `rnordv.obo` in the developmental-stage-ontologies
  repository: 41 KB and 62 entries, mostly stages by age such as "29-month-old stage", last dated
  24 February 2014 (measured, from the file). It is not loaded in OLS4, where only one dangling
  reference from UBERON appears, and it has no SemSQL file (measured). The same repository holds
  stage lists for about 30 species, among them dog, cow, pig, chicken and macaque
  ([developmental-stage-ontologies](https://github.com/obophenotype/developmental-stage-ontologies),
  read).
- **The fallback others use.** For every species other than human, mouse, worm, zebrafish and fly,
  the CELLxGENE schema requires "the most accurate descendant of UBERON:0000105 for life cycle stage,
  excluding UBERON:0000071 for death stage" (read). UBERON is already in every way above.

### Assay

- **EFO** (Experimental Factor Ontology) is what seqforge already uses for assay names
  (`src/seqforge/io/efo.py`). The CELLxGENE schema requires an EFO assay term, a descendant of
  "assay by molecule" (EFO:0002772) or "single cell library construction" (EFO:0010183) (read). EFO
  is in OLS4 (94,705 terms, release 3.96.0) and SemSQL (255 MB) (measured).
- **OBI** (Ontology for Biomedical Investigations) is what ENCODE uses: its experiment schema
  describes `assay_term_name` as "OBI (Ontology for Biomedical Investigations) ontology term for the
  assay", and a histone ChIP-seq experiment carries `OBI:0000716` (read and measured on the ENCODE
  API). OBI is in OLS4 (5,345 terms) and SemSQL (7.2 MB) (measured).

### Target

- **ENCODE does not use an ontology for targets.** Its target schema is "Schema for submitting a
  target gene": a target has a `label` (such as `H3K27me3`), a list of `genes` (NCBI Gene records
  with HGNC and UniProt cross-references), a list of `modifications` (such as trimethylation at
  lysine 27), and an `investigated_as` category from a closed list of 14, such as "broad histone
  mark", "transcription factor" or "tag" (read and measured:
  [target schema](https://www.encodeproject.org/profiles/target.json), and the record
  `/targets/H3K27me3-human/`).
- **Histone marks are scattered across ontologies.** An exact OLS4 search for "H3K27me3" found
  ChEBI (CHEBI:85045 "H3K27Me3"), NCIt (a term named for lysine 28, by a different numbering), SO
  (SO:0001709 "H3K27_trimethylation_site") and PR (measured). No one of them is a full list of marks.
- **A protein target** can be named by its UniProt entry or its gene (NCBI Gene or HGNC), as ENCODE
  does.

### Engineered vectors

- **No open vocabulary names individual vectors.** The Sequence Ontology has general kinds, such as
  "engineered plasmid", but no catalog of named constructs (inferred from SO's scope; not checked
  term by term).
- **Addgene** has a read-only API and daily JSON downloads covering plasmids and viral preps, with
  sequence, expression and vector type. Access is "granted to approved users" who request a token
  and accept a data licence; Addgene answers within five business days
  ([Addgene developers](https://developers.addgene.org/access-options/), read).
- Cellosaurus records the genes transfected into a cell line, not the vector as a term (inferred
  from its record format).

## Sources

- OLS4 ontology list and search API, `https://www.ebi.ac.uk/ols4/api/` (measured).
- [OLS4 source and README](https://github.com/EBISPOT/ols4).
- [EMBL-EBI terms of use](https://www.ebi.ac.uk/about/terms-of-use/).
- [semantic-sql README](https://github.com/INCATools/semantic-sql) and its
  [registry](https://github.com/INCATools/semantic-sql/blob/main/src/semsql/builder/registry/ontologies.yaml);
  files at `https://semanticsql.berkeleybop.io/<name>.db.gz` (sizes measured).
- [Ontology Access Kit source](https://github.com/INCATools/ontology-access-kit):
  `src/oaklib/datamodels/search.py`, `src/oaklib/utilities/caching.py`, `src/oaklib/constants.py`,
  `docs/faq/troubleshooting.rst`.
- [pronto](https://github.com/althonos/pronto).
- OBO files at `http://purl.obolibrary.org/obo/` (sizes measured).
- [Cellosaurus API](https://api.cellosaurus.org/) and `https://ftp.expasy.org/databases/cellosaurus/`
  (sizes and release notes measured).
- UniProt REST API, `https://rest.uniprot.org/`, and
  `https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete/` (sizes
  measured).
- NCBI taxonomy dump, `https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/` (size measured).
- [Gilda](https://github.com/gyorilab/gilda) and [PyOBO](https://github.com/biopragmatics/pyobo).
- [CELLxGENE schema 7.1.0](https://github.com/chanzuckerberg/single-cell-curation/blob/main/schema/7.1.0/schema.md).
- [developmental-stage-ontologies](https://github.com/obophenotype/developmental-stage-ontologies),
  `src/ontology/components/rnordv.obo`.
- [ENCODE target schema](https://www.encodeproject.org/profiles/target.json) and
  [experiment schema](https://www.encodeproject.org/profiles/experiment.json).
- [Addgene developers](https://developers.addgene.org/access-options/).
