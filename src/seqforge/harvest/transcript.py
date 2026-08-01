"""Giving a transcript an address: the one reader and the one writer of the ``.jsonl`` form.

:class:`~seqforge.harvest.meter.TokenMeter` records every :class:`~seqforge.harvest.meter.Exchange`
and deliberately writes no file — a meter that also chose a path would be two things. This module is
the other half. It is the only place the on-disk shape is spelled, and it spells it twice (write and
read) so that nobody downstream has to re-derive the format from a writer.

**One prompt plus N (document, response) pairs**, which is what a transcript *is*: the system prompt
is byte-identical across every request in a run — that is why prefix caching works at all — so a
983-exchange transcript that stored it per exchange would be three megabytes of one repeated string.
The first line is the header (provider, and the prompts by sha256); every line after it is one
exchange, pointing at its prompt by sha. That also makes the file streamable: a reader that wants the
twelfth exchange reads twelve lines, and a writer never has to hold a document tree in memory.

Written whole and renamed into place, so a reader never sees a half-written transcript — the same
idiom the resolve cache uses for every artifact it writes.
"""

from __future__ import annotations

import json
from pathlib import Path

from .meter import Exchange, Transcript

#: Beside ``usage.json``, which is where the cost ledger already lives: the ledger says what a run
#: spent, this says what it spent it on.
TRANSCRIPT_FILENAME = "transcript.jsonl"


def write_transcript(path: Path, transcript: Transcript) -> Path:
    """Write ``transcript`` as JSON lines and return the path it landed at."""
    lines = [
        json.dumps(
            {
                "provider": transcript.provider,
                "prompts": dict(transcript.prompts),
                "n_exchanges": transcript.n_exchanges,
            },
            sort_keys=True,
        )
    ]
    lines += [json.dumps(exchange.to_json(), sort_keys=True) for exchange in transcript.exchanges]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_transcript(path: Path) -> Transcript:
    """Read one back. The round trip is the contract; a writer with no reader is a format nobody has."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"{path.name}: empty transcript (no header line)")
    header = json.loads(lines[0])
    return Transcript(
        provider=str(header.get("provider", "")),
        prompts=dict(header.get("prompts", {})),
        exchanges=tuple(_exchange(json.loads(line)) for line in lines[1:]),
    )


def _exchange(row: dict[str, object]) -> Exchange:
    usage = row.get("usage")
    mode = row.get("mode")
    error = row.get("error")
    return Exchange(
        prompt_sha256=str(row.get("prompt_sha256", "")),
        user=str(row.get("user", "")),
        text=str(row.get("text", "")),
        usage={k: int(v) for k, v in usage.items()} if isinstance(usage, dict) else {},
        mode=dict(mode) if isinstance(mode, dict) else {},
        model=str(row.get("model", "")),
        error=None if error is None else str(error),
    )


__all__ = ["TRANSCRIPT_FILENAME", "read_transcript", "write_transcript"]
