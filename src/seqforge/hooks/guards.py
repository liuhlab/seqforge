"""Hook guards — the rules turned into mechanism (design §4.2).

`CLAUDE.md` says R3 (never read a whole FASTQ), R9 (no absolute paths in a manifest) and R4 (refusal
is an exit code). Written down, those are aspirations. Here they become something that actually stops
a tool call.

**The logic lives here, not in a shell script, for one reason: a guard that silently never fires is
indistinguishable from a guard that always allows — and that is the worst possible failure for a
safety check.** So the decisions are pure functions with types and tests, and the CLI is a thin
stdin/stdout shim over them. Payload parsing is deliberately forgiving (several key spellings are
accepted) because a hook that misreads one field name fails open, quietly, forever.

Three events, three jobs:

- ``PreToolUse``  — deny an unbounded FASTQ stream, and deny writing an absolute path into a manifest.
- ``PostToolUse`` — re-run ``manifest validate`` after any manifest edit; the model does not get to
  decide whether its own edit was valid (R2).
- ``Stop``        — refuse to end a turn while ``questions.md`` is non-empty; exit 4 and this hook are
  the only ways ambiguity clears, and both route to a human.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..workspace import state_dir

#: CalVer YYYY.M.PATCH. Bump when a guard's semantics change.
#: 2026.7.1 — the held-out guard is REMOVED. `PRJNA1027859` became the pilot's demo dataset, so
#: reserving it was a misunderstanding rather than a rule worth keeping; the `SEQFORGE_CASE_*` root
#: registry went with it. Deleted, not disabled: a guard nobody wants should not linger as dead code
#: that a future reader mistakes for protection.
HOOKS_VERSION = "2026.7.1"

#: Readers that will happily stream a 40 GB file to stdout.
_STREAMERS = ("cat", "zcat", "gzcat", "bzcat", "xzcat", "gunzip", "zless", "zmore")

#: A bound of any kind. `head`/`tail` cap the stream; the seqforge verbs are bounded by construction.
_BOUNDED = re.compile(
    r"(\|\s*(head|tail)\b)|(\bhead\s+-[nc])|(\btail\s+-[nc])|(--max-reads\b)|(--max-bytes\b)|(\bsed\s+-n\b.*\bq\b)",
    re.IGNORECASE,
)

_FASTQ = re.compile(r"[^\s'\"]+\.(fastq|fq)(\.gz|\.bz2|\.xz)?\b", re.IGNORECASE)

_ABS_PATH = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")

#: A URI is what R9 *wants* for data, but `s3://bucket/x.fastq.gz` contains `/bucket/x.fastq.gz`,
#: which looks exactly like an absolute path. Scrub URIs before the path scan or the guard rejects
#: the very manifests it exists to encourage — and a guard that blocks correct work gets switched off.
_URI = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)

#: Manifest/config files R9 applies to.
_MANIFEST_FILE = re.compile(r"(manifest[^/]*\.ya?ml|config\.ya?ml|units\.tsv)$", re.IGNORECASE)

#: Keys a manifest legitimately carries that look like paths but are not (assembly ids, env names).
_ALLOWED_ABS_PREFIXES = ("/dev/null",)


@dataclass(frozen=True)
class Denial:
    """A refused tool call. ``remedy`` must be actionable — a block with no way forward is a wall."""

    rule: str
    reason: str
    remedy: str

    def message(self) -> str:
        return f"{self.rule}: {self.reason}\nRemedy: {self.remedy}"


def _is_seqforge_command(command: str) -> bool:
    """Is this a sanctioned seqforge verb? Those are bounded by construction (R3) and are the API (R8).

    This is the line the guard draws: `seqforge probe` is the sanctioned, bounded, auditable path.
    `cat` on the same file is the leak. Blocking both would make the tool unusable; blocking neither
    would make the rule decorative.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    for i, tok in enumerate(parts):
        if tok in ("&&", "||", "|", ";"):
            continue
        if tok.endswith("seqforge"):
            return True
        # `pixi run -- seqforge ...` / `python -m seqforge.cli ...`
        if tok == "seqforge" or (tok == "-m" and i + 1 < len(parts) and "seqforge" in parts[i + 1]):
            return True
    return False


