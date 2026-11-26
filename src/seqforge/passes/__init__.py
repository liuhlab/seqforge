"""seqforge's side of the run engine: its unit kinds, and the passes over them.

The engine is liulab-llms's: a pass of a unit kind over its units, the response cache, the dollar
ceiling, the endings, resume by rerunning, and the review queue. This family names seqforge's kinds
and starts their passes; it does no model work of its own and defines none of the engine's words
(Unit, Unit kind, Pass, Task, Settled, Unsettled, Refused, Unfinished, Held, Review...), which are
liulab-llms's glossary's. It sits outside the six bounded contexts: the kind table names each
kind's module, and nothing here knows what a study is.

- :mod:`.kinds` -- the table of seqforge's kinds, by name, and what a kind is.
- :mod:`.task` -- opening a kind's task.
- :mod:`.model_text` -- the ``--model`` text, read into a model.
- :mod:`.status` -- where a kind's task stands, read with no gateway.
- :mod:`.progress` -- a pass's progress, written to stderr while it runs.

The gateway client, the ceiling check and :class:`SetupRefused` are :mod:`seqforge.gateway`'s,
which every command that calls a model shares.
"""

from __future__ import annotations

from ..gateway import SetupRefused

__all__ = ["SetupRefused"]
