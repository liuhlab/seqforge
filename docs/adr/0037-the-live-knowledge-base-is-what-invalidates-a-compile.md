# 37. The live knowledge base is what invalidates a compile

Date: 2026-08-05

## Status

Accepted. Closes the gap [ADR-0032](0032-a-spec-declares-the-shape-of-a-deposit.md) recorded against
[ADR-0005](0005-run-id-is-the-pairing.md)'s arrangement in its own consequences — *"composing an old
manifest under a new knowledge base reuses the old run id and overwrites, and nothing notices"* —
which is why the record that leaned on the property is not the record that fixes it. Called for by
[#333](https://github.com/liuhlab/seqforge/issues/333).

## Context

`compose` reads the **live** knowledge base. Not partly, and not as a fallback: `plan` calls
`load_spec(chemistry)` against whatever `kb/` is installed at the moment it runs, and three separate
things it emits are functions of what comes back —

- **the admission floor**: `_admit` applies `min_input_reads` off that spec, and the composer's own
  comment beside it says exactly what that means — *"the live KB's admission floor, applied here and
  recorded nowhere in the manifest"*. Which cells are in `config["samples"]` and `units.tsv` is
  therefore a function of a knowledge-base number;
- **`_resolve_params`**: every `backend.params` key STAR or chromap is handed;
- **`derived_params(spec)`**: the keys computed from the element model rather than typed into a spec.

`run_id` read `manifest.provenance.kb_version` — the value stamped at **fill** time, inside the
input. So `config.yaml` was a function of the live knowledge base while the identity of the directory
holding it was a function of a different one, and the two could disagree for as long as anyone liked.

**The failure is a silent overwrite, and it is demonstrable rather than theoretical.** Bump the
knowledge base, move a floor or a param, re-compose the *same* manifest without re-filling it: same
`run_id`, same `pipeline/<recipe>-<run_id>/` directory, different `config.yaml`, written over the
one that was there. Two compiles under two knowledge bases, one directory, no refusal, no warning,
and the earlier of the two gone. That is the exact collision ADR-0005 exists to have already fixed.

**ADR-0005 states the principle this violated, and the violation is visible from its own words.**
The pairing *"is recorded at compile time, in the compiled output — never inside either input"*, and
*"the pairing is a fact about a compile, so it belongs to the compile's output and nowhere else."* A
component of that key sourced from an **input** is the shape that record is about. It was written
against a manifest storing a `processing_hash`; the same defect arrived by the other door, as a
compile reading its own key out of a manifest.

**This was the only place it happened.** Everything else that folds a knowledge-base version into a
content address already uses the live one: `resolve/engine.py` hands `KB_VERSION` to
`resolve/cache.py` for the per-dataset candidates key, and `manifest/fill.py` stamps `KB_VERSION`
into provenance at fill — which is what makes that recorded value *the KB that decided this
chemistry* and not a general-purpose stamp. `compose` was the single reader that took a **recorded**
version into a cache key.

The same defect had a second instance, in prose rather than in a hash. `compose/admission.py`'s
exclusion record — the file a human opens to find out why a cell left the pipeline — already said the
floor was *"read from the knowledge base loaded at compile time (`{kb_version}`)"*, while the value
interpolated into that sentence was the one the manifest recorded at fill. The sentence was already
describing the decision taken here; only the argument was wrong.

## Decision

**The `kb` component of `run_id` is the knowledge base loaded at compile time, never the one the
manifest recorded at fill time.**

The formula is unchanged — it is still `H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, four components,
in that order, stated precisely in [ADR-0005](0005-run-id-is-the-pairing.md) and glossed everywhere
else. What changes is where one component is **read from**, and nothing else.

| | |
| --- | --- |
| **the source** | `KB_VERSION`, imported from `kb`, at the moment `compose` runs |
| **what it is not** | `manifest.provenance.kb_version`, which is an input and cannot describe a compile |
| **who else moved** | `render_record` in `compose/admission.py`, so the exclusion record's *"loaded at compile time"* names the version it was actually read from |
| **what the manifest keeps** | `provenance.kb_version`, unchanged in value and in meaning: the knowledge base that decided that manifest's chemistry |
| **what is disclosed** | `seqforge compose` writes one line on **stderr** when the recorded version and the live one differ, naming both and saying which one the params and the floor came from |

## Why not a per-spec fingerprint

The precise version of this decision is to hash the *spec that was actually loaded* — the chemistry's
own `spec.yaml`, content-addressed — so that a compile is re-keyed by a change to the entry it uses
and by nothing else. It is strictly more accurate than what is chosen here, and it loses on two
counts.

It invents a hash nobody has. No spec fingerprint exists anywhere in this tree today; `kb roundtrip`
proves an entry recovers what it declares and computes no address for it. Adding one means deciding
what is inside it (the file bytes? the loaded model? the derived params, which are computed from the
element model and would otherwise be invisible to it?), and every one of those choices is a new way
for two compiles to be judged identical when they are not.

And it gives `run_id` a **fifth component**, or else a fourth whose meaning varies with the dataset.
ADR-0005 fixes the key at four, and the value of that is not tidiness: it is that the components are
enumerable, all four are CalVer, and a reader can say what each one is. A key whose third element is
"the version, unless the chemistry is one we have fingerprinted" is a key nobody can reason about.

## Why not keep the recorded value and refuse on divergence

Compose already knows both numbers, so refusing when they differ is one comparison and reads like the
safe answer: the user re-fills, the manifest's recorded version catches up, and the old arrangement
keeps working. It is the alternative a future reader will reach from the same evidence.

It adds a refusal, and [ADR-0012](0012-produce-every-answer-rather-than-ask.md) is directly against
one here — *produce every answer rather than ask*. Every knowledge-base bump would refuse every
existing manifest in a corpus of 10⁴ datasets until each was re-filled, and re-filling is the
expensive stage: it re-probes bytes. There is nothing ambiguous to arbitrate, either. The compiler
knows precisely what to do with an old manifest under a new knowledge base — compile it under the new
one — and a refusal is what you write when you do not.

What survives from that reading is the *disclosure*, without the refusal attached: the stderr line,
which says the chemistry was decided under an older knowledge base and that the params and any floor
came from this one. Advisory, non-mutating, and it changes no exit code.

## Why the manifest's own `kb_version` does not move

It is tempting to conclude that a fill-time knowledge-base version is now the wrong thing to record
at all. It is not. `provenance.kb_version` answers a different question — *which knowledge base
decided this library's chemistry* — and that is a fact about a decision that was taken once, on
bytes, and is not retaken at compile. It is provenance in the sense
[ADR-0030](0030-a-measurement-lives-in-provenance.md) uses the word, and it stays exactly where it is
and means exactly what it meant. What was wrong was a *compile* reading it, not the recording of it.

## So in code

**A cache key names the run that produced it, so every component of one is read at the moment the
work is done — never from an artifact that was written earlier.** Concretely: `compose` imports
`KB_VERSION` from `kb` and hands that to `run_id`; a `manifest.provenance.*` version appearing in any
cache key is the defect this record fixes, arriving again. The test to apply when adding the next
component is the one this failed: *can the output change while the key does not?* If the stage reads
something live — a loaded spec, an installed binary, a registry — and keys on something recorded,
then yes, and the answer is to key on what it read.

**And a sentence that describes when a value was read must be handed the value that was read then.**
`render_record` said *"loaded at compile time"* over a fill-time number for as long as it existed;
prose in a generated artifact is a claim, and it is the kind nothing type-checks.

**Enforced by.** `test_a_kb_bump_re_keys_the_compile_without_moving_the_dataset_hash`
(`tests/test_compose.py`) — one manifest composed twice across a patched `KB_VERSION`, asserting two
surviving directories, an unmoved `dataset_hash` and a manifest compose did not rewrite. It patches
the name where `compose.core` binds it, which is the only place a patch proves anything about the
sourcing. Nothing yet holds the stderr disclosure line: it would take a CLI test composing a manifest
whose recorded version differs from the live one, and no fixture produces one today — every manifest
the suite builds is filled under the live knowledge base, which is precisely why this defect survived
a suite that composes constantly.

## Consequences

- **This over-invalidates, and that is the trade taken.** `KB_VERSION` is one repository-wide CalVer
  string, so adding an unrelated chemistry re-keys **every** compile in the corpus. The alternative
  is a fingerprint nobody has (above), and the asymmetry decides it: over-invalidating costs disk and
  a recompile, both of which are visible and recoverable, while under-invalidating is a directory
  silently overwritten with different contents, which is neither.
- **ADR-0032's separation now holds without a re-fill.** That record's argument — bump the knowledge
  base, get a new run directory rather than an overwrite, with one unmoved `dataset_hash` — was
  stated for the ordinary flow and depended on the re-fill carrying the bump into the id. It now
  holds for the flow that skips the re-fill too, which is the flow a corpus of 10⁴ datasets will
  actually take.
- **`dataset_hash` is untouched.** Nothing in this record reaches the manifest: what the data IS did
  not change, only what identifies a compile of it. A `processing.yaml` pinned to a dataset stays
  pinned.
- **An old pipeline directory is never invalidated in place, only left behind.** Re-composing under a
  newer knowledge base writes a new directory beside the old one; the old one keeps its config, its
  units table and its copy of the workflow module, and remains exactly as reproducible as it was. The
  cost of the over-invalidation above is therefore paid in directories, which is the currency this
  compiler is cheapest in.
- **The two knowledge-base numbers in a compile can now legitimately differ**, and only the stderr
  line says so. A reader of a pipeline directory sees a `run_id` keyed on one version and, in the
  manifest it was compiled from, another — with no artifact in the directory reconciling them. That
  is a real gap, and the smaller fix (a stderr line) was taken over the larger one (a compile record
  in the pipeline directory) because nothing yet needs to answer the question after the fact.
