"""Harvest's side of liulab-llms's fake gateway, for the tests of harvest, `run` and `eval run`.

The fake plays the gateway by its rules (the cache, each meter's dollar ceiling, the record of every
exchange) with a function the test supplies playing the provider. `fake` builds it with the one
provider rule the real gateway keeps and its fake does not; `install` puts it behind
:func:`seqforge.passes.gateway.make_gateway`, the one place a command builds its gateway client;
`seam_over` opens a meter on it directly for the tests below the CLI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from labllm import Model, Rejected
from labllm.testing import FakeGateway, Price

from seqforge.harvest import DEEPSEEK_DEFAULT_MODEL, ModelSeam, harvest_model

#: The fake counts tokens as characters. At these prices one request with harvest's ~9 KB prompt and
#: a few thousand output tokens holds back about seven cents of a meter's ceiling while in flight,
#: and costs about a cent once answered.
PRICES = {
    name: Price(input_usd_per_token=1e-6, output_usd_per_token=2e-6)
    for name in (DEEPSEEK_DEFAULT_MODEL, "deepseek-flash", "claude-test-1")
}

#: The reply text of a model that finds nothing in any document.
NOTHING = json.dumps({"drafts": []})


def answering(text: str = NOTHING) -> Callable[[dict[str, Any]], str]:
    """A provider that answers every request with ``text``."""
    return lambda form: text


def fake(
    provider: Callable[[dict[str, Any]], Any] | None = None,
    *,
    prices: Mapping[str, Price] = PRICES,
) -> FakeGateway:
    """A fake gateway with ``provider`` playing the provider, under the real gateway's DeepSeek rule.

    The gateway sends a DeepSeek schema as a strict function call, which DeepSeek takes only with
    thinking off, so it refuses a request with both before any paid call. liulab-llms's fake does
    not copy a provider's mapping, so harvest's fake keeps the rule itself.
    """
    answer = provider or answering()

    def keeping_the_rule(form: dict[str, Any]) -> Any:
        thinks = form["settings"]["thinking"] is True
        if form["provider"] == "deepseek" and thinks and form.get("schema") is not None:
            raise Rejected("DeepSeek strict function calls need thinking off")
        return answer(form)

    return FakeGateway(keeping_the_rule, prices=prices)


def install(
    monkeypatch: pytest.MonkeyPatch,
    provider: Callable[[dict[str, Any]], Any] | None = None,
    *,
    prices: Mapping[str, Price] = PRICES,
) -> FakeGateway:
    """Put a fake gateway behind every command that builds a gateway client, and return it."""
    gateway = fake(provider, prices=prices)
    monkeypatch.setattr(
        "seqforge.passes.gateway.make_gateway", lambda: gateway.client(repo="seqforge")
    )
    return gateway


def seam_over(
    fake: FakeGateway, *, ceiling_usd: float = 1.0, model: Model | None = None
) -> ModelSeam:
    """A seam over a new meter on `fake`, calling harvest's default model unless told otherwise."""
    meter = fake.client(repo="seqforge").meter("test", ceiling_usd=ceiling_usd)
    return ModelSeam(meter, model or harvest_model())


def sent_texts(fake: FakeGateway) -> list[str]:
    """The user half of every request the fake's provider was sent, in order."""
    return [e.form["messages"][-1]["content"] for e in fake.record if e.form is not None]
