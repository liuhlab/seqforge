# Add a technology to the knowledge base

seqforge knows a sequencing technology only if its knowledge base has an entry for it. This page
walks through adding one: you describe the reads in a `spec.yaml` file, then prove the description
with seqforge's self-test. It is for a contributor who knows the assay's read structure and wants
seqforge to recognize and compile it.

The rule that makes an entry trustworthy: every entry is executable and tests itself. From your
description seqforge writes synthetic reads, reads them back with its normal code, and checks that
it recovers what you declared. You need no real data and no download for that first test.

## What an entry is

Each technology has one folder under `src/seqforge/kb/specs/`:

```text
src/seqforge/kb/specs/<technology-id>/
    spec.yaml     for the machine: the read layout, the barcode positions, how to detect it, how to align it
    README.md     for a person: how the assay works, what it is confused with, what goes wrong
```

The README becomes the assay's page on this site, so write it for a biologist. Keep the reasoning
behind each value (strand derivations, why two entries are told apart) as comments in `spec.yaml`.
No code reads the README. `spec.yaml` is what seqforge runs.

## Start from the nearest entry

There is no command that writes a blank entry. Copy the folder of the closest existing technology and
edit it:

| Your assay's cell barcode | Copy |
| --- | --- |
| One block at a fixed place in one read | `10x-3p-gex-v3` |
| Several blocks separated by fixed linker sequences | `splitseq` or `bd-rhapsody-wta` |
| Behind a variable-length insert, found by its linkers | `bd-rhapsody-wta-enhanced-v1` |

`seqforge kb list` lists every entry. `seqforge kb show <technology-id>` prints one entry as
seqforge loaded and checked it.

## Fill in `spec.yaml`

A `spec.yaml` has these parts:

| Part | What it says |
| --- | --- |
| `identity` | The id, version and name; `aliases`, the names papers use for it; `assay_ontology`, its Experimental Factor Ontology (EFO) term; `modality`, such as `rna` |
| `reads` | Each read, its length range, and its elements in order: `barcode`, `umi`, `cdna`, `linker`, and others, each with its position |
| `onlists` | The barcode lists the barcodes come from, by their registered name |
| `signature` | How to detect it: `requires` (checks that must pass), `supports` (evidence that adds to its score), `excludes` (checks that rule it out) |
| `backend` | Which pipeline aligns it, and the aligner settings the read layout fixes |
| `parent` | The family it belongs to, if any, such as `10x-3p-gex` |
| `confusable_with` | Every entry whose reads look like yours, and what tells the two apart |

Look up every value. Write none from memory.

- **The ontology term.** Check it in the lab's ontology store (`labdata ontology search efo <text>`).
  A remembered term id is usually plausible and wrong.
- **The read structure, linkers and positions.** Take them from the primary paper's oligo tables or
  from [scg_lib_structs](https://teichlab.github.io/scg_lib_structs/), and name the kit version.
  SPLiT-seq's first-round barcode sits at bases 87 to 94 of the barcode read, counting from 1, in
  the original method, and eight bases earlier in its commercial descendant. A remembered position
  can be either.
- **The strand.** Derive it from the oligo orientation and record the derivation in a comment. A
  wrong strand leaves most reads uncounted while the aligner exits with no error.
- **The aligner's barcode positions.** Leave them out. seqforge computes them from the element
  positions you declared when it writes the pipeline.

Each element has a fixed `start` and `end`, or an `anchor` to a neighbouring element. A barcode
behind a variable-length insert needs an anchor, as in the BD Rhapsody Enhanced entries.

### Say how to read the bytes, never what to count

`backend.params` holds only what the bytes decide: where the barcode starts, how long it is, which
strand. What to count (the aligner's count features) belongs in the recipe, because the same
chemistry can be used for cells or for nuclei. seqforge refuses a spec that puts a counting setting
in `backend.params`. When one did, it cost a measured 40.7% of a single-nucleus library.

### Ship the barcode lists

An entry whose barcodes are told apart by a barcode list must ship that list. Pack each list into
seqforge's own format:

```bash
seqforge io onlist pack <list.txt.gz> --name <list-name> \
  --uri <where-it-came-from> --orientation forward
```

This writes the packed list and updates the index in `src/seqforge/io/onlists/`. Commit both files.
Take the sequences from an authoritative source. Without the list, the self-test still passes, but
real reads score as bulk RNA-seq and compile to the wrong pipeline at exit 0.

## Declare what your entry is confused with

Some technologies cannot be told apart from their reads alone. 10x 3' v3 and v3.1 share the same read
layout and the same barcode list. That is a fact about the chemistry, so write it down:

```yaml
confusable_with:
  - id: some-other-tech
    relationship: processing_equivalent   # or processing_divergent
    distinguishable_by: [onlist]          # what can tell them apart
    note: >
      Why they collide, in a sentence someone can check.
```

- **`processing_equivalent`**: the two are aligned with identical settings, so the difference
  changes nothing. seqforge records both names and asks nothing.
- **`processing_divergent`**: the two need different pipelines. Something must tell them apart:
  usually a barcode list (`onlist`), otherwise the metadata (`metadata`) or a trial alignment
  (`alignment`). If nothing in the data settles it, seqforge stops and asks a person (exit 4).

You cannot skip this. The test suite makes reads from each entry and asks every other entry whether
it would claim them on the cheap checks alone. An overlap you did not declare fails the suite.
Declare each one on both entries. A plain paired-end entry, `bulk-rnaseq`, accepts almost any pair of
reads, so a new barcoded entry usually needs a `processing_divergent` edge to it, told apart by
`onlist`.

## Run the self-test

These run on your machine and need nothing else.

```bash
pixi run -- seqforge kb lint                       # every spec against the schema
pixi run -- seqforge kb roundtrip <technology-id>  # declared == recovered
pixi run -- seqforge kb show <technology-id>       # read it back as seqforge loaded it
```

- `kb lint` checks every entry against the schema. It fails at the line where a typo or an unknown
  word sits, and on any counting setting in `backend.params`. It exits 3 if any entry is invalid.
- `kb roundtrip` writes synthetic reads from your `reads` section, runs them through the same code
  that reads real files, and compares what comes back with what you declared. It exits 3 on any
  difference. A linker sequence one base longer than its window shifts every later element, and
  this is where that shows.

Then run the tests that compare entries in pairs:

```bash
pixi run -e test pytest tests/test_kb.py -k "confusable or biconditional"
```

They report each pair of entries whose reads overlap with no declared relationship. They pick up a
new entry because its folder exists, so there is nothing to register.

## Before you open a pull request

1. Check the entry against real reads from one public dataset. A wrong strand or a wrong barcode list
   passes every simulated test, because a simulation tests your description against itself.
2. Write the README for a person: what the assay is, how it is read, how seqforge tells it from its
   neighbours, what goes wrong, and a link to its scg_lib_structs page if it has one.
3. Run the repository's gate once:

    ```bash
    pixi run check
    ```

## Read next

- [Knowledge base](../kb/index.md): the technologies seqforge already knows.
- [Why you can trust the output](../how-it-works/trust.md): why seqforge asks rather than picks.
