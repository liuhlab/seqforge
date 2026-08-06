# 13. The CLI is the API — refusal is an exit code, JSON is the default, resume is implicit

The caller is a machine, so machine JSON goes to stdout and human logs to stderr with no `--json`
flag for a caller to forget; refusal is an exit code — 3 when no human answer can clear it, 4 when
one can, so an agent tells "stop and report" from "ask and retry" without parsing prose; and there
is no `--resume`, because the content-addressed cache already knows what ran and a flag could only
disagree with it silently. Nothing infinite crosses the seam either: a forbidden evidence-matrix
cell is `-inf` in memory only and ships as a tagged `{"status": "forbidden"}`, never `null`, which
would conflate a gate's refusal with a cell nobody computed and with `ABSTAIN`.

**Status.** Absorbs ADR-0014 — no ±inf crosses the JSON seam; a forbidden cell is a tagged status.
