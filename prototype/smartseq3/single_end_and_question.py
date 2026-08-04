"""Two loose ends the ticket names explicitly.

  1. The QUESTION the shallowest cell now asks under the draft (cell 1291, 901 reads) — verbatim.
  2. SINGLE-END Smart-seq3 is published (50 bp and 75 bp SE). What does the two-read draft do when
     handed one file? And is the `read_count` TEST what refuses it, or the reads block?

Throwaway prototype for wayfinder #234.
"""

from __future__ import annotations

import glob
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SP = "/private/tmp/claude-501/-Users-hanqing-src-seqforge/e6d5af24-455c-4dc0-aa28-ccb45f262d44/scratchpad"
sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

from seqforge import kb  # noqa: E402
from seqforge.fingerprint.load import load_fingerprint, probed_from_fingerprint  # noqa: E402
from seqforge.io import DEFAULT_REGISTRY  # noqa: E402
from seqforge.kb.schema import Spec  # noqa: E402
from seqforge.resolve import resolve_dataset  # noqa: E402

draft = Spec.model_validate(
    yaml.safe_load((Path(__file__).parent / "spec-draft" / "smartseq3" / "spec.yaml").read_text())
)
specs = {**kb.load_all_specs(), "smartseq3": draft}


def resolve(paths, probed):
    with tempfile.TemporaryDirectory(prefix="sf-se-") as td:
        return resolve_dataset(
            paths, registry=DEFAULT_REGISTRY, specs=specs,
            workspace=Path(td), use_cache=False, _probed=probed,
        )


print("### 1. the shallowest cell's question, verbatim (cell 1291, 901 reads) ###")
root = glob.glob(f"{SP}/cells/*-1291-*")[0]
loaded = load_fingerprint(root)
paths, probed = probed_from_fingerprint(loaded)
out = resolve(paths, probed)
print("candidates:", [(c.technology, c.score.value) for c in out.result.candidates[:4]])
print("rung:", out.result.rung_reached)
print(json.dumps([q.model_dump(mode="json") for q in out.result.questions], indent=1))

print("\n### 2. single-end: the same cell's R1 alone ###")
r1 = [p for p in paths if str(p).endswith("_1.fastq.gz")]
out_se = resolve(r1, {str(p): probed[str(p)] for p in r1})
print("candidates:", [(c.technology, c.score.status, c.score.value) for c in out_se.result.candidates[:4]])
print("blockers:", json.dumps([b.model_dump(mode="json") for b in out_se.result.blockers], indent=1))
print("questions:", len(out_se.result.questions))

print("\n### 3. is `read_count` the thing that refuses SE, or the reads block? ###")
print("`evaluate()` on a ReadCount test returns:", end=" ")
from seqforge.kb.schema import ReadCount  # noqa: E402
from seqforge.resolve.evaluators import evaluate  # noqa: E402
from seqforge.resolve.window import WindowProbe  # noqa: E402

obs, seqs = probed[str(r1[0])]
wp = WindowProbe(observation=obs, seqs=seqs)
ev = evaluate(ReadCount(test="read_count", roles=2), draft.reads[0], wp, draft, DEFAULT_REGISTRY)
print(f"outcome={ev.outcome} score={ev.score} detail={ev.detail!r}")
print("-> `read_count` never gates anything; the 2-file demand comes from declaring two READS")
print("   (injective assignment leaves R2 unfillable), which no `min_rate` or gate choice can relax.")
