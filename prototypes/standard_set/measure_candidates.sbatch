#!/usr/bin/env bash
# The candidate measurement as one Slurm job, for when no held job is there to run it in.
# Submit from the folder that holds measure_candidates.py, with a work folder outside the store:
#
#   LABDATA_STORE=/path/to/store LABDATA_PAPERS=/path/to/paper-store \
#     PYTHON=/path/to/liulab-data/.pixi/envs/default/bin/python \
#     sbatch --chdir=WORK --output=WORK/sbatch/%x.%j.log measure_candidates.sbatch WORK/out.jsonl
#
# Both stores are only read. DuckDB spills to the node's own /tmp, in a folder the script
# deletes. The job can be stopped and submitted again: it keeps no state.
#SBATCH --job-name=standard-set-candidates
#SBATCH --partition=quick_preemptible
#SBATCH --time=00:30:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G

set -euo pipefail
: "${LABDATA_STORE:?set LABDATA_STORE to the GEO/SRA store root}"
: "${PYTHON:?set PYTHON to a Python that has duckdb}"
out="${1:?give the output file, outside the store}"
script="${SLURM_SUBMIT_DIR:-$PWD}/measure_candidates.py"

echo "$(date -u +%FT%TZ) job ${SLURM_JOB_ID:-local}, load average $(cut -d ' ' -f 1-3 /proc/loadavg)"
"$PYTHON" -B "$script" --tmp /tmp --threads "${SLURM_CPUS_PER_TASK:-8}" --memory 24GB --out "$out"
