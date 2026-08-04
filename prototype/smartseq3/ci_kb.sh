#!/bin/bash
set -uo pipefail
REPO=/Users/hanqing/src/seqforge/.claude/worktrees/agent-a6538a2d0e32ec644
DEST="$REPO/src/seqforge/kb/specs/smartseq3"
PY="/Users/hanqing/src/seqforge/.pixi/envs/default/bin/python"
cleanup() { rm -rf "$DEST"; echo "--- removed draft"; }
trap cleanup EXIT
mkdir -p "$DEST"
cp "$REPO/prototype/smartseq3/spec-draft/smartseq3/spec.yaml" "$DEST/spec.yaml"
printf '# draft\n' > "$DEST/README.md"
cd "$REPO"
export PYTHONPATH="$REPO/src"
echo "########## kb roundtrip smartseq3 ##########"
"$PY" -m seqforge.cli kb roundtrip smartseq3; echo "exit=$?"
echo "########## kb lint ##########"
"$PY" -m seqforge.cli kb lint; echo "exit=$?"
