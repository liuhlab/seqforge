"""Tests for the hook guards — **does each one actually fire, and does it stay out of the way?**

Two failure modes, and they are not symmetric.

A guard that never fires is indistinguishable from a guard that always allows: it is the worst
outcome, because the rule *looks* enforced. Every deny-case below exists to prove the mechanism
engages. But a guard that fires on everything is nearly as bad in practice — it gets disabled within
a day, and then nothing is enforced either. So each rule is tested from both sides: the thing it must
stop, and the neighbouring thing it must not.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from seqforge.hooks import (
    check_absolute_path_write,
    check_unbounded_fastq,
    post_tool_use_targets,
    pre_tool_use,
    questions_outstanding,
    stop_decision,
)

# ---------------------------------------------------------------------------------------------
# never read a whole FASTQ
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        pytest.param("zcat sample_R1.fastq.gz | wc -l", id="zcat-wc"),
        pytest.param("cat reads.fastq", id="cat"),
        pytest.param("zcat reads.fq.gz | awk '{print}'", id="zcat-awk"),
        pytest.param("gunzip -c reads.fastq.gz > out", id="gunzip-c"),
        pytest.param("bzcat reads.fastq.bz2 | grep AAAA", id="bzcat"),
    ],
)
def test_denies_an_unbounded_fastq_stream(cmd: str) -> None:
    """Every streaming reader is blocked, and the block names its rule and a way forward."""
    d = check_unbounded_fastq(cmd)
    assert d is not None, cmd
    assert "FASTQ" in d.rule
    assert d.remedy  # a block with no way forward is a wall


@pytest.mark.parametrize(
    "cmd",
    [
        # `head` caps the read -- the neighbouring command that must NOT be blocked
        pytest.param("zcat reads.fastq.gz | head -n 4000", id="head-n"),
        pytest.param("head -c 1000000 reads.fastq", id="head-c"),
        pytest.param("zcat reads.fastq.gz | head -4", id="head-4"),
        # `seqforge probe` is bounded by construction (200k reads / 256 MB) -- blocking it is nonsense
        pytest.param("seqforge probe reads.fastq.gz --json", id="probe"),
        pytest.param("pixi run -- seqforge probe reads.fastq.gz", id="pixi-probe"),
        pytest.param("python -m seqforge.cli probe reads.fastq.gz", id="module-probe"),
        # naming a FASTQ is not streaming it, and a stream with no FASTQ is not this guard's business
        pytest.param("ls -l reads.fastq.gz", id="ls"),
        pytest.param("rm reads.fastq.gz", id="rm"),
        pytest.param("cat README.md", id="no-fastq"),
        pytest.param("zcat archive.tar.gz | tar t", id="no-fastq-stream"),
        pytest.param("", id="empty"),
    ],
)
def test_allows_a_bounded_or_sanctioned_fastq_read(cmd: str) -> None:
    """The neighbours that must pass. A guard that blocks correct work gets switched off, and then
    it guards nothing."""
    assert check_unbounded_fastq(cmd) is None, cmd


# ---------------------------------------------------------------------------------------------
# no absolute path in a manifest
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["manifest.yaml", "manifest.draft.yaml", "config.yaml", "units.tsv"]
)
def test_denies_an_absolute_path_in_every_emitted_artifact(name: str) -> None:
    """The rule reaches every artifact seqforge emits, and the denial quotes the path it caught."""
    d = check_absolute_path_write(name, "genome:\n  fasta: /scratch/ref/hg38.fa\n")
    assert d is not None, name
    assert "machine-independent" in d.rule
    assert "/scratch/ref/hg38.fa" in d.reason


@pytest.mark.parametrize(
    "content",
    [
        # `s3://bucket/x.fastq.gz` contains `/bucket/x.fastq.gz` -- scrubbing URIs is what saves it
        pytest.param("files:\n  - s3://bucket/sample_R1.fastq.gz\n", id="s3"),
        pytest.param(
            "files:\n  - https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR287/SRR28716553_1.fastq.gz\n",
            id="https",
        ),
        pytest.param("files:\n  - gs://bucket/path/to/reads.fastq.gz\n", id="gs"),
        pytest.param("files:\n  - ftp://ftp.ncbi.nlm.nih.gov/x/y.fastq.gz\n", id="ftp"),
        # the whole point: assembly id + registered GTF name + literal env name + a URI
        pytest.param(
            "genome:\n  assembly: ce11\n  annotation_name: WS298\nenvironment: align-rna\n"
            "files:\n  - s3://bucket/sample_R1.fastq.gz\n  - sample_R2.fastq.gz\n",
            id="machine-independent",
        ),
    ],
)
def test_does_not_mistake_a_uri_for_an_absolute_path(content: str) -> None:
    """Data SHOULD be a URI, so flagging one rejects the exact manifest the rule wants written."""
    assert check_absolute_path_write("manifest.yaml", content) is None, content


def test_still_catches_an_absolute_path_next_to_a_uri() -> None:
    """Scrubbing URIs must not blind the guard to a real violation beside one."""
    content = "files:\n  - s3://bucket/ok.fastq.gz\ngenome:\n  fasta: /scratch/ref/hg38.fa\n"
    d = check_absolute_path_write("manifest.yaml", content)
    assert d is not None
    assert "/scratch/ref/hg38.fa" in d.reason


def test_ignores_files_that_are_not_manifests() -> None:
    """A script may legitimately hold an absolute path; a manifest may not."""
    assert check_absolute_path_write("run.sh", "cat /scratch/ref/hg38.fa") is None
    assert check_absolute_path_write("notes.md", "see /scratch/data") is None


# ---------------------------------------------------------------------------------------------
# the PreToolUse dispatcher
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "key", "value"),
    [
        pytest.param("Bash", "command", "zcat a.fastq.gz | wc -l", id="bash"),
        pytest.param("Write", "file_text", "g: /a/b/c", id="write"),
        pytest.param("Edit", "content", "g: /a/b/c", id="content"),
        pytest.param("Edit", "new_string", "g: /a/b/c", id="new_string"),
        pytest.param("Edit", "new_str", "g: /a/b/c", id="new_str"),
        pytest.param("NotebookEdit", "file_text", "g: /a/b/c", id="notebook"),
    ],
)
def test_pre_tool_use_routes_every_event_it_owns(tool: str, key: str, value: str) -> None:
    """Write and Edit spell the payload differently; a missed tool or key would fail OPEN, silently."""
    payload = {"tool_name": tool, "tool_input": {"file_path": "manifest.yaml", key: value}}
    assert pre_tool_use(payload) is not None


def test_pre_tool_use_has_no_opinion_on_unrelated_tools() -> None:
    assert pre_tool_use({"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}) is None
    assert pre_tool_use({}) is None


# ---------------------------------------------------------------------------------------------
# PostToolUse — code decides whether the edit validated, not the model
# ---------------------------------------------------------------------------------------------


def test_post_tool_use_targets_manifest_edits_only() -> None:
    assert post_tool_use_targets(
        {"tool_name": "Write", "tool_input": {"file_path": "/w/seqforge/manifest.yaml"}}
    )
    assert post_tool_use_targets(
        {"tool_name": "Edit", "tool_input": {"file_path": "manifest.draft.yaml"}}
    )
    assert (
        post_tool_use_targets({"tool_name": "Write", "tool_input": {"file_path": "config.yaml"}})
        is None
    )
    assert post_tool_use_targets({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


# ---------------------------------------------------------------------------------------------
# Stop — ambiguity routes to a human
# ---------------------------------------------------------------------------------------------


def test_stop_blocks_while_a_question_is_open_in_any_dataset(tmp_path: Path) -> None:
    """Turn-end is refused while any dataset's ledger is open, and the reason names every one."""
    for ds in ("a", "b"):
        q = tmp_path / "seqforge" / ds / "questions.md"
        q.parent.mkdir(parents=True)
        q.write_text(f"- open in {ds}\n")
    assert len(questions_outstanding(tmp_path)) == 2
    reason = stop_decision({}, workspace=tmp_path)
    assert reason is not None
    assert reason.count("questions.md") == 2


