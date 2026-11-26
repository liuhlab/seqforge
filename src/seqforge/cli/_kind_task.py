"""What the pass commands share: the kind, the task, and two exits.

Every one of them names a kind and opens the kind's task. :func:`for_kind` finds those, runs the
command's body on them, and gives the command's output for the two ways a body ends early: a setup
it cannot use exits 2 with its one-line reason, and a task another process holds exits 3 with
liulab-llms's message naming the holder. So a body only raises.
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
    from labllm import Gateway, Task

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

    def require_task(self) -> None:
        """Refuse a folder that holds no task, for a command that only reads one."""
        from ..passes.task import require_task

        require_task(self.folder, self.kind.name)

    def read(self) -> Task:
        """Open the task to read it, with no gateway, so it reads the same with the gateway down."""
        from labllm import Task

        from ..passes import gateway

        return self.open(Task, gateway=gateway.offline_gateway())


def for_kind(
    name: str,
    *,
    task: Path | None,
    body: Callable[[KindTask], _StageOut],
) -> _StageOut:
    """Run ``body`` on the kind named ``name`` and its task's folder.

    The folder is ``task``, or ``seqforge/passes/<kind>/`` here. A :class:`SetupRefused` from the
    setup or the body is exit 2; liulab-llms's ``TaskInUse`` from the body is exit 3.
    """
    from labllm import TaskInUse

    from ..passes.kinds import kind_named

    try:
        kind = kind_named(name)
        folder = task if task is not None else passes_dir(Path(), kind.name)
        setup = KindTask(kind, folder)
        return body(setup)
    except SetupRefused as exc:
        return _StageOut(str(exc), 2, err=True)
    except TaskInUse as exc:
        # Raised only by opening the task, or by a pass whose hold another process took over.
        held = {"kind": name, "task": str(folder), "stopped": "task_in_use"}
        return _StageOut({**held, "message": str(exc)}, 3)
