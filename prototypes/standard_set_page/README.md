# Pictures of the standard set (prototype)

**Throwaway.** A view-only page: one small picture per study, the paper on the left and its GEO
datasets on the right. It opens on one study at a time; "All" at the top right shows every study
as a gallery. It collects nothing. Open [`example.html`](example.html); the five studies in
[`fixtures/`](fixtures/) are made up, so the links lead nowhere.

```bash
pixi run python prototypes/standard_set_page/build_page.py \
    prototypes/standard_set_page/fixtures \
    prototypes/standard_set_page/example.html \
    --lookup prototypes/standard_set_page/fixtures/_lookup.yaml \
    --notice "Example data: these studies are made up, and the links lead nowhere."
```

It needs PyYAML, which the default pixi environment has.

- **The folder** holds one subfolder per study: `<ID>/study.yaml`, `draft.yaml`, `verdict.yaml`.
- **`--lookup`** names a YAML file of dataset titles, assays, sample counts and paper titles, which
  papers GEO lists on each dataset, and which datasets each SuperSeries holds. The study files
  never hold these, so the real build would read them from the record store. Without the file the
  boxes show identifiers only.
- **Two fields the study file needs** for this picture: `summary`, one plain line saying what the
  study is, and `bounds.left_out`, a reason of about eight words for each linked record left out.
- **A link to one study** is the page address followed by `#<ID>`; `#all` opens the gallery.
