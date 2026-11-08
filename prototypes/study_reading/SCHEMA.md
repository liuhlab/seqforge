# PROTOTYPE, throwaway: shared reading format

Both the hand readings (`gold/`) and the model readings (`runs/`) use this shape, so a script can
compare them field by field. One YAML file per study. Keep it this simple; add a field only if a
study needs it, and note the addition in your README.

```yaml
study:
  series: [GSE...]          # every GEO Series read, SubSeries included
  superseries: GSE... | null
  bioproject: [PRJNA...]
  pmid: [...]
  notes: "..."              # e.g. a Series left out because it re-cites older data
factors:                    # what the study varied on purpose (a DESeq2-style design)
  - name: genotype
    levels: [WT, APP/PS1]
    baseline: WT
    ordered: false          # true for a time or dose series
samples:                    # one row per GEO sample (GSM)
  - gsm: GSM...
    factors: {genotype: APP/PS1, age: "12 months"}
    biosample: B01          # minted here: the material one or more libraries were made from
    donor: D01              # the person, animal, strain or cell line
    assay: RNA-seq          # RNA-seq, ChIP-seq, ATAC-seq, CUT&RUN, ...
    target: null            # e.g. H3K27ac, CTCF; null when the assay has none
    role: signal            # signal | control
    control_type: null      # input | IgG | non-targeting | ... when role is control
    attributes:             # the study's own text; ontology id only where easy
      species: {text: "Mus musculus", id: "NCBITaxon:10090"}
      sex: {text: "male", id: "PATO:0000384"}
      age: {text: "12 months"}
      tissue: {text: "hippocampus", id: "UBERON:0002421"}
    evidence:               # per label: the quote and where it came from
      genotype: {quote: "...", source: "GSM characteristics"}
    probability: {}         # model readings only: per label, Jev's probability
    unsure: []              # hand readings: labels you could not settle, with a note
relations:                  # derived from the table; the hand reading writes them by hand
  contrasts:                # two groups that differ in exactly one factor, one side at baseline
    - {factor: genotype, test: [GSM...], baseline: [GSM...], fixed: {age: "12 months"}}
  replicates: [[GSM..., GSM...]]          # same value for every factor
  series: [{factor: age, members: [...]}] # a factor whose values are ordered numbers
  co_measurement: [[GSM..., GSM...]]      # libraries that share a biosample
  background_control: [{signal: GSM..., control: GSM...}]
```
