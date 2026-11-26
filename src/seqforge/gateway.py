"""The lab gateway, as every seqforge command that calls a model reaches it.

`harvest extract`, `run`, `eval run --llm` and the pass commands share three things here: the client
of the gateway, the check that a dollar ceiling can be used, and the refusal when no gateway address
is set. Each refusal is a :class:`SetupRefused`, and every command exits 2 for one. The module belongs
to neither the compiler's model stage nor the passes: both reach the gateway through it.

Tests put a client of liulab-llms's ``FakeGateway`` behind :func:`make_gateway`, so a command calls it
through this module at the moment it needs one, never binding it at import. liulab-llms is imported
inside each function: it costs a few tenths of a second, which every other verb's start would pay.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from labllm import Gateway

#: The repo label the gateway records with each call.
REPO = "seqforge"


class SetupRefused(Exception):
    """A setup a command cannot use. Its message is the one-line reason; exit 2."""


def check_ceiling(ceiling_usd: float) -> None:
    """Refuse a ``--ceiling-usd`` that is not a number of dollars, zero or more."""
    if not (math.isfinite(ceiling_usd) and ceiling_usd >= 0):
        raise SetupRefused(f"--ceiling-usd must be a number of dollars, not {ceiling_usd}")


def make_gateway() -> Gateway:
    """A client of the gateway at ``$LABLLM_GATEWAY``. Makes no network contact.

    Refuses when ``$LABLLM_GATEWAY`` is unset or is not an ``http://host:port`` address: nothing was
    sent, so that is a setup the command cannot use, not a stop.
    """
    from labllm import Gateway, GatewayUnreachable

    try:
        return Gateway(repo=REPO)
    except GatewayUnreachable as exc:
        raise SetupRefused(f"no gateway address: {exc}") from exc


class _NoGateway:
    def send(self, path: str, body: bytes) -> tuple[int, bytes]:
        raise ConnectionRefusedError("this command reaches no gateway")


def offline_gateway() -> Gateway:
    """A client that reaches no gateway, for a command that only reads a task.

    Opening a task takes a gateway client, which a read never calls, so a command that reads one
    works with the gateway down or ``$LABLLM_GATEWAY`` unset.
    """
    from labllm import Gateway

    return Gateway(repo=REPO, transport=_NoGateway())
