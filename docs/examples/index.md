# Example reports

Two study reports, made by seqforge from public GEO records. Each one is a single web page that
opens offline. It shows what seqforge writes about a study, and how each claim points back to its
source.

The two studies come from the **standard set**, 50 GEO studies picked to cover many kinds of
design. They were picked here because their designs differ, because each reading quotes only
public text (GEO records, and at most a paper's abstract on PubMed), and because both jobs settled
both studies. A job settles a study when each question it asks twice, the second time with the
options in reverse order, gets the same answer both times.

| Study | What it is | Design the reading found | Quotes from |
| --- | --- | --- | --- |
| [GSE112068](GSE112068/report.html) | Human lung cells exposed to a drug that damages DNA, sampled over 8 days | 3 factors: treatment, dose and time point (11 times). 23 samples | GEO records only |
| [GSE272899](GSE272899/report.html) | Open chromatin in fat-tissue immune cells of young and old mice | 2 factors: age and genotype (4 genotypes). 9 samples | GEO records and one abstract |

## How to read a report

A report has one tab, **Study**, in three parts.

- **Bounds.** Which GEO Series and papers are one study. A solid box is a record kept in the study,
  and a dashed, struck-through box is one left out. Point at a record to see why it was kept or
  left out.
- **Design.** The factors the study varied on purpose, and how many samples fall in each mix of
  levels. A diamond (◆) marks a factor's baseline, the level the others are compared with. Each
  factor carries a `p`: the model's confidence in that factor, from 0 to 1. Point at a count to
  list its samples.
- **Samples.** What each sample was, in one table per topic: assay, organism, material, and
  treatment and time. Each value is either **quoted** (a passage backs it), **inferred** (no
  passage states it), **unknown** or **n/a** (it does not apply). A paler cell has a lower `p`.
  Click a cell to see the passages behind it, each with the record and field it came from.

A report does not show how the job ended the study. Both studies on this page settled.

## The two studies

### GSE112068: a dose and time course

Human IMR-90 cells, a line of lung fibroblasts taken from a fetus at 16 weeks, were treated with
bleomycin, a drug that breaks DNA. The reading found three factors: treatment (none or bleomycin),
dose (0, 2 or 40 µg/ml) and time point (11 times, from the start to 8 days). Each factor has a
baseline, and the levels of all three run in order. The study has no paper, so every quote comes
from the GEO Series and Sample records.

[Open the GSE112068 report](GSE112068/report.html).

### GSE272899: two factors, and a chromatin assay

ATAC-seq (a method that maps the open, readable parts of the genome) of macrophages sorted from
mouse fat tissue. The mice were young or old, and carried one of four genotypes around the gene
GDF3, among them a knockout in myeloid cells, the blood-cell family that macrophages belong to.
Every mouse got the same dose of LPS, a bacterial toxin, for 4 hours. The reading records that dose as a shared label and not as a factor, since all
samples got it. Quotes come from the GEO records and the paper's PubMed abstract.

[Open the GSE272899 report](GSE272899/report.html).

## How these were made

Both were made on 2026-11-27, from a run over the whole standard set, with:

- seqforge at commit `9209509`, report version `2026.11.8`;
- the bounding job, version `2026.11.3`, and the reading job, version `2026.11.10`;
- Jev 1.14.0 from TypeSafe, a model that answers yes-or-no and choice questions with a
  probability, in both jobs, and DeepSeek Flash as the reading job's writer;
- ontology terms from the dated snapshots each reading names in its `ontology_snapshot` field.

The run cost nothing, because every answer was already in the model gateway's cache. The
[dated note on that run](https://github.com/liuhlab/seqforge/blob/main/docs/research/study-chain-on-the-50-2026-11-27.md) gives what a first run costs and how each of the 50 ended.

## Make one yourself

```bash
seqforge study run --studies <folder> --ceiling-usd 1
seqforge report --study <folder>/<GSE id>
```

The first command needs the lab's model gateway and its copy of the GEO and SRA records.
[Read your own studies](../guides/read-studies.md) says how to set up the folder and what each
step needs. The second command needs neither the gateway nor the records: it reads the two files
the first one wrote, and writes `report.html` and `report.json` beside them. With the lab's
ontology store set up, it also names each assay term.
