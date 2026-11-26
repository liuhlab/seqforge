"""What every pass command opens before it acts: the task.

Each refuses a setup it cannot use with :class:`SetupRefused`, whose message is the one line the
command prints before it exits 2. A kind opens the stores it reads itself, when a pass builds it.
"""

from __future__ import annotations

from pathlib import Path

from labllm import Gateway, Task, UnknownLayout

from . import SetupRefused

#: The folder liulab-llms reads a task's reviews from, inside the task's folder. No command makes
#: it: liulab-llms reads a missing folder as no reviews, and makes it on the first save.
REVIEWS_DIRNAME = "reviews"


def open_task[T: Task](cls: type[T], folder: Path, *, gateway: Gateway) -> T:
    """Open the task in ``folder``, taking its hold; make it if it is missing.

    Its reviews are read from :data:`REVIEWS_DIRNAME` inside ``folder``, so a review file a person
    puts there still counts. Raises liulab-llms's ``TaskInUse`` when another process holds it: that is a stop, exit 3, and
    the message names the holder. A state file this liulab-llms cannot read is a setup refusal.
    """
    try:
        return cls(folder, gateway=gateway, reviews=folder / REVIEWS_DIRNAME)
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
