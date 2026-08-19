# Changelog

Versioning is **CalVer `YYYY.M.PATCH`** — year, month without zero-padding, then a patch counter that
increments per release within the month and resets when the month changes. The version tracks
`[project].version` in `pyproject.toml`.

## Unreleased

**A plate report says what each well produced, not only how its reads were lost.** Every per-cell
number on the page was an account of a fragment that failed to reach a gene — unmapped, multiply
placed, no feature, no UMI. The page now leads with two more: how many molecules the cell counted
into genes, and how many genes hold at least one of them. Both are counted over the combined
exon + intron matrix, so the molecule total and the sequencing saturation printed beside it are two
readings of one number rather than two figures that have to be trusted to agree. On a chimeric
plate both render once per Component, so two organisms in one well are reported side by side and
never averaged. The numbers ride the h5ad the counter already writes, as the `n_umis` and
`genes_detected` columns; a plate counted before this release still renders, two columns narrower.

- **The gene column names the region it counted.** "Genes detected" became "Genes (exon)" or
  "Genes (combined)" on the droplet page, according to what the run was quantified over. STARsolo's
  `Gene` counts exons alone and every `GeneFull` variant counts the whole gene body, so one word was
  already covering two different measurements — and a reader comparing them would have read the gap
  as biology rather than as which part of a gene was counted.
- **Recompiling is required to pick this up.** The workflow stamp moves to `2026.8.18`, which is
  folded into `run_id`, so a dataset compiled before this release compiles into a fresh pipeline
  directory. Nothing had been produced under the previous stamp yet, so no existing result set is
  orphaned by it.
