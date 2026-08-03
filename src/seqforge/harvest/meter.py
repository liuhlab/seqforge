"""The meter at the model seam — the one thing that counts, refuses and records.

:class:`~seqforge.harvest.providers.LLMProvider` is the only wire call in the compiler, and its
interface is deliberately shallow: a request in, an :class:`LLMResponse` out. That shallowness meant
every fact spanning two calls had to be reassembled by a caller — usage was re-summed in ten places,
the reported call count was the *document* count (so a retried document cost tokens nothing counted),
and there was nowhere to stand between two calls to say "stop".

:class:`TokenMeter` is that place. It satisfies ``LLMProvider`` and wraps one, so it drops in with no
registration and the three adapters stay untouched. It is:

- **the only thing that counts** — one :class:`Exchange` per real request, retries included;
- **the only thing that refuses** — a **Ceiling** on the tokens one run may spend, raised as a
  :class:`CeilingExceeded` that the CLI turns into a ``Blocker`` and exit 3;
- **the only thing that records** — the whole transcript, in memory. It writes no file: giving a
  transcript an address is the caller's job, and a meter that also chose a path would be two things.

**It never reads ``response.text`` for meaning.** It measures, and hands the response back
byte-identical. Post-processing, repairing or partially accepting a batch stays forbidden
(``docs/adr/0009``); this is a separate module at the same seam, not a wider adapter.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from .providers import LLMProvider, LLMResponse, ProviderUnavailable

#: What a Ceiling counts. **Raw**: fresh input, cached input, cache writes and output all count,
#: because a ceiling is a backstop and not a price — the cheap tokens are still tokens, and a run
#: that spends three million of them has gone wrong however little they cost.
#:
#: Only two keys, and that is not an omission: ``input_tokens`` is normalized by both providers to
#: mean EVERY input token the request was billed for, with ``cache_read_tokens`` and
#: ``cache_write_tokens`` recorded beside it as a breakdown of that same total (see
#: :func:`~seqforge.harvest.providers._anthropic_usage`). Adding the breakdown back in would count
#: the cached half twice and make the reading provider-dependent, which is the one thing a ceiling
#: at a pluggable seam must not be.
RAW_KEYS = ("input_tokens", "output_tokens")

#: Characters per token: near enough to plan a whole run with, and near enough to reserve one
#: request's cost with before it is issued. A plan is a warning about an order of magnitude — "this
#: dataset is 900 calls" — and a real tokenizer would buy a second decimal place nobody acts on
#: while adding a dependency and a model-specific answer to a question asked before a model is
#: chosen. One constant for both readings on purpose: the estimate that admits a request must be the
#: one ``harvest extract --dry-run`` printed, or a plan that fitted under a ceiling would be refused
#: by it. :class:`~seqforge.harvest.plan.ExtractionPlan` reads it from here.
CHARS_PER_TOKEN = 4


def raw_tokens(usage: Mapping[str, int]) -> int:
    """What one usage record costs against a Ceiling."""
    return sum(int(usage.get(key, 0) or 0) for key in RAW_KEYS)


def estimated_tokens(*prompt: str) -> int:
    """What a request of this size is expected to cost, before there is a response to measure.

    The halves of the prompt, in characters, over :data:`CHARS_PER_TOKEN`. It is an INPUT estimate:
    the output half is not estimable here at all — the model decides how many claims a document
    supports — which is why the meter floors a reservation with what an exchange has really cost.
    """
    return sum(len(part) for part in prompt) // CHARS_PER_TOKEN


class CeilingExceeded(RuntimeError):
    """The run reached its Ceiling, so the next request is refused rather than issued.

    Deliberately **not** a :class:`~seqforge.harvest.providers.ProviderUnavailable`: the provider is
    fine and another attempt would only spend more. The retry loop must not see this as something to
    back off from, and the CLI must not report it as ``llm_unavailable`` at exit 1 — it is a refusal
    with a remedy, which is a ``Blocker`` and exit 3.
    """

    def __init__(
        self,
        *,
        ceiling: int,
        spent: int,
        n_exchanges: int,
        subject: str = "dataset",
        reserved: int = 0,
        estimate: int = 0,
    ) -> None:
        # `spent` alone cannot explain a refusal that reserves: a run whose whole document set is in
        # flight has banked nothing and is still out of budget, and "0 tokens spent, ceiling 2,500"
        # reads as a bug rather than as a refusal. So the two numbers that DID decide it are named.
        in_flight = f", {reserved:,} reserved for requests in flight" if reserved else ""
        next_up = f" estimated at {estimate:,} tokens" if estimate else ""
        super().__init__(
            f"token ceiling reached: {spent:,} tokens spent over {n_exchanges} exchange(s) at the "
            f"model seam{in_flight}, against a ceiling of {ceiling:,}. Refusing to issue another "
            f"request{next_up}."
        )
        self.ceiling = ceiling
        self.spent = spent
        self.n_exchanges = n_exchanges
        self.subject = subject
        #: Tokens deducted from the budget for requests that were issued and have not yet returned.
        self.reserved = reserved
        #: What the refused request was expected to cost. A ceiling below this one number refuses
        #: the run at the gate, which is the only honest answer to "I cannot afford one request".
        self.estimate = estimate

    def blocker(self) -> Blocker:
        """The refusal, as the structured object every other refusal in the compiler is."""
        return Blocker(
            id="blk-token-ceiling-exceeded",
            code=BlockerCode.TOKEN_CEILING_EXCEEDED,
            message=(
                f"harvest spent {self.spent:,} tokens over {self.n_exchanges} exchange(s) and "
                f"could not fit another request under the {self.ceiling:,}-token ceiling for this "
                f"run. That request and every one after it was refused, so the extraction is "
                f"incomplete and nothing was written from it."
            ),
            remedy=(
                "Re-run with a higher ceiling if the dataset is genuinely this large "
                "(`--ceiling 2000000`), or with `--ceiling 0` to remove it. If the cost surprised "
                "you, read `seqforge/logs/usage.json` first: a dataset whose spend is dominated by "
                "one-line archive records is asking one question per run, and `--no-llm` compiles "
                "it from the bytes for nothing."
            ),
            subject=BlockerSubject(kind="dataset", ref=self.subject),
            evidence=[
                f"tokens_spent={self.spent}",
                f"tokens_reserved={self.reserved}",
                f"tokens_estimated_for_the_refused_request={self.estimate}",
                f"ceiling={self.ceiling}",
                f"exchanges={self.n_exchanges}",
            ],
        )


@dataclass(frozen=True)
class Exchange:
    """One request and the response it got, kept whole. A retry is its own exchange.

    ``prompt_sha256`` rather than the system prompt itself: it is byte-identical across every request
    in a run (that is what makes prefix caching work at all), so a transcript holds it once and every
    exchange points at it. :class:`Transcript` owns the mapping.

    A refused attempt is still an exchange — it spent tokens — and carries ``error`` with the
    provider's message, an empty ``text``, and whatever usage the failure reported.
    """

    prompt_sha256: str
    #: The volatile half of the prompt: the document, and which fields were asked of it.
    user: str
    #: What came back, verbatim and UNVALIDATED. Nothing here reads it for meaning.
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    mode: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def tokens(self) -> int:
        """What this exchange cost against the Ceiling."""
        return raw_tokens(self.usage)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "prompt_sha256": self.prompt_sha256,
            "model": self.model,
            "user": self.user,
            "text": self.text,
            "usage": dict(self.usage),
            "mode": dict(self.mode),
        }
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass(frozen=True)
class Transcript:
    """Every exchange of one run, assembled — **one prompt plus N (document, response) pairs**.

    Not N full exchanges. The system prompt is byte-identical across a run, and at 983 exchanges the
    difference between storing it once and storing it 983 times is the difference between a file a
    human opens and one they do not. ``prompts`` is keyed by sha256 and normally holds exactly one
    entry; it is a mapping rather than a single string so that a run which somehow issued two
    different prompts records that fact instead of hiding it.

    A snapshot: callers get the whole thing already assembled, and reassembling it from parts is
    nobody's job.
    """

    provider: str
    prompts: dict[str, str]
    exchanges: tuple[Exchange, ...]

    @property
    def n_exchanges(self) -> int:
        return len(self.exchanges)

    def prompt_for(self, exchange: Exchange) -> str:
        """The system prompt this exchange was made under."""
        return self.prompts[exchange.prompt_sha256]

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "prompts": dict(self.prompts),
            "exchanges": [e.to_json() for e in self.exchanges],
        }


class TokenMeter:
    """Wraps one provider and satisfies the same protocol. The only counter, refuser and recorder.

    **Identity is proxied, not rewritten.** ``ExtractorProvenance.model_id`` is
    ``f"{provider.name}/{model}"``, so a meter that reported its own name would silently restamp
    every assertion in a corpus with a provider that does not exist. ``name`` is copied from the
    wrapped provider, ``default_model()`` delegates, and anything else a caller reaches for falls
    through to the provider itself.

    **The Ceiling bounds what a run may SPEND, and it reserves to do it.** Extraction fans out over
    threads — a pool per document under a pool per case — so the running total is shared mutable
    state and every read of it happens inside :attr:`_lock`. A lock is not enough, though: a check
    on the tokens *already banked* asks a question whose answer is not yet knowable, because the
    work that will spend them is in flight and has banked nothing. Every worker in one wave passes
    such a check, and a run whose whole document set fits in one wave can never refuse at all. So a
    request's estimated cost is deducted from the budget **before** it is issued and reconciled
    against the real usage when the response returns:

    - a request is admitted only while the unspent budget still covers its estimate, so what a
      ceiling admits does not depend on how wide the pool that offered it was — the alternative,
      a race-free check on banked totals, would leave an overshoot of one whole wave and make a
      ceiling mean something different on a laptop and on a 48-core node;
    - the request the budget cannot cover, and every one after it, is **refused, un-issued** — a
      ceiling below one request's estimate refuses the run at the gate rather than allowing one
      arbitrarily large call;
    - requests already in flight are left alone. They finish, and their usage is banked — a run that
      killed in-flight sockets would report a spend it did not know it had made.

    **The bound is approximate, and claiming otherwise is the defect this replaced.** A response's
    token count is not knowable until it returns, so what a Ceiling promises is that a run will not
    overshoot it by more than approximately one request's cost — never that it cannot exceed it.
    The size of that "approximately" is the estimate's error, which is a property of this code
    rather than of the machine it ran on, and :meth:`_reserve` is where it can be improved.
    """

    #: A plain instance attribute, assigned from the wrapped provider in ``__init__``. The protocol
    #: declares ``name: str``, and a read-only property would not satisfy it.
    name: str

    def __init__(
        self,
        provider: LLMProvider,
        *,
        ceiling: int | None = None,
        subject: str = "dataset",
        record: bool = True,
    ) -> None:
        self._inner = provider
        #: ``0`` and ``None`` both mean "no ceiling", so a CLI default of 0 needs no special case.
        self._ceiling = ceiling if ceiling else None
        self._subject = subject
        self._record = record
        self._lock = threading.Lock()
        self._usage: dict[str, int] = {}
        self._tokens = 0
        #: Estimated tokens deducted for requests that are issued and have not yet returned. Held
        #: apart from ``_tokens``, which stays what the run has really spent: a ledger that counted
        #: a guess would report a cost nobody was billed for.
        self._reserved = 0
        self._n_exchanges = 0
        self._prompts: dict[str, str] = {}
        self._exchanges: list[Exchange] = []
        self.name = provider.name

    # ---- the provider protocol -------------------------------------------------------------
    def default_model(self) -> str:
        return self._inner.default_model()

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, max_tokens: int
    ) -> LLMResponse:
        reserved = self._reserve(system, user)
        try:
            response = self._inner.complete_json(
                system=system, user=user, schema=schema, model=model, max_tokens=max_tokens
            )
        except ProviderUnavailable as exc:
            # A refused call still burned tokens, and it is still an exchange: the retry above this
            # will issue another one, and a counter that skipped the failures would report the same
            # floor `len(documents)` already reported.
            self._bank(system, user, "", exc.usage, {}, model, error=str(exc))
            raise
        else:
            self._bank(system, user, response.text, response.usage, response.mode, model)
            return response
        finally:
            # Reconciled on EVERY path, including one this module does not know about. A request
            # that raised holds its estimate against the budget forever otherwise, and the leak
            # compounds: a few flaky documents early on would refuse a run that spent nothing.
            self._release(reserved)

    def __getattr__(self, item: str) -> Any:
        """Anything else the caller reaches for belongs to the wrapped provider.

        The underscore guard is load-bearing rather than tidy: ``__getattr__`` runs whenever normal
        lookup fails, including for ``_inner`` itself before ``__init__`` has assigned it, and
        looking ``_inner`` up through here would recurse until the stack ran out.
        """
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(self._inner, item)

    # ---- what it measured ------------------------------------------------------------------
    @property
    def n_exchanges(self) -> int:
        """Real requests issued, retries included. This is what ``n_calls`` means."""
        with self._lock:
            return self._n_exchanges

    @property
    def tokens(self) -> int:
        """Raw tokens spent so far — the number the Ceiling is compared against."""
        with self._lock:
            return self._tokens

    @property
    def ceiling(self) -> int | None:
        return self._ceiling

    def usage(self) -> dict[str, int]:
        """The banked usage, by normalized key. A copy: nothing outside may edit the ledger."""
        with self._lock:
            return dict(self._usage)

    def transcript(self) -> Transcript:
        """A snapshot of every exchange so far, assembled."""
        with self._lock:
            return Transcript(
                provider=self.name,
                prompts=dict(self._prompts),
                exchanges=tuple(self._exchanges),
            )

    # ---- internals -------------------------------------------------------------------------
    def _reserve(self, system: str, user: str) -> int:
        """Deduct what this request is expected to cost, or refuse it. Returns what was deducted.

        Two estimates, and the larger wins. :func:`estimated_tokens` reads the prompt this caller is
        about to send, which is the only thing knowable about the *first* request of a run — but it
        is an input-only rule of thumb, so it under-reserves by whatever the output turns out to be.
        A systematic under-reservation is not a rounding error here: it scales with how many
        requests are in flight, which is the machine-shaped overshoot reserving exists to remove. So
        once the run has banked an exchange, the reservation is at least what an exchange has really
        cost, and the guess stops mattering after the first wave.

        The mean is read inside the lock rather than through :attr:`tokens`, because a reservation
        must be decided from one consistent view of the budget: two reads under two acquisitions
        would let a request be admitted against a total that no longer holds.
        """
        if self._ceiling is None:
            return 0
        estimate = estimated_tokens(system, user)
        with self._lock:
            if self._n_exchanges:
                estimate = max(estimate, self._tokens // self._n_exchanges)
            if self._tokens + self._reserved + estimate <= self._ceiling:
                self._reserved += estimate
                return estimate
            spent, n, reserved = self._tokens, self._n_exchanges, self._reserved
        raise CeilingExceeded(
            ceiling=self._ceiling,
            spent=spent,
            n_exchanges=n,
            subject=self._subject,
            reserved=reserved,
            estimate=estimate,
        )

    def _release(self, reserved: int) -> None:
        """Give the estimate back once the request it was made for has settled.

        Deliberately after :meth:`_bank` rather than as part of it: for the moment between the two,
        a returned request's real cost and its estimate both count against the budget. That errs
        towards refusing, which is the direction a backstop should err in, and it keeps banking a
        response independent of whether the request that carried it was ever reserved for.
        """
        if not reserved:
            return
        with self._lock:
            self._reserved -= reserved

    def _bank(
        self,
        system: str,
        user: str,
        text: str,
        usage: dict[str, int],
        mode: dict[str, Any],
        model: str,
        *,
        error: str | None = None,
    ) -> None:
        digest = hashlib.sha256(system.encode("utf-8")).hexdigest()
        exchange = Exchange(
            prompt_sha256=digest,
            user=user,
            text=text,
            usage=dict(usage),
            mode=dict(mode),
            model=model,
            error=error,
        )
        with self._lock:
            self._n_exchanges += 1
            for key, value in usage.items():
                self._usage[key] = self._usage.get(key, 0) + value
            self._tokens += raw_tokens(usage)
            if self._record:
                self._prompts.setdefault(digest, system)
                self._exchanges.append(exchange)
