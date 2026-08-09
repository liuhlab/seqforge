"""`seqforge hook` -- the agent hooks (pre/post-tool-use, stop) as mechanism, plus install/check."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from pydantic import ValidationError

from ..manifest import FillError, exit_code_for_report, validate_manifest
from ._common import _load_manifest
from .root import hook_app


@hook_app.command("pre-tool-use")
def hook_pre_tool_use() -> None:
    """Deny an unbounded FASTQ stream or an absolute path in a manifest.

    Reads the hook payload on stdin, emits a permissionDecision on stdout. Exit 0 always: the decision
    travels in the JSON, and a crashing guard must never wedge the agent.
    """
    from ..hooks import pre_tool_use

    payload = _hook_payload()
    denial = pre_tool_use(payload)
    if denial is None:
        raise typer.Exit(0)
    typer.echo(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": denial.message(),
                }
            }
        )
    )


@hook_app.command("post-tool-use")
def hook_post_tool_use() -> None:
    """After any manifest edit, re-run `manifest validate`. The model does not grade its own work."""
    from ..hooks import post_tool_use_targets

    payload = _hook_payload()
    target = post_tool_use_targets(payload)
    if target is None or not Path(target).is_file():
        raise typer.Exit(0)
    try:
        manifest = _load_manifest(Path(target))
        report = validate_manifest(manifest)
    except (FillError, ValidationError, ValueError, OSError) as exc:
        typer.echo(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"{target} did not parse as a Manifest: {exc}",
                }
            )
        )
        raise typer.Exit(0) from None
    if report.ok:
        typer.echo(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"manifest validate: OK ({Path(target).name}).",
                    }
                }
            )
        )
        raise typer.Exit(0)
    codes = [str(getattr(b.code, "value", b.code)) for b in report.blockers]
    typer.echo(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"manifest validate FAILED on {Path(target).name} (exit "
                    f"{exit_code_for_report(report)}): {codes}. Refusal is the contract — fix "
                    "the manifest; do not proceed as though it validated."
                ),
            }
        )
    )


@hook_app.command("stop")
def hook_stop(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding seqforge/."),
) -> None:
    """Refuse to end the turn while questions.md is non-empty — ambiguity routes to a human."""
    from ..hooks import stop_decision

    payload = _hook_payload()
    reason = stop_decision(payload, workspace=workspace)
    if reason is None:
        raise typer.Exit(0)
    typer.echo(json.dumps({"decision": "block", "reason": reason}))


@hook_app.command("install")
def hook_install(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Project root."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing hooks block."),
) -> None:
    """Write the three hooks into .claude/settings.json, merging with whatever is already there."""
    from ..hooks import HOOKS_VERSION

    settings_path = Path(workspace) / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, object] = {}
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            typer.echo(f"{settings_path} is not valid JSON: {exc}", err=True)
            raise typer.Exit(1) from exc
    existing = settings.get("hooks")
    if existing and not force:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "reason": f"{settings_path} already defines hooks; re-run with --force to replace",
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(2)

    run = "${CLAUDE_PROJECT_DIR}/.claude/hooks/seqforge-hook.sh"
    settings["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Bash|Write|Edit|NotebookEdit|Read|Grep|Glob",
                "hooks": [{"type": "command", "command": f"{run} pre-tool-use", "args": []}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit|NotebookEdit",
                "hooks": [{"type": "command", "command": f"{run} post-tool-use", "args": []}],
            }
        ],
        "Stop": [{"hooks": [{"type": "command", "command": f"{run} stop", "args": []}]}],
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    shim = Path(workspace) / ".claude" / "hooks" / "seqforge-hook.sh"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by `seqforge hook install`. The guard logic lives in seqforge.hooks (typed +\n"
        "# tested); this only routes the event. Fails OPEN by design: a broken hook must not wedge\n"
        "# the agent, and a guard that can hang is worse than the risk it manages.\n"
        "set -uo pipefail\n"
        'cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0\n'
        'exec pixi run -q -- python -m seqforge.cli hook "$@" 2>/dev/null || exit 0\n'
    )
    shim.chmod(0o755)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "hooks_version": HOOKS_VERSION,
                "settings": str(settings_path),
                "shim": str(shim),
                "events": ["PreToolUse", "PostToolUse", "Stop"],
            },
            indent=2,
        )
    )


#: The self-test's cases: a payload, and whether the shipped guards MUST deny it.
#:
#: Two of the four are allow-cases, and they are not filler. A guard that denies everything is as
#: broken as one that denies nothing, and it is the failure a person reaches for a hook to prevent:
#: `zcat x.fastq.gz | head -n 400` is the bounded read the rule exists to permit, and `seqforge probe`
#: is the verb that does the bounded read for you. An expectation per case is what lets this verb have
#: a VERDICT — before that it printed a `denied` column for a human to read, and an all-false column
#: (every guard dead) exited 0 exactly like a clean run.
_CHECK_CASES: tuple[tuple[str, dict[str, object], bool], ...] = (
    (
        "denies an unbounded FASTQ stream",
        {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | wc -l"}},
        True,
    ),
    (
        "allows a bounded stream",
        {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | head -n 400"}},
        False,
    ),
    (
        "allows the seqforge verb",
        {"tool_name": "Bash", "tool_input": {"command": "seqforge probe big.fastq.gz"}},
        False,
    ),
    (
        "denies an absolute path in a manifest",
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "manifest.yaml",
                "file_text": "genome: /data/ref/hg38.fa\n",
            },
        },
        True,
    ),
)

#: How long one shim invocation may take. The shim starts a `pixi run`, so seconds rather than
#: milliseconds is normal; a shim that exceeds this is reported as a failure and not waited on,
#: because the thing being checked is a guard that must never wedge an agent.
_SHIM_TIMEOUT_S = 90


@hook_app.command("check")
def hook_check(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding seqforge/."),
) -> None:
    """Self-test: prove each guard fires **through the hooks as installed here**.

    A hook that silently never fires is indistinguishable from one that always allows. That was true
    of this verb too until #348: it imported `pre_tool_use` and printed what each payload did, with no
    expectation to compare against and exit 0 whatever came back — so the thing whose whole job is to
    demonstrate the guards work could not itself fail.

    **It runs the shim, not the library, and that is the point.** `seqforge.hooks` already has unit
    tests, and they are better than this: faster, hermetic, and they cover cases this cannot. What
    nothing else can tell you is whether the hooks are live *in this checkout* — whether
    `settings.json` names the shim, whether the shim is executable, whether the environment it starts
    can import seqforge at all. The shim FAILS OPEN by design, because a broken hook must not wedge
    the agent, which means every one of those failures looks exactly like a tool call nobody objected
    to. Reaching past it to the library would check the half that is already proven and skip the half
    that silently degrades.

    Exit codes: ``0`` every case matched its expectation, ``1`` one did not, ``2`` the hooks are not
    installed in this workspace (run `seqforge hook install`).
    """
    import subprocess

    from ..hooks import HOOKS_VERSION, questions_outstanding

    shim = Path(workspace) / ".claude" / "hooks" / "seqforge-hook.sh"
    settings = Path(workspace) / ".claude" / "settings.json"
    installed = _hooks_declared(settings) and shim.is_file() and os.access(shim, os.X_OK)
    if not installed:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "installed": False,
                    "reason": (
                        f"no seqforge hooks are installed under {Path(workspace) / '.claude'}: "
                        f"`{settings}` must declare them and `{shim}` must be executable. Run "
                        f"`seqforge hook install`."
                    ),
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(2)

    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(Path(workspace).resolve())}
    results: list[dict[str, object]] = []
    for name, payload, must_deny in _CHECK_CASES:
        try:
            proc = subprocess.run(
                [str(shim), "pre-tool-use"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                cwd=str(workspace),
                env=env,
                timeout=_SHIM_TIMEOUT_S,
            )
            # The shim prints a permissionDecision only when a guard objects; silence is an allow, and
            # silence is ALSO what a shim that failed open produces. That ambiguity is not resolvable
            # from here and does not need to be: either way this installation did not deny, which is
            # the whole claim.
            denied = _denied(proc.stdout)
            note = None
        except subprocess.TimeoutExpired:
            denied = False
            note = f"the shim did not answer within {_SHIM_TIMEOUT_S}s"
        except OSError as exc:
            denied = False
            note = f"the shim could not be run: {exc}"
        results.append(
            {
                "case": name,
                "expected": "deny" if must_deny else "allow",
                "got": "deny" if denied else "allow",
                "ok": denied == must_deny,
                **({"note": note} if note else {}),
            }
        )

    ok = all(r["ok"] for r in results)
    typer.echo(
        json.dumps(
            {
                "ok": ok,
                "installed": True,
                "hooks_version": HOOKS_VERSION,
                "shim": str(shim),
                "open_questions": [str(p) for p in questions_outstanding(workspace)],
                "checks": results,
            },
            indent=2,
        )
    )
    if not ok:
        raise typer.Exit(1)


def _denied(stdout: str) -> bool:
    """Did the shim answer with a deny decision?

    Parsed rather than grepped: the shim's stdout is the hook protocol's own JSON, and a substring
    match would count the word appearing in a *reason* string as a decision. Unparseable output is not
    a denial — the runtime would not read it as one either.
    """
    try:
        answer = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return False
    specific = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
    return isinstance(specific, dict) and specific.get("permissionDecision") == "deny"


def _hooks_declared(settings: Path) -> bool:
    """Does ``settings.json`` route any event at the seqforge shim?

    Read rather than assumed: a `.claude/settings.json` that exists but declares another tool's hooks
    (or was hand-edited down to none) leaves the shim on disk and unreferenced, which is precisely the
    silently-not-installed state this verb exists to name.
    """
    try:
        declared = json.loads(settings.read_text() or "{}").get("hooks")
    except (OSError, json.JSONDecodeError):
        return False
    return "seqforge-hook.sh" in json.dumps(declared) if declared else False


def _hook_payload() -> dict[str, object]:
    """Read the hook event from stdin. A malformed payload means NO OPINION, never a crash."""
    import sys

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
