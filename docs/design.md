# seqforge — Design Document

**Status: design review, pre-implementation.** This document is the authoritative design for the
schemas and algorithms; [`../PROJECT_BRIEF.md`](../PROJECT_BRIEF.md) is the rationale, and
[`../CLAUDE.md`](../CLAUDE.md) is the enforced rule set (R1–R12). No package is scaffolded yet — the
maintainer reviews this design first.

It was produced by reading the brief in full, extracting conventions from `liulab-compute-skills`,
`liulab-runtime`, and `liulab-genome`, and running a multi-agent design workflow (4 drafters → per-
section adversarial verification → a cross-section completeness critic). The reconciliation the
critic demanded — one canonical spelling for every wire-format, and the previously-missing
score/compile output models — is applied here. Genomics values that could not be verified from first
principles are collected under **§8 FLAGS**; do not ship them without checking.

**Held-out acceptance case.** `/scratch/zhoulab/hanliu/260612-worm/PRJNA1027859` is untouched and
stays untouched (see §9).

---

## 0. Governing metaphor and the four stages

`seqforge` is **a compiler, not a chatbot**. Deterministic code owns every decision; the LLM has
exactly two jobs — (a) parse prose into span-verified `Assertion`s, (b) arbitrate ambiguity code has
*already flagged*.

```
probe(files)                     -> Observation                deterministic, no LLM, no network, bytes only
harvest(prose, metadata)         -> Assertion                  LLM extract, deterministic span-verify
score(Observation, KB, hypo?)    -> Candidates x RoleAssignment, Conflicts, Questions
compile(Decision)                -> config + workflow-module selection   deterministic
```

Stage → principle map (brief §3, rules R1–R12): probe ⇒ R3/R10/R11; harvest ⇒ R1/R5; score ⇒
R2/R6/R11; compile ⇒ R1/R9. All stages ⇒ R7 (disk is state), R8 (CLI is the API).

---

## 1. Pydantic v2 model hierarchy

Target py3.12+, `pydantic>=2`, `mypy --strict` on `models/`. Concrete `Evidenced[...]` subclasses
precede any model that references them (a parametrized generic subclass is a class statement, not a
deferred annotation), so the module concatenates and compiles top-to-bottom.

### 1.0 Scalars & controlled vocabulary

```python
from __future__ import annotations
from enum import Enum
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import (BaseModel, ConfigDict, Field, PositiveInt,
                      StringConstraints, AfterValidator)

def _reject_absolute_or_local(s: str) -> str:
    """P9/R9: a manifest URI is a relative path, a non-file scheme (s3://,gs://,https://,sra:),
    or a bare accession — NEVER an absolute or local filesystem path."""
    bad = (s.startswith(("/", "~")) or s.startswith("file:///")
           or (len(s) > 1 and s[1] == ":")          # C:\ ...
           or s.startswith("\\\\"))                    # UNC \\host\share
    if bad:
        raise ValueError(f"absolute/local path forbidden in a manifest URI: {s!r}")
    return s

Sha256    = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Uri       = Annotated[str, StringConstraints(min_length=1), AfterValidator(_reject_absolute_or_local)]
LocalPath = Annotated[str, StringConstraints(min_length=1)]        # internal-only (Observation.local_uri)
AssayTerm = Annotated[str, StringConstraints(pattern=r"^(EFO|OBI):\d{4,}$")]   # EFO/OBI CURIE
NcbiTaxid = PositiveInt                                             # 9606, 10090, 559292, 6239, ...
# NCBI + ENA + DDBJ runs/experiments/studies/samples, GEO, BioProject, and EBI/DDBJ BioSample:
Accession = Annotated[str, StringConstraints(
    pattern=r"^([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)$")]
ChemistryId = str                          # KB primary key, e.g. "10x-3p-gex-v3"; validated vs KB in code
Confidence  = Annotated[float, Field(ge=0.0, le=1.0)]
Rung        = Annotated[int,   Field(ge=0, le=7)]      # escalation-ladder rung 0..7
Basis       = Literal["observed", "asserted", "inferred", "user_confirmed"]
# NOTE (open, minor): processing policy-defaults use basis="inferred" with an `evidence` ref naming
# the policy rule. If policy-defaults must be distinguishable from evidence-inferred, add a
# "policy_default" basis. Kept to the brief's four for now.
```

### 1.1 `Evidenced[T]` — the three-truths carrier (R6)

```python
T = TypeVar("T")

class Evidenced(BaseModel, Generic[T]):
    """Wraps EVERY interpretive manifest field. A value never travels without its provenance.

    basis      how we know it: observed=from bytes, asserted=from humans/DBs, inferred=derived,
               user_confirmed. Disagreement across bases is a first-class Conflict, never a silent merge.
    evidence   ids of the Observation / Assertion / onlist-check records that justify the value.
    rung       the cheapest escalation-ladder step that settled this field (provenance + eval signal).
    """
    model_config = ConfigDict(frozen=True)
    value: T
    basis: Basis
    evidence: list[str] = Field(default_factory=list)
    confidence: Confidence
    rung: Rung
```

`frozen=True` makes a validated field immutable (R2: nothing edits a value post-validation).
Manifests are hashed by canonical serialization, never `hash()`, so the unhashable `list` field is a
non-issue.

### 1.2 `Observation` — probe output, per file, cached by sha (R3/R7); **role-free**

`Observation` is the *observed* leg of the three truths. It reports composition, segmentation,
distinct-value ratios, header grammar and integrity — and assigns **no roles**. The segment taxonomy
is structural (`constant` / `random` / `homopolymer`); mapping `constant→linker/TSO`,
`random→CB|UMI|cDNA`, `homopolymer-T→polyT` is the resolver's job, scored and second-guessable.

```python
class CycleComposition(BaseModel):
    """Base fractions at one 0-based cycle; a+c+g+t+n ~= 1.0."""
    cycle: int = Field(ge=0); a: float; c: float; g: float; t: float; n: float

class ConstantSegment(BaseModel):
    """A cycle span where one base dominates (>~90%): linker/adapter/TSO CANDIDATE (role NOT assigned)."""
    kind: Literal["constant"] = "constant"
    start: int; end: int; consensus: str; purity: Confidence   # consensus read straight off composition

class RandomSegment(BaseModel):
    """Near-uniform ACGT span: CB/UMI/cDNA CANDIDATE (role NOT assigned)."""
    kind: Literal["random"] = "random"
    start: int; end: int; mean_entropy_bits: float             # ~2.0 for uniform ACGT

class HomopolymerSegment(BaseModel):
    """A run of one base (polyT capture / polyA tail): structural only."""
    kind: Literal["homopolymer"] = "homopolymer"
    base: Literal["A", "C", "G", "T"]; start: int; end: int; mean_run: float

Segment = Annotated[ConstantSegment | RandomSegment | HomopolymerSegment,
                    Field(discriminator="kind")]

class FileIdentity(BaseModel):
    """Content identity of one FASTQ. Observation is internal, so a LOCAL path is allowed here only."""
    sha256: Sha256; size_bytes: PositiveInt; basename: str
    local_uri: LocalPath | None = None       # where probe read bytes; NEVER copied into a Manifest

class ProbeProvenance(BaseModel):
    """What the bounded probe did under its read/byte budget (R3). bytes_read = DECOMPRESSED;
    compressed_bytes_read drives estimated_total_reads (avoids the compression-ratio undercount)."""
    n_reads_sampled: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    compressed_bytes_read: int = Field(ge=0)
    tool_version: str
    params_hash: str                         # hash of {max_reads, max_bytes, seed}

class ReadLengthProfile(BaseModel):
    mode: int = Field(ge=0); n_distinct: int = Field(ge=1)
    min_len: int = Field(ge=0); max_len: int = Field(ge=0)
    percentiles: dict[str, int] | None = None
    # n_distinct>1 on a fixed-geometry read => pre-trimmed upload => PRETRIMMED_VARIABLE_LENGTH Blocker.

class WindowDistinctRatio(BaseModel):
    """distinct/total over a candidate cycle window. DEPTH-DEPENDENT: a SUPPORTS signal only, never a
    gate. Normalize with 4^len and sampled-N before interpreting (see §4.1)."""
    start: int; end: int; distinct_ratio: Confidence; n_sampled: int = Field(ge=1)

class ReadNameGrammar(BaseModel):
    """Parsed Illumina header; all optional. sra_normalized flags an @SRR....N rewrite (index gone)."""
    parsed: bool = False
    instrument: str | None = None; run: str | None = None; flowcell: str | None = None
    lane: int | None = None; tile: int | None = None
    index: str | None = None; sra_normalized: bool = False

class GzipIntegrity(BaseModel):
    ok: bool; truncated: bool                # truncated -> TRUNCATED_GZIP Blocker
    bgzf: bool | None = None; member_count: int | None = None

class Observation(BaseModel):
    """Deterministic, LLM-free, network-free probe output for ONE file, cached by file sha256.
    Structural signals ONLY — it MUST NOT assign roles."""
    model_config = ConfigDict(frozen=True)
    file: FileIdentity
    probe: ProbeProvenance
    per_cycle_composition: list[CycleComposition]
    segments: list[Segment]                                     # structural, NOT roles
    read_length: ReadLengthProfile
    distinct_value_windows: list[WindowDistinctRatio]
    read_name: ReadNameGrammar
    quality_encoding: Literal["phred33", "phred64", "unknown"]
    n_rate: Confidence
    estimated_total_reads: int = Field(ge=0)                    # extrapolated, never a full scan
    est_method: Literal["isize", "compressed_ratio"]
    gzip: GzipIntegrity
```

