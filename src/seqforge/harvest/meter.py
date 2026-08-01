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


def raw_tokens(usage: Mapping[str, int]) -> int:
    """What one usage record costs against a Ceiling."""
    return sum(int(usage.get(key, 0) or 0) for key in RAW_KEYS)


class CeilingExceeded(RuntimeError):
    """The run reached its Ceiling, so the next request is refused rather than issued.

    Deliberately **not** a :class:`~seqforge.harvest.providers.ProviderUnavailable`: the provider is
    fine and another attempt would only spend more. The retry loop must not see this as something to
    back off from, and the CLI must not report it as ``llm_unavailable`` at exit 1 — it is a refusal
    with a remedy, which is a ``Blocker`` and exit 3.
    """

    def __init__(
        self, *, ceiling: int, spent: int, n_exchanges: int, subject: str = "dataset"
    ) -> None:
        super().__init__(
            f"token ceiling reached: {spent:,} tokens spent over {n_exchanges} exchange(s) at the "
            f"model seam, against a ceiling of {ceiling:,}. Refusing to issue another request."
        )
        self.ceiling = ceiling
        self.spent = spent
        self.n_exchanges = n_exchanges
        self.subject = subject

    def blocker(self) -> Blocker:
        """The refusal, as the structured object every other refusal in the compiler is."""
        return Blocker(
            id="blk-token-ceiling-exceeded",
            code=BlockerCode.TOKEN_CEILING_EXCEEDED,
            message=(
                f"harvest spent {self.spent:,} tokens over {self.n_exchanges} exchange(s) and "
                f"reached the {self.ceiling:,}-token ceiling for this run. Every request after it "
                f"was refused, so the extraction is incomplete and nothing was written from it."
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

    **The Ceiling is checked at the gate, under a lock.** Extraction fans out over threads — a pool
    per document under a pool per case — so the running total is shared mutable state and every read
    of it happens inside :attr:`_lock`. The check is *before* the request, on the total banked so
    far, which fixes the semantics a test can pin:

    - the request that carries the total past the ceiling **is issued** (its cost is not knowable
      until it returns, and refusing on a guess would be a different ceiling every run);
    - every request admitted after that point is **refused, un-issued**;
    - requests already in flight are left alone. They finish, and their usage is banked — a run that
      killed in-flight sockets would report a spend it did not know it had made.
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
        self._admit()
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
        self._bank(system, user, response.text, response.usage, response.mode, model)
        return response

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
    def _admit(self) -> None:
        """Refuse before the request when the run has already reached its Ceiling."""
        if self._ceiling is None:
            return
        with self._lock:
            if self._tokens < self._ceiling:
                return
            spent, n = self._tokens, self._n_exchanges
        raise CeilingExceeded(
            ceiling=self._ceiling, spent=spent, n_exchanges=n, subject=self._subject
        )

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
