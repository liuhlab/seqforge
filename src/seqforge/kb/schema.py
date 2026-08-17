"""The KB ``spec.yaml`` schema — machine-checkable, closed-vocabulary, self-validating.

One directory per technology: ``kb/specs/<tech>/{spec.yaml, README.md}``. ``spec.yaml`` declares the
read layout (element coordinates), onlist references, a detection ``signature`` (requires / supports /
excludes), a ``backend`` param template, and a ``confusable_with`` list. Every model forbids extra
keys, so a typo fails validation exactly where the DSL is executed. The signature test vocabulary is
*exactly* the scorer's evaluator set.

Three authoring rules nothing below can enforce, because each is about where a value CAME FROM:

**Every value is pinned to a live source, or it does not enter a spec.** A barcode file, a linker
sequence, a strand, an ontology term — look each one up against whoever publishes it and pin it by URL
and checksum. Never assert one from memory, and never from a neighbouring entry. Being wrong here does
not fail: a wrong whitelist emits a thin-looking matrix at exit 0, and a plausible matrix is the one
failure mode nothing downstream recovers from, where a refusal always is. A value not yet pinned is an
open lookup in the tracker, never a placeholder in a ``spec.yaml`` — an unverified value parked in
prose is one nobody checks again. A whitelist may legitimately be pinned *ahead* of the entry that
will need it, since packing it is a separate and separately verifiable act, but a pin is a loan and
not a home: one sitting in the registry with no spec behind it is a debt.

**Nothing computes a cross-hit rate between two whitelists.** A ``distinguishable_by`` naming
``onlist`` is taken at its word — what CI checks is that a divergent pair names a mechanism at all,
never that the named mechanism can separate that pair. ``io.intersect_fraction`` over the packed
barcode arrays is the measurement; run it by hand and record the number in the spec beside the value
it justifies. A checksum is not the substitute waiting to be used: different hashes prove the files
differ, not that the barcode sets do, and a whitelist that is a superset of another shares a hash with
nothing.

**No second derivation is watching.** Every element declares a ``seqspec_region_type`` and every read
a ``seqspec_read_id``, so a seqspec export would be a pure derivation rather than a translation — but
the emitter is unbuilt, seqspec is not a dependency, and nothing here reads its output. Do not write
an entry as though a dual derivation will catch a mistake in it. What does check one is ``kb
roundtrip``, the parse-key gate below, and the confusability sweep.
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ElementType = Literal[
    "barcode", "umi", "cdna", "gdna", "linker", "poly_a", "poly_t", "fixed", "index", "diversity"
]
# ``diversity`` = a variable-length insert of random bases at a read's 5' end (BD Rhapsody Enhanced's
# 0-3 bp diversity/phasing insert). It carries no barcode/UMI value — its ONLY effect is to STAGGER
# every downstream element by its per-read length, which is why it is modelled as a first-class element
# (``min_len``/``max_len``): the generator draws a per-read length for it and the anchored resolver
# recovers the frame the stagger created (see ``kb/anchor.py``). It is not a linker/fixed (no literal
# sequence) and not a counted feature, so ``compose`` skips it and it maps to a ``linker`` manifest role.
Mechanism = Literal["none", "onlist", "metadata", "alignment", "user"]
Decidable = Literal["reads", "onlist", "metadata", "alignment", "user"]
Orientation = Literal["forward", "revcomp", "either"]
SeqspecRegion = Literal[
    "barcode",
    "umi",
    "cdna",
    "gdna",
    "index5",
    "index7",
    "linker",
    "poly_A",
    "poly_t",
    "custom_primer",
]

_ONLIST_TOKEN = re.compile(r"^\{onlist:([A-Za-z0-9._-]+)\}$")
_ANY_BRACE = re.compile(r"\{[^}]*\}")


class _Forbid(BaseModel):
    """Base that forbids unknown keys, so the closed vocabulary is enforced where it is executed."""

    model_config = ConfigDict(extra="forbid")


class Anchor(_Forbid):
    """Locate a variable-length / floating element (e.g. inDrop's post-W1 barcode)."""

    relative_to: Literal["read_start", "read_end", "element"] = "read_start"
    ref_element: str | None = None
    ref_side: Literal["start", "end"] = "end"
    offset: int = 0
    motif: str | None = None
    max_mismatch: int = 0


class Element(_Forbid):
    """One element of a read. 0-based half-open ``[start, end)``; ``end=None`` => open-ended (cDNA)."""

    type: ElementType
    name: str
    start: int | None = None
    end: int | None = None
    min_len: int | None = None
    max_len: int | None = None
    anchor: Anchor | None = None
    sequence: str | None = None
    onlist: str | None = None
    seqspec_region_type: SeqspecRegion

    @model_validator(mode="after")
    def _addressable(self) -> Element:
        fixed = self.start is not None and self.end is not None
        opened = self.start is not None and self.end is None
        varlen = self.min_len is not None or self.max_len is not None
        anchored = self.anchor is not None
        if self.type in ("linker", "fixed") and self.sequence is None:
            raise ValueError(f"element {self.name!r}: linker/fixed needs a literal `sequence`")
        if self.sequence is not None and self.start is not None and self.end is not None:
            # Two declarations of ONE width, and nothing downstream reconciles them. The generator
            # concatenates elements in order while every reader addresses `[start, end)`, so a literal
            # that disagrees with its own window shifts every element after it: `kb roundtrip`'s C1
            # then reddens on some LATER element, far from the cause, and only if the entry has a
            # fixture to run it against at all. Conditioned on all three being present because a
            # floating linker (BD Rhapsody Enhanced, behind its diversity insert) declares a literal
            # and NO window on purpose — it has one width, not two, and nothing to contradict.
            width = self.end - self.start
            if len(self.sequence) != width:
                raise ValueError(
                    f"element {self.name!r}: `sequence` is {len(self.sequence)} bp but "
                    f"[{self.start}, {self.end}) is {width} bp wide"
                )
        if self.type in ("cdna", "gdna"):
            return self  # open-ended is legal
        if not (fixed or opened or anchored or varlen):
            raise ValueError(f"element {self.name!r}: give [start,end), an anchor, or min/max_len")
        return self


class Read(_Forbid):
    """A read (== one FASTQ). ``id`` is a ROLE label (R1/R2/bc/cdna), never a filename claim."""

    id: str
    seqspec_read_id: str
    file_hint: str | None = None
    strand: Literal["pos", "neg"] = "pos"
    min_len: int | None = None
    max_len: int | None = None
    elements: list[Element]


#: The maximal read set's implicit name — ``reads`` itself, the set every other one is a subset of.
#: RESERVED, and reserved by ABSENCE from :data:`ReadSetName`: it already names ``reads``, so binding
#: it in ``read_sets`` would be a second declaration of the same set, free to disagree with the first.
FULL_READ_SET: Final = "full"

#: The names a spec may give a read set: a **closed vocabulary**, extended deliberately — adding a
#: word here is the same act as adding an ``ElementType``, not a local edit. A ``Literal`` because that
#: makes ``single_end:`` fail at spec LOAD, where every other DSL typo fails; the alternative is a read
#: set nothing ever selects, on an entry that reads as though it declared two configurations.
#:
#: ``se`` is the whole vocabulary today, and it covers the case the feature was built for: a protocol
#: that publishes a single-end configuration beside its paired-end one (bulk RNA-seq; SMART-seq3's
#: Methods publish "75-bp single end, 50-bp single end or 150-bp paired end" verbatim). Note what is
#: NOT here and must not be added: nothing naming how many files a DEPOSIT happens to hold. A read set
#: is a configuration the chemistry publishes, and the bytes choose between them.
ReadSetName = Literal["se"]


class OnlistRef(_Forbid):
    """Alias -> pooch-registry name. URL/sha256/length/orientation live in the registry, never here."""

    registry: str
    role: Literal["cell_barcode", "sample_index", "feature", "atac_barcode"]
    expected_orientation: Orientation = "forward"


# ---- signature tests: a CLOSED set == the scorer's evaluators ----
class _Seg(_Forbid):
    """A test addressed to a segment by element name XOR (start, end)."""

    read: str
    element: str | None = None
    start: int | None = None
    end: int | None = None

    @model_validator(mode="after")
    def _one_address(self) -> _Seg:
        by_name = self.element is not None
        by_coord = self.start is not None and self.end is not None
        if by_name == by_coord:
            raise ValueError("address a segment by element name XOR (start, end)")
        return self


class SegmentLength(_Forbid):
    test: Literal["segment_length"]
    read: str
    length: int
    tolerance: int = 0
    #: Over-length escape. A read whose mode is ``>= over_length_min`` PASSES regardless of ``length``
    #: — an insert-bearing / over-sequenced barcode read (e.g. a 10x R1 sequenced to 150 bp: CB+UMI in
    #: bp0-28, the rest junk). Keep it strictly above ``length`` so a canonical read is never
    #: "over-length" and the exact-length gate still separates near-neighbours (v2's 26 vs v3's 28).
    over_length_min: int | None = None


class HasSegment(_Seg):
    test: Literal["has_segment"]
    kind: Literal["constant", "random", "polyT", "polyA"]


class DistinctRatio(_Seg):
    test: Literal["distinct_ratio"]
    expect: Literal["low", "high"]  # SUPPORTS-only; depth-dependent, never a gate


class OnlistHitRate(_Seg):
    test: Literal["onlist_hit_rate"]
    onlist: str
    orientation: Orientation = "either"
    min: float
    #: How far to slide the barcode window around ``start`` (± this many columns) when hunting the
    #: whitelist hit. Default 2 absorbs sequencer phasing slack. A chemistry whose cell barcode sits
    #: behind a constant lead-in of a length the archive does not pin (10x Multiome ATAC: an 8 bp
    #: lead-in before the 16 bp barcode) widens this so the SAME spec resolves both the clean
    #: 16 bp-at-0 deposit and the prefixed 24 bp one — the bytes locate the barcode, the spec never
    #: pins the offset. The winning ``(offset, orientation)`` is recorded so the manifest, not the
    #: spec, carries the geometry the composer emits.
    offset_scan: int = Field(default=2, ge=0, le=32)


#: Where a motif is looked for. Named, rather than spelled inline, because the resolver switches on
#: it: a fifth place added here must fail loudly at the site that classifies it, not fall through to
#: "scan the whole read" — the shape that was a live defect in the whitelist scan.
MotifWhere = Literal["read_start", "read_end", "anywhere", "window"]


class MotifPresent(_Forbid):
    test: Literal["motif_present"]
    read: str
    motif: str
    where: MotifWhere = "anywhere"
    search_start: int | None = None
    search_end: int | None = None
    max_mismatch: int = 1
    min_rate: float = 0.5


class BaseComposition(_Seg):
    test: Literal["base_composition"]
    base: Literal["A", "C", "G", "T", "N"]
    min_fraction: float


class HeaderIndex(_Forbid):
    test: Literal["header_index"]
    present: bool


Test = Annotated[
    SegmentLength
    | HasSegment
    | DistinctRatio
    | OnlistHitRate
    | MotifPresent
    | BaseComposition
    | HeaderIndex,
    Field(discriminator="test"),
]


class Support(_Forbid):
    when: Test
    weight: float = 1.0


class Signature(_Forbid):
    requires: list[Test]  # hard AND-gates (no distinct_ratio here — it's depth-dependent)
    supports: list[Support]  # additive evidence (onlist + distinct_ratio live here)
    excludes: list[Test]  # anti-gates: any pass => disqualify


class Backend(_Forbid):
    """A data template mapping to a workflow module. Only ``{onlist:<alias>}`` interpolation is legal.

    ``params`` is the chemistry-defining MINIMUM: the keys whose value varies with the chemistry, and
    no others. Ownership is decided by what a value varies with, never by what it is for — and
    "chosen for CellRanger parity" is a reason to pick a VALUE, never evidence about who owns the key.
    That one set of knobs splits both ways. ``soloUMIdedup 1MM_CR`` and ``soloCellFilter
    EmptyDrops_CR`` are the same string for every chemistry there will ever be, so they are literals in
    the workflow module's own shell block: not this file's, and (being unconditional) not the recipe's
    either. ``soloCBmatchWLtype`` and ``clipAdapterType`` are not — the first tracks whichever barcode
    correction the vendor's reference pipeline ships, the second which read trimmer it runs, and a
    five-prime kit runs none of the three-prime one's — so each is declared here, one row per
    chemistry. Sorting them by what they are FOR files all four together and is wrong about half.
    Every entry that names a trimmer points back at this paragraph; the per-vendor evidence it rests
    on is ``docs/research/starsolo-read-preprocessing-per-family.md``.

    Two spellings of one geometry is one spelling too many, so ``soloCBposition`` / ``soloUMIposition``
    are omitted here and derived from the element coordinates at compose time rather than hand-typed.
    """

    module: str
    params: dict[str, str | int | float | list[str]]

    @model_validator(mode="after")
    def _not_a_chimeric_variant(self) -> Backend:
        """A chimera-aware twin is reachable through its base's declaration and no other way.

        A twin is selected by the composer swapping it in for its base when the recipe's assembly is
        spelled like a **chimera**'s. A spec naming one directly would reach the same module by a
        route that never asked the question the swap exists to ask — so a plain assembly would
        compile to a pipeline that splits a BAM by a suffix nothing put there, and the failure would
        be a refusal deep in a run rather than at compose time.

        **The set is DERIVED from the module registry** (:data:`~seqforge.workflows.CHIMERIC_VARIANTS`)
        rather than listed here, which is the same rule as everything else in this file that knows
        about modules: a second list is one a new twin is missing from, and the guard would then pass
        while guarding nothing. It fires before :meth:`Spec._cell_axis_matches_the_module` gets to
        speak, so a spec naming the plate twin is told the real reason rather than being asked to
        declare a cell axis it may not have anyway.
        """
        from ..workflows import CHIMERIC_VARIANTS

        if self.module in CHIMERIC_VARIANTS:
            raise ValueError(
                f"backend.module {self.module!r} is a chimera-aware twin, which no spec may name. "
                f"A twin is selected by compose swapping it in for the pipeline this chemistry "
                f"already binds to, and only when the recipe names a chimeric assembly — naming it "
                f"here reaches it by a route that never asks whether the reference is a chimera at "
                f"all. Declare the base pipeline; the twin follows from the recipe."
            )
        return self

    @model_validator(mode="after")
    def _only_parse_keys(self) -> Backend:
        """A count/reference/runtime knob may not be declared here — only this pipeline's parse keys.

        The line is parse vs. count. What to COUNT (``soloFeatures``, ``quantMode``) is *intent*: it
        belongs to the processing manifest, where a user may instruct it and a gate may check it.
        ``soloFeatures`` once sat in a backend's params and cost a measured **40.7 % of a nuclear
        library** — 10x 3' v3.1 chemistry is byte-identical for cells and nuclei, so counting was never
        a chemistry property. The parse namespace is now **per pipeline** (``Pipeline.parse_keys``), so
        the allowed set is looked up by ``self.module`` rather than shared globally: a chromap backend is
        policed against chromap's knobs, a starsolo backend against ``solo*``.

        ``params`` is a ``dict``, so ``extra="forbid"`` cannot reach inside it — hence an explicit
        validator. It fires in ``load_spec``, in ``kb lint``, and in every test that loads a spec, which
        is what makes the parse/count line a property of the DSL rather than a convention.
        """
        from ..workflows import parse_keys_for

        try:
            allowed = parse_keys_for(self.module)
        except KeyError as exc:
            raise ValueError(
                f"backend.module {self.module!r} is not a registered pipeline: {exc}"
            ) from exc
        stray = sorted(set(self.params) - allowed)
        if stray:
            raise ValueError(
                f"backend.params declares key(s) {stray} that pipeline {self.module!r} does not parse: "
                f"backend.params says how to PARSE reads (byte-decided); what to COUNT belongs in the "
                f"processing manifest, where a user may instruct it. {self.module!r} parses: "
                f"{sorted(allowed)}"
            )
        return self

    def check_tokens(self, onlist_aliases: set[str]) -> None:
        """Reject any interpolation token that is not a declared ``{onlist:<alias>}``."""
        for value in self._strings():
            for match in _ANY_BRACE.finditer(value):
                token = _ONLIST_TOKEN.match(match.group(0))
                if token is None:
                    raise ValueError(
                        f"illegal template expression {match.group(0)!r} "
                        "(only {onlist:<alias>} is allowed)"
                    )
                if token.group(1) not in onlist_aliases:
                    raise ValueError(f"unknown onlist alias {token.group(1)!r}")

    def _strings(self) -> list[str]:
        out: list[str] = []
        for value in self.params.values():
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, list):
                out.extend(v for v in value if isinstance(v, str))
        return out


