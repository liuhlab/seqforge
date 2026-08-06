# 25. The module that writes a QC artifact owns reading it, and a registry names who does not

A QC number means nothing without a verdict, and a verdict is knowledge about one tool: valid
barcodes below 0.5 is the signature of a wrong chemistry call, not a low number. A per-module `if`
in the report collector would ship sooner and fail invisibly — a fourth aligner falls through,
reports nothing, and an empty results section is byte-identical to a pipeline that never started —
so every module either registers a reader beside its own writer or names itself as reporting none,
which makes the omission fail at build time. The leaf vocabulary stays out of `models/`, whose
types are exported wire shapes, so that a threshold lives in the file that also knows which
artifact key carries it.
