#!/usr/bin/env bash
# THE SELECTION RULE, EXECUTABLE. Rebuilds this case's fingerprint package from the archive.
#
#     sort the 1440 runs SRP383998 resolves to by `run_accession` ascending, take the first 96.
#
# That is the whole rule, and it reads exactly one archive-declared field. It yields the contiguous
# block SRR19884905-SRR19885000: 96 runs, 96 SRX, 96 BioSamples (this deposit is strictly 1:1:1:1).
# Nothing measured from the bytes enters it — no read count, no tag fraction, no verdict — because a
# plate picked to contain the cell that makes a rule fire measures our selection and not the
# compiler (#258). The sort is load-bearing rather than decorative: ENA's filereport does NOT
# return these rows in accession order, so "the first 96 ENA hands back" is not reproducible and
# "the 96 lexicographically-lowest run accessions" is.
#
# This is a SHELL script wrapping the two functions `seqforge preflight --accession
# --multi-experiment` itself calls, because the verb takes one accession and packages every run it
# resolves to — there is no way to say "96 of 1440" on the command line, and adding a flag for one
# fixture was declined. The one line between the two calls IS the selection rule, which is why this
# ships as something you run rather than as a sentence you trust.
#
# ~16 min wall and ~0.8 GB over the wire for a ~31 MB package, measured 2026-08-04 on one cell
# (10.2 s, 328 KB, 8.5 MB pulled) x 96. Nothing lands on disk: it is a bounded .sra spot stream.
#
#     ./build-package.sh /some/scratch/dir
#
set -euo pipefail

WORKSPACE="${1:?usage: build-package.sh <workspace-dir>}"

python - "$WORKSPACE" <<'PY'
import json
import sys

from seqforge.io.sra import build_fingerprint_sra, resolve_package_runs

ACCESSION = "SRP383998"  # PRJNA853582. NOT the GEO SubSeries — that is a different resolution path.
CELLS = 96
READS = 2000  # DEFAULT_MAX_READS: exactly what resolve reads, so the package reproduces the hash.
NAME = "GSE207085-nasal-prox1-96cells"

runs = resolve_package_runs(ACCESSION, multi_experiment=True)
selected = sorted(runs, key=lambda r: r["run_accession"])[:CELLS]
result = build_fingerprint_sra(selected, workspace=sys.argv[1], reads=READS, name=NAME)
print(
    json.dumps(
        {
            "accession": ACCESSION,
            "n_runs_declared": len(runs),
            "n_runs_selected": len(selected),
            "first_run": selected[0]["run_accession"],
            "last_run": selected[-1]["run_accession"],
            "package": str(result.package),
            "package_bytes": result.package_bytes,
            "n_files": len(result.manifest.files),
            "total_reads_written": result.total_reads_written,
        },
        indent=2,
    )
)
PY