def check_unbounded_fastq(command: str) -> Denial | None:
    """R3: a code path that CAN stream a whole multi-GB FASTQ is a bug, not a risk to manage."""
    if not command or _is_seqforge_command(command):
        return None
    hits = _FASTQ.findall(command)
    if not hits:
        return None
    if not any(re.search(rf"\b{s}\b", command) for s in _STREAMERS):
        return None
    if _BOUNDED.search(command):
        return None
    return Denial(
        rule="R3 (never read a whole FASTQ)",
        reason=(
            "this streams a FASTQ with no read/byte bound. Wall-clock is not a budget: the file may "
            "be 40 GB and the command would happily read all of it."
        ),
        remedy=(
            "use `seqforge probe FILE --json` (bounded: 200k reads / 256 MB by construction), or if "
            "you truly need shell, bound it explicitly — e.g. `zcat f.fastq.gz | head -n 4000`."
        ),
    )


def check_absolute_path_write(file_path: str, content: str) -> Denial | None:
    """R9: a manifest with a machine-specific path is not a manifest, it is a note to one machine."""
    if not file_path or not _MANIFEST_FILE.search(file_path):
        return None
    scrubbed = _URI.sub(" ", content or "")  # a URI is the RIGHT answer here, not a violation
    for match in _ABS_PATH.finditer(scrubbed):
        path = match.group(0)
        if path.startswith(_ALLOWED_ABS_PREFIXES):
            continue
        return Denial(
            rule="R9 (machine-independent manifest)",
            reason=(
                f"{Path(file_path).name} would carry the absolute path {path!r}. The manifest must "
                "resolve on any machine; a baked path silently pins it to this one."
            ),
            remedy=(
                "reference a genome by UCSC assembly id + registered GTF name (liulab-genome), an "
                "environment by its literal liulab-runtime name, and data by a URI."
            ),
        )
    return None


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input") or payload.get("toolInput") or {}
    return value if isinstance(value, dict) else {}


def _content_of(tool_input: dict[str, Any]) -> str:
    """Write/Edit spell the payload differently; a missed key here would fail OPEN, so take them all."""
    for key in ("file_text", "content", "new_string", "new_str"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def pre_tool_use(payload: dict[str, Any]) -> Denial | None:
    """Decide a PreToolUse call. ``None`` == no opinion (the normal permission flow still applies)."""
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = _tool_input(payload)

    if tool == "Bash":
        return check_unbounded_fastq(str(tool_input.get("command") or ""))

    if tool in ("Write", "Edit", "NotebookEdit"):
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        return check_absolute_path_write(file_path, _content_of(tool_input))

    return None


def post_tool_use_targets(payload: dict[str, Any]) -> str | None:
    """The manifest a PostToolUse call just edited, if any — else ``None``.

    R2 in one line: the model does not get to decide whether its own edit was valid. If it touched a
    manifest, `manifest validate` runs, and the exit code (not the model's opinion) is the verdict.
    """
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    if tool not in ("Write", "Edit", "NotebookEdit"):
        return None
    file_path = str(_tool_input(payload).get("file_path") or "")
    if file_path and re.search(r"manifest[^/]*\.ya?ml$", file_path, re.IGNORECASE):
        return file_path
    return None


def questions_outstanding(workspace: Path) -> list[Path]:
    """Every non-empty ``questions.md`` under ``.seqforge/`` — the open-ambiguity ledger."""
    state = state_dir(workspace)
    if not state.is_dir():
        return []
    return [p for p in sorted(state.rglob("questions.md")) if p.read_text().strip()]


def stop_decision(payload: dict[str, Any], *, workspace: Path) -> str | None:
    """Refuse to end the turn while a question is open. Returns a reason, or ``None`` to allow.

    Guarded by ``stop_hook_active``: once the runtime says it has already blocked repeatedly, this
    MUST allow. A hook that blocks forever is not a safety feature, it is a hang — and the failure
    mode of "the agent can never finish" is worse than the one being prevented.
    """
    if payload.get("stop_hook_active") or payload.get("stopHookActive"):
        return None
    open_files = questions_outstanding(workspace)
    if not open_files:
        return None
    names = ", ".join(str(p) for p in open_files)
    return (
        f"{len(open_files)} open question file(s) remain: {names}. Ambiguity clears exactly two ways "
        "— a human answers, or code decides — and neither has happened. Answer them (then "
        "`seqforge resolve apply`), or say plainly that the dataset needs a human. Do not guess: an "
        "unanswered question that gets quietly resolved is how a wrong manifest reaches the corpus."
    )
