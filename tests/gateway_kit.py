"""The one place a test puts liulab-llms's fake gateway behind seqforge's commands.

Every command that calls a model builds its client with :func:`seqforge.gateway.make_gateway`.
:func:`install` replaces it with a client of a ``FakeGateway``, for the harvest, `run`, `eval run`,
study and pass tests alike, and :func:`without_address` puts the real one back with no gateway
address set. No test replaces the client any other way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from labllm.testing import FakeGateway, Price

from seqforge import gateway

#: The real builder, before any test replaces it.
_MAKE_GATEWAY = gateway.make_gateway


def install(
    monkeypatch: pytest.MonkeyPatch,
    provider: Callable[[dict[str, Any]], Any],
    *,
    prices: Mapping[str, Price],
) -> FakeGateway:
    """A fake gateway with ``provider`` playing the provider, behind every command; returned."""
    fake = FakeGateway(provider, prices=prices)
    monkeypatch.setattr(gateway, "make_gateway", lambda: fake.client(repo=gateway.REPO))
    return fake


def without_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client builder behind every command, with ``$LABLLM_GATEWAY`` unset."""
    monkeypatch.setattr(gateway, "make_gateway", _MAKE_GATEWAY)
    monkeypatch.delenv("LABLLM_GATEWAY", raising=False)
