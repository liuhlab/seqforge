"""Harvest's side of liulab-llms's fake gateway, for the tests of harvest, `run` and `eval run`.

The fake plays the gateway by its rules (the cache, each meter's dollar ceiling, the record of every
exchange) with a function the test supplies playing the provider. Both `fake` and `install` build it
through the gateway kit, at harvest's prices and with a provider that finds nothing unless told
otherwise; `install` puts it behind every command; `seam_over` opens a meter on it directly for the
tests below the CLI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from labllm import Model
from labllm.testing import FakeGateway, Price

import gateway_kit
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
    """A fake gateway with ``provider``, or one that finds nothing, playing the provider."""
    return gateway_kit.fake(provider or answering(), prices=prices)


def install(
    monkeypatch: pytest.MonkeyPatch,
    provider: Callable[[dict[str, Any]], Any] | None = None,
    *,
    prices: Mapping[str, Price] = PRICES,
) -> FakeGateway:
    """Put a fake gateway behind every command that builds a gateway client, and return it."""
    return gateway_kit.install(monkeypatch, provider or answering(), prices=prices)


def seam_over(
    fake: FakeGateway, *, ceiling_usd: float = 1.0, model: Model | None = None
) -> ModelSeam:
    """A seam over a new meter on `fake`, calling harvest's default model unless told otherwise."""
    meter = fake.client(repo="seqforge").meter("test", ceiling_usd=ceiling_usd)
    return ModelSeam(meter, model or harvest_model())


def sent_texts(fake: FakeGateway) -> list[str]:
    """The user half of every request the fake's provider was sent, in order."""
    return [e.form["messages"][-1]["content"] for e in fake.record if e.form is not None]
