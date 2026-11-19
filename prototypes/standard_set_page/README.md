# Reading page for the standard set (prototype)

**Throwaway.** It answers one question: what does "everything ready" look like for the person who
reads the drawn studies? Nothing here is production code, and the file layout it reads is provisional.

Open [`example.html`](example.html) in a browser. It is built from five made-up studies in
[`fixtures/`](fixtures/); every identifier in them is invented, so the links open empty records.

## Run it

Both scripts need Python with PyYAML, which the repo's default pixi environment has.

Write the page from a folder of study files:

```bash
pixi run python prototypes/standard_set_page/build_page.py \
    prototypes/standard_set_page/fixtures \
    prototypes/standard_set_page/example.html \
    --notice "Example data, not real studies. Every study, identifier and reason on this page is made up to show the layout, so the links open empty records."
```

For the real set, point it at `evals/standard-set` and leave `--notice` out. `--built` fixes the
"page written" time, which keeps a committed example from changing on every build.

File the answers a reader exported from the page:

```bash
pixi run python prototypes/standard_set_page/file_answers.py \
    ~/Downloads/answers-fixtures-20261119-1623-example-reader.json \
    prototypes/standard_set_page/fixtures
```

Add `--dry-run` to see what it would write. To try it without touching the fixtures, copy the
folder somewhere else first.

## What the page does

- **A list on the left, one study open on the right.** The list is grouped: studies where the judge
  disagreed with the draft, then the ones it was unsure about, then the ones where both agree. Each
  row shows the study's mark, or "to read".
- **One table per study** puts the records, the draft and the judge side by side: a row for each
  GEO Series and each paper, a column for each reader. Rows where the two differ are highlighted.
  Every identifier links out to GEO, PubMed or the DOI.
- **Below the table:** the draft's reason, the judge's reason, and what the judge had to read.
- **A bar that stays at the bottom** holds Accept, Fix, Drop and a note. Fix adds a column of tick
  boxes to the table, a box for records that are not listed, and a box for the samples of a split
  Series.
- **Keys:** `J` and `K` move, `A`, `F` and `D` mark, `N` writes a note, `U` clears, `Esc` leaves a
  text box. Accept moves on to the next unmarked study.
- **Marks are kept in the browser** under a key named after the folder. If the browser refuses to
  keep them, the page says so and still works until it is closed.
- **Export answers** saves one JSON file and shows the command that files it. A study that already
  has an answer file shows that answer when the page is rebuilt, so progress carries across sittings.

## The exported file

```json
{
  "format": "seqforge-standard-set-answers/1",
  "set": "fixtures",
  "page_built": "2026-11-19T16:00:00Z",
  "exported": "2026-11-19T16:23:45.845Z",
  "who": "Example Reader",
  "answers": [
    {
      "id": "EX02",
      "mark": "fix",
      "note": "023 belongs in part: two samples were made for this paper.",
      "who": "Example Reader",
      "when": "2026-11-19T16:23:45.845Z",
      "bounds": {
        "series": ["GSE9990021", "GSE9990022", "GSE9990023"],
        "papers": ["99900021"],
        "split_samples": {"GSE9990023": ["GSM1", "GSM2"]}
      },
      "read_against": {
        "bounds_from": "judge",
        "draft_when": "2026-11-19T09:02:40+00:00",
        "verdict_when": "2026-11-19T09:44:51+00:00"
      }
    }
  ]
}
```

`bounds` is present only for a fix. `read_against` records which draft and which verdict the
person was looking at, so an answer filed weeks later can be told apart from one about a study
that has since been redrawn. `file_answers.py` writes each entry as `<ID>.answer.yaml` with the
fields `mark`, `note`, `bounds`, `who`, `when`, `read_against` and `page_built`. It never
overwrites: the same answer is skipped, a different one is reported and left alone, and the
command exits 1.

## What I assumed about the study files

The real files are being written elsewhere, so these are guesses to correct:

- `bounds.split_samples` is a map from a Series to the samples that belong to this study.
- `bounds.papers` holds PMIDs as strings, or DOIs.
- A verdict that agrees carries no bounds of its own, and the page shows the draft's bounds for it.
- A study with no verdict file is listed as "not checked by the judge yet".
- The draft file names its pass in a field called `pass`. The page does not show it.

## Questions for the maintainer

1. **A study has no name.** No record text is stored, so the list shows only the identifier, the
   direction and the shape. Weeks later that tells the reader little. *Recommended:* the study file
   gains one short line in the drafter's own words, such as "aged mouse hippocampus,
   single-nucleus RNA", and the list shows it.
2. **Accept moves on, Fix and Drop stay.** Accept jumps to the next unmarked study. Fix and Drop
   stay and put the cursor in the note. *Recommended:* keep it; a fix or a drop without a note is
   hard to use later.
3. **A changed mind.** Filing refuses to replace an answer that is already filed. *Recommended:*
   keep refusing, and add a way to replace only when it first happens.
4. **How far Fix goes.** Tick boxes for the Series and papers, free text for anything not listed
   and for the samples of a split Series. A sample picker would need sample titles, which the repo
   does not hold. *Recommended:* free text is enough for 50 studies.
5. **`read_against` in the answer.** It is a field the ticket did not ask for. *Recommended:* keep
   it; it is the only thing that says what the person saw.
6. **Words.** The page says "draft", "judge", "bounds" and "in use", and explains them once in a
   fold-out that opens on the first visit. *Recommended:* keep them until the glossary settles what
   the bounds are called.
7. **What Drop means.** The page says "take it out of the set". It could also mean "no bounds can
   be drawn". *Recommended:* one meaning, the first; the note carries the reason.
