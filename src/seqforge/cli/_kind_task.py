"""What the pass commands share: the kind, the task, and two exits.

Every one of them names a kind and opens the kind's task. :func:`for_kind` finds those, runs the
command's body on them, and gives the command's output for the two ways a body ends early: a setup
it cannot use exits 2 with its one-line reason, and a task another process holds or is writing
exits 3 with liulab-llms's message. So a body only raises.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..passes import SetupRefused
from ..workspace import passes_dir
from ._common import _StageOut

if TYPE_CHECKING:
    from labllm import Gateway, Task, TaskReader

    from ..passes.kinds import Kind


@dataclass(frozen=True)
class KindTask:
    """A kind from the kind table, and its task's folder (not yet opened)."""

    kind: Kind
    folder: Path

    def open[T: Task](self, cls: type[T], *, gateway: Gateway) -> T:
        """Open the task, taking its hold; make it if it is missing."""
        from ..passes.task import open_task

        return open_task(cls, self.folder, gateway=gateway)

    def read(self) -> TaskReader:
        """Open the task only to read it: it reads with the gateway down and while a pass holds it.

        Refuses a folder that holds no task, and makes none.
        """
        from ..passes.task import read_task

        return read_task(self.folder, self.kind.name)


def for_kind(
    name: str,
    *,
    task: Path | None,
    body: Callable[[KindTask], _StageOut],
) -> _StageOut:
    """Run ``body`` on the kind named ``name`` and its task's folder.

    The folder is ``task``, or ``seqforge/passes/<kind>/`` here. A :class:`SetupRefused` from the
    setup or the body is exit 2, and so is a task that a pass stopped in the middle of a write,
    which a read cannot use until a pass opens it again. liulab-llms's ``TaskInUse`` from the body
    is exit 3, and so is its ``TaskBusy``: a pass's write kept a read waiting, and a rerun reads.
    """
    from labllm import TaskBusy, TaskInUse, TaskNeedsRepair

    from ..passes.kinds import kind_named

    try:
        kind = kind_named(name)
        folder = task if task is not None else passes_dir(Path(), kind.name)
        setup = KindTask(kind, folder)
        return body(setup)
    except SetupRefused as exc:
        return _StageOut(str(exc), 2, err=True)
    except TaskNeedsRepair:
        # Waiting never clears it; a writing open, which every pass makes, repairs the task.
        said = (
            f"a pass on {folder} stopped in the middle of a write, and a read cannot undo it: "
            f"run the pass again with seqforge pass start {name}, which repairs the task"
        )
        return _StageOut(said, 2, err=True)
    except (TaskInUse, TaskBusy) as exc:
        # In use: raised only by opening the task to run a pass, or by a pass whose hold another
        # process took over. Busy: raised only by a read.
        stopped = "task_in_use" if isinstance(exc, TaskInUse) else "task_busy"
        held = {"kind": name, "task": str(folder), "stopped": stopped}
        return _StageOut({**held, "message": str(exc)}, 3)
