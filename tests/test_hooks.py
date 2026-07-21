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


def test_denies_an_unbounded_fastq_stream() -> None:
    d = check_unbounded_fastq("zcat sample_R1.fastq.gz | wc -l")
    assert d is not None
    assert "FASTQ" in d.rule
    assert d.remedy  # a block with no way forward is a wall


def test_denies_every_streaming_reader() -> None:
    for cmd in (
        "cat reads.fastq",
        "zcat reads.fq.gz | awk '{print}'",
        "gunzip -c reads.fastq.gz > out",
        "bzcat reads.fastq.bz2 | grep AAAA",
    ):
        assert check_unbounded_fastq(cmd) is not None, cmd


def test_allows_a_bounded_stream() -> None:
    """`head` caps the read. This is the neighbouring command that must NOT be blocked."""
    for cmd in (
        "zcat reads.fastq.gz | head -n 4000",
        "head -c 1000000 reads.fastq",
        "zcat reads.fastq.gz | head -4",
    ):
        assert check_unbounded_fastq(cmd) is None, cmd


def test_allows_the_sanctioned_seqforge_verb() -> None:
    """`seqforge probe` is bounded by construction (200k reads / 256 MB) — blocking it is nonsense."""
    for cmd in (
        "seqforge probe reads.fastq.gz --json",
        "pixi run -- seqforge probe reads.fastq.gz",
        "python -m seqforge.cli probe reads.fastq.gz",
    ):
        assert check_unbounded_fastq(cmd) is None, cmd


def test_ignores_commands_with_no_fastq() -> None:
    assert check_unbounded_fastq("cat README.md") is None
    assert check_unbounded_fastq("zcat archive.tar.gz | tar t") is None
    assert check_unbounded_fastq("") is None


def test_does_not_fire_on_merely_naming_a_fastq() -> None:
    """Naming a file is not streaming it — `ls` and `rm` must pass."""
    assert check_unbounded_fastq("ls -l reads.fastq.gz") is None
    assert check_unbounded_fastq("rm reads.fastq.gz") is None


# ---------------------------------------------------------------------------------------------
# no absolute path in a manifest
# ---------------------------------------------------------------------------------------------


def test_denies_an_absolute_path_in_a_manifest() -> None:
    d = check_absolute_path_write("manifest.yaml", "genome:\n  fasta: /scratch/ref/hg38.fa\n")
    assert d is not None
    assert "machine-independent" in d.rule
    assert "/scratch/ref/hg38.fa" in d.reason


def test_covers_every_emitted_artifact() -> None:
    for name in ("manifest.yaml", "manifest.draft.yaml", "config.yaml", "units.tsv"):
        assert check_absolute_path_write(name, "p: /data/x/y.fa") is not None, name


def test_allows_a_machine_independent_manifest() -> None:
    """The whole point: assembly id + registered GTF name + literal env name + a URI."""
    good = (
        "genome:\n  assembly: ce11\n  annotation_name: WS298\n"
        "environment: align-rna\n"
        "files:\n  - s3://bucket/sample_R1.fastq.gz\n"
        "  - sample_R2.fastq.gz\n"
    )
    assert check_absolute_path_write("manifest.yaml", good) is None


def test_does_not_mistake_a_uri_for_an_absolute_path() -> None:
    """Regression: `s3://bucket/x.fastq.gz` contains `/bucket/x.fastq.gz`.

    Data SHOULD be a URI, so flagging one rejects the exact manifest the rule wants. A guard
    that blocks correct work gets switched off, and then it guards nothing.
    """
    for uri in (
        "s3://bucket/sample_R1.fastq.gz",
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR287/SRR28716553_1.fastq.gz",
        "gs://bucket/path/to/reads.fastq.gz",
        "ftp://ftp.ncbi.nlm.nih.gov/x/y.fastq.gz",
    ):
        assert check_absolute_path_write("manifest.yaml", f"files:\n  - {uri}\n") is None, uri


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


def test_pre_tool_use_routes_bash_and_write() -> None:
    assert (
        pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "zcat a.fastq.gz | wc -l"}})
        is not None
    )
    assert (
        pre_tool_use(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "manifest.yaml", "file_text": "g: /a/b/c"},
            }
        )
        is not None
    )


def test_pre_tool_use_reads_every_content_key_spelling() -> None:
    """Write and Edit spell the payload differently; a missed key would fail OPEN, silently."""
    for key in ("file_text", "content", "new_string", "new_str"):
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "manifest.yaml", key: "g: /a/b/c"},
        }
        assert pre_tool_use(payload) is not None, key


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


