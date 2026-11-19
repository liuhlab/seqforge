"""The judging step: a large model checks one study's drafted bounds, in one headless call.

The judge is Claude run from the command line, one call per study and never two at once. It gets
the draft and the text the drafting model saw, and may open the full records and the full paper
texts, which :func:`~.material.stage` writes into a work folder outside the repo. It answers once:
agree or disagree, its own bounds when it disagrees, a reason, whether it is unsure, and what it
read. :func:`judge_study` then writes ``verdict.yaml`` and ``study.yaml`` beside the draft. Where
the judge disagrees its bounds become the study's; a study it is unsure about stays, flagged.

A judge that refuses, or a call that fails, raises :class:`JudgeStopped` and writes neither file.
Nothing here retries or rewords the request: a refusal is reported, and the study waits.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from . import DRAFT, STUDY, VERDICT
from .draft import BOUNDS_SCHEMA, RULES, bounds_problems, file_bounds
from .material import drafter_text, record_ids, stage

#: The judging model, as the command line names it.
JUDGE_MODEL = "claude-fable-5-2"
#: What the judge may say it read.
READ = ("records", "abstract", "full_paper")

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agrees": {"type": "boolean"},
        "bounds": {"anyOf": [BOUNDS_SCHEMA, {"type": "null"}]},
        "reason": {"type": "string"},
        "unsure": {"type": "boolean"},
        "read": {"type": "array", "items": {"type": "string", "enum": list(READ)}},
    },
    "required": ["agrees", "bounds", "reason", "unsure", "read"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You check the bounds another model drafted for one study of GEO sequencing data. You read "
    "files in the working folder and answer once, as the structured output asked for. You change "
    "no file."
)
_PROMPT = """\
{rules}
A smaller model drafted the bounds below from the records and the paper abstracts alone. Decide
whether the draft is right.

- "agrees" is true when the draft names the right Series, papers and sample split. Wording does not
  matter. When it is false, give your own complete bounds in "bounds", in the same shape and under
  the same rules, with a short reason for each record you leave out. When it is true, "bounds" is
  null.
- "reason" says in a few plain sentences why, naming what settled it.
- "unsure" is true when what you could read does not settle the bounds. Give your best bounds
  anyway and say in "reason" what is missing.
- "read" lists what you had to read to decide: "records", "abstract", "full_paper". List
  "full_paper" only if the abstract and the records did not settle it and you opened a paper's
  full text.

The working folder holds what you may open; read as much as you need and no more:
{files}

=== THE DRAFT ===
{draft}
{malformed}
=== WHAT THE DRAFTING MODEL SAW ===
{material}"""


class JudgeStopped(Exception):
    """The judge gave no verdict: it refused, the call failed, or its answer cannot be used."""


def ask_claude(prompt: str, folder: Path) -> dict[str, Any]:
    """Run the judge once in ``folder`` and return the command line's result object.

    It may read files under ``folder`` and nothing else, runs no command and reaches no network.
    """
    command = [
        "claude",
        "-p",
        "--model",
        JUDGE_MODEL,
        "--restricted",
        "--strict-mcp-config",
        "--tools",
        "Read,Grep,Glob",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(VERDICT_SCHEMA),
        "--system-prompt",
        _SYSTEM,
    ]
    try:
        done = subprocess.run(
            command,
            input=prompt,
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JudgeStopped(f"the judge could not be run: {exc}") from exc
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise JudgeStopped(
            f"the judge printed no result (exit {done.returncode}): {done.stderr.strip()[:500]}"
        ) from None
    if not isinstance(result, dict):
        raise JudgeStopped("the judge printed something that is not a result object")
    return result


def _verdict_from(result: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """The judge's answer and the model that wrote it, or :class:`JudgeStopped` saying why not."""
    if result.get("stop_reason") == "refusal" or result.get("safety_stops"):
        raise JudgeStopped(
            "the judge refused this study. Nothing was written and nothing was retried: "
            "report it, and judge the study with the fallback model."
        )
    answer = result.get("structured_output")
    if result.get("is_error") or not isinstance(answer, dict):
        raise JudgeStopped(f"the judge gave no verdict: {str(result.get('result'))[:500]}")
    used = result.get("modelUsage") or {}
    model = max(used, key=lambda name: used[name].get("outputTokens", 0), default=JUDGE_MODEL)
    return answer, str(model)