### 1.3 `Assertion` — LLM-facing draft split from stored (R1/R5)

The LLM cannot count character offsets, so it emits only `{doc_sha256, quote, context?}`;
deterministic code searches the normalized document for the quote, computes offsets, and sets the
two verification flags. This makes the P4 tripwire *fail-closed* instead of *false-rejecting*.

```python
class SourceSpan(BaseModel):
    doc_sha256: str
    quote: str                               # the exact substring the claim derives from
    context: str | None = None               # optional short left/right context to disambiguate
    char_start: int | None = None            # COMPUTED by code (not the LLM)
    char_end: int | None = None

class AssertionDraft(BaseModel):
    """The ONLY LLM structured-output surface for harvest (job a). Kept trivially simple: no unions,
    no offsets, value is a plain string."""
    field: str                               # dotted manifest path, e.g. "experiment.organism"
    value: str
    span: SourceSpan                          # LLM supplies doc_sha256 + quote (+ context) only
    llm_confidence: Confidence

class ExtractorProvenance(BaseModel):
    model_id: str; prompt_version: str

class Assertion(BaseModel):
    """Composed by code from an AssertionDraft. Both verification flags are code-owned so a
    hallucinated or mis-attributed claim fails closed."""
    id: str
    field: str
    value: str
    span: SourceSpan
    span_verified: bool = False              # code sets True: quote FOUND in normalized doc (R5a)
    entailment_ok: bool = False              # code sets True: quote ENTAILS value (R5b)
    llm_confidence: Confidence
    extractor: ExtractorProvenance
```

`span_verified` catches *fabricated provenance*; `entailment_ok` catches a *real quote mis-attached
to a wrong value* (the more common LLM failure — a verbatim "single-cell RNA-seq" span pinned to
"10x 3′ v3.1"). Both must hold before an Assertion flows into `manifest fill`.

### 1.4 `Conflict` — first-class, surfaced (R6)

```python
Decidable = Literal["reads", "onlist", "metadata", "alignment", "user"]   # includes onlist

class ConflictPosition(BaseModel):
    value: str; basis: Basis                  # value is the canonical string form (heterogeneous fields)
    evidence: list[str] = Field(default_factory=list); confidence: Confidence

class Resolution(BaseModel):
    chosen_value: str; basis: Basis; rung: Rung
    decided_by: Literal["code", "user", "benign_equivalence"]; note: str | None = None

class Conflict(BaseModel):
    """A surfaced disagreement between truths, never auto-picked. positions[] generalizes the common
    observed/asserted pair; kind is derivable from the position bases."""
    id: str
    field: str                                # dotted manifest path
    positions: list[ConflictPosition] = Field(min_length=2)
    kind: Literal["observed_vs_asserted", "asserted_vs_asserted", "other"]
    decidable_by: list[Decidable]
    status: Literal["open", "resolved", "benign"] = "open"
    resolution: Resolution | None = None
```

`status="benign"` is the §12 escape hatch: two confusable KB entries that emit *identical*
`backend.params` (v3 vs v3.1) are recorded together with zero questions. `decidable_by` includes
`onlist` — the mechanism §12 actually uses to split Multiome/GEM-X from v3.

### 1.5 `Blocker` / `Warning` — refusal as an exit code (R4)

```python
class BlockerCode(str, Enum):
    MISSING_TECHNICAL_READ     = "MISSING_TECHNICAL_READ"
    TRUNCATED_GZIP             = "TRUNCATED_GZIP"
    CORRUPT_FASTQ              = "CORRUPT_FASTQ"
    UNSUPPORTED_TECHNOLOGY     = "UNSUPPORTED_TECHNOLOGY"
    PRETRIMMED_VARIABLE_LENGTH = "PRETRIMMED_VARIABLE_LENGTH"
    NO_VALID_ROLE_ASSIGNMENT   = "NO_VALID_ROLE_ASSIGNMENT"
    ONLIST_VERIFICATION_FAILED = "ONLIST_VERIFICATION_FAILED"
    UNRESOLVED_CONFLICT        = "UNRESOLVED_CONFLICT"
    MISSING_CONTROLLED_VOCAB   = "MISSING_CONTROLLED_VOCAB"
    ABSOLUTE_PATH              = "ABSOLUTE_PATH"

class BlockerSubject(BaseModel):
    kind: Literal["file", "field", "dataset"]
    ref: str                                  # basename / dotted path / dataset id — never an absolute path

class Blocker(BaseModel):
    """A structured refusal emitted alongside a nonzero exit. A Blocker is ALWAYS fatal — advisory
    diagnostics are a separate Warning type, so branching code never inspects severity to know if it blocks."""
    id: str; code: BlockerCode
    message: str                              # human-readable diagnosis
    remedy: str                               # MUST be actionable
    subject: BlockerSubject
    evidence: list[str] = Field(default_factory=list)

class Warning(BaseModel):
    code: str; message: str; subject: BlockerSubject     # non-blocking; exits 0
```

`MISSING_TECHNICAL_READ.remedy` is operable: *"re-fetch with `fasterq-dump --include-technical`, or
pull the original submitted files `sra-pub-src-*` via the SRA Data Locator / SDL API."*

### 1.6 `Manifest` — three sections, three authorities (R9)

```python
class ReadElement(BaseModel):
    """One ordered sub-region of a read. A SUPERSET/adapter over a seqspec Region: adds a derived
    `start` and an interpretive `role` that seqspec Regions do not carry (seqspec derives position
    from ordering + min_len/max_len). seqspec sequence_type maps: fixed->sequence; random->random ACGT;
    onlist->onlist_ref."""
    role: Literal["CB", "UMI", "cDNA", "gDNA", "index", "linker", "polyT", "polyA"]
    region_type: Literal["barcode", "umi", "cdna", "gdna", "index5", "index7",
                         "linker", "poly_A", "poly_t", "custom_primer"]
    start: int | None = None; length: int | None = None       # None if variable/leading
    min_len: int | None = None; max_len: int | None = None    # seqspec-canonical for variable elements
    sequence: str | None = None                                # fixed linker/adapter IUPAC
    onlist_ref: str | None = None                              # Onlist.name backing a barcode element

class ReadDef(BaseModel):
    read_id: str                              # role id "R1"/"R2"/"I1" — NOT a filename claim
    strand: Literal["pos", "neg"]
    min_len: int = Field(ge=0); max_len: int = Field(ge=0)
    elements: list[ReadElement]               # ordered 5'->3'

class ReadLayout(BaseModel):
    modality: Literal["rna", "atac", "protein", "crispr", "dna"]
    reads: list[ReadDef]

class Onlist(BaseModel):
    """Barcode-whitelist registry entry; fetched via pooch + hash-verified, NEVER vendored.
    orientation_hint is a NON-authoritative default; the authoritative orientation lives per-read in
    the KB (one list can be forward for GEX and revcomp for ATAC). Tier B tests both anyway."""
    name: str; uri: Uri; sha256: Sha256; length: int = Field(ge=1)
    orientation_hint: Literal["forward", "reverse_complement"] | None = None
    n_entries: int | None = None

class GenomeRef(BaseModel):
    """liulab-genome selection: UCSC assembly id + a REGISTERED GTF name. Never a path (R9).
    liulab-genome does not fetch annotations; seqforge stages the GTF and calls register_gtf(name)."""
    assembly: str                             # hg38 / mm39 / sacCer3 / ce11 (validated live vs UCSC)
    annotation_name: str                      # "gencode" / "ensembl" / "WS298"
    ncbi_taxid: NcbiTaxid | None = None

RuntimeEnv = Literal["align-rna", "align-dna", "ml", "ml-gpu"]   # literal liulab-runtime env; no profile layer

class ResourceHints(BaseModel):
    threads: int = Field(ge=1, default=8); mem_gb: int = Field(ge=1, default=32)
    disk_gb: int | None = None; gpus: int = Field(ge=0, default=0)

# --- concrete Evidenced specializations: stable named $defs for schema export; MUST precede use ---
class EvidencedStr(Evidenced[str]): ...
class EvidencedBool(Evidenced[bool]): ...
class EvidencedTaxid(Evidenced[NcbiTaxid]): ...
class EvidencedAssay(Evidenced[AssayTerm]): ...
class EvidencedChemistrySet(Evidenced[list[ChemistryId]]): ...   # equivalence class, not a single pick
class EvidencedAccessionList(Evidenced[list[Accession]]): ...
class EvidencedReadLayout(Evidenced[ReadLayout]): ...
class EvidencedGenome(Evidenced[GenomeRef]): ...
class EvidencedRuntimeEnv(Evidenced[RuntimeEnv]): ...

class FileInventoryItem(BaseModel):
    """One physical file + checksum + assigned role. NO absolute path (R9). Identity is raw observed
    truth; the role ASSIGNMENT is the joint-optimization output, so read_id is Evidenced."""
    uri: Uri; basename: str; sha256: Sha256; size_bytes: PositiveInt
    read_id: EvidencedStr | None = None       # -> ReadLayout.reads[].read_id

class LibrarySection(BaseModel):
    """Physical truth. Authority = EVIDENCE."""
    assay: EvidencedAssay
    chemistry: EvidencedChemistrySet          # equivalence class: benign twins (v3 + v3.1) are machine-visible
    read_layout: EvidencedReadLayout
    onlists: list[Onlist] = Field(default_factory=list)
    files: list[FileInventoryItem]

class SampleGroup(BaseModel):
    sample_id: str
    tissue: EvidencedStr | None = None; condition: EvidencedStr | None = None
    file_uris: list[Uri] = Field(default_factory=list)   # sample -> file mapping

class ExperimentSection(BaseModel):
    """Biological/metadata truth. Authority = METADATA + humans."""
    organism: EvidencedTaxid                  # NCBI taxid; probe cannot see it -> basis is asserted/inferred
    accessions: EvidencedAccessionList
    samples: list[SampleGroup]

class ProcessingSection(BaseModel):
    """Intent. Authority = DERIVED from library+experiment + policy (basis usually 'inferred')."""
    genome: EvidencedGenome
    aligner: EvidencedStr
    quantification: EvidencedStr              # "gene" / "gene_full" / "velocyto" / "sj" / "none"
    variant_calling: EvidencedBool
    environment: EvidencedRuntimeEnv          # literal liulab-runtime env name (R9/R12)
    resources: ResourceHints = Field(default_factory=ResourceHints)

class Provenance(BaseModel):
    manifest_hash: str; kb_version: str; workflow_version: str; seqforge_version: str

class Manifest(BaseModel):
    """One machine-independent manifest. compose() is a PURE function of the three sections.
    validate() ALSO enforces referential integrity: every experiment file_uri ∈ library inventory."""
    model_config = ConfigDict(frozen=True)
    library: LibrarySection
    experiment: ExperimentSection
    processing: ProcessingSection
    provenance: Provenance
```

