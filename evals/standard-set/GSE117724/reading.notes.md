# Notes on the reading of GSE117724

**Read:** Series records GSE117700 (RNA-seq) and GSE117719 (ChIP-seq), all 20 sample records, and the abstract and full text (article XML in the paper store) of PMID 30668573. Also read, not quoted: the SuperSeries record GSE117724. Not read: figures, supplementary tables, BioSample and SRA records. A scratch script found all 54 passages word for word. Terms confirmed at the EBI lookup service: `WBls:0000038` (L4 larva), `WBbt:0007833` (organism), `CHEBI:28262` (dimethyl sulfoxide).

**Same biosample across the two assays: no.** Both ChIP-seq libraries are strain VL648. Every RNA-seq library is wild-type or an nhr-86 mutant. So no worm population gave both an RNA-seq and a ChIP-seq library.

## What the format could not hold

1. **A factor that covers part of the study.** Genotype was varied only among the 18 RNA-seq samples. The two ChIP-seq samples have a genotype that is not a level, so their genotype claims carry no `level`. A strict check may reject that. A factor has no way to name the samples it covers.
2. **No "every sample of this Series" source.** `each_sample` means the whole study. With two Series, 10 passages needed a typed list of 18 or 2 sample ids.
3. **`shared` is one block for the study.** Here it holds the RNA-seq value, and the two ChIP-seq rows replace it, so "shared" really means "default". There is no block for a group: the 18 RNA-seq rows repeat genotype, strain, treatment and dose (72 claims) where 6 groups would need 24.
4. **Series membership has no slot.** Only two comments say which Series a sample is in. Here it equals the assay, so nothing is lost. `study:` names the SuperSeries, a record the bounds leave out.
5. **Record and paper disagree, and one claim holds one answer.** Every RNA-seq record says `strain: N2`, mutants too. The paper names the mutant strains VL491 and RPW137. I wrote the paper's names as `inferred` with `see`. There is no slot for genetic background, and no way to mark a record field as overruled.
6. **One stage slot, two time points.** The records say L4. The worms were L4 to young adult when the overnight exposure began, so older at harvest. The format does not say which moment the slot means (p 0.6).
7. **A baseline used as the peak-calling control.** The DMSO ChIP-seq library is the treatment baseline, and the peaks were called as R24 against it (`GSE117719_R24_2_vs_DMSO_2_peaks.txt.gz`). No input or IgG library was deposited. Both libraries are `na` by the format's rule, so the derived signal-to-control pairs are empty though the study paired them.
8. **`na` and `unknown` cannot cite passages.** `see` exists only on `inferred`. The two ChIP-seq control-type claims name their passages in the note text.
9. **A tagged target.** The pull-down is anti-GFP on NHR-86::GFP. `target` holds NHR-86. The tag and the antibody have no slot, and target has no term source.
10. **Treatment.** R24 worms also got 1% DMSO as solvent: two compounds, one slot, only R24 written. R24 has no term. `overnight` is a duration with no number, so no `value` or `unit`. Temperature (20°C) has no slot.
11. **Evidence from other samples.** "No RNA-seq library shares this biosample" rests on the RNA-seq records' strain field. A claim cites only its own sample's passages, so that part is prose. No claim says "these two assays share no biosample" with its own p; only the distinct ids say it.
12. **Batch.** The RNA-seq file names number the replicates differently from the titles (`N2DMSO_1, _2, _4`; `tm2590DMSO_2, _3, _4` against `tm2590R24_1, _2, _3`). That hints at experiment batches. No label holds a batch, so it is not written.
13. **Notes are repeated.** One biosample note is written 18 times, each strain note 6 times.

## Choices the format did not fix

- **Donor is `na`** for a pool of many worms of one strain. The other reading is one donor per worm population.
- **Biosample ids:** 20, one per library. The records never say whether replicates are separate worm populations (p 0.9).
- **Genotype text for the ChIP-seq samples** drops the leading `VL648` of the record's value; that is the strain.
- **Disease is `healthy`, inferred:** no infection is named for the sequenced worms. The ChIP-seq worms were on "slow killing" plates, and no passage names the bacteria on them.
- **Units:** `μM`, `percent`, `hour`. I found no written unit list.
- **Paper sections** are `abstract`, `results`, `methods`. A paper quote is checked against the article text with italics and links removed.
- **Sex** comes only from the full text; the records are silent.
