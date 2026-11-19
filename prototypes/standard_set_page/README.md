# Gallery of the standard set (prototype)

**Throwaway.** A view-only page: one small picture per study, the paper on the left and its GEO
datasets on the right. It collects nothing. Open [`example.html`](example.html); the five studies
in [`fixtures/`](fixtures/) are made up, so the links lead nowhere.

```bash
pixi run python prototypes/standard_set_page/build_page.py \
    prototypes/standard_set_page/fixtures \
    prototypes/standard_set_page/example.html \
    --lookup prototypes/standard_set_page/fixtures/_lookup.yaml \
    --notice "Example data: these studies are made up, and the links lead nowhere."
```

It needs PyYAML, which the default pixi environment has.

- **`--lookup`** names a YAML file of dataset titles, assays, sample counts, paper titles, and which
  papers GEO lists on each dataset. That is record text the study files never hold, so the real
  build would read it from the record store. Without it the boxes show identifiers only.
- **Two fields the study file needs** for this picture: `summary`, one plain line saying what the
  study is, and `bounds.left_out`, a short reason for each linked record that was left out.
- **The sentence under a picture** is built from the two models' files, and ends with the checking
  model's own reason, word for word. That reason has to be written for a bench reader.
