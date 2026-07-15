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
    check_heldout_access,
    check_unbounded_fastq,
    heldout_roots,
    post_tool_use_targets,
    pre_tool_use,
    questions_outstanding,
    stop_decision,
)

# ---------------------------------------------------------------------------------------------
# R3 — never read a whole FASTQ
# ---------------------------------------------------------------------------------------------


def test_r3_denies_an_unbounded_fastq_stream() -> None:
    d = check_unbounded_fastq("zcat sample_R1.fastq.gz | wc -l")
    assert d is not None
    assert "R3" in d.rule
    assert d.remedy  # a block with no way forward is a wall (R4: remedies must be actionable)


def test_r3_denies_every_streaming_reader() -> None:
    for cmd in (
        "cat reads.fastq",
        "zcat reads.fq.gz | awk '{print}'",
        "gunzip -c reads.fastq.gz > out",
        "bzcat reads.fastq.bz2 | grep AAAA",
    ):
        assert check_unbounded_fastq(cmd) is not None, cmd


def test_r3_allows_a_bounded_stream() -> None:
    """`head` caps the read. This is the neighbouring command that must NOT be blocked."""
    for cmd in (
        "zcat reads.fastq.gz | head -n 4000",
        "head -c 1000000 reads.fastq",
        "zcat reads.fastq.gz | head -4",
    ):
        assert check_unbounded_fastq(cmd) is None, cmd


def test_r3_allows_the_sanctioned_seqforge_verb() -> None:
    """`seqforge probe` is bounded by construction (200k reads / 256 MB) — blocking it is nonsense."""
    for cmd in (
        "seqforge probe reads.fastq.gz --json",
        "pixi run -- seqforge probe reads.fastq.gz",
        "python -m seqforge.cli probe reads.fastq.gz",
    ):
        assert check_unbounded_fastq(cmd) is None, cmd


def test_r3_ignores_commands_with_no_fastq() -> None:
    assert check_unbounded_fastq("cat README.md") is None
    assert check_unbounded_fastq("zcat archive.tar.gz | tar t") is None
    assert check_unbounded_fastq("") is None


def test_r3_does_not_fire_on_merely_naming_a_fastq() -> None:
    """Naming a file is not streaming it — `ls` and `rm` must pass."""
    assert check_unbounded_fastq("ls -l reads.fastq.gz") is None
    assert check_unbounded_fastq("rm reads.fastq.gz") is None


# ---------------------------------------------------------------------------------------------
# R9 — no absolute path in a manifest
# ---------------------------------------------------------------------------------------------


def test_r9_denies_an_absolute_path_in_a_manifest() -> None:
    d = check_absolute_path_write("manifest.yaml", "genome:\n  fasta: /scratch/ref/hg38.fa\n")
    assert d is not None
    assert "R9" in d.rule
    assert "/scratch/ref/hg38.fa" in d.reason


def test_r9_covers_every_emitted_artifact() -> None:
    for name in ("manifest.yaml", "manifest.draft.yaml", "config.yaml", "units.tsv"):
        assert check_absolute_path_write(name, "p: /data/x/y.fa") is not None, name


def test_r9_allows_a_machine_independent_manifest() -> None:
    """The whole point: assembly id + registered GTF name + literal env name + a URI."""
    good = (
        "genome:\n  assembly: ce11\n  annotation_name: WS298\n"
        "environment: align-rna\n"
        "files:\n  - s3://bucket/sample_R1.fastq.gz\n"
        "  - sample_R2.fastq.gz\n"
    )
    assert check_absolute_path_write("manifest.yaml", good) is None


def test_r9_does_not_mistake_a_uri_for_an_absolute_path() -> None:
    """Regression: `s3://bucket/x.fastq.gz` contains `/bucket/x.fastq.gz`.

    R9 says data SHOULD be a URI, so flagging one rejects the exact manifest the rule wants. A guard
    that blocks correct work gets switched off, and then it guards nothing.
    """
    for uri in (
        "s3://bucket/sample_R1.fastq.gz",
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR287/SRR28716553_1.fastq.gz",
        "gs://bucket/path/to/reads.fastq.gz",
        "ftp://ftp.ncbi.nlm.nih.gov/x/y.fastq.gz",
    ):
        assert check_absolute_path_write("manifest.yaml", f"files:\n  - {uri}\n") is None, uri


def test_r9_still_catches_an_absolute_path_next_to_a_uri() -> None:
    """Scrubbing URIs must not blind the guard to a real violation beside one."""
    content = "files:\n  - s3://bucket/ok.fastq.gz\ngenome:\n  fasta: /scratch/ref/hg38.fa\n"
    d = check_absolute_path_write("manifest.yaml", content)
    assert d is not None
    assert "/scratch/ref/hg38.fa" in d.reason


def test_r9_ignores_files_that_are_not_manifests() -> None:
    """A script may legitimately hold an absolute path; a manifest may not."""
    assert check_absolute_path_write("run.sh", "cat /scratch/ref/hg38.fa") is None
    assert check_absolute_path_write("notes.md", "see /scratch/data") is None


# ---------------------------------------------------------------------------------------------
# design §8 — the held-out case stays held out
# ---------------------------------------------------------------------------------------------

ROOT = "/scratch/zzz/heldout-example"


def test_heldout_denies_ad_hoc_access() -> None:
    """ls/head/stat is exactly how a held-out set stops being held out, usually by accident."""
    for cmd in (f"ls {ROOT}", f"head -c 100 {ROOT}/a.fastq.gz", f"stat {ROOT}/a.fastq.gz"):
        d = check_heldout_access(cmd, [ROOT])
        assert d is not None, cmd
        assert "held-out" in d.rule