### 1.7 Score / compile output models (were missing — Blocker A)

The four-stage contract emits ranked candidates, decisions, questions, and compiled configs; each is
a first-class Pydantic type so `schema export` references only types that exist, and every stdout
object round-trips through JSON Schema.

```python
class TechScore(BaseModel):
    """JSON-safe: no ±inf ever appears in serialized output. 'forbidden' == a requires/excludes gate
    failed; 'scored' carries the finite normalized value."""
    technology: ChemistryId
    status: Literal["forbidden", "scored"]
    value: float | None = None; reason: str | None = None

class RoleAssignment(BaseModel):
    assignment: dict[str, str]                # role_id -> file sha256
    unassigned: list[str] = Field(default_factory=list)   # leftover file shas (index/ignored)

class Candidate(BaseModel):
    technology: ChemistryId
    score: TechScore
    role_assignment: RoleAssignment
    rung_resolved: dict[str, int]             # per-field deciding rung (provenance + eval signal)
    equivalence_members: list[ChemistryId] = Field(default_factory=list)   # CI-proven processing_equivalent twins
    evidence: list[str] = Field(default_factory=list)

class Question(BaseModel):
    id: str; field: str; prompt: str
    options: list[str]                        # code decides the option set; the LLM/human picks among it
    decidable_by: list[Decidable]; rung: Rung

class Decision(BaseModel):
    question_id: str; chosen: str; basis: Basis
    actor: Literal["user", "agent", "code"]; evidence: list[str] = Field(default_factory=list)

class ResolveResult(BaseModel):
    dataset_id: str; kb_version: str; rung_reached: Rung
    candidates: list[Candidate]; conflicts: list[Conflict]
    questions: list[Question]; blockers: list[Blocker] = Field(default_factory=list)

class ArbitrationRequest(BaseModel):          # LLM job (b) INPUT schema (opt-in resolve adjudicate)
    conflict_id: str; positions: list[ConflictPosition]

class ArbitrationResponse(BaseModel):         # LLM job (b) OUTPUT schema — references by index, re-derives no values
    conflict_id: str; chosen_index: int; rationale: str

class ValidationReport(BaseModel):
    ok: bool; blockers: list[Blocker]; conflicts: list[Conflict]
    warnings: list[Warning] = Field(default_factory=list)

class ModuleSelection(BaseModel):
    name: str; version: str; env: RuntimeEnv

class ComposeResult(BaseModel):
    modules: list[ModuleSelection]; config_path: Uri; units_path: Uri
    gate: dict[str, Literal["pass", "fail", "skip"]]   # {wiring, params, e2e}
    params_preview: dict[str, object]                  # incl. params_problems: why a gate failed

class RunResult(BaseModel):
    dataset_id: str; stages: dict[str, str]; exit: int
    artifacts: dict[str, Uri]; provenance_id: str

class EvalReport(BaseModel):
    n_cases: int; field_accuracy: float
    false_accept_rate: float; false_refuse_rate: float
    questions_asked: dict[str, float]         # {mean, needed_but_not_asked}
    cost: dict[str, float]; per_case: list[dict]
```

### 1.8 JSON Schema export — the single source of truth

`Manifest.model_json_schema()` (2020-12) feeds validation (Pydantic itself) and docs. The **only**
LLM-facing schemas are `AssertionDraft` and `ArbitrationRequest`/`ArbitrationResponse`. Derive the
LLM-facing variant from the canonical one with a deterministic, CI-tested transform — never a
hand-maintained second schema:

- emit with `model_json_schema(ref_template="#/$defs/{model}")`;
- for the provider "strict" subset: rewrite `oneOf → anyOf`, drop the `discriminator` keyword (keep
  the literal tag field), inline single-member `allOf`, hoist `$ref`-sibling descriptions onto the
  referenced `$def`, strip `default`, set `additionalProperties: false`, put every property in
  `required` (nullability via the null branch);
- keep numeric/`pattern` constraints in the canonical schema only (Pydantic enforces them at ingest —
  the real guardrail per R2); strip them from the LLM schema.

Generics are materialized via the named `Evidenced[...]` subclasses (stable `$defs`); no `value: Any`
anywhere (`Assertion.value` and `ConflictPosition.value` are `str`); discriminated unions live only
inside `Observation` (code-emitted, never LLM-produced).

---

## 2. KB `spec.yaml` schema

Layout: `kb/<tech>/{spec.yaml, README.md}` — one directory per technology. `README.md` is prose for
the LLM (how the assay works, aliases, gotchas, SRA failure modes); `spec.yaml` is machine-checkable.
The schema is a Pydantic v2 model (`extra="forbid"` on **every** model, including each test leaf), so
a typo'd key fails validation exactly where the DSL is executed. Rationale for Pydantic here is R1/R10
(single executable validator + self-test), **not** R1's LLM-output clause — `spec.yaml` is
human-authored and CI-validated, not LLM output.

### 2.1 The schema (abridged; full closed vocabularies)

