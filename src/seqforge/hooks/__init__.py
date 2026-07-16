"""``hooks`` — policy becomes mechanism (design §4.2).

`CLAUDE.md` can *say* "never read a whole FASTQ". Only a hook can stop one. These are the checked
edges of the rules: bounded reads, no absolute path in a manifest, and code — not the model — decides
whether an edit validated.

Wire them with ``seqforge hook install`` — see :mod:`.guards` for why the logic is typed and tested
rather than living in a shell script.
"""

from __future__ import annotations

from .guards import (
    HOOKS_VERSION,
    Denial,
    check_absolute_path_write,
    check_unbounded_fastq,
    post_tool_use_targets,
    pre_tool_use,
    questions_outstanding,
    stop_decision,
)

__all__ = [
    "HOOKS_VERSION",
    "Denial",
    "pre_tool_use",
    "post_tool_use_targets",
    "stop_decision",
    "check_unbounded_fastq",
    "check_absolute_path_write",
    "questions_outstanding",
]
