# How often one study spans several GEO Series

Query date: 2026-11-08. Sources: NCBI E-utilities and GEO record pages, public data only.

Terms used once and then abbreviated: GEO (Gene Expression Omnibus, NCBI's functional-genomics
archive); a Series (accession GSE…) is one GEO submission; a SuperSeries is a Series that only
groups other Series, its SubSeries; a leaf Series is any Series that is not a SuperSeries; PMID is
a PubMed article ID; BioProject (PRJNA…) is NCBI's project record; PMC is PubMed Central,
NCBI's full-text archive; a library strategy is the assay label GEO records per sample (RNA-Seq,
ATAC-seq, ChIP-Seq, Bisulfite-Seq and so on).

## Answer

- **Measured (census):** of 121,803 sequencing Series made public 2018–2024, 97,794 (80.3%) cite a
  paper. Of their 61,638 papers, 15,199 (24.7%) are cited by two or more of these Series
  (SuperSeries counted), and those papers hold 55,193 Series (56.4% of Series that cite a paper).
- **Measured (150 sampled papers, 142 with two or more sequencing leaf Series):** one SuperSeries
  joins all of a paper's sequencing Series in 69 (49%), some of them in 13 (9%), none in 60 (42%).
  A shared BioProject joins none of the 60. **Inferred:** SuperSeries alone under-groups about half
  of multi-Series papers; the PMID link is needed.
- **Measured:** 29 of the 142 papers (20%) also cite a sequencing Series that lists an earlier
  PMID, i.e. data first published by another paper (an upper bound, see Limits). Grouping by PMID
  would merge that older study in. Without those Series, 124 papers keep two or more of their own:
  SuperSeries joins all in 66 (53%), some in 7 (6%), a shared BioProject only in 1 (1%), only the
  paper in 50 (40%).
- **Measured:** in 75 of those 124 papers (60%) the paper's own Series use different library
  strategies. In 27 a differing pair sits outside any common SuperSeries; read by hand, 21 of the
  27 are different assays on the same cells or tissue under the same design. So **21 of 124 papers
  (17%)** keep co-measured assays in Series that only the paper joins (in one case, a shared
  BioProject).
- **Measured (PMC full text, 129 papers):** the texts name 123 Series that GEO does not link to the
  paper; 117 are other studies' data. Only 1 is the authors' own Series citing no paper at all
  (truly unjoined); 1 more is linked only to a later paper and 3 to the same lab's earlier papers.
- **Inferred:** link-based candidates (SuperSeries, shared BioProject, shared PMID) reach nearly
  every published multi-Series study. The common failure is over-merging, about one paper in five,
  through a re-cited older Series; a rule "a Series belongs to the earliest paper it cites" would
  split most of those, leaving same-lab companion papers for judgement. The 19.7% of sequencing
  Series that cite no paper are outside this measure.

## Method

### Frame (census, not sampled)

One `esearch` on db=gds (GEO DataSets), history on, then `esummary` in pages of 500 over the whole
result, keeping each Series' accession, type, public date, PubMed IDs and BioProject. The term:

```text
gse[ETYP] AND ("expression profiling by high throughput sequencing"[GTYP]
 OR "genome binding/occupancy profiling by high throughput sequencing"[GTYP]
 OR "methylation profiling by high throughput sequencing"[GTYP]
 OR "genome variation profiling by high throughput sequencing"[GTYP]
 OR "non coding rna profiling by high throughput sequencing"[GTYP]) AND 2018:2024[PDAT]
```

121,803 records returned and 121,803 unique records fetched. A Series counts as sequencing when
one of its listed types is one of those five; a SuperSeries carries the union of its SubSeries'
types, so SuperSeries are in the frame. The PubMed IDs are the ones GEO stores on the Series
(`pubmedids` in the summary, the same as `!Series_pubmed_id`).

### Sample

The 15,199 PMIDs cited by two or more frame Series, sorted by number, shuffled with Python's
`random.Random(530)`; the first 150 taken. For each PMID:

1. `elink` dbfrom=pubmed, db=gds, linkname=pubmed_gds: every GEO Series that cites the paper, of
   any year and type (553 Series for the 150 papers).