```python
ElementType  = Literal["barcode","umi","cdna","gdna","linker","poly_a","poly_t","fixed","index"]
Mechanism    = Literal["none","onlist","metadata","alignment","user"]
Decidable    = Literal["reads","onlist","metadata","alignment","user"]
Orientation  = Literal["forward","revcomp","either"]
SeqspecRegion= Literal["barcode","umi","cdna","gdna","index5","index7","linker","poly_A","poly_t","custom_primer"]

class Anchor(BaseModel):           # locate a variable/floating element (inDrop-class; SPLiT-seq is fixed)
    model_config = ConfigDict(extra="forbid")
    relative_to: Literal["read_start","read_end","element"] = "read_start"
    ref_element: str | None = None; ref_side: Literal["start","end"] = "end"
    offset: int = 0; motif: str | None = None; max_mismatch: int = 0

class Element(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ElementType; name: str
    start: int | None = None; end: int | None = None          # 0-based half-open; end=null => open (cdna)
    min_len: int | None = None; max_len: int | None = None    # variable/anchored
    anchor: Anchor | None = None; sequence: str | None = None  # fixed linker IUPAC
    onlist: str | None = None                                  # local alias into spec.onlists
    seqspec_region_type: SeqspecRegion                         # Literal, validated for export
    # model_validator enforces: exactly one coherent addressing mode (fixed [start,end) XOR anchor
    #   XOR (min_len/max_len)); linker/fixed require `sequence`; end=null only for cdna/gdna.

class Read(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str                                    # ROLE label (R1/R2/bc/cdna) — not a filename claim
    seqspec_read_id: str                       # a role id ("R1"); the real filename is substituted at export
    file_hint: str | None = None               # rung-1 weak prior only
    strand: Literal["pos","neg"] = "pos"
    min_len: int | None = None; max_len: int | None = None
    elements: list[Element]

class OnlistRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registry: str                              # pooch-registry name holding URL+sha256+length+orientation
    role: Literal["cell_barcode","sample_index","feature","atac_barcode"]
    expected_orientation: Orientation = "forward"   # non-authoritative hint

# ---- signature tests: a CLOSED set == the scorer's evaluators (§3). every leaf: extra="forbid" ----
class _Seg(BaseModel):                          # address a segment by element name XOR (start,end)
    model_config = ConfigDict(extra="forbid")
    read: str; element: str | None = None; start: int | None = None; end: int | None = None
class ReadCount(BaseModel):     test: Literal["read_count"];  roles: int          # biological+barcode ROLE count, not raw files
class SegmentLength(BaseModel): test: Literal["segment_length"]; read: str; length: int; tolerance: int = 0
class HasSegment(_Seg):         test: Literal["has_segment"]; kind: Literal["constant","random","polyT","polyA"]
class DistinctRatio(_Seg):      test: Literal["distinct_ratio"]; expect: Literal["low","high"]   # SUPPORTS-ONLY
class OnlistHitRate(_Seg):      test: Literal["onlist_hit_rate"]; onlist: str
                                orientation: Orientation = "either"; min: float                  # width from registry length
class MotifPresent(BaseModel):  test: Literal["motif_present"]; read: str; motif: str
                                where: Literal["read_start","read_end","anywhere","window"] = "anywhere"
                                search_start: int | None = None; search_end: int | None = None   # inclusive; cover W1@11
                                max_mismatch: int = 1; min_rate: float = 0.5
class BaseComposition(_Seg):    test: Literal["base_composition"]; base: Literal["A","C","G","T","N"]; min_fraction: float
class HeaderIndex(BaseModel):   test: Literal["header_index"]; present: bool

Test = Annotated[ReadCount | SegmentLength | HasSegment | DistinctRatio | OnlistHitRate
                 | MotifPresent | BaseComposition | HeaderIndex, Field(discriminator="test")]

class Support(BaseModel):   model_config = ConfigDict(extra="forbid"); when: Test; weight: float = 1.0
class Signature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requires: list[Test]      # hard AND-gates. NO distinct_ratio here (depth-dependent).
    supports: list[Support]   # additive positive evidence (onlist + distinct_ratio live here)
    excludes: list[Test]      # anti-gates: ANY pass => disqualify

class Backend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str                                # versioned, CI-tested workflow module id, e.g. "map/starsolo"
    params: dict[str, str | int | float | list[str]]     # CHEMISTRY-DEFINING MINIMUM only
    # only interpolation token allowed anywhere in params is "{onlist:<alias>}" (validated); any other {..} fails.

class Confusable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str; relationship: Literal["processing_equivalent","processing_divergent"]
    distinguishable_by: list[Mechanism]; note: str = ""

class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int
    identity: "Identity"                       # id, version, name, aliases, assay_ontology[CURIE], modality
    reads: list[Read]
    onlists: dict[str, OnlistRef]
    signature: Signature
    backend: Backend
    confusable_with: list[Confusable] = []
    decidable_by: list[Decidable] = []         # CI-COMPUTED; a validator asserts it equals the computed union
    # Spec._cross_refs resolves EVERY test `read`/`element`, every anchor.ref_element, and every onlist
    # alias against the reads/elements block (not just onlist aliases). Cross-entry facts
    # (decidable_by, confusable labels) are validated by the CI matrix, not here.
```

**What moved out of the KB backend:** CellRanger-parity knobs (`soloUMIdedup 1MM_CR`,
`soloUMIfiltering MultiGeneUMI_CR`, `clipAdapterType CellRanger4`, `outFilterScoreMin 30`) are
processing **policy**, not chemistry — they are applied at `compose` time from `processing`, so
`backend_identical` (below) stays sensitive to chemistry, not policy.

### 2.2 Worked spec — `10x-3p-gex-v3` (fixed offsets)

```yaml
# kb/10x-3p-gex-v3/spec.yaml
schema_version: 1
identity:
  id: 10x-3p-gex-v3
  version: "3"
  name: "10x Chromium Single Cell 3' Gene Expression v3"
  aliases: ["10x 3' v3", "Chromium 3' v3", "SC3Pv3"]
  assay_ontology: ["EFO:0009922"]        # FLAG-1: verify exact EFO id
  modality: rna
reads:
  - id: R1                                # technical/barcode read: 28bp = 16 CB + 12 UMI
    seqspec_read_id: R1
    strand: pos
    min_len: 28
    max_len: 28
    elements:
      - {type: barcode, name: CB,  start: 0,  end: 16, onlist: cb_whitelist, seqspec_region_type: barcode}
      - {type: umi,     name: UMI, start: 16, end: 28,                       seqspec_region_type: umi}
  - id: R2                                # cDNA (open-ended)
    seqspec_read_id: R2
    strand: pos
    min_len: 25
    max_len: null
    elements:
      - {type: cdna, name: cdna, start: 0, end: null, seqspec_region_type: cdna}
onlists:
  cb_whitelist:  {registry: 3M-february-2018, role: cell_barcode, expected_orientation: forward}
  arc_v1_probe:  {registry: 737K-arc-v1,      role: cell_barcode, expected_orientation: forward}   # exclude probe
signature:
  requires:                               # structural gates, rungs 0-2 friendly (NO distinct_ratio, NO onlist)
    - {test: read_count, roles: 2}
    - {test: segment_length, read: R1, length: 28, tolerance: 0}
    - {test: has_segment, read: R1, start: 0,  end: 16, kind: random}
    - {test: has_segment, read: R1, start: 16, end: 28, kind: random}
  supports:                               # additive evidence (rung 3+)
    - {when: {test: onlist_hit_rate, read: R1, start: 0, end: 16, onlist: cb_whitelist, orientation: forward, min: 0.6}, weight: 5.0}
    - {when: {test: distinct_ratio,  read: R1, start: 0,  end: 16, expect: low},  weight: 2.0}   # CB recurs
    - {when: {test: distinct_ratio,  read: R1, start: 16, end: 28, expect: high}, weight: 1.0}   # UMI ~unique
    - {when: {test: header_index, present: true}, weight: 0.2}
  excludes:                               # anti-gate: if the Multiome list hits, it's not 3' GEX
    - {test: onlist_hit_rate, read: R1, start: 0, end: 16, onlist: arc_v1_probe, orientation: forward, min: 0.6}
backend:
  module: map/starsolo
  params:                                 # chemistry-defining MINIMUM (parity knobs come from processing policy)
    soloType: CB_UMI_Simple
    soloCBstart: 1
    soloCBlen: 16
    soloUMIstart: 17
    soloUMIlen: 12
    soloCBwhitelist: "{onlist:cb_whitelist}"
    soloStrand: Forward
    soloFeatures: [Gene]
confusable_with:
  - {id: 10x-3p-gex-v3.1,  relationship: processing_equivalent, distinguishable_by: [none],
     note: "identical 16+12=28bp geometry + 3M-february-2018 + identical params -> §12 benign, 0 questions"}
  - {id: 10x-multiome-gex, relationship: processing_divergent,  distinguishable_by: [onlist],
     note: "same 28bp geometry, whitelist 737K-arc-v1 not 3M -> rung-3 onlist separates; params differ in soloCBwhitelist"}
  - {id: 10x-gemx-3p-v4,   relationship: processing_divergent,  distinguishable_by: [onlist],
     note: "REQUIRED: same 28bp/16+12 geometry, newer GEM-X whitelist. Without this the flagship fails its own CI."}
  - {id: 10x-5p-gex,       relationship: processing_divergent,  distinguishable_by: [metadata, alignment],
     note: "5' reads antisense cDNA -> soloStrand Reverse; read-undecidable when geometry+whitelist coincide (FLAG-7)"}
decidable_by: [onlist, metadata, alignment]     # CI-computed union over divergent confusables
```

### 2.3 Worked spec — `splitseq` (combinatorial; pilot #3)

SPLiT-seq stresses **combinatorial multi-block indexing**: the cell barcode is the concatenation of
three round-specific 8 bp barcodes drawn from small (~96-entry) whitelists, separated by **fixed**
linkers. Unlike inDrop the positions are fixed, so no `anchor` is needed — the `anchor` path stays in
the schema for a future inDrop entry (coverage caveat, §7).

