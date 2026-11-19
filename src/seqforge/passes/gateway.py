"""The one place a seqforge command builds its client of the lab gateway.

Every model call a pass makes goes through it. Tests replace :func:`make_gateway` with a client of
liulab-llms's ``FakeGateway``, so the commands must call it through this module at the moment they
need it, never bind it at import.
"""

from __future__ import annotations

from labllm import Gateway

#: The repo label the gateway records with each call.
REPO = "seqforge"


def make_gateway() -> Gateway:
    """A client of the gateway at ``$LABLLM_GATEWAY``. Makes no network contact.

    Raises liulab-llms's ``GatewayUnreachable`` when ``$LABLLM_GATEWAY`` is unset or is not an
    ``http://host:port`` address. That is a setup the command cannot use, not a stop.
    """
    return Gateway(repo=REPO)


class _NoGateway:
    def send(self, path: str, body: bytes) -> tuple[int, bytes]:
        raise ConnectionRefusedError("this command reaches no gateway")


def offline_gateway() -> Gateway:
    """A client that reaches no gateway, for a command that only reads a task.

    Opening a task takes a gateway client, which a read never calls, so a command that reads one
    works with the gateway down or ``$LABLLM_GATEWAY`` unset.
    """
    return Gateway(repo=REPO, transport=_NoGateway())