2. `esummary` db=gds on those Series: type, date, PMIDs, BioProject, sample count.
3. The GEO header of each Series and of each SuperSeries it names
   (`https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE…&targ=self&form=text&view=brief`):
   the `!Series_relation = SuperSeries of:` / `SubSeries of:` / `BioProject:` lines.
4. The sample records of every sequencing leaf Series with at most 1,500 samples (412 Series; the
   3 larger ones fell back to the Series type) (`…&targ=gsm&form=text&view=brief`): library
   strategy, sample title, source, characteristics, BioSample.
5. The paper's PubMed Central (PMC) full text where it exists (`elink` pubmed→pmc, `efetch`
   db=pmc), searched for `GSE` accessions, and the paper's author list (`esummary` db=pubmed).

### Rules

- **Leaves.** A Series whose header lists `SuperSeries of:` is a SuperSeries; every other linked
  Series is a leaf. Only sequencing leaves are classified. 8 of the 150 papers had fewer than two
  (a SuperSeries with one sequencing SubSeries, its other SubSeries typed "Other" or microarray)
  and are set aside.
- **Class, in this order.** *SuperSeries joins all*: one SuperSeries lists every sequencing leaf.
  *SuperSeries joins some*: some SuperSeries lists two or more of them, but none lists all; the
  rest are joined by the paper. *Shared BioProject only*: no SuperSeries joins any two, and every
  leaf has the same BioProject accession. *Paper only*: none of these. "Not joined at all" cannot
  occur in this frame, because every Series in it cites the paper; the full-text check estimates
  it instead.
- **Earlier study's Series.** A leaf that also lists a PMID smaller than the sampled PMID (PMIDs
  rise with time) was first published with an earlier paper. The "own" view drops those leaves and
  classifies again.
- **Different assays.** Two leaves differ in assay when their sets of `!Sample_library_strategy`
  values differ. Bulk and single-cell RNA-seq share the strategy "RNA-Seq", so they count as the
  same assay here.
- **Co-measured.** For the 27 papers with a differing pair outside any common SuperSeries, I read
  each pair's titles, sample names and sources and marked it *yes* when the two assays profile the
  same biological system under the same design (same cell line, tissue or organism, same
  genotype or treatment contrast), *partly* when they share the system but not the samples or the
  design, *no* otherwise (*partly*: PMIDs 38875320, 31015454, 36797228; *no*: 35342888,
  39773984, 35829695). An automatic rule (shared GSM, shared BioSample, a shared donor ID, an
  identical characteristics set or a matching sample-title stem) found no shared GSM, BioSample or
  donor ID in any pair, and caught only 8 of the 21 hand-marked *yes*; it is not used for the
  headline.
- **Full text.** A Series the text names but GEO does not link to the paper is *joined via
  SuperSeries* when it is a SubSeries of a linked SuperSeries; *own data* when its GEO contributor
  list shares two or more names (surname and first initial) with the paper's authors, or one name
  and the Series cites no paper; otherwise *another study*.

## Classification table

Sample: 150 PMIDs, 142 with two or more sequencing leaf Series. All counts **measured**.

| Class | All sequencing leaves (n = 142) | Own leaves only (n = 124) |
|---|---|---|
| SuperSeries joins all | 69 (49%) | 66 (53%) |
| SuperSeries joins some; rest by paper only | 13 (9%) | 7 (6%) |
| Shared BioProject only | 0 | 1 (1%) |
| Paper only | 60 (42%) | 50 (40%) |
| Papers carrying an earlier study's Series | 29 (20%) | – |

| Co-measurement, own leaves (n = 124) | Papers |
|---|---|
| Own leaves differ in library strategy | 75 (60%) |
| … every differing pair inside one SuperSeries | 48 |
| … some differing pair outside any common SuperSeries | 27 |
| … of the 27, hand-marked same system and design (*yes*) | 21 (17% of 124) |
| … of the 27, *partly* | 3 |
| … of the 27, *no* | 3 |
| A single leaf Series holding two or more library strategies | 10 |