**Scope decision (implemented):** this entry models the **original published SPLiT-seq only**
(Rosenberg et al., *Science* 2018, doi:10.1126/science.aam8999). Parse Biosciences **Evercode** is a
separate, actively-versioned commercial descendant with different linkers/whitelists — it is
**deferred to its own future KB entry** and never conflated with `splitseq`. The read structure and
the two 30 bp linker sequences below are **pinned verbatim from scg_lib_structs** (CC-BY): the
[methods page](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html) and the
["Published Manuscript" read-2 variant in issue #13](https://github.com/Teichlab/scg_lib_structs/issues/13).
Read 2 (94 cycles) is `[10 UMI][8 bc3][30 linker1][8 bc2][30 linker2][8 bc1]`; Read 1 (66 cycles) is
the cDNA. Still-open FLAGs: the round whitelists (register the real barcode files from scg_lib_structs
with URL + sha256), `soloStrand`, and the EFO CURIE. Per FLAG-3, `soloCBposition`/`soloUMIposition`
are **omitted from the KB and derived from the element coordinates at compose time**, never hand-typed.

```yaml
# kb/specs/splitseq/spec.yaml         # linkers pinned from scg_lib_structs (Science-2018 SPLiT-seq)
schema_version: 1
identity:
  id: splitseq
  version: science-2018                  # published-manuscript variant (not preprint, not Parse)
  name: "SPLiT-seq (combinatorial split-pool barcoding, Rosenberg et al. 2018)"
  aliases: ["SPLiT-seq", "split-pool", "combinatorial indexing", "split-pool ligation"]
  assay_ontology: []                     # FLAG: pin the SPLiT-seq EFO/OBI CURIE from live EFO
  modality: rna
reads:
  - id: cdna
    seqspec_read_id: R1                  # Read 1 (66 cycles) = cDNA
    strand: pos
    min_len: 25
    max_len: null
    elements:
      - {type: cdna, name: cdna, start: 0, end: null, seqspec_region_type: cdna}
  - id: bc                               # Read 2 (94 cycles): UMI + bc3 + linker1 + bc2 + linker2 + bc1
    seqspec_read_id: R2
    strand: pos
    min_len: 94
    max_len: 94
    elements:
      - {type: umi,     name: UMI, start: 0,  end: 10, seqspec_region_type: umi}
      - {type: barcode, name: bc3, start: 10, end: 18, onlist: round3, seqspec_region_type: barcode}
      - {type: linker,  name: linker1, start: 18, end: 48, seqspec_region_type: linker,
         sequence: "GTGGCCGATGTTTCGCATCGGCGTACGACT"}    # verbatim Science-2018 Round3->Round2 spacer
      - {type: barcode, name: bc2, start: 48, end: 56, onlist: round2, seqspec_region_type: barcode}
      - {type: linker,  name: linker2, start: 56, end: 86, seqspec_region_type: linker,
         sequence: "ATCCACGTGCTTGAGAGGCCAGAGCATTCG"}    # verbatim Science-2018 Round2->Round1 spacer
      - {type: barcode, name: bc1, start: 86, end: 94, onlist: round1, seqspec_region_type: barcode}
onlists:
  round1: {registry: splitseq-round1, role: cell_barcode, expected_orientation: forward}   # 96 x 8 bp
  round2: {registry: splitseq-round2, role: cell_barcode, expected_orientation: forward}
  round3: {registry: splitseq-round3, role: cell_barcode, expected_orientation: forward}
  tenx_probe: {registry: 3M-february-2018, role: cell_barcode, expected_orientation: forward}
signature:
  requires:
    - {test: read_count, roles: 2}
    - {test: has_segment, read: bc, start: 18, end: 48, kind: constant}    # linker1 (fixed)
    - {test: has_segment, read: bc, start: 56, end: 86, kind: constant}    # linker2 (fixed)
  supports:
    - {when: {test: onlist_hit_rate, read: bc, element: bc1, onlist: round1, orientation: forward, min: 0.5}, weight: 3.0}
    - {when: {test: onlist_hit_rate, read: bc, element: bc2, onlist: round2, orientation: forward, min: 0.5}, weight: 3.0}
    - {when: {test: onlist_hit_rate, read: bc, element: bc3, onlist: round3, orientation: forward, min: 0.5}, weight: 3.0}
    - {when: {test: distinct_ratio,  read: bc, start: 0, end: 10, expect: high}, weight: 1.0}   # UMI ~unique
    - {when: {test: distinct_ratio,  read: cdna, start: 0, end: 20, expect: high}, weight: 1.0}  # score the cDNA role
  excludes:
    - {test: onlist_hit_rate, read: bc, start: 0, end: 16, onlist: tenx_probe, orientation: forward, min: 0.6}  # a 16bp 10x CB => not SPLiT-seq
backend:
  module: map/starsolo
  params:
    soloType: CB_UMI_Complex
    soloCBwhitelist: ["{onlist:round1}", "{onlist:round2}", "{onlist:round3}"]
    soloStrand: Forward                  # FLAG: confirm SPLiT-seq cDNA strand before the e2e run
    soloFeatures: [Gene]
    # soloCBposition / soloUMIposition: DERIVED from the element coordinates at compose (FLAG-3).
confusable_with: []                       # onlist + combinatorial geometry separate it from 10x/inDrop
decidable_by: []
```

The `onlist_hit_rate` evaluator is **width-generic**: it reads the barcode length from the registry
entry (8 bp here → still a `uint32` pack, small sorted arrays), not a hardcoded 16 bp window (R6/§4.1).

### 2.4 The CI confusability matrix (computed purely from `reads` + `signature` + `backend`)

For every ordered pair `(A, B)`, CI derives three facts and validates the declared labels — there is
no hand-maintained truth table:

1. **`rung02_separable(A,B)`** — do the cheap structural probes (rungs 0–2, no onlist) separate them?
   Generate synthetic reads from `A.reads`, run the non-onlist subset of `A.signature` against
   `synth_B` and symmetrically. **Under-declaration guard:** `not separable ∧ B ∉ A.confusable_with`
   → **CI ERROR** (this is why `10x-gemx-3p-v4` is a *required* entry in the flagship's
   `confusable_with` — the 28 bp / 16+12 geometry is not rung-0-2-separable). **Over-declaration:**
   `separable ∧ B ∈ A.confusable_with` → CI WARNING.

2. **`onlist_separable(A,B)`** — for still-confusable pairs, do their `onlist_hit_rate` tests
   distinguish (rung 3)? True iff their whitelists have **low cross-hit** — computed by actual
   set-intersection (`np.intersect1d` over the two sorted packed arrays), **not** sha256 inequality
   (different hashes prove the files differ, not that the barcode sets differ).

3. **`backend_identical(A,B)`** — resolve each `backend.params`, expand `{onlist:alias}` to the
   registry **sha256**, canonicalize (sort keys, normalize `soloFeatures` order), **and include the
   read→role placement** (`readFilesIn` order derived from `reads`). Two are identical iff those
   canonical forms are byte-equal. *(Including role placement matters: two techs differing only in
   which read is biological would otherwise be falsely labeled benign.)*

**§12 benign rule (the biconditional CI asserts):** `backend_identical(A,B) ⟺ relationship ==
processing_equivalent`. v3 vs v3.1 → identical module + `soloCB*/UMI*` + whitelist sha + strand +
role placement → benign → `distinguishable_by:[none]`. At runtime, a score tie between two candidates
with a CI-proven `processing_equivalent` edge **must not** escalate: record both ids into
`library.chemistry` (equivalence class) and ask **0** questions. `backend_identical == False` ⟹
`processing_divergent`, `distinguishable_by` non-empty ≠ `[none]`; listing `onlist` requires
`onlist_separable == True`. **`decidable_by` is generated** (per entry, the union over divergent
confusables of the minimal sufficient mechanism) and asserted equal to the declared list.

### 2.5 Synthetic generation (round-trip, R10) and adversarial fixtures

The generator is a pure function of `reads[].elements[]` only (never `signature`/`backend`, so the
round-trip is a real test, not a tautology): walk elements in order, drawing `barcode`+onlist from a
fixed synthetic cell pool of K barcodes reused across reads (so the recurrence signal is realistic —
**reconcile K with the probe window** so the distinct-ratio lands in-band, e.g. K≈2–5k over a 200k
window, not K=100), `umi` fresh-random, `linker`/`fixed` literal, `cdna` from a tiny bundled
reference, homopolymers as runs. Variable/anchored layout falls out of concatenation.

**Round-trip assertion:** `spec → synth FASTQ → seqforge probe → recovered layout`; `assert recovered
== declared`. **Adversarial variants, generated from the same block, assert the correct
Blocker/Conflict** (not a wrong answer): reverse-complement the read (probe recovers via the revcomp
onlist path + flags orientation); linker with 1–2 mismatches; drop the barcode read entirely (§12
missing-technical-read → `Blocker(MISSING_TECHNICAL_READ)` + remedy); a 26 bp R1 (must miss v3's
`segment_length:28` gate — proving 28 bp alone doesn't pick a chemistry); a fixed-cycle read with
`n_distinct>1` (pre-trimmed → `Blocker(PRETRIMMED_VARIABLE_LENGTH)`); SRA-normalized header
(`header_index` ABSTAINs, does not gate).

### 2.6 seqspec export/check

`kb seqspec-export` maps each `Spec` onto seqspec's `Assay/Read/Region` decomposition (using the
explicit `seqspec_region_type`), and `kb seqspec-check` runs `seqspec check` + diffs `seqspec index -t
starsolo` against `backend.params` — two independent derivations of the geometry must agree.
**Caveat (FLAG-6):** seqspec's starsolo emitter targets fixed `CB_UMI_Simple` geometry; whether it
emits `CB_UMI_Complex` position strings for combinatorial/anchored barcodes is unverified, so the
dual-derivation check is currently scoped to fixed-offset chemistries — confirm before relying on it
for `splitseq`.

---

## 3. Scoring, joint role-assignment, and escalation

### 3.1 Signature-test evaluators

`local_score(test, role, obs_f | tech) → [0,1]` (for `supports`) or `{PASS, FAIL, ABSTAIN}` (for
`requires`/`excludes`). **`ABSTAIN` is first-class** — "the probe cannot see this signal" ≠ "the
signal is absent" — and it **never gates** (a `header_index` ABSTAIN on an SRA-normalized corpus must
not reject every SRA dataset). Gate semantics: `requires` FAIL → cell forbidden; `excludes` PASS →
cell forbidden; `supports` → `Σ weight · score` with `Σ weight = 1` so a finite cell ∈ [0,1].