def judge_study(
    entry: Mapping[str, Any],
    group: Mapping[str, Any],
    *,
    set_dir: Path,
    work: Path,
    store_build: str | None,
    when: dt.datetime,
) -> dict[str, Any]:
    """Judge one study's draft, write its verdict and study files, and return a short account.

    ``entry`` is the study's row of the pick and ``group`` its linked group. ``work`` is where the
    judge's reading material, its prompt and its raw result go; record text lands there and never
    in ``set_dir``. Raises :class:`JudgeStopped`, having written neither file, when the study has
    no draft, already has a verdict, or the judge gives no usable one.
    """
    key = str(entry["id"])
    folder = set_dir / key
    if not (folder / DRAFT).exists():
        raise JudgeStopped(f"{key} has no draft: run the drafting pass and write its draft first")
    if (folder / VERDICT).exists() or (folder / STUDY).exists():
        raise JudgeStopped(f"{key} is already judged: no file is overwritten")
    draft = yaml.safe_load((folder / DRAFT).read_text())

    work.mkdir(parents=True, exist_ok=True)
    files = stage(group, work)
    malformed = ""
    if draft.get("checks_failed"):
        malformed = (
            "\nThe draft is malformed, so it cannot stand as written: "
            + "; ".join(draft["checks_failed"])
            + "\n"
        )
    prompt = _PROMPT.format(
        rules=RULES,
        files="\n".join(f"- {name}" for name in files),
        draft=yaml.safe_dump(
            {"summary": draft["summary"], "bounds": draft["bounds"]}, sort_keys=False, width=100
        ),
        malformed=malformed,
        material=drafter_text(group),
    )
    (work / "prompt.txt").write_text(prompt)
    result = ask_claude(prompt, work)
    (work / "result.json").write_text(json.dumps(result, indent=2))
    answer, model = _verdict_from(result)

    own = None if answer["agrees"] or answer["bounds"] is None else answer["bounds"]
    if not answer["agrees"] and own is None:
        raise JudgeStopped(f"the judge disagreed about {key} but gave no bounds of its own")
    bounds = draft["bounds"] if own is None else file_bounds(own)
    as_answered = {
        **bounds,
        "left_out": [{"id": k, "reason": v} for k, v in bounds["left_out"].items()],
        "split_samples": [{"series": k, "samples": v} for k, v in bounds["split_samples"].items()],
    }
    if problems := bounds_problems(as_answered, group):
        raise JudgeStopped(
            f"the bounds the judge left for {key} are malformed: {'; '.join(problems)}"
        )

    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    verdict = {
        "id": key,
        "model": model,
        "when": stamp,
        "agrees": bool(answer["agrees"]),
        "bounds": None if own is None else bounds,
        "reason": " ".join(answer["reason"].split()),
        "unsure": bool(answer["unsure"]),
        "read": [name for name in READ if name in answer["read"]],
    }
    study = {
        "id": key,
        "summary": draft["summary"],
        "direction": entry["direction"],
        "also_fits": list(entry.get("also_fits") or []),
        "hard_shape": entry.get("hard_shape"),
        "reason_picked": entry["reason_picked"],
        "store_build": store_build,
        "records": record_ids(group),
        "bounds": bounds,
        "bounds_from": "draft" if own is None else "judge",
        "status": "unsure" if verdict["unsure"] else "settled",
        "read_to_decide": verdict["read"],
    }
    for name, body in ((VERDICT, verdict), (STUDY, study)):
        (folder / name).write_text(yaml.safe_dump(body, sort_keys=False, width=100))
    return {
        "id": key,
        "model": model,
        "agrees": verdict["agrees"],
        "unsure": verdict["unsure"],
        "bounds_from": study["bounds_from"],
        "status": study["status"],
        "read": verdict["read"],
        "cost_usd": result.get("total_cost_usd"),
        "seconds": None if result.get("duration_ms") is None else result["duration_ms"] / 1000,
        "turns": result.get("num_turns"),
        "work": str(work),
    }
