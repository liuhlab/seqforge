# 1. A probe joins a head to a whole file; there is no read-source seam

Date: 2026-07-30

## Status

Accepted.

## Context

`build_observation` exported an 8-parameter interface, and four callers each re-assembled the same
orchestration longhand:

| caller | head from | identity from |
| --- | --- | --- |
| `probe.probe_sample` | a local file | a bounded key over basename + size + ISIZE + head |
| `io.remote.probe_remote` | an HTTP range read | the provider md5, else a bounded key with no ISIZE |
| `io.sra.probe_sra` | a re-serialized spot stream | the ENA md5, else a synthetic whole-run address |
| `fingerprint.probed_from_fingerprint` | a head-slice | a **pin** — a file it is not reading |

An architecture review read this as a missing abstraction and recommended a `ReadSource` adapter: one
protocol, four implementations, each knowing how to produce bytes and name what produced them.

That reading is the obvious one, and a future reader will reach it again from the same evidence. This
record exists so they find the reason it was rejected rather than re-deriving a design that does not
work here.

## Decision

**No read-source seam.** Instead, `build_observation(head, file)` takes two values:

- `FastqHead` — what was read and how: the records, the `Budget` that bounded them, and the
  `source_path` they came from (`None` for a stream).
- `WholeFile` — what the file *is*: `basename`, `sha256`, `size_bytes`, `isize`.

Each source constructs its own `WholeFile`, in the module that holds the knowledge:
`probe.local_whole_file`, `io.remote.hosted_whole_file`, `io.sra.sra_whole_file`,
`fingerprint.load.pinned_whole_file`. `probe` owns the *type*, not the construction.

## Why not the seam

**1. It cannot live in `probe/`.** An adapter that range-reads HTTP or streams SRA needs `requests`
and `labdata`. Putting it in `probe` forfeits the stdlib + numpy + `models.observation` foundation
status that lets `fingerprint`, `io` and `resolve` all depend on `probe`. The alternative — `probe`
exports a protocol, three outside modules implement it — adds machinery in four places to remove
longhand from four places. Complexity moves and grows.

**2. There is no read variance left to abstract.** PR #96 already unified reading behind one
`BoundedReader`; all four callers obtain a head identically. A `ReadSource` would abstract over a
difference that no longer exists.

**3. One of the four is not a read source.** `probed_from_fingerprint` reads a slice and describes the
*original*. "A source you can read bytes from" is the wrong shape for a thing whose defining property
is that the bytes and the identity belong to **different files**.

## Why not "one owner for file identity"

The tempting second framing — since the reading is unified, unify the naming — is also wrong, and for
a sharper reason: **there cannot be one owner.** The knowledge is irreducibly distributed. A local key
needs a gzip trailer only a local file has. A hosted address needs the provider md5 and must know that
a range read can *never* reach an ISIZE. An SRA address needs whole-run archive metadata `probe` has no
business seeing. Four naming authorities is correct; what was wrong was that they had no shared type.

## Consequences

- `build_observation` goes from 8 parameters to 2. Nothing is passed twice.
- `params_hash` and `local_uri` are **derived from the read** rather than re-supplied. They previously
  came from parameters a caller passed alongside a budget it merely promised it had used; a caller
  that passed a different one stamped a hash that lied, and nothing would have noticed —
  `params_hash` is written in one place and read nowhere.
- `local_uri` moves onto the head. It is *where bytes were read*, not *what the file is*: at
  `probed_from_fingerprint` it names the slice while every other identity field names the original.
  The on-disk format had already made this call — `FilePin` carries the four whole-file facts and no
  `local_uri`.
- `FileIdentity` is unchanged. It stays the narrow boundary `resolve/records.py` consumes, and `isize`
  never enters it — a gzip-trailer fact has no business in the sample resolver.
- The `sha256=` parameter on `probe_sample`/`probe_file` is deleted. It was the documented injection
  path for a provider md5 and in its whole lifetime nothing ever passed it; the md5 is adopted in
  `io/`, where the md5 is known. A caller that genuinely needs a different identity for local bytes
  builds the `WholeFile` and calls `build_observation` — that is now the injection path.
- The projection from `FilePin` lives in `fingerprint.load`, not as a `FilePin` method. `models` is
  the foundation `probe` imports; a model method returning a probe type would invert that.

## Alternatives considered

- **`ReadSource` protocol with four adapters.** Rejected: reasons 1–3 above.
- **Pass `FileIdentity` plus a loose `isize`.** Rejected: `isize` travels with identity at all four
  sites, so `probe_sample` would keep threading it twice — the exact defect being removed elsewhere.
  And `isize` cannot join `FileIdentity` without widening the metadata resolver's input.
- **Widen `FileIdentity` to carry `isize`.** Rejected on the same architectural ground.
- **Sequence the budget change separately.** Rejected: it edits the same signature and the same four
  call sites, so sequencing means rewriting all four twice to land an intermediate shape nobody wants.
