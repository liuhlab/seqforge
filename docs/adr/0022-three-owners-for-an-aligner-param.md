# 22. An aligner param has three owners, not two

Date: 2026-08-03

## Status

Accepted. Amends [0011](0011-closed-instructable-surface.md) — specifically its claim that the
CellRanger-parity knobs *"are applied at `compose` time from the recipe"*, which no code ever
implemented. The parse/count split 0011 exists to state is untouched; what changes is that a third
owner, which had been shipping undocumented since `--outSAMtype` was first hardcoded, is now named.

## Context

0011 divided every aligner param between two artifacts: the KB spec says how to **parse** reads and is
never instructable; the recipe says what to **count** and is. Because the key sets are disjoint, *"a
user instruction contradicts the bytes"* is inexpressible. That is right, and it stands.

Its section *"What moved out of the KB backend"* then recorded a third claim: the CellRanger-parity
knobs — `soloUMIdedup 1MM_CR`, `soloUMIfiltering MultiGeneUMI_CR`, `clipAdapterType CellRanger4`,
`outFilterScoreMin 30` — *"are processing **policy**, not chemistry. They are applied at `compose`
time from the recipe."*

Grepped on `main` at `70ba9fd`, none of the four appeared in `starsolo.smk`, in
`models/processing.py`, or in any `spec.yaml`. **They moved out of the KB and landed nowhere.** For as
long as that was true, every matrix seqforge shipped was counted with STARsolo's *defaults*, which are
not the same numbers CellRanger produces — a real defect for a corpus whose stated value is
comparability against published matrices, and the defect [#198](https://github.com/liuhlab/seqforge/issues/198)
was opened over.

**The obvious reading, on arriving at that gap, is to do what 0011 says: add the four to the recipe.**
A reader will reach it from the same evidence — the ADR is explicit, the keys are absent, the fix
looks like one `SoloQuant` field per knob. This file exists so they do not have to re-derive why that
is the wrong home, and so the third owner is written down instead of being inferred from the one
literal that already used it.

Because the third owner did already exist. `--outSAMtype` has been a bare literal in `star.smk` and
`starsolo.smk` since before 0011 was written — no spec declares it, no recipe carries it, and the
params gate has never seen it. It is neither a parse fact nor a count fact, and it was never a policy
anyone could instruct. It was a fact about *driving the aligner*, filed nowhere, and it worked.

## Decision

**An aligner param has exactly one of three owners, and which one is decided by what the value varies
with — not by what the value is for.**

| owner | the value varies with | instructable | where it lives |
| --- | --- | --- | --- |
| the **KB spec** | the chemistry | never — decided by bytes and by the vendor | `backend.params` |
| the **processing manifest** | the user's intent | yes | the recipe |
| the **workflow module** | nothing | no — there is one correct value for every dataset | a literal in the `.smk` shell block |

So the CellRanger-parity set, plus `--soloCellFilter EmptyDrops_CR`, is hardcoded in
`workflows/map/starsolo.smk`. `--outSAMtype`, `--outSAMattributes` and `--limitBAMsortRAM` are the
same owner and always were.

**`soloCBmatchWLtype` is the exception that proves the rule is about variance**: its 10x value was
chosen for CellRanger parity, exactly like the four above, and it is nevertheless the **KB's** — see
below.

## Why not the recipe, which is what 0011 said

1. **Nothing varies.** A recipe key exists so that a user may set it. `clipAdapterType CellRanger4`
   has one correct value for every dataset seqforge will ever compile; a knob whose only other
   reachable state is "wrong" is not a choice, it is a trap with a UI.
2. **It widens a surface whose whole point is being closed.** 0011 and R11 make the instructable
   surface small on purpose. Five keys nobody may sensibly set are five new ways for a recipe to
   disagree with the corpus it was compiled beside, guarded by nothing but the reviewer's memory of
   which five values were right.
3. **The value is not about the dataset at all — it is about which STAR we drive.** If a future
   `align-rna` renames `MultiGeneUMI_CR`, a recipe-owned knob means editing every `processing.yaml`
   ever written. A module literal means editing one file and bumping `WORKFLOW_VERSION`, which is
   precisely the `run_id` axis that already exists for *"the module changed"*
   ([0005](0005-run-id-is-the-pairing.md)).

## Why not the KB, for the parity set

`required_config` is **computed from the module source** (`workflows/__init__.py::keys_read_by`), so
the choice of owner is made by how the flag is written, not by anything anyone declares. A
`{params.solo[clipAdapterType]}` subscript silently obliges all eleven starsolo specs to declare a
value that is identical in all eleven — eleven copies of one fact, each free to drift.

It would also corrupt a predicate. `backend_identical` means *"parses reads identically"*, and 0011
sharpened it by removing count keys precisely so two specs differing only in policy stay equivalent.
Putting a parity knob back into `backend.params` walks that back.

## Why `soloCBmatchWLtype` IS the KB's, though its 10x value was chosen for CellRanger parity

Apply 0011's own test — *"a new key belongs to `backend.params` only if reads cannot be parsed without
it"* — and the answer is yes, literally:

- For the four `CB_UMI_Complex` specs, **STAR's global default `1MM_multi` is rejected outright** for
  that soloType. A Complex chemistry that names no match mode does not parse reads badly; it FATALs
  after the genome loads. Measured against the pinned STAR 2.7.11b binary, run directly rather than
  read off `--help`, over all six values × both soloTypes:

  | value | `CB_UMI_Simple` | `CB_UMI_Complex` |
  | --- | --- | --- |
  | `Exact`, `1MM` | legal | legal |
  | `1MM_multi`, `1MM_multi_pseudocounts`, `1MM_multi_Nbase_pseudocounts` | legal | **rejected** |
  | `EditDist_2` | **rejected** | legal |

- And it **varies with the chemistry**, which no member of the parity set does: `1MM_multi_Nbase_pseudocounts`
  for the seven 10x specs, `1MM` for BD Rhapsody and SPLiT-seq, `EditDist_2` for a planned Parse
  Evercode entry. The last two share a soloType, which is why the `soloType` branch the module used to
  carry could not express it: a yes/no branch yields two answers where three are needed.

The parity rationale attaches to *which value 10x takes*, not to *whether the key is chemistry-decided*.
Owner is decided by variance; "this value was picked to match CellRanger" is a fact about one row.

## Why the legality pair is gated at compose rather than left to STAR

An illegal `(soloType, soloCBmatchWLtype)` pair is a hard STAR FATAL, and STAR raises it **after** the
genome has loaded — so it costs a queue wait and a node before it says anything. `params_gate` refuses
it by name at compose, which is that gate's stated job: the semantic assertions a dry run cannot make.
The same check refuses a starsolo spec that declares no match mode at all, because the module
dereferences the key unconditionally and silence is a `KeyError` at Snakemake parse time on the same
node.

Note the shape of the bug this closes: **every wrong answer here leaves all seven 10x specs green and
breaks only the four Complex ones.** A 10x-only suite cannot see it.

## So in code

**Before adding an aligner flag, say what its value varies with — and if the answer is "nothing", it
is the module's, written as a literal.** The mechanism is not a registry you update: `required_config`
is scanned out of the module source, so writing `{params.solo[<key>]}` *makes* the key the KB's and
obliges every spec on that module to declare it, while a literal keeps it out of the gate entirely.
Do not put a value that never varies into a spec or a recipe, and do not put one that varies per
chemistry into the module — the `soloType` branch that pinned `1MM` is what that mistake looks like
after the second chemistry with the same soloType arrives.

**Enforced by.** `test_every_chemistry_emits_its_required_keys_and_passes_the_params_gate` and
`test_soloCBmatchWLtype_is_required_of_every_starsolo_chemistry` (`tests/test_compose.py`) for the
KB-owned half; `test_the_composed_pipeline_plans_the_h5ad_the_whitelist_and_the_command_star_receives`
(same module) reads the module's literals out of the rendered argv, which is the only place a literal
is visible at all; `test_every_starsolo_spec_declares_a_cb_match_type_its_solotype_accepts`
(`tests/test_kb.py`) holds the eleven specs against the measured legality matrix.

**Nothing enforces the third owner's boundary in the other direction.** A module literal that *should*
have been a KB key — a flag someone hardcodes today and a second chemistry needs to differ on tomorrow
— is invisible to every gate here, because a literal is by construction not a config key. It surfaces
as a wrong matrix from `kb e2e`, or not at all. Noticing it would need a check that reads the shell
block for flags absent from every owner's key set, and no such check exists.

## Consequences

- **0011's disjointness is untouched.** Parse keys and count keys are still disjoint and still
  policed; this record adds an owner whose keys are in neither set because they are in no config at
  all.
- **The module literal is invisible to the params gate, by construction, and that is the price.** The
  gate proves the emitted key set is exactly `union(KB, processing, derived)`; a literal is not
  emitted, so a wrong one is caught by `kb e2e`, by a real run, or by a reader — not here.
- **Three documents were false the moment this landed and were corrected with it**:
  [0011](0011-closed-instructable-surface.md)'s *"applied at compose time from the recipe"*,
  [`docs/agents/kb.md`](../agents/kb.md)'s *"What moved out of the backend"*, and `CONTEXT.md`'s
  **Backend params** entry.
- **One flag from the same source set was rejected rather than filed.** `--soloMultiMappers EM Uniform`
  is not a third-owner literal; it is not adopted at all. 87 % of the multi-gene signal on the measured
  library was the tandem rDNA array, where EM splits identical copies evenly and emits a large
  arbitrary number that reads as data, and all four multimapper matrices are fractional, which breaks
  pseudobulk. Ownership answers *where a value lives*, never *whether it is right*.
- **A Parse Evercode entry is now the test of whether this was done right**: it declares
  `EditDist_2` in its own `spec.yaml` and needs no module change. If it ever needs one, this record is
  wrong.
