#!/bin/bash
# Temporarily install the DRAFT spec into the real KB, run the CI gates that police a KB entry,
# then remove it. Nothing is committed under src/. Throwaway, wayfinder #234.
set -uo pipefail
REPO=/Users/hanqing/src/seqforge/.claude/worktrees/agent-a6538a2d0e32ec644
DEST="$REPO/src/seqforge/kb/specs/smartseq3"
PY="/Users/hanqing/src/seqforge/.pixi/envs/default/bin/python"

cleanup() { rm -rf "$DEST"; echo "--- removed $DEST"; }
trap cleanup EXIT

mkdir -p "$DEST"
cp "$REPO/prototype/smartseq3/spec-draft/smartseq3/spec.yaml" "$DEST/spec.yaml"
printf '# draft, prototype only\n' > "$DEST/README.md"
echo "--- installed draft at $DEST"

cd "$REPO"
export PYTHONPATH="$REPO/src"
"$PY" -c "import seqforge.kb.loader as l; print('SPECS_DIR =', l.SPECS_DIR); print('ids =', l.list_spec_ids())"
"$PY" -m pytest tests/test_kb.py tests/test_evals.py -p no:randomly -q --no-header 2>&1 | tail -70
