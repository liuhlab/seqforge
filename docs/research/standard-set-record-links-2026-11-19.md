# What the links between GEO records say about study bounds, on the standard set

Dated 2026-11-19, for [#590](https://github.com/liuhlab/seqforge/issues/590), on build
`20261107T163933Z` of liulab-data's store. **A link between records is a reason to look, not a
bound. Across the 50 studies of the standard set, 7 carried a linked paper that does not report
them and 4 a linked Series that belongs to another study. The records and the abstracts settled 43
of the 50; the full paper was needed for 7, and for two of the three Series picked as possibly holding two
studies.**

The question: when a GEO Series is linked to other Series and to papers, which of those links mean
"same study"? The bounding stage has to answer this for every study. This note collects what two
passes over real records showed. It sets no rule.

Evidence classes: **picked** means seen while the candidates were picked, when 302 Series records
and 263 PubMed abstracts were read ([#588](https://github.com/liuhlab/seqforge/issues/588)).
**Drawn** means counted over the 50 study files in `evals/standard-set/`, whose bounds a small
model drafted and a large model checked. No count here was checked by a person.

Terms: a **linked group** is a Series, every Series linked to it, and every paper any of them
links. A study's **bounds** are the Series, papers and samples that belong to it.

## Method

Each of the 50 studies went through `seqforge pass start standard-set-bounds`, then
`seqforge standard-set draft`, then `seqforge standard-set judge`, as
[`evals/standard-set/README.md`](https://github.com/liuhlab/seqforge/blob/main/evals/standard-set/README.md)
describes. The counts below were taken from the resulting `study.yaml` and `verdict.yaml` files.
The checking model could read the toy store only, so it could not look for a paper that no record
links.

## Seen while picking

- All 18 SuperSeries read were one study. Seven had no PubMed link on any member.
- Of 18 groups tied only by a shared paper, 16 were one study split by assay, experiment or
  species.
- A two-year gap between Series does not mark a re-cite by itself. The reliable sign is an older
  Series with its own paper that the newer Series does not share. Re-use is sometimes stated in the
  record: a "Reanalysis of" link, a re-use tag in sample titles, or a sentence in the design.
- Of 43 Series with no PubMed link, 4 carried a citation text or a DOI.
- Of about 39 Series with two papers, about 14 were a preprint and its journal version and about
  20 a later paper re-using the same samples.
- One Series under one paper can hold two unrelated experiments.
- Sibling Series tied by no record exist: same title, no shared link.

## Seen while drawing the 50

| What | Studies of 50 |
| --- | --- |
| Bounds hold one Series and one paper, nothing left out | 33 |
| Bounds hold more than one Series | 7 |
| A linked paper is left out | 7 (10 papers) |
| A linked Series of another study is left out | 4 |
| No paper in the bounds | 4 |
| The Series is split by sample | 1 |
| The full paper was needed | 7 |
| The checking model replaced the draft | 2 |
| The checking model was unsure | 0 |

- **A second paper on a Series was never a second study.** The three Series picked as possibly
  holding two studies (GSE101112, GSE131269, GSE132886) each came out as one study. In two, the
  later paper re-analyses the deposited data and adds only other experiments. In the third, the
  second paper is on a different organism and names a different accession in its own text: the
  link is a mistake in the record, and the Series title was copied from that paper.
- **A wrong PubMed link exists.** So a paper linked by the record is not always a paper about the
  data, even before re-use is considered.
- **A plain Series can hide a split.** GSE126954 was picked as a plain case. One of its eight
  samples is a second deposit of a sample from an older study, added to carry reprocessed output.
  The Series' design text says so; no link does. This is the only confirmed case in the set of one
  Series holding samples of two studies, and the split is one sample.
- **Re-use runs both ways and looks the same in the links.** In GSE234962 a later Series re-uses
  this study's samples and three later papers link back to it. In GSE306935, GSE254687 and
  GSE147401 this study's paper links an older Series that has its own paper. In all four the sign
  that held was the one seen while picking: the other Series has a paper of its own that reports
  it.
- **A paper's data statement can name a Series it only re-used.** In GSE306935 the paper lists
  both its own and the older accession. The older Series' own paper is what kept it out.
- **A preprint and its journal version** appeared as two linked papers of the same later study
  (GSE234962). Both were left out together.
- **A SuperSeries was one study every time** (3 of 3 drawn), and so was a paper over two Series
  with no SuperSeries (3 of 3).
- **The abstract was enough to tell re-use in most cases.** A later paper's abstract often says
  the data came from earlier work. The full paper was needed where the abstract was silent
  (GSE101112) and to confirm which accession a paper names as its own.
- **The four studies with no linked paper were bounded from the record alone** and called
  settled. Whether an unlinked paper exists was not checked.

## Limits

- The drawn counts come from model output on 50 studies picked for variety, 16 of them for a hard
  shape. They are not rates for GEO.
- "Left out as re-use" rests on the checking model's reading; no person has reviewed the 50.
- One spare, GSE250280, was also picked as possibly holding two studies and was not drawn.
