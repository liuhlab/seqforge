"""The model seam: harvest's calls through one liulab-llms meter, and what harvest keeps of them.

The gateway sends every call, holds the provider keys, retries, caches and enforces the dollar
**Ceiling**; liulab-llms's glossary defines those words. This module is harvest's side of it:

- :func:`harvest_model` reads ``--provider`` and ``--model`` into the model harvest calls;
- :class:`ModelSeam` sends each request through the run's meter, tagged with its document, and
  records each **Exchange** for the **Transcript**. A request the ceiling refuses while others are
  in flight waits for one of them to settle and is sent again, so the ceiling bounds what a run
  spends, not how many requests it has in flight;
- :func:`stop_blocker` turns a stop into the ``Blocker`` that ends the run with exit 3.

The seam never reads a reply's text for meaning: extraction validates it.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..models.blocker import Blocker, BlockerCode, BlockerSubject

if TYPE_CHECKING:
    from labllm import Meter, Model, Stopped

#: The provider harvest calls when ``--provider`` names none.
DEFAULT_PROVIDER = "deepseek"

#: The most one harvest may spend, in dollars, when ``--ceiling-usd`` names none: `harvest extract`'s,
#: `run`'s and, per case, `eval run`'s. There is no "no ceiling" value.
DEFAULT_CEILING_USD = 1.0

#: The model harvest calls on DeepSeek when ``--model`` names none. `-pro` won the benchmark's
#: head-to-head on every axis, cost included (`evals/README.md`, "The default model, and the run
#: that decided it").
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"


def harvest_model(provider: str | None = None, name: str | None = None) -> Model:
    """``--provider`` and ``--model`` -> the model harvest calls. Raises ``ValueError`` on a gap.

    DeepSeek is called with thinking on at high effort, its documented default, sent explicitly so
    the setting is in the gateway's cache key and the Transcript. Any other provider takes its own
    defaults, and must be given ``--model``: harvest knows no other provider's model names.
    """
    from labllm import Model

    chosen = (provider or DEFAULT_PROVIDER).strip()
    if chosen == DEFAULT_PROVIDER:
        return Model(chosen, name or DEEPSEEK_DEFAULT_MODEL, thinking=True, effort="high")
    if not name:
        raise ValueError(f"--provider {chosen} needs --model: harvest knows only DeepSeek's names")
    return Model(chosen, name)


@dataclass(frozen=True)
class Reply:
    """One request's answer: the text UNVALIDATED, the provider's usage, and what it cost."""

    text: str
    usage: dict[str, Any]
    cost_usd: float
    cached: bool


@dataclass(frozen=True)
class Exchange:
    """One request harvest sent through its meter and what came back, kept whole.

    ``prompt_sha256`` points at the system prompt, which a :class:`Transcript` holds once. A request
    that stopped is still an exchange, with ``error`` set and an empty ``text``.
    """

    prompt_sha256: str
    #: The volatile half of the prompt: the documents, and which fields were asked of them.
    user: str
    #: What came back, verbatim and UNVALIDATED.
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    #: The model's settings this request was sent with.
    mode: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    cost_usd: float = 0.0
    cached: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether a reply came back rather than a stop."""
        return self.error is None

    def to_json(self) -> dict[str, Any]:
        """The exchange as plain JSON, with ``error`` present only on a request that stopped."""
        out: dict[str, Any] = {
            "prompt_sha256": self.prompt_sha256,
            "model": self.model,
            "user": self.user,
            "text": self.text,
            "usage": dict(self.usage),
            "mode": dict(self.mode),
            "cost_usd": self.cost_usd,
            "cached": self.cached,
        }
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass(frozen=True)
class Transcript:
    """Every exchange of one run: one system prompt, by sha256, plus N (document, response) pairs.

    ``prompts`` normally holds one entry, since the system prompt is byte-identical across a run; it
    is a mapping so a run that sent two different prompts records that instead of hiding it.
    """

    provider: str
    prompts: dict[str, str]
    exchanges: tuple[Exchange, ...]

    @property
    def n_exchanges(self) -> int:
        """How many requests the transcript holds."""
        return len(self.exchanges)

    def prompt_for(self, exchange: Exchange) -> str:
        """The system prompt this exchange was made under."""
        return self.prompts[exchange.prompt_sha256]


