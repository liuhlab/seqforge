#!/bin/bash
# Gather the standard set's linked groups from liulab-data's full store.
#
# From a compute node you hold, with the three folders set:
#   export WORK=<scratch work folder> DATALAKE=<folder holding both stores> \
#          LIULAB_DATA=<liulab-data checkout with its pixi env>
#   mkdir -p "$WORK/sbatch"
#   sbatch --chdir="$WORK" --output="$WORK/sbatch/%x.%j.out" --error="$WORK/sbatch/%x.%j.err" \
#     gather_linked_groups.sbatch
# It reads the two stores and writes only under $WORK: a shallow clone of this branch, DuckDB's
# spill folder, the logs and the two outputs. A rerun starts over and replaces both outputs whole.
#SBATCH --job-name=sf615-linked-groups
#SBATCH --partition=cpu
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=01:00:00
set -euo pipefail
: "${WORK:?set WORK}" "${DATALAKE:?set DATALAKE}" "${LIULAB_DATA:?set LIULAB_DATA}"
BRANCH=task/615-gather-linked-groups
cd "$WORK"
mkdir -p sbatch out cache tmp

# Nothing may land in the home folder or the package store: caches, spill files and bytecode
# stay in the work folder.
export XDG_CACHE_HOME="$WORK/cache" TMPDIR="$WORK/tmp" PYTHONDONTWRITEBYTECODE=1
export LABDATA_STORE="$DATALAKE/geo-sra-store" LABDATA_PAPERS="$DATALAKE/paper-store"
export LABDATA_THREADS="${SLURM_CPUS_PER_TASK:-4}"

if [ -d seqforge/.git ]; then
  git -C seqforge fetch --depth 1 origin "$BRANCH"
  git -C seqforge checkout --detach FETCH_HEAD
else
  git clone --depth 1 --branch "$BRANCH" https://github.com/liuhlab/seqforge.git seqforge
fi
SEQFORGE_COMMIT="$(git -C seqforge rev-parse HEAD)"
LIULAB_DATA_COMMIT="$(git -C "$LIULAB_DATA" rev-parse HEAD)"
export SEQFORGE_COMMIT LIULAB_DATA_COMMIT
echo "job ${SLURM_JOB_ID:-none} at $(date -u +%FT%TZ); seqforge $SEQFORGE_COMMIT; liulab-data $LIULAB_DATA_COMMIT"
cat "$LABDATA_STORE/labdata-store.toml"

"$LIULAB_DATA/.pixi/envs/default/bin/python" -B \
  seqforge/scripts/research/615/gather_linked_groups.py \
  --set-dir seqforge/evals/standard-set \
  --out out/results.json --summary out/summary.tsv
cat out/summary.tsv
rm -rf tmp cache/liulab-data/duckdb