def test_stop_blocks_while_a_question_is_open(tmp_path: Path) -> None:
    q = tmp_path / "seqforge" / "ds1" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("- Which chemistry: v2 or v3?\n")
    reason = stop_decision({}, workspace=tmp_path)
    assert reason is not None
    assert "questions.md" in reason


def test_stop_allows_when_no_questions_are_open(tmp_path: Path) -> None:
    assert stop_decision({}, workspace=tmp_path) is None


def test_stop_ignores_an_empty_questions_file(tmp_path: Path) -> None:
    """An empty ledger is a closed ledger; whitespace must not wedge the turn."""
    q = tmp_path / "seqforge" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("   \n\n")
    assert stop_decision({}, workspace=tmp_path) is None
    assert questions_outstanding(tmp_path) == []


def test_stop_yields_once_the_runtime_says_it_has_blocked_enough(tmp_path: Path) -> None:
    """`stop_hook_active` guards against a hook that blocks forever.

    A guard that can hang the agent is worse than the risk it manages: "can never finish" is a worse
    failure than "finished with an open question", because the second is at least visible.
    """
    q = tmp_path / "seqforge" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("- unresolved\n")
    assert stop_decision({"stop_hook_active": True}, workspace=tmp_path) is None
    assert stop_decision({"stopHookActive": True}, workspace=tmp_path) is None


def test_questions_outstanding_finds_every_dataset(tmp_path: Path) -> None:
    for ds in ("a", "b"):
        p = tmp_path / "seqforge" / ds / "questions.md"
        p.parent.mkdir(parents=True)
        p.write_text(f"- open in {ds}\n")
    assert len(questions_outstanding(tmp_path)) == 2


def test_questions_outstanding_is_empty_without_state(tmp_path: Path) -> None:
    assert questions_outstanding(tmp_path) == []


def test_sync_questions_writes_a_stop_hook_visible_file_and_clears_it(tmp_path: Path) -> None:
    """The `questions.md` writer feeds the Stop hook: an OPEN conflict blocks turn-end, resolving clears.

    This is the human-in-the-loop half of the family-level change — a genuine cross-family disagreement
    lands a visible, editable artifact, and a re-run that settles it removes the file so the hook stops
    wedging. A within-family difference is recorded `resolved`, so it is never `open` and never writes.
    """
    from types import SimpleNamespace

    from seqforge.cli.manifest import _sync_questions
    from seqforge.models.conflict import Conflict, ConflictPosition
    from seqforge.workspace import state_dir

    def _run(conflicts: list[Conflict]) -> SimpleNamespace:
        result = SimpleNamespace(conflicts=conflicts, questions=[])
        return SimpleNamespace(run_id="run-1", output=SimpleNamespace(result=result))

    open_c = Conflict(
        id="conflict-single-cell-collapsed-to-bulk",
        field="library.chemistry",
        kind="observed_vs_asserted",
        positions=[
            ConflictPosition(value="10x-3p-gex-v2", basis="asserted", confidence=0.9),
            ConflictPosition(value="bulk-rnaseq-pe", basis="observed", confidence=0.99),
        ],
        decidable_by=["reads", "user"],
        status="open",
    )
    state = state_dir(tmp_path)
    _sync_questions(state, [_run([open_c])])
    qmd = state / "questions.md"
    assert questions_outstanding(tmp_path) == [qmd]
    body = qmd.read_text()
    assert "10x-3p-gex-v2" in body and "bulk-rnaseq-pe" in body

    # a resolved (non-open) conflict is not surfaced -> file cleared, the hook stops blocking
    _sync_questions(state, [_run([open_c.model_copy(update={"status": "resolved"})])])
    assert not qmd.exists()
    assert questions_outstanding(tmp_path) == []


def test_sync_questions_unlinks_a_stale_file_on_a_clean_run(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from seqforge.cli.manifest import _sync_questions
    from seqforge.workspace import state_dir

    state = state_dir(tmp_path)
    state.mkdir(parents=True)
    (state / "questions.md").write_text("- a stale question from a prior run\n")
    clean = SimpleNamespace(
        run_id="r", output=SimpleNamespace(result=SimpleNamespace(conflicts=[], questions=[]))
    )
    _sync_questions(state, [clean])
    assert not (state / "questions.md").exists()
