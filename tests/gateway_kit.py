"""The one place a test builds liulab-llms's fake gateway, and puts it behind seqforge's commands.

:func:`fake` builds it, under the one rule of the real gateway's that its fake does not keep yet.
Every command that calls a model builds its client with :func:`seqforge.gateway.make_gateway`;
:func:`install` replaces that with a client of a :func:`fake`, for the harvest, `run`, `eval run`,
study and pass tests alike, :func:`without_address` puts the real one back with no gateway
address set, and :func:`forbidden` fails the test as soon as a command builds a client. No test
builds a fake gateway or replaces the client any other way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from labllm import Rejected
from labllm.testing import FakeGateway, Price

from seqforge import gateway

#: The real builder, before any test replaces it.
_MAKE_GATEWAY = gateway.make_gateway


def fake(provider: Callable[[dict[str, Any]], Any], *, prices: Mapping[str, Price]) -> FakeGateway:
    """A fake gateway with ``provider`` playing the provider, under the real gateway's DeepSeek rule."""
    return FakeGateway(_keeping_the_rule(provider), prices=prices)


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


def _keeping_the_rule(provider: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """``provider`` behind the real gateway's DeepSeek rule, worded as the gateway words it.

    The gateway sends a DeepSeek schema or tools as strict function calls, and refuses one with
    thinking on before any paid call. The release of liulab-llms seqforge pins has a fake that does
    not keep the rule, so the kit keeps a copy of it until a release whose fake does.
    """

    def keeping_the_rule(form: dict[str, Any]) -> Any:
        strict = form.get("tools") or form.get("schema") is not None
        if form["provider"] == "deepseek" and strict and form["settings"]["thinking"] is True:
            raise Rejected("DeepSeek strict function calls need thinking off")
        return provider(form)

    return keeping_the_rule
