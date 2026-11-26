"""The ``--model`` text, ``ROLE=PROVIDER/NAME[,KEY=VALUE...]``, read into a model.

``pass start --model`` reads it. The settings it may carry are :data:`SETTINGS`, each read in one
row: a new setting is one edit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import SetupRefused

if TYPE_CHECKING:
    from labllm import Model


@dataclass(frozen=True)
class Setting:
    """One ``KEY=VALUE`` a model text may carry: the ``Model`` field it sets, and how it is read.

    ``read`` raises ``ValueError`` saying why a value is wrong.
    """

    field: str
    read: Callable[[str], Any]


def _on_off(value: str) -> bool:
    if value not in ("on", "off"):
        raise ValueError(f"thinking is on or off, not {value!r}")
    return value == "on"


def _tokens(value: str) -> int:
    tokens = int(value)
    if tokens < 1:
        raise ValueError(f"max-output-tokens is one or more, not {tokens}")
    return tokens


def _word(value: str) -> str:
    if not value:
        raise ValueError("effort needs a value")
    return value


#: Every setting a model text may carry after its name, by key, in the order it is written.
SETTINGS = {
    "thinking": Setting("thinking", _on_off),
    "effort": Setting("effort", _word),
    "temperature": Setting("temperature", float),
    "max-output-tokens": Setting("max_output_tokens", _tokens),
}


def parse_models(texts: Iterable[str]) -> dict[str, Model]:
    """Each ``--model`` text -> its model, by role. A role named twice is refused."""
    texts = list(texts)
    roles = [text.partition("=")[0].strip() for text in texts]
    if twice := sorted({r for r in roles if roles.count(r) > 1}):
        raise SetupRefused(f"--model names role {', '.join(twice)} more than once")
    return dict(parse_model(text) for text in texts)


def parse_model(text: str) -> tuple[str, Model]:
    """``ROLE=PROVIDER/NAME[,KEY=VALUE...]`` -> ``(role, Model)``.

    The name is split from the provider at the first ``/`` only: a provider's model name may hold
    one. Settings not written take liulab-llms's ``Model`` defaults, not the kind's: the string is
    the whole model value, because the settings are part of the gateway's cache key.
    """
    from labllm import Model

    shape = f"--model {text!r}: write ROLE=PROVIDER/NAME[,KEY=VALUE...]"
    role, eq, rest = text.partition("=")
    head, *pairs = rest.split(",")
    provider, slash, model_name = head.partition("/")
    if not (eq and role.strip() and slash and provider.strip() and model_name.strip()):
        raise SetupRefused(shape)
    fields: dict[str, Any] = {}
    for pair in pairs:
        key, eq, value = (s.strip() for s in pair.partition("="))
        if not eq or key not in SETTINGS:
            raise SetupRefused(f"{shape}, where KEY is one of {', '.join(SETTINGS)}")
        setting = SETTINGS[key]
        if setting.field in fields:
            raise SetupRefused(f"--model {text!r} sets {key} twice")
        try:
            fields[setting.field] = setting.read(value)
        except ValueError as exc:
            raise SetupRefused(f"--model {text!r}: {exc}") from None
    return role.strip(), Model(provider.strip(), model_name.strip(), **fields)
