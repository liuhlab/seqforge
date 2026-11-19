"""`seqforge standard-set`: list the standard set of studies kept under ``evals/``, and draw one.

A rough first version; the file format is provisional. A study is drawn in three steps: the
drafting pass (``seqforge pass start standard-set-bounds``), ``draft``, which copies each unit's
answer beside its study, and ``judge``, which asks the large model about one study and writes its
verdict and its study file. No step overwrites a file.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer
import yaml

from ..standard_set import STUDY, default_set_dir
from ._common import _emit, _StageOut
from .root import standard_set_app

_DIR = typer.Option(None, "--dir", help="Folder of study folders (default: evals/standard-set).")


@standard_set_app.command("list")
def standard_set_list(set_dir: Path | None = _DIR) -> None:
    """List the standard set: one object per study, from its ``<id>/study.yaml``."""
    root = set_dir or default_set_dir()
    payload = []
    for path in sorted(root.glob(f"*/{STUDY}")):
        study = yaml.safe_load(path.read_text())
        payload.append(
            {
                "id": study["id"],
                "summary": study["summary"],
                "direction": study["direction"],
                "hard_shape": study["hard_shape"],
                "status": study["status"],
                "bounds_from": study["bounds_from"],
                "reason_picked": " ".join(study["reason_picked"].split()),
                "bounds": {"series": study["bounds"]["series"]},
            }
        )
    typer.echo(json.dumps(payload, indent=2))


@standard_set_app.command("draft")
def standard_set_draft(
    set_dir: Path | None = _DIR,
    task: Path | None = typer.Option(
        None,
        "--task",
        help="The drafting kind's task folder. Default: seqforge/passes/<kind>/ here.",
    ),
    reviews: Path | None = typer.Option(
        None, "--reviews", envvar="SEQFORGE_REVIEWS", help="A local clone of the review repo."
    ),
) -> None:
    """Write ``<id>/draft.yaml`` from the drafting pass's answers, for each study that has none.

    It reads the task only, so it works with the gateway down. Exit 0 with one line per unit; 2 for
    no task or no review clone; 3 when a running pass holds the task.
    """
    from ..standard_set.draft import KIND_NAME, write_drafts
    from ._kind_task import KindTask, for_kind

    def run(setup: KindTask) -> _StageOut:
        from ..passes.status import read_only_kind

        setup.require_task()
        with setup.read() as opened:
            done = write_drafts(
                opened,
                read_only_kind(KIND_NAME),
                set_dir or default_set_dir(),
                dt.datetime.now(dt.UTC),
            )
        return _StageOut({"kind": KIND_NAME, "task": str(setup.folder), "drafts": done}, 0)

    _emit(for_kind(KIND_NAME, task=task, reviews=reviews, body=run))


@standard_set_app.command("judge")
def standard_set_judge(
    study: str = typer.Argument(..., help="The study to judge: its id in the pick."),
    set_dir: Path | None = _DIR,
    work: Path | None = typer.Option(
        None,
        "--work",
        help=(
            "Where the judge's reading material, prompt and raw result go, one folder per study. "
            "It holds record and paper text, so keep it out of the repo. "
            "Default: seqforge/standard-set/judge/ here."
        ),
    ),
) -> None:
    """Ask the judging model about one study's draft, and write its verdict and its study file.

    One study per call, and never two calls at once. The records and papers come from liulab-data's
    stores, named by ``LABDATA_STORE`` and ``LABDATA_PAPERS``. Exit 0 with a short JSON account; 2
    for a study that is not picked or a store that cannot be opened; 3 when the judge gave no usable
    verdict, a refusal included, and then nothing is written.
    """
    from labdata.exceptions import LabdataError

    from ..standard_set.judge import JudgeStopped, judge_study
    from ..standard_set.material import open_sources, pick_entries, read_group
    from ..workspace import state_dir

    root = set_dir or default_set_dir()
    entry = pick_entries(root).get(study)
    if entry is None:
        _emit(_StageOut(f"{study} is not a picked study in {root}", 2, err=True))
        return
    try:
        store, papers = open_sources()
        group = read_group(store, papers, entry)
    except LabdataError as exc:
        _emit(_StageOut(f"liulab-data's store cannot be read: {exc}", 2, err=True))
        return
    if papers is None and group["papers"]:
        # The group's papers, not the pick's: a record may link a paper the pick never listed.
        _emit(_StageOut("no paper store is configured: set LABDATA_PAPERS", 2, err=True))
        return
    build = store.settings.get("build")
    try:
        account = judge_study(
            entry,
            group,
            set_dir=root,
            work=(work or state_dir(Path(), "standard-set", "judge")) / study,
            store_build=None if build is None else str(build),
            when=dt.datetime.now(dt.UTC),
        )
    except JudgeStopped as exc:
        _emit(_StageOut({"id": study, "stopped": str(exc)}, 3))
        return
    _emit(_StageOut(account, 0))