- **`segment_length`** — triangular around L for scoring; `PASS iff |mode − L| ≤ tol_gate` (the gate
  that separates 3′v3=28 from 3′v2=26). Open-ended cDNA uses a `min` variant.
- **`has_segment{constant|random|polyT|polyA}`** — mean per-cycle evidence over the window
  (constant: max-base fraction ≥ .9; random: near-uniform; polyT/polyA: base fraction ≥ min).
- **`distinct_ratio`** — `distinct/total` over the window. **SUPPORTS-ONLY, never a gate.**
  Depth-dependent (§6 pushback #2): only meaningful when normalized. `expect="high"` (UMI/cDNA)
  requires `4^len ≫ n_sampled`, else the ratio saturates below the band; `expect="low"` (CB) is
  conditioned on estimated reads-per-cell. It *proposes* CB-vs-UMI; the onlist confirms.
- **`onlist_hit_rate`** (rung 3, the hypothesis test) — width-generic (barcode length from the
  registry, not hardcoded 16), tests forward **and** reverse-complement **and** a small positional
  offset scan; records the winning `(orientation, δ)`. `floor = onlist.length / 4^len`; `score =
  clip((best − floor)/(min − floor), 0, 1)`; `PASS iff best ≥ min` (≈0.6). At Q30 discriminative
  power ≈ 0.9/floor ≈ 500:1. A revcomp hit means the barcode read is on the other strand → supply the
  revcomp whitelist file; it does **not** flip `--soloStrand` (which is the KB's 3′/5′ property).
- **`motif_present`** — fraction of reads matching an IUPAC motif (≤ max_mismatch) in an **inclusive**
  window; used for fixed linkers (SPLiT-seq) and, as an `excludes` test, to reject 10x when an
  internal linker appears in a barcode read.
- **`base_composition`** — element-addressable (via `_Seg`), so it can target a floating region, not
  just cycles 0–3.
- **`header_index`** — `ABSTAIN` if the header is SRA-normalized (and this is how probe *detects* the
  normalization); otherwise checks a per-file ~constant 8–10 bp index.

### 3.2 The evidence matrix (JSON-safe)

For technology `t` with roles `R_t` and files `F`:

```
M_t[r][f] = FORBIDDEN                          if any requires(r) gate FAILs, or any excludes(r) gate PASSes
          = Σ_{s ∈ supports(r)} w_s · local_score(s,r,obs_f)     otherwise   ∈ [0,1]
```

`FORBIDDEN` is the internal sentinel `float("-inf")` **for computation only**. Serialized (`--json`)
it is `{"status":"forbidden"}`; a scored cell is `{"status":"scored","value":x}` — **no `±inf` ever
crosses the JSON boundary** (JSON can't represent it and Pydantic's inf handling is lossy).

### 3.3 The joint optimization (cardinality-normalized)

An assignment `A: R_t ↪ F` is **injective** (each role a distinct file); leftover files are
unassigned at penalty. `valid(A)` = no forbidden selected cell ∧ dataset-level `requires` (mostly
*decomposed* into per-cell gates + injectivity — "exactly one 28 bp read AND one cDNA read" falls out
of the per-row gates plus injectivity; only genuinely non-decomposable globals need an explicit
post-check).

```
raw(t) = max_{A valid} [ Σ_{r∈R_t} ( M_t[r][A(r)] + β · prior(r, A(r)) ) ]
score(t) = raw(t) / |R_t|  −  (λ / |R_t|) · |F \ A*(R_t)|          # cardinality-NORMALIZED (pushback #3)
score(t) = −∞ (forbidden) if no valid A exists
best_tech = argmax_t score(t)
```

Normalizing by `|R_t|` makes a 2-role 10x and a 6-role SPLiT-seq comparable (an unnormalized sum
would bias `argmax` toward high-role-count techs). **The filename prior** `β · prior(r,f)` (1 if the
file's `_1/_2` token matches the role's conventional slot, else 0) has `β ≪ min(w)`, so it can only
break an **exact byte-tie** — never override bytes or flip validity. `fasterq-dump` `_1/_2/_3` say
nothing about R1/R2/I1, so filename is a weak prior, never a gate.

**Algorithm.** `|F| ≤ 4` (the common single-cell case): brute-force all ≤ `4! = 24` injective maps,
filter by `valid` (which natively enforces non-decomposable globals). Large `|F|`: Hungarian on
`−(M + β·prior)` with forbidden cells as `+BIG` edges, **then post-check that no selected edge is a
BIG/forbidden edge** (an all-forbidden row → the role is unfillable → `score(t) = −∞`, *not* a padded
assignment); re-check globals, escalating to Murty k-best if a non-decomposable global fails. Building
`M` over all techs is dominated by `onlist_hit_rate` (~100 ms) — which is exactly why the hypothesis
orders and prunes which onlists are computed.

### 3.4 How the asserted hypothesis enters (pushback #1)

The brief's `score(Observation, KB)` cannot implement rung 3 ("test ONE list") because the list to
load is a *metadata* fact, not recoverable from the bytes being identified. So:

```
score(Observation, KB, hypothesis: Assertion | None) -> ResolveResult
```

The span-verified `hypothesis` has exactly two **control-flow** effects and zero evidential ones: (1)
**selector** — picks the one rung-3 onlist/signature to evaluate first, enabling early-stop at ~100
ms; (2) **tie-break prior** — a sub-threshold nudge on evaluation order. It **never** enters `M`,
un-gates a forbidden cell, or wins a Conflict.

**Determinism.** For fixed `Observation`, the validity and finite score of any candidate that gets
computed is a pure function of the bytes; `hypothesis` only changes *which* candidates are computed,
at *what cost*, to *which rung*. So: same `Obs`, same `h` → identical output; same `Obs`, different
`h` → **identical winner whenever the bytes are decisive** (a wrong `h` just fails its `requires`
gate, blocks early-stop, forces the ladder down). Only where bytes are genuinely non-decisive (a
processing-divergent pair) may `h` break the tie — and then only via the escalation function at rung
0, recorded `basis: asserted` and **surfaced**, never merged into `observed`.

### 3.5 Escalation → `{Decision | Conflict | Question | Blocker}`

Inputs: ranked `[(t, score(t), A_t, rung_reached_t)]`, `Observation`, verified `Assertion`s, and the
CI confusability metadata. `margin = score(top) − score(second)`; `θ` a small tie threshold.

```
if no candidate passes requires:
    if a required PHYSICAL read is unfillable by any file      -> Blocker(MISSING_TECHNICAL_READ, remedy)   # rung 2
    elif gzip/integrity failed                                 -> Blocker(TRUNCATED_GZIP | CORRUPT_FASTQ)   # rung 2
    else                                                       -> Blocker(UNSUPPORTED_TECHNOLOGY)           # "unsupported", not a guess
divergent_ties = { t in confusable_ties(top) : not processing_equivalent(top, t) }
if margin > θ and divergent_ties == {}:                        -> Decision(top)                             # rung 2 or 3
elif divergent_ties == {} and the tie set is a DECLARED processing_equivalent group:
                                                               -> Decision(record ALL); Questions=0; Conflicts=0   # §12 benign, rung 3
else:  # processing_DIVERGENT tie
    for mode in top.decidable_by (ladder order):
        onlist(3)   : already tried in scoring
        metadata(0) : if a span-verified Assertion disambiguates & is byte-consistent -> Decision(asserted), SURFACED
        alignment(6): mini-align to a tiny reference (strand / 3'-vs-5') -> Decision if it resolves
        user(7)     : -> Question  => the batch DEFERS to a human via exit 4   (locked decision)
```

An **undeclared** sub-θ tie (a hole in the confusability matrix) is **never** record-both — it
escalates to a Question, so record-both fires *only* for a CI-declared processing-equivalent group.
**Conflict detection runs unconditionally in parallel:** if an observed value contradicts an asserted
one (e.g. asserted 26 bp vs observed 28 bp), emit a surfaced `Conflict`; the library section (authority
= evidence) takes the observed value, the Conflict stays attached, and `compile` refuses until
`user_confirmed`. **Exit-code contract:** an open `Conflict` or non-empty `questions.md` → exit **4**
everywhere (including `manifest validate`); exit **3** is reserved for hard `Blocker`s that no human
answer can clear.

### 3.6 Worked example — §12 (synthetic ce11-shaped fixture; real dataset untouched)

Two files/sample, one 28 bp read + one ~90 bp read; `_1/_2` carry no role info; `header_index`
ABSTAINs on both.

| | f1 (_1) | f2 (_2) |
|---|---|---|
| len (mode, n_distinct) | 28, 1 | 90, many |
| distinct_ratio [0,16) | 0.08 (→ CB) | ~1.0 |
| distinct_ratio [16,28) | 0.98 (→ UMI) | — |
| composition | uniform ACGT, no linker | gene-biased |

Byte-derived roles: f1 → CB16+UMI12, f2 → cDNA. Candidate family (28 bp, 16+12) = {v3, v3.1, GEM-X
v4, Multiome}. Hypothesis "10x 3′ v3" selects `3M-february-2018`: forward hit 0.82 (>0.6), RC ≈ floor.

```
             f1                                   f2
 CB    1.00 (28 PASS; 3M .82 PASS; supports .5+.3+.2)   FORBIDDEN (segment_length 28 FAIL: 90≠28)
 cDNA  FORBIDDEN (len≥40 FAIL: 28<40)                    1.00 (90 PASS; supports .6+.4)
```

Only one valid injective A: CB→f1, cDNA→f2; `raw = 2.00`, `|R_t|=2` → `score(3′v3) = 1.00`. **3′v3.1**
identical matrix → 1.00. **GEM-X v4** and **Multiome** require their own onlists which hit ≈ floor on
these reads → FORBIDDEN → excluded *(FLAG-8: certified by CI cross-hit, not memory; if a whitelist
were a superset the pair would become a divergent tie routed to `decidable_by`, not silently gated)*.
Tie {v3, v3.1} is CI-proven `processing_equivalent` → **Decision: record both**, `library.chemistry =
["10x-3p-gex-v3","10x-3p-gex-v3.1"]`, `basis: observed`, **Questions = 0, Conflicts = 0**, rung 3.

| Adversarial variant | Outcome | Rung | Q | Conflicts |
|---|---|---|---|---|
| Base (§12) | Decision — record {v3, v3.1}, identical STARsolo params | 3 | 0 | 0 |
| No technical read (both ~90 bp) | `Blocker(MISSING_TECHNICAL_READ)` + `--include-technical`/SDL remedy | 2 | 0 | — |
| Swapped `_1/_2` names | Identical manifest (byte-driven; β sub-threshold) | 2 | 0 | 0 |
| Lying metadata (asserted v2) | `Conflict` (26 bp asserted vs 28 bp observed), surfaced; library → observed v3/v3.1 | 3 | →user | 1 |

---

## 4. CLI verb surface (Typer; every verb `--json`)

**Conventions.** Machine JSON to **stdout**, human logs to **stderr** (so `--json` stdout is a clean
pipe). Exit code is identical in `--json` and pretty mode. Universal flags: `-C/--workspace`,
`--kb`/`--kb-version`, `--no-cache`/`--refresh`, `--offline`, `-q/-v`. **Exit codes (uniform):**
`0` OK · `1` ERROR (bug/IO, not a domain refusal) · `2` USAGE · `3` BLOCKED (≥1 `Blocker`) ·
`4` NEEDS_HUMAN (open `Conflict` / non-empty `questions.md`). `probe`/`io peek` never return 3/4 —
they only observe; refusal happens downstream when a validator reads the observation.

```
seqforge
  probe FILES…                 det   bytes→Observation (--max-reads 200k, --max-bytes 256MB; never whole FASTQ)
  io peek URI | resolve ACC | onlist {list|show|fetch|add}   det†  the only network surface; pooch+hash-verified
  harvest normalize DOC…       det   PDF/text → info/normalized/*.txt canonical span space
  harvest extract              LLM   normalized text (+KB prose) → AssertionDraft[]      ← the ONE batch LLM touchpoint
  harvest verify               det   quote-search back into canonical text + value-entailment → Assertion[]
  resolve score                det   Obs+KB[+hypothesis:Assertion] → ResolveResult; NO LLM
  resolve apply                det   persist a human/agent answer → Decision
  resolve adjudicate           LLM*  opt-in job (b) over code-flagged questions; OFF in `run` (locked decision)
  manifest fill | validate | hash    det  validate → Blocker[] + exit; referential-integrity + no-abs-path checks
  compose                      det   manifest → config.yaml + units.tsv + modules; SPLIT gate (§4.1); pure fn of manifest
  kb list|show|lint|new|roundtrip|confusability|seqspec-export|seqspec-check|e2e   det  self-tests + CI gates
  eval run                     det‡  evals harness: field-acc, false-accept, false-refuse, questions, tokens/wallclock
  schema export [MODEL|--all]  det   dump JSON Schema (single source of truth)
  journal {append|show|distill} | status   det  (distill LLM drafting lives in the SKILL layer; the verb is deterministic)
  run (alias compile)          LLM★  headless probe→harvest→resolve→manifest→compose; --no-llm; --resume; --stop-at
```

`†` io is the only network surface. `‡` eval invokes `harvest extract` only for prose cases; `--no-llm`
restricts to deterministic cases. `*` adjudicate is off in default `run`. `★` `run`'s only LLM
touchpoint is `harvest extract`; `run --no-llm` is a pure deterministic pipeline.

Key behaviors: `probe` decompresses incrementally and **stops** at the budget (no whole-file seek).
`harvest verify` is a substring search for `quote` (LLM offsets are a hint recorded in `found_at`) plus
the value-entailment gate; `--tolerance whitespace`. `io resolve` handles ENA/SRA/GEO and **SDL
`sra-pub-src-*`** (the remedy path for the dropped-technical-read Blocker); ENA-declared library fields
are carried `basis: asserted`, never observed. `estimated_total_reads` divides **compressed** size by
compressed-bytes-per-read (or reads the gzip ISIZE) — the naive decompressed form undercounts by the
~3–5× ratio.

### 4.1 The split compose gate + `kb e2e` (pushback #8)

`compose` is a pure function of the manifest (no data on disk). Its gate has **three** parts, because
a dry-run cannot catch a strand inversion.

**`skip` is first-class (implemented).** Parts 1 and 3 depend on a toolchain seqforge does not own
(`snakemake`; STAR + liulab-genome + network), and the count-matrix run is a Linux/cluster operation.
A gate reporting `pass` because it never ran would let green CI be mistaken for coverage, so each gate
reports `pass` / `fail` / **`skip`**, and `params` — which needs no toolchain — always runs.

1. **Wiring** — touch zero-byte files at every path in the file inventory **∪ resolved-onlist cache
   paths** (so a whitelist declared as a Snakemake `input:` doesn't raise a spurious
   `MissingInputException`), then `snakemake -n` + `snakemake --lint`. Catches config, wildcard
   resolution, rule wiring.
2. **Params** — deterministic assertions that the emitted `--soloStrand` / `--soloUMIlen` / `--soloCBlen`
   and the `--readFilesIn` order (cDNA read before barcode read, derived from the role assignment)
   match the KB `backend.params`. These are the semantic bugs a linter *cannot* see and must **not**
   be attributed to `snakemake -n`.
3. **`kb e2e`** — the brief's one real end-to-end run (**IMPLEMENTED and passing**): reads simulated
   from sacCer3 transcripts with injected barcodes/UMIs, driven through the *whole* compiler —
   probe → resolve (which must decide the chemistry from the bytes alone, no metadata hint) → fill →
   validate → compose → **STARsolo run with the composed params** → assert the matrix against the
   injected truth. Runs on a Linux compute node (STAR + liulab-genome); `skip` elsewhere.

   **The assertion is "accounted", not naively exact.** Real transcripts mean real ambiguity: reads
   from paralog/subtelomeric-repeat families (Y′/`YRF1`) legitimately multimap and STARsolo drops
   them, so `observed == injected` is unachievable and demanding it would only teach us to weaken the
   gate. Instead the gate asserts what indicts *us*:
   - **0 spurious pairs** — never count a read for a gene it did not come from;
   - **0 inflated counts** — never invent a UMI (a dedup/geometry bug looks exactly like this);
   - **`unexplained_loss <= 2%`** — subtract STAR's own multimapper loss (read from `Log.final.out`);
     what remains is the compiler's error, and it must be ~0;
   - **strand sensitivity** — the same reads re-run under an inverted `--soloStrand` must collapse,
     or the gate could not have caught an inversion in the first place.

   Measured on arc (2 000 reads, 120 genes, 8 cells): resolve decided `10x-3p-gex-v3` unaided;
   1 909/2 000 recovered with **0 spurious / 0 inflated**; STAR uniquely mapped 1 923 (77 multi/too-many
   loci), leaving **0.7 % unexplained**; the inverted strand collapsed to **49/2 000 (2.5 %)**.

   Still open: an **intron-rich fixture** (yeast is nearly intron-free and cannot exercise
   `GeneFull`), and a **SPLiT-seq** e2e — this run certifies 10x 3′ v3's `soloStrand Forward` only, so
   `splitseq`'s strand FLAG stays open until it gets its own simulation.

### 4.2 Skill → verb map, hooks, state

All nine §10 skills are thin clients: `orchestrate`→`run`/`status`/`manifest hash`; `exam`→`probe`
(+`io peek`); `harvest`→`normalize`(det)+`extract`(LLM)+`verify`(det); `resolve`→`score`(det)+`apply`(det)
+`adjudicate`(opt-in); `manifest`→`fill`/`validate`/`hash`; `compose`→`compose`; `io`→`io *`;
`kb-author`→`kb *` (the skill's LLM drafts README/spec prose; the verbs that write/verify are
deterministic); `journal`→`journal *`. **Every verb is shell-scriptable with no LLM except `harvest
extract`** (+ opt-in `adjudicate`, off in `run`) — so `run --no-llm` drives the whole compiler minus
extraction.

**Hooks (policy → mechanism):** `PreToolUse` blocks a Bash call that would stream a FASTQ > ~200 MB
without a head/budget flag **and** any write of an absolute path / `/scratch/**` into a manifest or
config; `PostToolUse` auto-runs `manifest validate --json` after any manifest edit; `Stop` refuses to
end a turn while `resolve/*/questions.md` is non-empty. Because `run` leaves `adjudicate` off, the
Stop hook and exit 4 are the only ways ambiguity clears — both route to a human — which is what keeps
the batch to one LLM touchpoint.

**`.seqforge/` (resumable, content-addressed):** per-file `Observation` keyed by file sha256;
`NormalizedDoc`/`Assertions`/`VerifyReport` by `doc_sha256` (+ `normalizer_version`; assertions also
by `(model, prompt_version)`; verify by `(doc_sha256, assertions_sha)`); dataset
`candidates`/`conflicts`/`questions` by `dataset_id = sha256(sorted(file_shas) ⊕ kb_version)` with
`probe_version`/`resolve_version` folded into the key; `manifest.yaml` written **only** after a clean
`validate`; `manifest.lock.json` = `provenance_id = H(manifest_sha, kb_version, workflow_version)`.

---

## 5. Pushback appendix (the arguments to settle)

Ranked; ✔ = folded into this design, ✱ = open for the maintainer. Full detail in the approved plan.

1. ✔ **`score()` needs the hypothesis** — the brief's byte-blind signature can't do "cheap first"; it
   enters as a selector/prior, never evidence (§3.4).
2. ✔ **Distinct-ratio is depth-dependent** — supports-only, normalized; onlist confirms CB/UMI (§3.1).
3. ✔ **Score must be role-cardinality-normalized** — else `argmax` favors high-role-count techs (§3.3).
4. ✔ **`decidable_by` must include `onlist`** — the mechanism §12 actually uses (§1.4, §2).
5. ✔ **Onlist orientation is per (chemistry, read), not per list** — registry value is a hint only (§1.6).
6. ✔ **Onlist index must be width-generic** — SPLiT-seq's 8 bp blocks, not a hardcoded 16 (§3.1).
7. ✔ **The exact-span contract is infeasible as an LLM instruction** — LLM emits `quote`, code
   computes offsets; verify also checks entailment (§1.3).
8. ✔ **The dry-run gate can't catch a strand inversion** — split gate + `kb e2e` count-matrix run (§4.1).
9. ✱ **Pre-registering PRJNA1027859's organism vs "don't tune against it"** — safe reading:
   `expected.yaml` uses GEO-declared metadata + provider-independent prior only, committed before any
   run; never a value read from the data. Authored later, on the maintainer's go (§6).
- ✱ **Basis for policy defaults** — kept to the brief's four (`inferred` + an evidence ref naming the
  policy rule); add a `policy_default` basis if you want it machine-distinguishable (§1.0).

---

## 6. Milestone-0 scope + coverage caveats

Vertical slice, all four stages at reduced coverage (breadth before depth), for three technologies
chosen for architectural coverage: **(1) bulk Illumina RNA-seq PE** (no-barcode branch, header
parsing, run/lane grouping); **(2) 10x 3′ GEX v3** (onlist matching, technical-read identification,
SRA-mangling); **(3) SPLiT-seq** (original Rosenberg-2018 combinatorial multi-block indexing — the
pilot-#3 generalization test; Parse Evercode deferred to its own future entry). Plus the three
day-one negatives: truncated gzip → `Blocker`; a KB-absent
technology (ONT) → `UNSUPPORTED_TECHNOLOGY`, not a guess; a contradiction (metadata v2, reads v3) →
surfaced `Conflict`.

**Coverage caveat (recorded so green CI isn't mistaken for full coverage):** SPLiT-seq exercises
combinatorial barcodes + fixed linkers + small onlists, but **not** variable-length/anchored elements.
The `anchor`/`motif` element path is in the schema and the scorer, but its dedicated test fixture (an
inDrop-class chemistry with a floating W1 linker) is **deferred** — add an inDrop entry to exercise it
before claiming the element model fully generalizes.

---

## 7. Genomics-correctness FLAGS (unverified — do not ship without checking)

1. **EFO/OBI CURIEs — all four pilot techs verified against the EBI OLS** (not memory): `EFO:0009922`
   (10x 3′ v3), `EFO:0009899` (10x 3′ v2), `EFO:0009919` (SPLiT-seq), `EFO:0008896` ("RNA-Seq", bulk).
   Any *future* tech must be looked up against live EFO/OBI the same way before use. (Noted for later:
   v3.1 has its own term `EFO:0022980`, and Parse Evercode's are `EFO:0022600/1/2` — distinct assays.)
2. **GEM-X 3′ v4 whitelist filename** — referenced conceptually; register it by URL+sha256, do not
   invent a filename.
3. **SPLiT-seq — DONE (read structure + linkers) / still-open (whitelists, strand).** The Read-2
   layout and both 30 bp linker sequences are now pinned verbatim from scg_lib_structs (Science-2018
   variant; see §2.3). Still to pin: the three round whitelists (register the real barcode files by
   URL+sha256), `soloStrand`, and the EFO CURIE. `soloCBposition`/`soloUMIposition` are generated from
   the element model at compose, never hand-entered (FLAG-3). Parse Evercode is a separate future entry.
4. **10x 5′ `soloStrand`** — 5′ vs 3′ have identical CB/UMI geometry (read-undecidable); 5′ is a known
   confusion source. KB carries `soloStrand` per chemistry; CI round-trip confirms it. Do not assert
   from memory.
5. **seqspec `CB_UMI_Complex` support** — the dual-derivation check (`seqspec index` vs `backend.params`)
   may not cover adapter-anchored/combinatorial barcodes; scope it to fixed-offset chemistries until
   confirmed.

**Confident (encode directly, verified by `kb roundtrip`):** 10x 3′ v2 = 16+10 = 26 bp,
`737K-august-2016`; 3′ v3/v3.1 = 16+12 = 28 bp, `3M-february-2018` (~6.79M), identical STARsolo params
(§12 benign); Multiome GEX `737K-arc-v1`; 28 bp does not identify chemistry (v3/v3.1/GEM-X v4/Multiome
all 28 bp, separated only by onlist); STARsolo `CB_UMI_Simple` param names; `--readFilesIn` is cDNA
read first, then barcode read.

---

## 8. Held-out acceptance case — untouched

`/scratch/zhoulab/hanliu/260612-worm/PRJNA1027859` (arc server) is the single real acceptance test.
It has **not** been read, sampled, listed, stat'd, profiled, or tuned against, and will not be until
the maintainer says so. `ce11` (C. elegans, taxid 6239, WBcel235) is confirmed available in
liulab-genome, so the case is *resolvable* without touching it. Its pre-registration
(`evals/cases/PRJNA1027859/expected.yaml`) is a later task, written from GEO-declared metadata +
provider-independent prior knowledge only, committed before any run — never from a value read out of
the data.
