"""Tests for the hook guards — **does each one actually fire, and does it stay out of the way?**

Two failure modes, and they are not symmetric.

A guard that never fires is indistinguishable from a guard that always allows: it is the worst
outcome, because the rule *looks* enforced. Every deny-case below exists to prove the mechanism
engages. But a guard that fires on everything is nearly as bad in practice — it gets disabled within
a day, and then nothing is enforced either. So each rule is tested from both sides: the thing it must
stop, and the neighbouring thing it must not.
"""

from __future__ import annotations

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
