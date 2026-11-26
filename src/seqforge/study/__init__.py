"""The Study context: a study's files, their checks, and the two jobs that write them.

The files and their checks:

- :mod:`.bounds` — the bounds file: which GEO Series and papers belong to one study.
- :mod:`.reading` — the reading file: what each sequenced sample of the study is.
- :mod:`.claims` — how a claim is written and backed in every study file, for each file kind's
  check to build on.
- :mod:`.relations` — what a valid reading implies between its samples, worked out on demand and
  stored nowhere.
- :mod:`.folder` — a folder of study folders: what each holds, which studies it lists, and how a
  job's file is checked and written.

A format is a whole number on the file's first line, and only the current one is accepted.

The two jobs, each a unit kind of the run engine, and what they read and ask:

- :mod:`.bounding` — the bounding job: Jev places each record of a study's linked group, and code
  writes the bounds.
- :mod:`.group` — what bounding reads from liulab-data's stores: a study's linked Series and their
  papers.
- :mod:`.reader` — the reading job: a writer lists a study's values, Jev picks each sample's, and
  code writes the reading.
- :mod:`.sources` — what the reader reads from liulab-data's stores, and where a quote is found in
  it.
- :mod:`.lists` — the open lists a writing model drafts for a reading.
- :mod:`.ontology_search` — the one door the reader asks for ontology terms through.
- :mod:`.closed` — a closed question asked of Jev: yes or no, or one of a list. Both jobs ask Jev
  through it.
"""