class Confusable(_Forbid):
    id: str
    relationship: Literal["processing_equivalent", "processing_divergent"]
    distinguishable_by: list[Mechanism]
    note: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Confusable:
        if self.relationship == "processing_divergent" and self.distinguishable_by == ["none"]:
            raise ValueError("a processing_divergent pair cannot be distinguishable_by [none]")
        return self


class Identity(_Forbid):
    id: str
    version: str
    name: str
    #: Spellings that NAME this node: writing one is a claim that any text carrying it IS this
    #: chemistry, and it outranks every ``descriptive_alias`` on every other node. So a bare field word
    #: (``WTA``, ``RNA-seq``) is one you must never write here — it would claim a whole field of assays
    #: for one entry. This is also the ONLY list shown to the extraction model, so a spelling the model
    #: needs in order to name this node at all belongs here and not below.
    aliases: list[str] = Field(default_factory=list)
    #: Spellings that only DESCRIBE how a library was run — "paired-end RNA-seq" is true of a bulk
    #: library and equally true of a SPLiT-seq one. They still reach this node (an archive describing
    #: a real bulk record that way must resolve, #184), but they lose to any form that names one, so
    #: they name it only when nothing else is named (#266). A form belongs here when you can picture
    #: a *different* chemistry's record carrying it truthfully. Both lists are surface forms the span
    #: verifier will accept; only ``aliases`` reaches the extraction model.
    descriptive_aliases: list[str] = Field(default_factory=list)
    assay_ontology: list[str] = Field(default_factory=list)
    modality: Literal["rna", "atac", "multi"] = "rna"
    #: One ``Sample`` of this chemistry IS one cell — demultiplexing happened at the BENCH, so the
    #: cell barcode is the file and not a read. **Declared, never derived.** ``umi and not barcode``
    #: was tried and is backwards in both directions: SMART-seq2 has neither and is still one cell
    #: per file, UMI-tagged bulk has a UMI and no barcode and is one file per specimen. What the
    #: property is about is *where demultiplexing happened*, which is outside the bytes entirely —
    #: which is exactly why it earns a name rather than a rule. It says ``Sample``, not file and not
    #: run, because 20 of 190 well-labelled plate deposits are not strictly 1:1. Rejected: a
    #: three-value cell-axis field (two of its three values are derivable, re-importing the disease
    #: the declaration cures), and any name built on "demultiplexed" — every Illumina run is
    #: sample-demultiplexed at bcl2fastq, so a reader would tick that box for a droplet chemistry
    #: too. (#253 decision 1)
    sample_is_cell: bool = False


