# 34. A user-written record set declares structure, never a fact

Accepting the existing record type wholesale would let a hand-written YAML line carry `attributes`,
and `_basis_for` grants `asserted` to any claim a record makes about its own sample — true of an
archive, false of a human typing YAML, and such a line would outrank a harvested claim that has a
quote and a span, permanently, since `experiment` is inside `dataset_hash`. A `source: user` set
therefore declares only `level`, `id`, `parent` and `filenames`, over `run` and `sample` alone —
nothing in the tree reads an experiment level, and a lab that does know its genotypes writes them in
a README and harvests them, which keeps the span. Fusing runs the filenames would have separated is
a `Warning` and not a `Blocker`: refusing the feature's primary use case on first run teaches
callers to route around exit codes.