class ModelSeam:
    """Harvest's side of one liulab-llms meter: sends each request and keeps each exchange.

    Thread-safe: extraction fans requests out over a pool, and every one goes through here. The
    first stop it meets stops the meter, so requests not yet sent raise at once rather than each
    meeting the stop again.

    **The ceiling bounds spend, not width.** The gateway holds back each request's largest possible
    cost, its input and its whole output allowance, while the request is in flight, and refuses one
    that would cross the ceiling. Harvest fans out up to two dozen requests at once, so under a
    one-dollar ceiling those holds alone would refuse a run that spends a few cents. A request
    refused while others of this seam are in flight therefore waits until one of them settles and
    is sent again; only a refusal with nothing else in flight stops the run.
    """

    def __init__(self, meter: Meter, model: Model, *, record: bool = True) -> None:
        self.meter = meter
        self.model = model
        self._record = record
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._in_flight = 0
        self._n_settled = 0
        self._halted = False
        self._prompts: dict[str, str] = {}
        self._exchanges: list[Exchange] = []
        self._n_exchanges = 0

    @property
    def provider(self) -> str:
        """The provider, as provenance and the Ledger name it."""
        return self.model.provider

    @property
    def model_id(self) -> str:
        """``provider/model``: who an Assertion's extractor was."""
        return f"{self.model.provider}/{self.model.name}"

    def mode(self, max_tokens: int) -> dict[str, Any]:
        """The settings a request with this output allowance is sent with: the Ledger's ``mode``."""
        return {
            **{
                k: v
                for k, v in (
                    ("thinking", self.model.thinking),
                    ("effort", self.model.effort),
                    ("temperature", self.model.temperature),
                )
                if v is not None
            },
            "max_output_tokens": max_tokens,
        }

    @property
    def n_exchanges(self) -> int:
        """Requests sent so far, those that stopped included."""
        with self._lock:
            return self._n_exchanges

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int, tag: str
    ) -> Reply:
        """Send one request under ``schema``, tagged ``tag``, and return its reply.

        Raises liulab-llms's ``Stopped`` (after stopping the meter) or ``Rejected``.
        """
        from labllm import CeilingReached, Stopped

        model = replace(self.model, max_output_tokens=max_tokens)
        mode = self.mode(max_tokens)
        while True:
            with self._lock:
                self._in_flight += 1
                settled_before = self._n_settled
            try:
                r = self.meter.chat(model, system=system, user=user, schema=schema, tag=tag)
            except CeilingReached as exc:
                if self._wait_for_room(settled_before):
                    continue
                self._stop(exc, system, user, mode)
                raise
            except Stopped as exc:
                self._settle()
                self._stop(exc, system, user, mode)
                raise
            except BaseException:
                self._settle()
                raise
            self._settle()
            self._keep(system, user, r.text, r.usage, mode, r.cost_usd, r.cached, None)
            return Reply(r.text, dict(r.usage), r.cost_usd, r.cached)

    def totals(self) -> dict[str, Any]:
        """The meter's counters, in dollars, and the requests harvest sent."""
        m = self.meter
        return {
            "spent_usd": m.spent_usd,
            "first_cost_usd": m.first_cost_usd,
            "ceiling_usd": m.ceiling_usd,
            "provider_requests": m.requests,
            "cache_hits": m.cache_hits,
            "n_calls": self.n_exchanges,
        }

    def transcript(self) -> Transcript:
        """A snapshot of every exchange so far."""
        with self._lock:
            return Transcript(self.provider, dict(self._prompts), tuple(self._exchanges))

    def _settle(self) -> None:
        """One request of this seam is no longer in flight; wake those waiting for room."""
        with self._lock:
            self._in_flight -= 1
            self._n_settled += 1
            self._settled.notify_all()

    def _wait_for_room(self, settled_before: int) -> bool:
        """After a ceiling refusal: wait for another request to settle; False if none is in flight.

        A request that settled since this one was sent may already have made room, so it is sent
        again at once.
        """
        with self._lock:
            self._in_flight -= 1
            while not self._halted and self._n_settled == settled_before and self._in_flight > 0:
                self._settled.wait()
            return not self._halted and self._n_settled != settled_before

    def _stop(self, exc: Stopped, system: str, user: str, mode: dict[str, Any]) -> None:
        """Stop the meter for every later request, and keep the request that stopped."""
        with self._lock:
            self._halted = True
            self._settled.notify_all()
        self.meter.stop(exc)
        self._keep(system, user, "", {}, mode, exc.cost_usd, False, f"{exc.reason}: {exc}")

    def _keep(
        self,
        system: str,
        user: str,
        text: str,
        usage: dict[str, Any],
        mode: dict[str, Any],
        cost_usd: float,
        cached: bool,
        error: str | None,
    ) -> None:
        digest = hashlib.sha256(system.encode("utf-8")).hexdigest()
        exchange = Exchange(
            digest, user, text, dict(usage), mode, self.model.name, cost_usd, cached, error
        )
        with self._lock:
            self._n_exchanges += 1
            if self._record:
                self._prompts.setdefault(digest, system)
                self._exchanges.append(exchange)


#: What a person does about each stop, by its reason.
_REMEDIES = {
    "ceiling_reached": "Rerun with a higher `--ceiling-usd` if the dataset is this large. Requests "
    "already answered are replayed from the gateway's cache at no cost. If the cost surprised you, "
    "read `seqforge/logs/usage.json` first; `--no-llm` compiles from the bytes for nothing.",
    "provider_exhausted": "The provider's prepaid balance or monthly cap is used up. Ask the "
    "gateway's admin to top it up, or rerun with another `--provider` and `--model`.",
    "provider_unavailable": "The provider kept failing past the gateway's retry budget. Rerun "
    "later; answered requests are replayed from the cache at no cost.",
    "gateway_unreachable": "Open a tunnel to the lab gateway and set `LABLLM_GATEWAY` to its "
    "address, then rerun.",
}


def stop_blocker(exc: Stopped, *, subject: str, spent_usd: float) -> Blocker:
    """The stop that ended a harvest, as the ``Blocker`` its exit 3 carries."""
    return Blocker(
        id=f"blk-model-calls-{exc.reason.replace('_', '-')}",
        code=BlockerCode.MODEL_CALLS_STOPPED,
        message=(
            f"harvest's model calls stopped ({exc.reason}: {exc}) after spending "
            f"${spent_usd:.4f}. The extraction is incomplete and nothing was written from it."
        ),
        remedy=_REMEDIES.get(exc.reason, "Read the reason, fix its cause, and rerun."),
        subject=BlockerSubject(kind="dataset", ref=subject),
        evidence=[f"reason={exc.reason}", f"spent_usd={spent_usd}"],
    )


__all__ = [
    "DEEPSEEK_DEFAULT_MODEL",
    "DEFAULT_CEILING_USD",
    "DEFAULT_PROVIDER",
    "Exchange",
    "ModelSeam",
    "Reply",
    "Transcript",
    "harvest_model",
    "stop_blocker",
]