def test_stop_allows_when_no_question_is_open(tmp_path: Path) -> None:
    """No ledger and an empty ledger are both closed; whitespace must not wedge the turn."""
    assert stop_decision({}, workspace=tmp_path) is None  # no seqforge/ tree at all
    q = tmp_path / "seqforge" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("   \n\n")
    assert stop_decision({}, workspace=tmp_path) is None
    assert questions_outstanding(tmp_path) == []


def test_stop_yields_once_the_runtime_says_it_has_blocked_enough(tmp_path: Path) -> None:
    """A hook that blocks forever is a hang, not a safety feature: `stop_hook_active` must win."""
    q = tmp_path / "seqforge" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("- unresolved\n")
    assert stop_decision({"stop_hook_active": True}, workspace=tmp_path) is None
    assert stop_decision({"stopHookActive": True}, workspace=tmp_path) is None


# `_sync_questions`, the `questions.md` writer, lives in `cli/manifest.py` -- its tests are in
# tests/test_cli.py (#113), asserting THROUGH `questions_outstanding`, the reader used above.


# ------------------------------------------------------------------------------------------------
# `hook check` -- the verb whose job is to demonstrate the guards, and which could not fail (#348)
# ------------------------------------------------------------------------------------------------
#
# These drive the verb against a FAKE shim rather than the real one. The real shim starts a
# `pixi run`, so a test of it would be slow, `external`-marked, and would prove the environment as
# much as the code. A fake is not a weaker substitute here: the claim under test is that `hook check`
# renders a correct VERDICT on whatever the installation does, and a fake is the only way to install
# a deliberately broken one. What the fakes below stand in for is measured, not invented -- the real
# shim ends in `|| exit 0` with stderr to /dev/null, so "fails open, silently" is its documented
# behaviour under every error, not a hypothetical.

