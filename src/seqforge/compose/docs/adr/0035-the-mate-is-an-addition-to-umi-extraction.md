# 35. The mate is an addition to UMI extraction, not half of it

The extraction happens entirely within the tagged read — anchor, cut, trim the span — and the mate
only inherits the resulting `UB`, so single-end is the base case and one verb either takes a mate
or does not. A second verb for the single-end form lost twice over: there is no second operation to
justify it, and a snakemake `shell:` block is a static string, so a module choosing between two
verbs must render the whole command line through `params` or split one rule into two under a
`ruleorder`. A `paired:` key lost because `read_files_in` already states the fact, and a key every
plate owed would widen what the module demands in order to widen what it accepts.
