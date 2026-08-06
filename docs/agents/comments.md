# Comments: name the idea, never the section number

**Covers.** No module — a writing rule over every surface that consumes the numbered rules, which is
most of the tree. The surfaces it is scanned on are named below.

**On every surface that CONSUMES the numbered rules, a comment may not point at a governing document
by number.** That is `src/`, `tests/`, `skills/`, `evals/` and `pyproject.toml` — the code, the thin
clients that wrap it, the corpus that pre-registers what it should decide, and the project config.
The documents that *define* the numbering are not scanned: the router, the glossary, `docs/agents/`
and `docs/adr/`. A rule table that may not name its own rules is not a rule table.

Three shapes are forbidden, and
`tests/test_repo_invariants.py::test_no_comment_points_at_a_governing_document_by_number` fails on
any of them:

- the section sign, in any form — `design §4.1`, `§12`, `brief §9`. It has no domain meaning here,
  so it is forbidden outright.
- the same pointer with the sign transliterated to a bare capital `S` — `brief S9`, `design S4.1`.
  Transliterating a forbidden character is not a way around the rule: the one pointer that outlived
  the first sweep was spelled this way, and it named two documents that had already been deleted.
  Only behind a governing-document word, so `Table S12` and `..._S1_L001_...` stay untouched.
- a rule citation — `(R7)`, `rule R5`, `per R10`, `R6:`. The guard matches a bare `R` plus a rule
  number of four or above, and the `rule R<n>` / `per R<n>` phrasings at any number.

**Write the idea instead.** A number is a mutable label: renumber the document and the comment lies,
with nothing to notice. A comment that only carried a pointer gets deleted; a comment that carried an
explanation keeps the explanation and loses the number — "the read budget is two-part, so a function
holding one bound cannot enforce it" says everything the citation did and survives a renumbering.
`CONTEXT.md` is the glossary: *the read budget*, *a Blocker*, *the byte resolver*, *a benign twin*
are all defined terms, and one of them is almost always the thing you meant.

**`R1`, `R2`, `R3` and `I1`/`I2` are read designations and must never be swept.** `R1 = CB + UMI`,
`--readFilesIn R2,R1`, `..._S1_L001_R1_001.fastq.gz`, `library.read_layout.R1.length`, the sacCer3
genome-build token and a `daf-2 R3` replicate label are all legitimate, which is why the guard leaves
the low numbers alone except in a `rule`/`per` phrasing. `tests/test_docs.py` is the one exempt file:
the rule ids are the data it tests, not a pointer.
