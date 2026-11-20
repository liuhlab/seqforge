# The study readings, laid out to look over (prototype)

**Throwaway.** A view-only page: one study's reading per screen, for a person to look over what a
model wrote. It collects nothing and loads nothing from the network.

```bash
pixi run python prototypes/study_reading_page/build_reading_page.py \
    evals/standard-set seqforge/standard-set/readings.html
```

The page carries record and paper text: write it to a folder git ignores, such as `seqforge/`. It
needs PyYAML and seqforge itself, which the default pixi environment has.

- **The folder** holds one subfolder per study. A study is included when it has a `reading.yaml`.
  The page also reads `study.yaml` for the summary line, `reading.verdict.yaml` for the second
  model's verdict, and `reading.notes.md` for what the format could not hold. A study without one
  of those three still gets its screen.
- **Each screen** shows the design (the factors, and a grid of sample counts for each kind of
  library), the labels every sample shares, a table of the labels that differ, the relations the
  reading implies, the verdict, and the notes.
- **A cell** shows the source's words, the ontology term, how the claim is backed and the model's
  probability. A probability under 0.8 is marked low. Choose a cell to see its quotes and where
  each was read. A cell that the second model disagrees with carries a `!`.
- **The reading is checked first** with `seqforge.study.reading.read_reading`. A reading that fails
  the check gets a screen that lists the problems and nothing else. The relations come from
  `seqforge.study.relations.relations`.
- **A link to one study** is the page address followed by `#<ID>`. `?theme=dark` or `?theme=light`
  before the `#` fixes the colours; without it the page follows the system setting.

The build prints a short JSON report: each study's sample count, any problem with its reading, and
any disagreement that names no cell on the page.