_DENY = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "no"}}'


def _install_fake_shim(workspace: Path, script: str) -> None:
    """A `.claude/` that `hook check` accepts as installed, routing at a shim we control."""
    import json as _json
    import stat

    hooks = workspace / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shim = hooks / "seqforge-hook.sh"
    shim.write_text(script)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    (workspace / ".claude" / "settings.json").write_text(
        _json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/seqforge-hook.sh pre-tool-use",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )


def test_hook_check_refuses_a_workspace_where_the_hooks_are_not_installed(tmp_path: Path) -> None:
    """Exit 2, not a pass. "No hooks here" and "hooks that allow everything" look identical to an
    agent, and reporting the first as success is how a checkout runs unguarded for a week.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app

    result = CliRunner().invoke(app, ["hook", "check", "-C", str(tmp_path)])

    assert result.exit_code == 2, result.stdout
    assert "hook install" in result.stderr


def test_hook_check_fails_when_the_installed_shim_silently_allows_everything(
    tmp_path: Path,
) -> None:
    """THE failure this verb exists for, and the one it could not previously report.

    The shipped shim ends `|| exit 0` with stderr discarded, deliberately -- a broken hook must not
    wedge the agent. So every way it can break (no pixi, an env that cannot import seqforge, a
    corrupted settings file) produces exactly this: exit 0, no output, every tool call permitted. In
    process, `pre_tool_use` would answer correctly and this verb would report a clean bill of health
    for an installation that is guarding nothing.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app

    _install_fake_shim(tmp_path, "#!/usr/bin/env bash\nexit 0\n")

    result = CliRunner().invoke(app, ["hook", "check", "-C", str(tmp_path)])

    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    failed = [c for c in payload["checks"] if not c["ok"]]
    assert [c["expected"] for c in failed] == ["deny", "deny"], (
        "both deny-cases must be reported as unguarded; the allow-cases pass for the wrong reason "
        "and that is exactly why an all-allow shim needs the expectations to be caught"
    )


def test_hook_check_fails_when_the_installed_shim_denies_what_it_must_permit(
    tmp_path: Path,
) -> None:
    """The other half, and the reason two of the four cases are allow-cases.

    A guard that denies everything is as broken as one that denies nothing, and it is worse to live
    with: the bounded read (`| head -n 400`) and the `seqforge` verb are the two things the rule
    exists to permit, so a hook refusing them stops the work it was installed to make safe.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app

    _install_fake_shim(tmp_path, f"#!/usr/bin/env bash\ncat >/dev/null\necho '{_DENY}'\n")

    result = CliRunner().invoke(app, ["hook", "check", "-C", str(tmp_path)])

    assert result.exit_code == 1, result.stdout
    failed = [c for c in json.loads(result.stdout)["checks"] if not c["ok"]]
    assert [c["expected"] for c in failed] == ["allow", "allow"]


def test_hook_check_passes_against_an_installation_that_answers_correctly(tmp_path: Path) -> None:
    """The green path, driven by a shim that routes to the REAL guard the way the shipped one does.

    `python -m seqforge.cli hook` rather than `pixi run -- python -m seqforge.cli hook`: the pixi
    prefix is what makes the shipped shim slow and environment-dependent, and it is not what is under
    test here -- the routing is. That this verb reports a correct installation as correct is the claim
    the three failure tests above are calibrated against.
    """
    import sys

    from typer.testing import CliRunner

    from seqforge.cli import app

    _install_fake_shim(
        tmp_path,
        f'#!/usr/bin/env bash\nexec {sys.executable} -m seqforge.cli hook "$@"\n',
    )

    result = CliRunner().invoke(app, ["hook", "check", "-C", str(tmp_path)])

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["installed"] is True
    assert [c["got"] for c in payload["checks"]] == ["deny", "allow", "allow", "deny"]


def test_hook_install_writes_hooks_that_hook_check_then_accepts(tmp_path: Path) -> None:
    """The two verbs are one contract: what `install` writes is what `check` looks for.

    Neither had a test of any kind before #348. This pins the seam rather than either half -- an
    install that stopped writing the shim, or a check that started looking somewhere else, breaks the
    pair and not the piece. It stops short of running the shim, which is `pixi`'s to answer for.
    """
    from typer.testing import CliRunner

    from seqforge.cli import app

    result = CliRunner().invoke(app, ["hook", "install", "-C", str(tmp_path)])
    assert result.exit_code == 0, result.stdout + result.stderr

    installed = json.loads(result.stdout)
    assert installed["ok"] is True
    assert sorted(installed["events"]) == ["PostToolUse", "PreToolUse", "Stop"]

    from seqforge.cli.hook import _hooks_declared

    shim = Path(installed["shim"])
    assert shim.is_file() and os.access(shim, os.X_OK), "the shim must be executable to be a hook"
    assert _hooks_declared(Path(installed["settings"])), (
        "`hook check` reads settings.json to decide the hooks are installed; `hook install` just "
        "wrote it, so it must satisfy that reader"
    )