| Full text, 129 papers with a PMC body | Count |
|---|---|
| Series named in text, not linked to the paper | 123 |
| … another study (reuse) | 117 (in 31 papers) |
| … joined via a linked SuperSeries | 1 |
| … own data, citing no paper | 1 (1 paper) |
| … own data, linked only to a later paper | 1 (1 paper) |
| … own data, linked to the same lab's earlier paper | 3 (2 papers) |
| Multi-Series papers (n = 122) whose text names every leaf, or the SuperSeries holding them | 111 |
| … names only some leaves, or none | 11 |

Sequencing leaves per paper (n = 142): 2 in 82, 3 in 32, 4 in 16, 5 in 6, 6 or more in 6
(**measured**). 22 of the 142 also cite leaf Series of other types: 21 typed "Other", 10
microarray (Series counts, **measured**).

## Worked examples

- **PMID 30454645, paper only, co-measured.** RNA-seq GSE104837, ATAC-seq GSE113608 and bisulfite
  sequencing GSE104998 all profile YB5 colon-cancer cells treated with the compound HH1; a fourth
  Series, GSE104997, is DNA-methylation sequencing of a mouse ovarian-cancer line. Four
  BioProjects, no SuperSeries, public on two dates three months apart. Only the PMID joins them.
- **PMID 41340160, shared BioProject and three older studies.** The new data are ATAC-seq
  GSE282534, CUT&RUN GSE282535 and targeted DamID GSE282537 on fly brains, all three under one
  BioProject (PRJNA1188621) and no SuperSeries. GEO also links to the paper RNA-seq GSE100578
  (first cited by PMID 29490272), GSE245380 (first cited by PMID 38597673) and SuperSeries
  GSE70591 with its SubSeries GSE70589 (PMID 26673323, public 2015). Grouping by PMID would merge
  four studies.
- **PMID 36797228, SuperSeries joins some.** circRNA, small-RNA and total-RNA sequencing of liver
  tumours and paired adjacent tissue (GSE169287, GSE169288, GSE169289) and a pull-down
  (GSE190239) sit under SuperSeries GSE169391. The organoid RNA-seq GSE182593 from the same paper
  sits outside it.
- **PMID 38030596, paper only, earlier study.** Two new melanoma cell-line RNA-seq Series
  (GSE218625, GSE240307) plus single-cell GSE174401, first cited by PMID 34035069 and public in
  2021. The two new Series are one study joined only by the PMID; the third is reuse.
- **PMID 38875320, a Series joined by nothing.** GEO links ATAC-seq GSE265872 and RNA-seq
  GSE266561 to the paper; the text also names GSE266323, submitted by nine of the paper's authors,
  which cites no paper and sits in no SuperSeries.
- **PMID 36690616, SuperSeries joins all.** SuperSeries GSE168415 lists ChIP-seq, RNA-seq and
  bisulfite sequencing leaves (GSE168411–GSE168414, GSE207730, GSE222844): the case link-based
  grouping handles without judgement.

## Limits

- Sequencing is read from the Series type. A Series typed only "Other" is outside the frame even
  when it holds sequencing data (the targeted DamID Series GSE282537 above is one); 21 such Series
  appear among the sampled papers. This undercounts multi-Series papers.
- The frame starts from Series that cite a paper. The 24,009 sequencing Series (19.7%) that cite
  none are not classified; any of them that belong to a study with other Series are "not joined at
  all" and are invisible here.
- "Earlier study's Series" uses PMID order. A later erratum or companion paper by the same lab can
  look like reuse; the 29 are an upper bound on cross-study merges, not a hand-checked count.
- A shared BioProject means an identical accession. Umbrella BioProjects that group Series from
  outside GEO were not followed.
- The co-measurement call is one reader's judgement on titles and sample names, not on the papers.
- The full-text check is a lower bound on unjoined Series: it sees only accessions written out in
  PMC text (129 of 150 papers), and 11 of 122 texts do not name all of their own linked Series.
  The author-overlap rule is coarse.
- One sample of 150; a 95% interval on a proportion near 17% at n = 124 is roughly ±7 points.

## Scripts

Five standard-library Python scripts: the frame census, the seeded sample, the per-paper fetch,
the classifier and the full-text check. They are not in the repository; the queries above
reproduce the frame.
