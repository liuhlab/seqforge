"""The one place a test builds liulab-llms's fake gateway, and puts it behind seqforge's commands.

:func:`fake` builds it, and the fake refuses every request the real gateway refuses from the
request alone, DeepSeek with thinking on and a schema or tools among them. Every command that
calls a model builds its client with :func:`seqforge.gateway.make_gateway`; :func:`install`
replaces that with a client of a :func:`fake`, for the harvest, `run`, `eval run`, study and pass
tests alike, :func:`without_address` puts the real one back with no gateway address set, and
:func:`forbidden` fails the test as soon as a command builds a client. No test builds a fake
gateway or replaces the client any other way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from labllm.testing import FakeGateway, Price

from seqforge import gateway

#: The real builder, before any test replaces it.
_MAKE_GATEWAY = gateway.make_gateway


def fake(provider: Callable[[dict[str, Any]], Any], *, prices: Mapping[str, Price]) -> FakeGateway:
    """A fake gateway with ``provider`` playing the provider, refusing what the gateway refuses."""
    return FakeGateway(provider, prices=prices)


def install(
    monkeypatch: pytest.MonkeyPatch,
    provider: Callable[[dict[str, Any]], Any],
    *,
    prices: Mapping[str, Price],
) -> FakeGateway:
    """A :func:`fake` with ``provider`` playing the provider, behind every command; returned."""
    built = fake(provider, prices=prices)
    monkeypatch.setattr(gateway, "make_gateway", lambda: built.client(repo=gateway.REPO))
    return built


def without_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client builder behind every command, with ``$LABLLM_GATEWAY`` unset."""
    monkeypatch.setattr(gateway, "make_gateway", _MAKE_GATEWAY)
    monkeypatch.delenv("LABLLM_GATEWAY", raising=False)


def forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test the moment a command builds a gateway client, before any request."""

    def make_gateway() -> object:
        raise AssertionError("a gateway client was built")

    monkeypatch.setattr(gateway, "make_gateway", make_gateway)
