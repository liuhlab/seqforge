"""A study's files, as later stages hand them on: their written formats and the checks of them.

:mod:`.bounds` is the bounds file: which GEO Series and papers belong to one study. :mod:`.reading`
is the reading file: what each sequenced sample of the study is. :mod:`.claims` is how a claim is
written and backed in every study file, for each file kind's check to build on. :mod:`.relations`
is what a valid reading implies between its samples, worked out on demand and stored nowhere.
A format is a whole number on the file's first line, and only the current one is accepted.
"""
