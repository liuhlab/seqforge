"""seqforge's side of the run engine: its unit kinds, and the passes over them.

The engine is liulab-llms's: a pass of a unit kind over its units, the response cache, the dollar
ceiling, the endings, resume by rerunning, and the review queue. This family names seqforge's kinds
and starts their passes; it does no model work of its own and defines none of the engine's words
(Unit, Unit kind, Pass, Task, Settled, Unsettled, Refused, Unfinished, Held, Review...), which are
liulab-llms's glossary's. It sits outside the five bounded contexts: until the study context exists,
nothing here knows what a study is.

- :mod:`.kinds` -- the registry of seqforge kinds, by name.
- :mod:`.gateway` -- the one place a command builds its gateway client, which tests replace.
- :mod:`.task` -- opening a kind's task, and the review clone every pass and review command reads.
- :mod:`.review` -- the review form: a queued unit as an issue body, and its ticks read back.
- :mod:`.github` -- the one place a review command builds its client of the review repo.
- :mod:`.clone` -- committing a reviews file in the review clone, and pushing it.
- :mod:`.status` -- where a kind's task stands, read with no gateway.
"""

from __future__ import annotations


class SetupRefused(Exception):
    """A setup a pass or review command cannot use. Its message is the one-line reason; exit 2."""
