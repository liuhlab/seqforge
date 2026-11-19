"""What every pass and review command opens before it acts: the review clone, the store, the task.

Each refuses a setup it cannot use with :class:`SetupRefused`, whose message is the one line the
command prints before it exits 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from labllm import Gateway, Task, UnknownLayout

from . import SetupRefused

if TYPE_CHECKING:
    from labdata import Store

    from .kinds import Kind

#: The folder of review files inside a clone of the review repo, one ``<kind>.jsonl`` per kind.
REVIEWS_DIRNAME = "reviews"


def reviews_folder(clone: Path | None) -> Path:
    """The review files' folder in ``clone``, a local clone of the review repo.

    Every pass needs it, not only the review commands: which units a person's review holds decides
    which units ``--only`` leaves out.
    """
    if clone is None:
        raise SetupRefused(
            "no reviews folder: give --reviews DIR or set SEQFORGE_REVIEWS, "
            "a local clone of the review repo"
        )
    folder = clone / REVIEWS_DIRNAME
    if not folder.is_dir():
        raise SetupRefused(
            f"no reviews folder: {clone} has no {REVIEWS_DIRNAME}/ folder, "
            "so it is not a clone of the review repo"
        )
    return folder


def store_for(kind: Kind) -> tuple[Store | None, str | None]:
    """The store a pass of ``kind`` reads, opened once, and the build it reads.

    ``(None, None)`` for a kind that reads no store. Opening pins one build for as long as the
    returned store lives, so a store refreshed mid-pass cannot mix two builds into one pass.
    """
    if not kind.reads_store:
        return None, None
    import labdata
    from labdata.exceptions import NotAStoreError, StoreNotConfiguredError

    try:
        store = labdata.open_store()
    except (StoreNotConfiguredError, NotAStoreError) as exc:
        raise SetupRefused(f"kind {kind.name!r} reads liulab-data's store: {exc}") from exc
    build = store.settings.get("build")
    return store, None if build is None else str(build)


def open_task[T: Task](cls: type[T], folder: Path, *, gateway: Gateway, reviews: Path) -> T:
    """Open the task in ``folder``, taking its hold; make it if it is missing.

    Raises liulab-llms's ``TaskInUse`` when another process holds it: that is a stop, exit 3, and
    the message names the holder. A state file this liulab-llms cannot read is a setup refusal.
    """
    try:
        return cls(folder, gateway=gateway, reviews=reviews)
    except UnknownLayout as exc:
        raise SetupRefused(str(exc)) from exc


#: The file liulab-llms keeps a task's state in, inside the task's folder.
STATE_FILENAME = "state.sqlite"


def require_task(folder: Path, kind: str) -> None:
    """Refuse a folder that holds no task.

    Opening a task makes one where there is none, so a command that only reads a task checks
    first, rather than read an empty one it just made.
    """
    if not (folder / STATE_FILENAME).is_file():
        raise SetupRefused(
            f"no task in {folder}: run seqforge pass start {kind} first, or give --task DIR"
        )
