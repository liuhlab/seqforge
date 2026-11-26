"""The test kit of `seqforge pass`, imported by `test_passes.py` alone.

The pass commands are exercised on the run engine's test-only kind, apart from any production
kind: one writer role that writes a text in capitals, over fixture texts, behind liulab-llms's fake
gateway. It lives here and not in ``conftest.py`` so that no other test session imports liulab-llms.
"""

from __future__ import annotations

import json
from typing import Any

from labllm import Held, Inputs, Model, Role, Unit, UnitKind
from labllm.testing import Price

from seqforge.passes.kinds import Built, Kind

#: The texts the echo kind's units read, by key, in the kind's default order.
ECHO_TEXTS = {"u1": "alpha", "u2": "beta", "u3": "gamma", "u4": "delta"}
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
    "additionalProperties": False,
}


ECHO_WEAK = Model("test", "echo-weak", max_output_tokens=50)
ECHO_STRONG = Model("test", "echo-strong", max_output_tokens=50)
#: Input is free and output costs a tenth of a cent a character, so each call holds back five cents
#: against the ceiling: a ceiling of six cents lets exactly one call through.
ECHO_PRICES = {
    m.name: Price(input_usd_per_token=0.0, output_usd_per_token=0.001)
    for m in (ECHO_WEAK, ECHO_STRONG)
}


def echo_writer(form: dict[str, Any]) -> str:
    """Plays the writer. The strong model answers every text right; the weak one answers alpha right,
    beta and delta wrong (unsettled) and gamma with no word (refused)."""
    text = form["messages"][-1]["content"]
    if form["model"] == ECHO_STRONG.name:
        return json.dumps({"word": text.upper()})
    return json.dumps({"word": {"alpha": "ALPHA", "gamma": ""}.get(text, text[::-1])})


def echo_unit_kind(texts: dict[str, str], *, name: str = "echo") -> UnitKind:
    """The liulab-llms kind: write the unit's text in capitals, checked by code."""

    def load(key: str) -> Inputs:
        if key not in texts:
            raise Held(f"no text for {key}")
        return Inputs(texts[key], tag=f"text={texts[key]}")

    def run(u: Unit) -> dict[str, str]:
        word = json.loads(u.chat("writer", user=u.inputs.data, schema=ECHO_SCHEMA).text)["word"]
        if not word:
            u.refuse("answered", "the writer gave no word")
        return {"word": word}

    def in_capitals(inputs: Inputs, answer: dict[str, str]) -> str | None:
        wanted = inputs.data.upper()
        return None if answer["word"] == wanted else f"{answer['word']!r} is not {wanted!r}"

    return UnitKind(
        name=name,
        version="1",
        roles={"writer": Role("chat", "writes a text in capitals")},
        load=load,
        run=run,
        answer_schema=ECHO_SCHEMA,
        answer_checks=(in_capitals,),
    )


#: The echo kind, which a test lists in the kind table as ``echo``, under this module's name.
KIND = Kind(
    name="echo",
    build=lambda: Built(echo_unit_kind(ECHO_TEXTS)),
    default_models={"writer": ECHO_WEAK},
    default_keys=lambda: list(ECHO_TEXTS),
)