#: Which END of a read each read trimmer will take a declared adapter for. The modes are exactly
#: COMPLEMENTARY, and that is what makes a clip's legality one rule rather than a table of illegal
#: pairs: ``CellRanger4`` builds its own fixed three-prime poly-A and refuses to be handed a second
#: one, while taking a five-prime sequence in place of the TSO it hardcodes; ``Hamming``, the default,
#: is the reverse and rejects a five-prime adapter outright. Measured against the pinned STAR binary
#: at parameter initialization and never read off its help, which is stale relative to its own code —
#: it still advertises a third mode that fails to parse, which is why a value absent from here is
#: refused rather than assumed harmless. Adding a mode is the same deliberate act as adding an
#: ``ElementType``, and the measurement belongs beside the rest of the arity table in
#: ``docs/research/smartseq3-tn5-read-through.md``.
_CLIP_END_A_TRIMMER_TAKES: Final[dict[str, str]] = {
    "CellRanger4": "five-prime",
    "Hamming": "three-prime",
}


class Spec(_Forbid):
    """A complete, self-validating technology specification (one node in the KB tree)."""

    schema_version: int
    identity: Identity
    #: The **maximal** read set, implicitly named :data:`FULL_READ_SET`. Unchanged by read sets: a read
    #: is declared here exactly once, whichever configurations use it.
    reads: list[Read]
    #: Named SUBSETS of ``reads`` — the alternative sequencing configurations this one chemistry
    #: publishes, so a paired-end and a single-end run of one protocol are one entry rather than two.
    #: Each value is a list of ids ``reads`` already declares, **never a re-declaration**: that is the
    #: whole of why the shape is cheap, since no read's coordinates are written twice and two
    #: configurations of one chemistry cannot drift apart. Empty is the ordinary case.
    read_sets: dict[ReadSetName, list[str]] = Field(default_factory=dict)
    onlists: dict[str, OnlistRef]
    signature: Signature
    #: The runnable STARsolo/STAR template. ``None`` on an ABSTRACT family node — a classifier that
    #: narrows to its children but has no single recipe (``10x-3p-gex`` parses v2 and v3 differently).
    #: A leaf, and a runnable family like ``bulk``, always declare one.
    backend: Backend | None = None
    #: The family id this node descends from; ``None`` at a root. Siblings (same parent) are
    #: confusable-by-construction, decided by the parent's ``children_decided_by`` — which is why a
    #: sibling clique of ``processing_divergent`` ``confusable_with`` edges collapses to one ``parent``.
    parent: str | None = None
    node_kind: Literal["family", "leaf"] = "leaf"
    #: How a FAMILY node's children are told apart (``onlist`` / ``metadata`` / ``alignment``). Empty on
    #: a leaf. Replaces the per-sibling ``distinguishable_by`` for the divergent-tie question.
    children_decided_by: list[Mechanism] = Field(default_factory=list)
    confusable_with: list[Confusable] = Field(default_factory=list)
    #: Reads a ``Sample`` must carry before its own bytes are allowed to speak for it. **Top level,
    #: not inside ``identity``**: ``identity`` NAMES the technology, and this names no technology —
    #: it is an admission threshold, with two consumers (the dataset reduction, where a starved cell
    #: abstains and inherits its plate's chemistry instead of dissenting; and compose, which drops
    #: it). Summed over the ``Sample``'s runs, never per run: gating the run would make a threshold
    #: of 1000 silently mean 500 on exactly the 10.5% of plates that are not 1:1.
    #:
    #: A number here must sit **under the probe budget**. Below it the per-file count is exact (the
    #: probe read the file to EOF); above it the count is an ISIZE or compressed-ratio
    #: extrapolation, so a threshold set there would be compared against an estimate and would move
    #: with ``--max-reads``. ``None`` — every shipped spec — admits everything. (#253 decisions 5, 7)
    min_input_reads: int | None = Field(default=None, gt=0)
    #: The sequence past which a read has stopped being genomic — the non-genomic tail a fragment
    #: shorter than the read runs off the end of its own cDNA into, whatever that construction puts
    #: there (a tagmentation adapter, a capture poly-A). **Terminal, not a span**: everything behind
    #: the match has stopped being genomic too, so the whole tail goes. That is exactly what an
    #: aligner's clip does, and it is why this is not a trimming knob — it states a fact about the
    #: MOLECULE, which makes it chemistry and puts it here rather than in a recipe (ADR-0048).
    #:
    #: Stated ONCE for the chemistry and never per read. What the entry owes is the sequence; every
    #: pipeline works out its own flag from it, which is the same division that keeps the barcode
    #: geometry from being written twice. A read the clip must not reach is kept away from it by ROLE
    #: placement, not by a list here: the aligner is handed the reads compose placed as cDNA, and the
    #: params gate re-checks that placement.
    #:
    #: ``None`` — every other shipped spec — clips nothing, which is also every aligner's own
    #: default. Declaring it obliges the backend's pipeline to consume it; a pipeline that cannot is
    #: refused at compose, where what each one derives is known. Measurements, and the STAR arity
    #: table the flag has to satisfy: ``docs/research/smartseq3-tn5-read-through.md``.
    read_through: str | None = Field(default=None, pattern=r"^[ACGT]+$")

    def require_backend(self) -> Backend:
        """The runnable backend, or a clear error if this is an abstract family node.

        Only leaves and runnable families compile; descent always resolves to one of those, so every
        compose/params/policy site is entitled to a backend. This turns the ``Backend | None`` into a
        checked ``Backend`` at exactly those call sites.
        """
        if self.backend is None:
            raise ValueError(
                f"{self.identity.id!r} is an abstract family node with no runnable backend; "
                "only leaves and runnable families compile"
            )
        return self.backend

    def read_set_names(self) -> list[str]:
        """Every read set this spec offers — the **maximal set first**, then the declared subsets sorted.

        The order is not cosmetic: it is the tie-break. Scoring keeps the best-scoring set and an exact
        tie prefers the LARGER one (it explains more of the data), which falls out of visiting the
        maximal set first and replacing only on a strict improvement. Two subsets of equal size are
        ordered by name, so the answer is deterministic — it feeds a content-addressed artifact, and a
        winner that depended on dict iteration order would re-key a dataset for no reason.
        """
        return [FULL_READ_SET, *sorted(self.read_sets)]

    def reads_in(self, read_set: str) -> list[Read]:
        """The reads of one named set, in ``reads`` declaration order.

        Declaration order rather than the order the set lists, because a read set is a SET of ids: the
        reads it names are the same reads whichever way round they were typed, and role placement is
        the composer's business (``read_files_in``) rather than a set's. An unknown name raises — the
        names are a closed vocabulary validated at load, so reaching here with one is a code defect.
        """
        if read_set == FULL_READ_SET:
            return list(self.reads)
        # Matched by iteration rather than by `.get`, because the mapping's keys are the closed
        # `ReadSetName` Literal while a caller holds a set NAME read off an evaluation or a candidate,
        # which is a plain `str`. Narrowing it back to the Literal at every call site would push the
        # closed vocabulary out of the schema and into its consumers.
        ids = next((v for name, v in self.read_sets.items() if name == read_set), None)
        if ids is None:
            raise ValueError(
                f"{self.identity.id!r} declares no read set {read_set!r} "
                f"(it has {self.read_set_names()})"
            )
        wanted = set(ids)
        return [r for r in self.reads if r.id in wanted]

    @property
    def decidable_by(self) -> list[Decidable]:
        """How this technology can be separated from the ones it is confusable with. **Derived.**

        This was a hand-typed field on every spec, and two of them carried the comment "CI-computed
        union over the divergent confusables". No CI computed it. Nothing read it either — `escalate`
        builds a Question's ``decidable_by`` from ``confusable_with[].distinguishable_by``, inline,
        which is precisely the union the comment described. So the field was a claim about behaviour
        that caused no behaviour: a comment with a list's syntax, free to drift from the thing it
        claimed to summarize, with nothing to notice.

        That is the exact shape of `RegistryEntry.fetchable` before it was derived, and of
        `required_config` before that. Deriving it is the only fix that stays fixed.

        ``processing_equivalent`` twins are excluded on purpose: two entries with identical
        ``backend.params`` are declared equivalent and recorded together, so there is nothing to
        decide between them and no mechanism that could.
        """
        out: set[Decidable] = set()
        for c in self.confusable_with:
            if c.relationship == "processing_divergent":
                out.update(m for m in c.distinguishable_by if m != "none")
        return sorted(out)

    @model_validator(mode="after")
    def _node_shape(self) -> Spec:
        """A leaf must be runnable; ``children_decided_by`` is a family-only knob."""
        if self.node_kind == "leaf":
            if self.backend is None:
                raise ValueError(
                    f"{self.identity.id!r}: a leaf node must declare a runnable backend"
                )
            if self.children_decided_by:
                raise ValueError(
                    f"{self.identity.id!r}: children_decided_by is only meaningful on a family node"
                )
        return self

    @model_validator(mode="after")
    def _cross_refs(self) -> Spec:
        aliases = set(self.onlists)
        read_ids = {r.id for r in self.reads}
        elements_by_read = {r.id: {e.name for e in r.elements} for r in self.reads}

        # every onlist alias referenced by an element must be declared
        for read in self.reads:
            for el in read.elements:
                if el.onlist and el.onlist not in aliases:
                    raise ValueError(f"element {el.name!r}: unknown onlist {el.onlist!r}")
                if el.anchor and el.anchor.ref_element:
                    if el.anchor.ref_element not in elements_by_read[read.id]:
                        raise ValueError(
                            f"element {el.name!r}: anchor ref_element "
                            f"{el.anchor.ref_element!r} not in read {read.id!r}"
                        )

        # every signature test must reference a declared read (and element/onlist). A test's ``read``
        # is a ROLE id — the ``Read.id`` string — never the ``Read`` object the loop above walked,
        # which is why the two loops must not share a name.
        tests: list[Test] = [
            *self.signature.requires,
            *self.signature.excludes,
            *(s.when for s in self.signature.supports),
        ]
        for t in tests:
            role = getattr(t, "read", None)
            if role is not None and role not in read_ids:
                raise ValueError(f"signature test references unknown read {role!r}")
            element = getattr(t, "element", None)
            if element is not None and role is not None:
                if element not in elements_by_read.get(role, set()):
                    raise ValueError(
                        f"signature test references unknown element {element!r} in read {role!r}"
                    )
            onlist = getattr(t, "onlist", None)
            if onlist is not None and onlist not in aliases:
                raise ValueError(f"signature test references unknown onlist {onlist!r}")

        if self.backend is not None:
            self.backend.check_tokens(aliases)
        return self

    @model_validator(mode="after")
    def _read_through_is_reachable(self) -> Spec:
        """A declared read-through needs a read that could reach it, or it names nothing.

        The block asserts that a fragment ran PAST the genomic part of its read, so a layout with no
        genomic part cannot produce one — and the composer, which hands the sequence to an aligner
        on the strength of this declaration, would emit a clip no read could match and record the
        chemistry as handled when it is not. Refused at load, where every other mistake in this DSL
        dies, rather than surviving as a config key that quietly clips nothing.
        """
        if self.read_through is None:
            return self
        if not any(el.type in ("cdna", "gdna") for r in self.reads for el in r.elements):
            raise ValueError(
                f"{self.identity.id!r}: read_through is declared but no read carries cdna or gdna "
                f"for a fragment to run off the end of"
            )
        return self

    @model_validator(mode="after")
    def _clip_end_matches_the_trimmer(self) -> Spec:
        """A declared clip must sit at an END the declared trimmer will take an adapter for.

        ONE rule, and deliberately not a list of the pairs that are illegal: the trimmers are
        complementary, so a reader who learns "the end has to be an end that trimmer takes" has
        learned all of it, while a list of two cases is two facts to remember and a third to add
        later. A chemistry names its trimmer in ``backend.params`` and may declare a clip at either
        end — ``clip5pAdapterSeq`` beside it, ``read_through`` at the top level, where it is because
        two pipelines consume it. The rule spans both halves, so this model is the only thing that
        sees it whole.

        Refused at LOAD, where every other mistake in this DSL dies, because the alternative is not a
        wrong number: the aligner rejects the combination at parameter initialization, BEFORE the
        genome is loaded, so a deposit's every sample fails after its queue wait over a flag nobody
        typed and no output at all is produced.

        Dispatched on what the entry DECLARES, never on the module it names — the discipline
        :meth:`_cell_axis_matches_the_module` follows, reached here without needing the module at
        all. A pipeline that passes no trimmer has no such key in its parse namespace, so no spec on
        it can declare one and this is silent there: the aligner's own default governs, which is what
        lets the plate pipeline clip a read-through today. A trimmer this schema knows no end for is
        refused rather than skipped, because skipping would switch the rule off for precisely the
        entry that got its trimmer wrong — the "defined by silence" shape that made the key required.
        """
        if self.backend is None:
            return self
        trimmer = self.backend.params.get("clipAdapterType")
        if trimmer is None:
            return self
        takes = _CLIP_END_A_TRIMMER_TAKES.get(str(trimmer))
        if takes is None:
            raise ValueError(
                f"{self.identity.id!r}: clipAdapterType {trimmer!r} is a trimmer this schema knows "
                f"no end for, so no clip declared beside it could be checked against it — and an "
                f"unchecked pairing is how a chemistry acquires one STAR rejects before it loads a "
                f"genome. Known: {sorted(_CLIP_END_A_TRIMMER_TAKES)}"
            )
        override = self.backend.params.get("clip5pAdapterSeq")
        declared = (
            ("five-prime", "backend.params.clip5pAdapterSeq", override),
            ("three-prime", "read_through", self.read_through),
        )
        for end, field, sequence in declared:
            if sequence is None or end == takes:
                continue
            raise ValueError(
                f"{self.identity.id!r} declares a {end} clip in {field}, beside clipAdapterType "
                f"{trimmer!r}, which takes an adapter at the {takes} end and no other. A clip is "
                f"performed by whichever trimmer runs, so the end it sits at has to be an end that "
                f"trimmer takes — STAR rejects this pair at parameter initialization, before the "
                f"genome loads, so every sample of the deposit would die after its queue wait "
                f"instead of this entry failing here"
            )
        return self

    @model_validator(mode="after")
    def _read_sets_are_subsets(self) -> Spec:
        """A read set names ids ``reads`` already declares, and a ``requires`` gate reaches every set.

        Three refusals, all at LOAD — which is where every other DSL mistake in this file dies, and the
        reason the feature cannot be got wrong slowly:

        1. **an undeclared id.** A read set is a subset, so a name with no ``Read`` behind it would
           reach the scorer as a role it cannot look up: a ``KeyError`` mid-scoring rather than a
           refusal on the file that is wrong.
        2. **an empty set, or a repeated id.** A set with no roles can be assigned nothing and can
           never win; a repeated id would make one read two roles, which injectivity would then seat on
           two different files — the same FASTQ counted twice, at exit 0.
        3. **a ``requires`` gate a set cannot reach.** A test whose read is absent from the active set
           is *inapplicable*: it has no cell, so it enters neither the score numerator nor its
           normalizer, exactly as a nonexistent cell already behaves. That is right for evidence and
           wrong for a hard AND-gate — the gate would silently stop gating for the sets that lack the
           read, which is a claim the spec appears to make and does not. So a ``requires`` test may
           address only reads present in EVERY declared set, and a set-specific claim belongs in
           ``supports``, where losing the read loses evidence rather than a gate.

        ``excludes`` is deliberately NOT held to the same rule: an anti-gate that cannot fire admits
        more, and the smaller set is already penalized for what it leaves behind (``λ/|R|`` per orphaned
        file, which bites harder the fewer roles it has). Making excludes universal too would forbid
        the ordinary shape where the maximal set anti-gates a read only it declares.
        """
        if not self.read_sets:
            return self
        declared = {r.id for r in self.reads}
        for name, ids in self.read_sets.items():
            if not ids:
                raise ValueError(
                    f"read set {name!r} is empty: a set with no reads has no roles to assign"
                )
            if len(set(ids)) != len(ids):
                raise ValueError(f"read set {name!r} repeats a read id: {ids}")
            unknown = sorted(set(ids) - declared)
            if unknown:
                raise ValueError(
                    f"read set {name!r} names read(s) {unknown} that this spec does not declare — "
                    f"a read set is a SUBSET of {sorted(declared)}, never a second declaration"
                )
        universal = set.intersection(*(set(ids) for ids in self.read_sets.values()), declared)
        for t in self.signature.requires:
            role = getattr(t, "read", None)
            if role is None or role in universal:
                continue
            missing = sorted(n for n, ids in self.read_sets.items() if role not in ids)
            raise ValueError(
                f"the requires test {getattr(t, 'test', '?')!r} gates read {role!r}, which read "
                f"set(s) {missing} do not have. A requires test is a hard gate, and one addressed to "
                f"a read a set lacks is inapplicable there — it would silently stop gating. Move it "
                f"to `supports` (where a set-specific claim belongs), or address a read every set has."
            )
        return self

    @model_validator(mode="after")
    def _cell_axis_matches_the_module(self) -> Spec:
        """``identity.sample_is_cell`` is true **iff** this spec's module fans in to one deliverable.

        A biconditional, not an implication, and both halves are live failures rather than tidiness:

        - **A cell is a sample, beside a per-sample module.** Every cell compiles to its own object
          and the deposit's answer is a directory of 1440 matrices nobody asked for, at exit 0. The
          declaration would be true and the pipeline would quietly disagree with it.
        - **A fan-in module, beside a chemistry that says nothing.** The dataset reduction reads
          ``sample_is_cell`` and nothing else to tell "a plate whose cells must not be split apart"
          from "a project that genuinely mixes two assays". Undeclared, one cell scoring differently
          partitions the deposit into two manifests — one of them bulk — and again at exit 0.

        Same idiom as :meth:`Backend._only_parse_keys`: it fires in ``load_spec``, in ``kb lint``,
        and in every test that loads a spec, so the pairing is a property of the DSL rather than a
        rule somebody remembers. An abstract family node has no backend and therefore no fan-in, so
        it may not claim the cell axis either — a classifier decides nothing about how a deposit is
        shaped, and the leaf it descends to is where both halves are stated together.
        """
        from ..workflows import get_module

        declared = self.identity.sample_is_cell
        if self.backend is None:
            if declared:
                raise ValueError(
                    f"{self.identity.id!r} declares identity.sample_is_cell on an ABSTRACT node "
                    f"with no backend. One sample being one cell is a claim about the pipeline that "
                    f"runs it, and a family node runs nothing — declare it on the leaves it "
                    f"descends to, beside the module that aggregates them"
                )
            return self
        try:
            module = get_module(self.backend.module)
        except KeyError:
            return self  # `Backend._only_parse_keys` already refuses an unregistered module
        aggregates = module.fan_in_artifact is not None
        if declared and not aggregates:
            raise ValueError(
                f"{self.identity.id!r} declares identity.sample_is_cell, but its module "
                f"{module.name!r} is per-sample end to end and declares no dataset-scoped "
                f"deliverable. One Sample is one cell only if something counts them together; "
                f"otherwise a plate compiles to one matrix per cell at exit 0"
            )
        if aggregates and not declared:
            raise ValueError(
                f"{self.identity.id!r} names module {module.name!r}, which fans in to "
                f"{module.fan_in_artifact!r} over the whole deposit, but does not declare "
                f"identity.sample_is_cell. The dataset reduction reads that flag and nothing else "
                f"to tell a plate from a project that mixes two assays, so without it one dissenting "
                f"cell silently splits this chemistry's deposit into two manifests"
            )
        return self
