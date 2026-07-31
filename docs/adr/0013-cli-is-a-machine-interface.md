# 13. The CLI is the API — refusal is an exit code, JSON is the default, resume is implicit

Date: 2026-07-31

## Status

Accepted.

## Context

Every skill action maps to a deterministic `seqforge <verb>` (R6), and `run` drives the whole
compiler headless from a single `claude -p` turn. The obvious conveniences — a `--json` flag, a
`--resume` flag, a friendly message on refusal — each hand a decision back to the caller, which is
exactly the population (agents, batch scripts) least able to make it.

## Decision

Three conventions, one shape: **the caller is a machine, and a machine reads the exit code.**

1. **Machine JSON to stdout, human logs to stderr**, so stdout is a clean pipe. There is **no
   `--json` flag** — JSON is the default and the only machine format (`kb list` is the one
   plain-text verb).
2. **Refusal is an exit code**, uniform across every verb:

   | code | meaning |
   | --- | --- |
   | `0` | OK |
   | `1` | ERROR (bug/IO — *not* a domain refusal) |
   | `2` | USAGE |
   | `3` | BLOCKED (≥1 `Blocker`) |
   | `4` | NEEDS_HUMAN (open `Conflict` / non-empty `questions.md`) |

3. **No `--resume` flag.** Re-running resumes through each stage's content-addressed cache (R5);
   `--no-cache` opts out.

## Why no `--json`

A machine format behind a flag is a flag every caller can forget, and a verb that prints prose by
default will grow prose consumers that break the day it stops. Making JSON the default makes the
*human* view the exception, which is the correct direction for a headless compiler.

## Why 3 and 4 are different codes

They route differently. **3 is a refusal no human answer can clear** — a truncated gzip, an
unsupported technology. **4 is one a human can**: an open `Conflict`, a pending question. An agent
must be able to tell "stop and report" from "ask and retry" without parsing prose.

For the same reason a `Blocker` is **always fatal** and advisory diagnostics are a separate
`Warning` type (non-blocking, exits 0): branching code never inspects a severity field to learn
whether something blocks. `probe` / `io peek` never return 3 or 4 — they only observe; refusal
happens downstream, when a validator reads the observation.

## Why no `--resume`

A resume flag is the caller asserting what has already been done. The cache already knows, keyed by
content — so the flag could only ever *disagree* with it, and the disagreement would be silent.

## So in code

**Machine JSON to stdout, human logs to stderr, refusal as an exit code.** Never add a `--json`
flag, never add `--resume`, and never branch on a severity field: a `Blocker` is always fatal and a
`Warning` never is, so the *type* is the decision. Return 3 for a refusal no human answer can clear
and 4 for one a human can, uniformly across verbs — an orchestrator must tell "stop and report" from
"ask and retry" without parsing prose. Every `Blocker` you raise carries an actionable `remedy` and a
`subject` that is a basename, a dotted path or a dataset id, never an absolute path.

**Enforced by.** `test_the_cli_surface_exits_and_answers_as_documented` (`tests/test_cli.py`);
`test_skill_documents_only_real_cli_verbs` (`tests/test_skills.py`).

## Consequences

- Every `Blocker` carries an actionable `remedy` and a `subject` that is a basename, a dotted path,
  or a dataset id — never an absolute path (R7). `MISSING_TECHNICAL_READ`'s remedy is operable:
  re-fetch with `fasterq-dump --include-technical`, or pull `sra-pub-src-*` via the SDL API.
- `manifest validate` returns the same 4 for an open `Conflict` that `run` does, so the orchestrator
  has one rule rather than a per-verb table.
- The `Stop` hook and exit 4 are the only ways ambiguity clears, and both route to a human — which
  is what keeps a headless batch to **one** LLM touchpoint (`harvest extract`).
