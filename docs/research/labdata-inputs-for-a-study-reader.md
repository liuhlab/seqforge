# What a study reader can draw from liulab-data

Read 2026-11-08 for [#531](https://github.com/liuhlab/seqforge/issues/531), from liulab-data
`main` at `415b62f` (2026-11-07), its docs and its GitHub issues. File paths are relative to the
liulab-data repository root. **Read** means seen in code, docs or an issue; **inferred** means
reasoned from what was read and not checked. Counts quoted from liulab-data issues were measured
there, not here. Nothing here was run against the live store.

Terms used below:

- **GEO** (Gene Expression Omnibus) records: Series (`GSE…`), Sample (`GSM…`), Platform (`GPL…`).
- **SRA** (Sequence Read Archive) records: study (`SRP…`), experiment (`SRX…`), sample (`SRS…`),
  run (`SRR…`). Also NCBI's BioProject (`PRJNA…`) and BioSample (`SAMN…`).
- **SOFT**: GEO's line-based text format, one `!Key = value` per line. A Series' "family SOFT"
  file holds the Series and all its Samples and Platforms.
- **PMID**: a PubMed ID. **PMC**: PubMed Central, the free full-text archive. **PMCID**: a
  paper's PMC ID. **JATS XML**: the article XML that PMC serves. **DOI**: a digital object
  identifier.
- **Target Series**: a GEO Series whose type names high-throughput sequencing; the store holds
  these and everything they link to.

## Answer

- **Every GEO and SRA text field is kept, word for word, with no selection.** GEO records keep
  every `!` line of the family SOFT file, with only the microarray data tables cut out. That
  covers `Series_summary`, `Series_overall_design`, `Sample_characteristics_ch1`, the
  `Sample_*_protocol_ch1` lines, `Sample_data_processing` and `Sample_description`. SRA,
  BioProject and BioSample records keep every XML element and attribute as path and value pairs,
  plus the raw XML (**read**).
- **All links between records are in one link table, with 14 named relations and the source that
  stated each one.** A reader can walk Series → Sample → SRA experiment → run, SRA sample and
  BioSample. It can also walk Series → BioProject, SuperSeries → SubSeries, and Series or
  BioProject → PMID or DOI (**read**).
- **GEO supplementary files are listed and never downloaded.** Each row gives the owner, name,
  URL, size, date, type and the archive that holds the file. SRA original files are listed the
  same way, from the SRA XML: name, size, MD5 and URL (**read**).
- **The paper store holds a lot for a paper in PMC, and little for any other paper.** For a
  paper in the PMC bucket it holds the JATS XML full text and figures with their labels and
  captions. It downloads supplements up to 10 MB and lists larger ones with a URL. Every paper
  also gets its PubMed title, abstract, journal, authors and affiliations. A paper outside PMC
  gets at most a PDF, with no figures or supplements (**read**).
- **The backfill is about a quarter done.** The 1% and 10% rounds passed on 2026-11-07 and
  2026-11-08. The 100% round stopped early on 2026-11-08, after 13,491 of 69,513 PMC papers,
  because the shared disk filled. It resumes when space is free. By then about 21,300 of the
  roughly 77,600 PMC papers were fetched (**read**, liulab-data#169 and #170; the sum is
  **inferred**). The daily job that fetches PDFs from outside PMC has not started.
- **Expected coverage when done:** about 95,400 PMIDs are linked to target Series. About 81% get
  JATS XML from PMC, about 10% get a PDF from OpenAlex or a publisher's site, and about 10% have
  no route. **22% of target Series (43,633 of 198,489) link no paper at all**, so a reader of those
  has only the GEO and SRA text (**read**, liulab-data#137 and the tier-coverage note; the end
  state is **inferred**).
- **What is missing needs new fetching:** the contents of GEO supplementary files (sample
  sheets, metadata tables, cell annotations), paper supplements over 10 MB, supplements of papers
  outside PMC, and papers for Series that link none. No code in either repo fetches GEO
  supplementary contents. The liulab-data maintainer raised it as a possible stage 5
  (liulab-data#18, 2026-11-07), with no design yet (**read**).
- **seqforge must bump its liulab-data pin first.** Its lock pins `3eeca24` (2026-11-06), which
  predates the paper store (`labdata.papers`) and the DOI links. The GEO/SRA calls below already
  exist at that pin (**read**, `pixi.lock` in seqforge and `git merge-base` in liulab-data).

## Store 1: the GEO/SRA store

Open it with `labdata.open_store()`. The root comes from the argument, the `LABDATA_STORE`
environment variable or the pointer file. A `Store` reads the build that was current when it was
opened, and makes no network call. An accession the store does not hold raises `NotMirrored`, and
one that no snapshot lists raises `NotFound` (**read**, `src/labdata/store/root.py`). The store
is refreshed monthly; the first refresh ran on 2026-11-07 (**read**, liulab-data#18).

A `Record` has `accession`, `type`, `status`, the dates `submitted`, `updated` and `published`,
the `fields` pairs, `content_hash`, `snapshot_date` and `links`. `Record.field(name)` returns
every value of one field in source order, with repeats kept. `Record.follow(relation)` returns
the accessions at the far end of one relation (**read**, `src/labdata/store/model.py`).

| Record type | Text and fields it holds | `labdata` call | Notes |
| --- | --- | --- | --- |
| GEO Series | Every `!Series_*` line: `title`, `summary`, `overall_design`, `type`, `contributor`, `pubmed_id`, `web_link`, `relation`, `sample_id`, `platform_id`, `supplementary_file`, `contact_*`, dates, status | `Store.open`, `Store.open_many`, `Record.field("Series_overall_design")`; `Store.raw` for the SOFT block as served; `Store.table("GEO Series", [...], where=...)` for all Series as columns | **read**: `soft_fields` and `soft` in `src/labdata/store/sources.py`; field names seen in `tests/data/geo_slice/soft/` |
| GEO Sample | Every `!Sample_*` line: `title`, `source_name_ch1`, `organism_ch1`, `characteristics_ch1` (repeated, `key: value`), `molecule_ch1`, `extract_protocol_ch1`, `growth_protocol_ch1`, `treatment_protocol_ch1`, `data_processing` (repeated), `description`, `library_strategy`, `library_source`, `library_selection`, `instrument_model`, `relation` (BioSample, SRA), `supplementary_file_N` | the same calls, with `Store.follow(gse, "sample")` to list a Series' Samples | **read**. Two-channel `_ch2` lines are kept too, because the parser keeps every `!` line (**inferred**). A Sample in several Series keeps its newest copy (ADR 0002, **read**). |
| GEO Platform | Every `!Platform_*` line: `title`, `technology`, `organism`, `taxid`, dates | `Store.open`; `Store.follow(gse, "platform")` | **read**. The data table is cut out. |
| SRA experiment | Raw text is the whole `EXPERIMENT_PACKAGE`. Fields are the flattened `EXPERIMENT` element: `TITLE`, `DESIGN/DESIGN_DESCRIPTION`, `LIBRARY_DESCRIPTOR` (name, strategy, source, selection, layout, `LIBRARY_CONSTRUCTION_PROTOCOL`), `PLATFORM/…/INSTRUMENT_MODEL` | `Store.open`; `Store.raw`; `Store.experiments_for(acc)` returns labdata `Experiment` objects with `runs`, built from stored XML with no network | **read**: `sra_package` in `src/labdata/store/sources.py`; `Store.experiments_for` |
| SRA run | The `RUN` element: `total_spots`, `total_bases`, `size`, `published`, pool members, and `SRAFiles` (each file's original name, size, MD5, `semantic_name` such as fastq or bam, and URLs) | `Store.open`; `Run.total_spots`, `Run.read_lengths`, `Run.files`, `Run.original_files` on a store-built `Run` | **read**. Only file names and sizes are held, not the bytes. |
| SRA sample | The `SAMPLE` element: `TITLE`, `SAMPLE_NAME`, and `SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE/TAG` and `/VALUE` pairs | `Store.open`; `Store.follow(srx, "SRA sample")` | **read** |
| SRA study | The `STUDY` element: `STUDY_TITLE`, `STUDY_ABSTRACT`, `STUDY_TYPE`, `CENTER_PROJECT_NAME`, plus `SUBMISSION@center_name` | `Store.open`; `Store.follow(srx, "study")` | **read** |
| BioProject | The whole bulk `<Package>`: `ProjectDescr/Title`, `Description`, `Relevance`, `Publication`, `ProjectType`, submitting organisation | `Store.open`; `Store.follow(gse, "BioProject")` | **read**. For a project GEO created, the `Description` repeats the Series summary and overall design (**inferred** from one test fixture). |
| BioSample | The whole `<BioSample>`: title, organism, and each `Attributes/Attribute` with `attribute_name` and `harmonized_name`, IDs, owner | `Store.open`; `Store.follow(srs, "BioSample")` or `Store.follow(gsm, "BioSample")` | **read**. An attribute's name and its value come back as separate path rows, so use `Store.raw` to keep each pair together (**inferred** from `flatten`). |

**Links.** Each link is stored once, under its forward name, once for each source that states it.
`Store.links(acc, relation)` and `Store.follow(acc, relation)` take the forward or the reverse
name (**read**, `RELATIONS` in `src/labdata/store/model.py` and `src/labdata/store/sources.py`).

| Relation (reverse) | From → to | Stated by (`source`) |
| --- | --- | --- |
| `SuperSeries of` (`SubSeries of`) | GSE → GSE | SOFT |
| `sample` (`series`) | GSE → GSM | SOFT |
| `platform` (`platform of`) | GSE or GSM → GPL | SOFT |
| `SRA` (`GEO`) | GSM → SRX | SOFT, Entrez link |
| `BioProject` (`BioProject of`) | GSE or SRP → PRJ | SOFT, Entrez link, GEO summary, SRA XML |
| `BioSample` (`BioSample of`) | GSM or SRS → SAM | SOFT, SRA XML |
| `study` (`experiment`), `SRA sample` (`SRA experiment`), `run` (`run of`) | SRX → SRP, SRS, SRR | SRA XML |
| `umbrella of` (`under umbrella`) | PRJ → PRJ | BioProject XML |
| `PubMed` (`PubMed of`) | GSE, GSM, GPL, PRJ, SRP → `PMID:<n>` | SOFT, BioProject XML, PubMed (`<accession>[si]` search) |
| `DOI` (`DOI of`) | GSE or PRJ → `DOI:<doi>` | `SOFT Series_web_link`, `SOFT Series_citation`, BioProject XML |
| `Reanalyzed by` (`Reanalysis of`), `Named Annotation` | GSE → GSE, or GSE → annotation | SOFT |

The DOI relation is in the code on `main`. The live build gets DOI links only at the next refresh
(**read**, liulab-data#173).

**Supplementary files.**

| What | Fields | `labdata` call | Notes |
| --- | --- | --- | --- |
| Files a GEO Series, Sample or Platform lists | `SupplementaryFile`: `owner`, `name`, `url`, `size`, `date`, `type` (such as `TAR` or `TXT`), `archive` (such as `GSE…_RAW.tar`) | `Store.supplementary_files([gse, *gsms])` | **read**: `src/labdata/store/suppl.py`. Names and URLs come from the SOFT `_supplementary_file` lines. Sizes, types and the contents of each archive come from the Series' `filelist.txt`. A Series lists its own files, and each Sample lists its own. The files are never downloaded. |

To read across all of GEO in bulk, use `Store.table(type, fields, where)`, which returns an Arrow
table, or query DuckDB over `builds/<id>/records/<type>.parquet`, `links_by_from.parquet` and
`supplementary_files.parquet` directly. `src/labdata/store/store_readme.md` shows example queries
(**read**). One benchmark query, `table GEO Sample`, took 6.4 s on a lightly loaded node
(**read**, liulab-data#165).

## Store 2: the paper store

Open it with `labdata.papers.open_paper_store()`. The root comes from the argument, the
`LABDATA_PAPERS` environment variable or the pointer file. The store's `reader` is a
`PaperReader` joined to the GEO/SRA store's current build. It holds the papers that **target
Series and held BioProjects** link. Links from GEO Samples are out of scope. A DOI counts only
for a record that links no PMID. A preprint and its journal version are two papers (**read**,
`src/labdata/papers/reader.py`, `src/labdata/papers/sync.py` `linked_papers`,
`src/labdata/papers/papers_readme.md`).

| Item | What it holds | `labdata` call | Notes |
| --- | --- | --- | --- |
| A record's papers | Each linked paper's key and its status: `fetched`, `waiting`, `manual` or `skipped`, with a reason | `PaperStore.reader.papers_for([gse, prj, …])` returns a `PaperLinks` per accession, with `.linked`, `.fetched` and `.papers` (each a `LinkedPaper`) | **read**. It reads only the accession's own links, so ask for SubSeries and BioProjects too (**inferred**). |
| A paper's IDs and details | `pmid`, `pmcid`, `doi`, `openalex`, `published`, `xml_tier`, `pdf_tier`; `info` is a `PaperInfo` with `title`, `journal`, `published`, `authors` (name and affiliations), `abstract` and `publication_type` | `PaperStore.reader.paper("PMID:<n>" or a DOI)` returns a `Paper` | **read**. Details come from PubMed, or from OpenAlex for a paper known only by DOI. |
| Full text | JATS XML, stored zstd-compressed (`papers/<n>nnn/<pmid>/<pmid>.xml.zst`) | the `Paper.files` entry with `kind == "xml"`; `PaperFile.open()` returns the decompressed bytes | **read**. Comes from the PMC bucket only. PMC's plain-text version is not kept. |
| PDF | Held when there is no PMC XML: from PMC, OpenAlex's paid copy, a free link on the publisher's site, or Paperpile | the entry with `kind == "pdf"` | **read**. When PMC has the XML, PMC's PDF is listed, not held. |
| Figures | The images, with `label` and `caption` taken from the JATS XML | the entries with `kind == "figure"` | **read**. Held up to 10 MB; PMC papers only. |
| Supplements | The files, with `label` (such as "Table S1"), `caption`, `size`, `md5` and `url` | the entries with `kind == "supplement"`; check `path is None` for a file that is only listed | **read**. Held up to 10 MB; larger files and every video are listed with their URL. PMC papers only. |
| Store state | The last pass, counts by status and reason, today's fetches and quota | `labdata.papers.read_status(root)` returns a `PaperStatus` | **read**: `src/labdata/papers/status.py` |

## What is missing

| Input a reader wants | Held? | Who would fetch it, and roughly how big |
| --- | --- | --- |
| Contents of GEO supplementary files: sample sheets, metadata tables, cell annotations, processed matrices | **No.** Listed only (name, URL, size, type) | Nothing fetches them today. There are two homes: liulab-data, as a size-capped bulk fetch like the paper supplements (the stage 5 idea in liulab-data#18), or seqforge, on demand by URL for each study it reads. For a read of all GEO, a size-capped fetch in liulab-data spares GEO one request per study per run (**inferred**). The total size has not been measured. One DuckDB sum of `size` over `supplementary_files.parquet` gives it, and a filter on `type` and `size` gives the small tabular subset a reader needs. |
| Paper supplements over 10 MB | Listed with URL and MD5 | seqforge on demand, over HTTPS from the PMC bucket. The cap keeps about 90% of files; above 5 MB the files are mostly zips, large PDFs and videos. Per file: median 217 KB, 90th percentile 8.9 MB, 99th percentile 59 MB (**read**, liulab-data#145 and the paper-supplements note, measured on 242 articles). |
| Supplements of papers outside PMC (about 19% of PMIDs), and of PMC author manuscripts (about 16% of bucket papers; their XML names supplement files, but 5 of 6 sampled had none in the bucket) | **No** | No scripted route was found. Most publisher pages block bots, OpenAlex has no supplement data, and Paperpile is manual and deferred (liulab-data#154) (**read**, the paper-supplements and tier-coverage notes). |
| Full text of papers outside PMC | A PDF at most, once the paper job runs | liulab-data's paper job (liulab-data#171, not started). About 9,500 PDFs are reachable through OpenAlex or publisher sites, and about 9,700 PMIDs have no route (**inferred** in the tier-coverage note). A reader must parse these from PDF, since there is no JATS XML. |
| Papers known only by DOI (about 1,046 DOIs on 1,350 Series with no PMID) | **Not yet** | liulab-data, after the next monthly refresh adds DOI links to the build (liulab-data#173); the code is on `main` (**read**). |
| A paper for each of the 43,633 target Series that link none | **No** | No repo owns this. A full-text citation search for the GSE accession would find papers that cite a Series without being linked from it. liulab-data's paper store would be the natural home (**inferred**). |
| SRA original file contents, such as a submitted BAM's header | Names, sizes, MD5s and URLs only | seqforge's own bounded remote probe reads the bytes; it is not a store item (**inferred**). |
| Studies with no GEO Series, submitted to SRA only | **No** | Out of the store's scope: it reaches SRA only through GEO (**read**, ADR 0001 and liulab-data#137). |
| PubMed MeSH terms, keywords and grants | **No** | `paper_info` holds title, journal, date, authors, abstract and publication type only. Adding the rest would be a small change in `src/labdata/papers/pubmed.py` (**inferred**). |

## Paper backfill status

The paper backfill is a PMC sync run in nested rounds: 1% of papers, then 10%, then all. Each
round skips the work of the round before. The runs are tracked under the run spec
liulab-data#165.

| Date (UTC) | Event | Issue |
| --- | --- | --- |
| 2026-11-07 | Paper store set up. 1% round: 989 papers, 798 fetched, 4.71 GB, 0 failures. | liulab-data#168 (closed) |
| 2026-11-08 03:30 | 10% round passes its gate. The whole 9,632-paper sample: 7,834 fetched; 946 waiting (OpenAlex backlog 793, PMC embargo 137, publisher link 16); 848 with no route; 4 skipped. Files held: 88,490 (7,834 XML, 50,173 figures, 30,483 supplements), 46.9 GB in all. | liulab-data#169 (closed) |
| 2026-11-08 06:57 | 100% round, run 1, stopped after 3 h 15 min: the shared disk filled with another user's data. 13,491 of 69,513 bucket papers synced, 0 failed. It resumes when at least 1 TB is free. Estimate for the full store: about 96,300 papers and about 470 GB. | liulab-data#170 (open) |
| Not started | The paper job: daily PDFs from OpenAlex and publisher links, a monthly PMC sync, the first copy to Google Cloud Storage | liulab-data#171 (open) |
| Not started | Acceptance: 7 unattended days, and a paper status for every target Series | liulab-data#172 (open) |
| Waits on the next refresh | Papers known only by DOI | liulab-data#173 (open) |
| Deferred | PDFs saved by hand in Paperpile | liulab-data#154 (open) |

No issue reports progress after 2026-11-08 06:57 UTC. Check liulab-data#170 for later runs.

## The calls a reader makes, in order

```python
from labdata import open_store
from labdata.papers import open_paper_store

store = open_store()                                # LABDATA_STORE or the pointer file
series = store.open("GSE…")                         # Record: .fields, .field(), .links, .follow()
subs = store.follow("GSE…", "SuperSeries of")["GSE…"]
gsms = store.follow(["GSE…", *subs], "sample")      # Series -> its Samples
samples = store.open_many([g for gs in gsms.values() for g in gs])
experiments = store.experiments_for("GSE…")         # Experiment objects with runs, no network
packages = store.raw([e.accession for e in experiments])   # each EXPERIMENT_PACKAGE as served
projects = store.follow("GSE…", "BioProject")["GSE…"]
files = store.supplementary_files(["GSE…", *[g for gs in gsms.values() for g in gs]])

papers = open_paper_store().reader
links = papers.papers_for(["GSE…", *subs, *projects])
paper = papers.paper("PMID:…")                       # .info (abstract, authors), .files
jats = next(f for f in paper.files if f.kind == "xml").open().read()
tables = [f for f in paper.files if f.kind == "supplement" and f.path is not None]
```

**Read** for every call name, from `src/labdata/store/root.py`, `src/labdata/papers/root.py`
and `src/labdata/papers/reader.py`. The order is **inferred**.
