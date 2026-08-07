# Knowledge base

The knowledge base of sequencing chemistries: one directory per technology, declaring how its reads
are laid out, how to detect it from bytes, and which other technologies it can be confused with.

Words every context shares — **Evidenced**, **Basis**, **Observed**, **Conflict** and the rest — are
defined once in the repo-root `CONTEXT-MAP.md`.

## Language

### What a spec declares

**Role**:
What a read *is* within a chemistry — a KB spec's read id (`R1`, `bc`, `cdna`). An open label the
spec names, never a filename claim.
_Avoid_: read type, mate, file kind, R1/R2 as identity

**Read set**:
One complete set of a **Spec**'s roles that a **Role assignment** may fill — a subset of read ids,
never a second declaration of a read. A spec declares a maximal set and may name subsets of it, so
one entry covers the paired-end and single-end configurations its protocol publishes.
_Avoid_: configuration, layout (a layout is the KB's declared structure), mode, variant, flavour

**Chemistry**:
The library construction the bytes are evidence for, named by KB spec ids. Carried as an equivalence
class, because CI-proven twins (v3 and v3.1) are recorded together rather than chosen between.
_Avoid_: kit, platform, protocol, version; `technology` is the field name in code — prefer chemistry
in prose

**Family term**:
A chemistry claim naming a family **Spec** rather than a leaf — "10x 3'", not `10x-3p-gex-v3`. It
*narrows*: an observed leaf inside that node's subtree satisfies it, so it is agreement and never a
**Conflict**.
_Avoid_: partial match, vague chemistry, family-level conflict

**Spec**:
One node of the KB — a directory holding `spec.yaml` (read layout, onlist refs, detection signature,
backend params) plus a `README.md`. Executable and self-testing: `kb roundtrip` proves it recovers
what it declares.
_Avoid_: config, definition, rule, profile; the *schema* is what validates a spec, not the spec
itself

**Backend params**:
A spec's parse half — how to *read* reads (`soloType`, CB/UMI offsets, whitelist, strand, barcode
match mode). Decided by bytes and never instructable; what to *count* belongs to the **Recipe**, and
the two key sets are disjoint.
_Avoid_: settings, options, aligner flags

**Read-through**:
The sequence past which a read has stopped being genomic — the non-genomic tail a fragment shorter
than the read runs off the end of its own cDNA into, whatever that construction puts there (an
adapter, a poly-A run). **Terminal**, so the whole tail goes, which is what makes it a fact about the
molecule and therefore a **Spec**'s rather than a **Recipe**'s: a trim is a choice with alternatives,
a read-through is where the fragment ended (ADR-0048). Declared once per chemistry, and every
pipeline derives its own flag from it.
_Avoid_: trim, adapter trimming; *clip* is what a pipeline does with one, not the thing itself

**Onlist**:
A barcode whitelist, identified by the *set* of barcodes it holds rather than by the file carrying
them. A pipeline builds one by rule and deletes it, never storing it expanded.
_Avoid_: allowlist, barcode file, reference list; "whitelist" names the vendor's file, `onlist` is
the spelling on the wire

### Telling two specs apart

**Confusable**:
A declared pair of specs the cheap rungs cannot separate, naming the mechanism that can. Declaring
it is mandatory — CI fails a pair that collides at rungs 0-2 in silence.
_Avoid_: ambiguous, similar, overlapping, competing

**Answerable**:
Whether *these bytes* could have answered a signature test at all. An unanswerable test leaves the
support numerator **and its normalizer**, because no chemistry could have got an answer there.
_Avoid_: inapplicable (that is the **Read set** rule below), unmeasured, missing, N/A

**Unconfirmed**:
A signature test the bytes *were* willing to answer and we could not ask — the whitelist was not
registered or would not materialize. It keeps its full weight in the normalizer: a spec is never
credited for evidence nobody was able to check.
_Avoid_: unavailable, failed, missing whitelist

**Inapplicable**:
Reserved for the **Read set** rule: a signature test addressed to a read the *active* set does not
carry has no cell at all. The same arithmetic as an unanswerable test, reached from the declaration
rather than from the bytes.
_Avoid_: using it for either of the two above

**Processing-equivalent**:
Two specs whose canonical backend params — onlists resolved, role placement included — are
byte-equal: they parse reads identically. A tie between them is recorded as an equivalence class and
asks zero questions.
_Avoid_: identical, interchangeable, duplicate; "benign" is the **Conflict** status this produces,
not the relationship

**Processing-divergent**:
Two **Confusable** specs that would parse reads differently. A tie between them is the one trigger
that escalates past **Rung** 3, and only after metadata fails to settle it.
_Avoid_: incompatible, contradictory, mutually exclusive