def test_heldout_allows_the_sanctioned_seqforge_verb() -> None:
    """The pre-registered run is the POINT of the case; only ad-hoc shell is the leak."""
    assert check_heldout_access(f"seqforge probe {ROOT}/a.fastq.gz --json", [ROOT]) is None


def test_heldout_is_inert_when_no_roots_are_configured() -> None:
    """The repo carries the RULE; the paths live in out-of-git config. No config => nothing to guard."""
    assert check_heldout_access(f"ls {ROOT}", []) is None


def test_heldout_roots_come_from_case_env_vars(monkeypatch) -> None:
    """An eval case declares its root via `root_env`, so registering a case protects its data.

    One source of truth: there is no way to add a held-out case and forget to guard it.
    """
    monkeypatch.setenv("SEQFORGE_CASE_PRJNA1027859", ROOT)
    monkeypatch.setenv("SEQFORGE_CASE_OTHER", "/data/other")
    monkeypatch.setenv("SEQFORGE_NOT_A_CASE", "/data/ignored")
    roots = heldout_roots(dict(__import__("os").environ))
    assert ROOT in roots
    assert "/data/other" in roots
    assert "/data/ignored" not in roots, "only SEQFORGE_CASE_* names a held-out root"


def test_heldout_root_paths_are_never_in_this_repo() -> None:
    """Design §8: this public repo carries the rule, never the paths."""
    from seqforge.hooks import guards

    source = Path(guards.__file__).read_text()
    assert "/scratch/" not in source.replace("/scratch/**", ""), (
        "a held-out path leaked into the repo"
    )


# ---------------------------------------------------------------------------------------------
# the PreToolUse dispatcher
# ---------------------------------------------------------------------------------------------


def test_pre_tool_use_routes_bash_write_and_read() -> None:
    assert (
        pre_tool_use(
            {"tool_name": "Bash", "tool_input": {"command": "zcat a.fastq.gz | wc -l"}}, roots=[]
        )
        is not None
    )
    assert (
        pre_tool_use(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "manifest.yaml", "file_text": "g: /a/b/c"},
            },
            roots=[],
        )
        is not None
    )
    assert (
        pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": f"{ROOT}/a"}}, roots=[ROOT])
        is not None
    )


def test_pre_tool_use_reads_every_content_key_spelling() -> None:
    """Write and Edit spell the payload differently; a missed key would fail OPEN, silently."""
    for key in ("file_text", "content", "new_string", "new_str"):
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "manifest.yaml", key: "g: /a/b/c"},
        }
        assert pre_tool_use(payload, roots=[]) is not None, key


def test_pre_tool_use_has_no_opinion_on_unrelated_tools() -> None:
    assert (
        pre_tool_use({"tool_name": "WebFetch", "tool_input": {"url": "https://x"}}, roots=[])
        is None
    )
    assert pre_tool_use({}, roots=[]) is None


def test_pre_tool_use_denies_writing_into_a_heldout_root() -> None:
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": f"{ROOT}/notes.txt", "file_text": "x"},
    }
    assert pre_tool_use(payload, roots=[ROOT]) is not None


# ---------------------------------------------------------------------------------------------
# PostToolUse — code decides whether the edit validated, not the model (R2)
# ---------------------------------------------------------------------------------------------


def test_post_tool_use_targets_manifest_edits_only() -> None:
    assert post_tool_use_targets(
        {"tool_name": "Write", "tool_input": {"file_path": "/w/.seqforge/manifest.yaml"}}
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
    q = tmp_path / ".seqforge" / "ds1" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("- Which chemistry: v2 or v3?\n")
    reason = stop_decision({}, workspace=tmp_path)
    assert reason is not None
    assert "questions.md" in reason


def test_stop_allows_when_no_questions_are_open(tmp_path: Path) -> None:
    assert stop_decision({}, workspace=tmp_path) is None


def test_stop_ignores_an_empty_questions_file(tmp_path: Path) -> None:
    """An empty ledger is a closed ledger; whitespace must not wedge the turn."""
    q = tmp_path / ".seqforge" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("   \n\n")
    assert stop_decision({}, workspace=tmp_path) is None
    assert questions_outstanding(tmp_path) == []


def test_stop_yields_once_the_runtime_says_it_has_blocked_enough(tmp_path: Path) -> None:
    """`stop_hook_active` guards against a hook that blocks forever.

    A guard that can hang the agent is worse than the risk it manages: "can never finish" is a worse
    failure than "finished with an open question", because the second is at least visible.
    """
    q = tmp_path / ".seqforge" / "questions.md"
    q.parent.mkdir(parents=True)
    q.write_text("- unresolved\n")
    assert stop_decision({"stop_hook_active": True}, workspace=tmp_path) is None
    assert stop_decision({"stopHookActive": True}, workspace=tmp_path) is None


def test_questions_outstanding_finds_every_dataset(tmp_path: Path) -> None:
    for ds in ("a", "b"):
        p = tmp_path / ".seqforge" / ds / "questions.md"
        p.parent.mkdir(parents=True)
        p.write_text(f"- open in {ds}\n")
    assert len(questions_outstanding(tmp_path)) == 2


def test_questions_outstanding_is_empty_without_state(tmp_path: Path) -> None:
    assert questions_outstanding(tmp_path) == []
