#!/usr/bin/env bash
# THE TRANSCRIPT, EXECUTABLE — how `records.json` beside this file is derived from the archive.
#
# Two steps, and the second is the unusual one.
#
# 1. NARROW BY RECORD, which every benchmark case does and the harness does again at run time
#    (`_records_the_package_reaches`): drop every record no slice in the package can join to. 1440
#    cells in, 96 out — 1 project + 96 samples + 96 experiments + 96 runs = 289 records.
#
# 2. NARROW BY FIELD, which no other case does. A plate deposit gives every cell its own sample AND
#    experiment AND run, so 96 cells cost 289 records where a 4-sample series costs 13, and the whole
#    transcript came to 294,138 B — seven times the corpus's previous largest. This keeps an
#    allowlist and drops the archive's per-record boilerplate: 157,938 B, 46% smaller, same 289
#    records and the same six graded field checks.
#
# WHAT IS KEPT, AND WHY EACH ONE:
#   project   every attribute + study_title (GRADED as `experiment.study.title`) + study_abstract,
#             which is the only prose about the study and costs 298 B.
#   sample    taxonomy_id (GRADED, via `experiment.organism`), strain (GRADED, the 96-element
#             multiset), cell_type (GRADED on two accessions), sample_title (the cell's own name —
#             the only thing distinguishing one cell's record from another's, and what a human checks
#             a join against).
#   experiment  library_construction_protocol — the archive's own "processed by Smart-Seq3 protocol",
#             which `expected.yaml`'s header names as this case's available-but-unspent metadata
#             rung, and whose 96 identical copies are the near-identical-record shape harvest's
#             collapse exists for. AND library_strategy ("RNA-Seq"), which is the string that
#             competes with it for a chemistry hypothesis: dropping it would leave only the string
#             naming the right answer, which is tilting the case toward its own expectation.
#   run       filenames, which is half of the file -> run join (the accession is the other half).
#
# WHAT IS DROPPED, AND WHAT A READER LOSES: the submitting centre and BioSample package, `source_name`
# (a verbatim duplicate of `cell_type`), the GSM alias forms of accessions the record already carries,
# `library_source` / `library_selection` / `instrument_model` (HiSeq X Ten), `library_name`,
# `experiment_title` (a concatenation of sample_title + organism + library_strategy) and `run_alias`.
# None is graded here and none is joined on. A reader loses the instrument and the GEO alias without
# re-fetching — and `seqforge io records SRP383998` re-fetches them, which is step 0 below.
#
# THE TENSION, STATED RATHER THAN PAPERED OVER: `eval-corpus.md` calls a committed `records.json`
# "the archive's own BioSample and SRA transcript", and under `--llm` `forbidden_fields` is computed
# over the whole accepted set — so a field-narrowed transcript is a slightly different artifact from
# every other case's, and a claim about what the model did NOT say is weaker over it. This case
# declares no `assertions:` and no `forbidden_fields`, so nothing it grades depends on that. The
# reason this is a script rather than a hand-edit is exactly so the narrowing is a stated rule
# somebody can re-run, rather than a file nobody can tell from a full transcript.
#
#     seqforge io records SRP383998 -C /some/scratch        # step 0: the full 1440-cell dump
#     ./build-records.sh /some/scratch/seqforge/records/SRP383998.json <the .fingerprint.tar.gz>
#
set -euo pipefail

FULL="${1:?usage: build-records.sh <SRP383998.json> <package.fingerprint.tar.gz>}"
PACKAGE="${2:?usage: build-records.sh <SRP383998.json> <package.fingerprint.tar.gz>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python - "$FULL" "$PACKAGE" "$HERE/records.json" <<'PY'
import json
import sys
import tarfile
from pathlib import Path

from seqforge.evals.case import _records_the_package_reaches
from seqforge.models.records import ArchiveRecordSet

KEEP_ATTRIBUTES = {
    "project": {"center_name", "data_type", "submission_date"},
    "sample": {"taxonomy_id", "strain", "cell_type"},
    "experiment": {"library_strategy"},
    "run": set(),
}
KEEP_FREE_TEXT = {
    "project": {"study_title", "study_abstract"},
    "sample": {"sample_title"},
    "experiment": {"library_construction_protocol"},
    "run": set(),
}

full_path, package_path, out_path = (Path(a) for a in sys.argv[1:4])
with tarfile.open(package_path) as tar:
    slices = [Path(name) for name in tar.getnames() if name.endswith(".fastq.gz")]

records = _records_the_package_reaches(
    ArchiveRecordSet.model_validate_json(full_path.read_text()), slices
)
payload = records.model_dump(mode="json")
for record in payload["records"]:
    level = record["level"]
    record["attributes"] = [
        a for a in record["attributes"] if a["name"] in KEEP_ATTRIBUTES[level]
    ]
    record["free_text"] = [t for t in record["free_text"] if t["label"] in KEEP_FREE_TEXT[level]]
out_path.write_text(json.dumps(payload, indent=2) + "\n")

levels: dict[str, int] = {}
for record in payload["records"]:
    levels[record["level"]] = levels.get(record["level"], 0) + 1
print(json.dumps({"levels": levels, "records": len(payload["records"]),
                  "bytes": out_path.stat().st_size}))
PY
