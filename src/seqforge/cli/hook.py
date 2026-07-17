"""`seqforge hook` -- the agent hooks (pre/post-tool-use, stop) as mechanism, plus install/check."""

from __future__ import annotations

import json
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


@hook_app.command("check")
def hook_check(
    workspace: Path = typer.Option(Path("."), "-C", "--workspace", help="Root holding seqforge/."),
) -> None:
    """Self-test: prove each guard fires. A guard nobody has watched deny is not a guard.

    A hook that silently never fires is indistinguishable from one that always allows — so this
    exercises every rule against a known-bad payload and reports what it caught.
    """
    from ..hooks import HOOKS_VERSION, pre_tool_use, questions_outstanding

    cases = [
        (
            "unbounded FASTQ",
            {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | wc -l"}},
        ),
        (
            "allows a bounded stream",
            {"tool_name": "Bash", "tool_input": {"command": "zcat big.fastq.gz | head -n 400"}},
        ),
        (
            "allows the seqforge verb",
            {"tool_name": "Bash", "tool_input": {"command": "seqforge probe big.fastq.gz"}},
        ),
        (
            "absolute path in manifest",
            {
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "manifest.yaml",
                    "file_text": "genome: /data/ref/hg38.fa\n",
                },
            },
        ),
    ]
    results = []
    for name, payload in cases:
        denial = pre_tool_use(payload)
        results.append(
            {"case": name, "denied": denial is not None, "rule": denial.rule if denial else None}
        )
    typer.echo(
        json.dumps(
            {
                "hooks_version": HOOKS_VERSION,
                "open_questions": [str(p) for p in questions_outstanding(workspace)],
                "checks": results,
            },
            indent=2,
        )
    )


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
