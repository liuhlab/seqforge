"""What a **Workflow module** is, and the scanners that read its config contract off its source.

The registry itself lives in the package's entry file, ``seqforge.workflows``, which imports every
name here and re-exports the public ones, so ``from seqforge.workflows import X`` is the one import
path. This file holds the machinery the registry needs, so a reader of the entry file reaches the
registry without scrolling past it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from ..models.processing import RuntimeEnv

_MODULE_DIR = Path(__file__).parent

#: liulab-runtime's published image. **A reference to their artifact, never a definition of one**:
#: we name a tag they build and push, and this repo contains no conda YAML and no Dockerfile.
#: `align-rna` is where the STAR that RUNS A DATASET comes from, and no dependency table here can
#: reach a rule. A test-only pixi environment does carry a STAR the `external` tests exec; it ships
#: in no wheel, joins no solve group, and is invisible from here.
RUNTIME_IMAGE = "ghcr.io/liuhlab/liulab-runtime"

#: How liulab-runtime names a prebuilt Singularity image. Read off their own `build-sifs.sh` on
#: 2026-07-15 (`$LIU_LAB_PACKAGES/liulab-runtime_<env>.sif`), not remembered — the four files there
#: are exactly the four `RuntimeEnv` names, which is an independent confirmation of that literal.
_SIF_NAME = "liulab-runtime_{env}.sif"


def container_uri(env: RuntimeEnv, sif_dir: str | Path | None = None) -> str:
    """The container image for ``env``: a ghcr tag, or a prebuilt ``.sif`` if one is on this machine.

    ``docker://`` by default, which is portable and needs no setup — Snakemake pulls it. But a
    compute node that cannot reach ghcr.io cannot pull anything, and the lab already builds these
    images ahead of time, so ``sif_dir`` names where. Missing dir or missing file falls back to the
    ghcr tag rather than emitting a path to nothing: a config naming an absent SIF fails at run time
    on a node, while the tag at least tries.

    This is a **machine fact**, so it belongs in the config and never in the manifest — same
    boundary as ``--fastq-dir`` and ``--onlist-dir``, and the same escape hatch for the same reason.
    """
    if sif_dir is not None:
        sif = Path(sif_dir) / _SIF_NAME.format(env=env)
        if sif.is_file():
            return str(sif.resolve())
    return f"docker://{RUNTIME_IMAGE}:{env}"


@cache
def keys_read_by(snakefile: Path) -> frozenset[str]:
    """The dotted config keys a module actually reads, **derived from its source**.

    Two forms, because the modules use both: `config["a"]["b"]` directly, and the indirection
    `params: solo=config["solo"]` followed by `{params.solo[soloCBlen]}` in the shell block.

    Comments are stripped first, and that is not fussiness — starsolo.smk's own header prose says
    "every chemistry-defining knob arrives via `config["solo"]`", which a naive scan reads as a bare
    read of the whole block. A check that cries wolf gets deleted.
    """
    code = "\n".join(line.split("#")[0] for line in snakefile.read_text().splitlines())
    keys: set[str] = set()

    # A bare `<name> = config["<section>"]` binds the whole block to a name. Track those, including
    # one rebinding hop (`SOLO = config["solo"]` at module level, then `solo=SOLO` in a params
    # block), because that chain is exactly how the shell reaches `{params.solo[soloType]}`.
    # The lookahead matters: `ASSEMBLY = config["genome"]["assembly"]` is a nested read, not a
    # binding, and must fall through to the direct scan below.
    bound: dict[str, str] = dict(re.findall(r'(\w+)\s*=\s*config\["(\w+)"\](?!\[)', code))
    for name, src in re.findall(r"^\s*(\w+)\s*=\s*(\w+)\s*,?\s*$", code, re.M):
        if src in bound:
            bound.setdefault(name, bound[src])

    for name, section in bound.items():
        # `{params.<name>[<key>]}` in a shell block, or `<NAME>["<key>"]` in Python.
        subscripts = set(re.findall(rf"\{{params\.{name}\[(\w+)\]\}}", code)) | set(
            re.findall(rf"""\b{name}\[["'](\w+)["']\]""", code)
        )
        # Subscripted -> it is a block alias and each subscript is the real read. Never subscripted
        # -> it was a scalar read all along (`OUTDIR = config["outdir"]`), so the section IS the key.
        keys |= {f"{section}.{k}" for k in subscripts} or {section}

    # Direct reads: config["a"]["b"] -> a.b | config["a"] -> a. Binding sites are already accounted
    # for above, so drop them here rather than double-count the block as a bare key.
    direct = re.sub(r'\w+\s*=\s*config\["\w+"\](?!\[)', "", code)
    for section, sub in re.findall(r'config\["(\w+)"\](?:\["(\w+)"\])?', direct):
        keys.add(f"{section}.{sub}" if sub else section)

    return frozenset(keys)


#: The config blocks that carry an aligner's params. One per module, and :meth:`param_block` refuses a
#: module that reads none or several. Also the parameter names an argv renderer binds its block to,
#: which is what lets :func:`argv_keys_read_by` find the reads without being told where to look.
_PARAM_BLOCKS: frozenset[str] = frozenset({"solo", "bulk", "chromap", "umi"})


def argv_keys_read_by(source: Path) -> frozenset[str]:
    """The dotted config keys an argv renderer reads, **walked out of its AST**.

    The counterpart to :func:`keys_read_by` for the half of a module's config reads that no longer
    live in its Snakefile. `starsolo.smk` used to render STAR's whole command line itself, so
    scanning that one file for ``SOLO["..."]`` recovered every key the composer owes; the geometry
    and clip closures now live in `workflows.starsolo_args`, where a Snakefile scanner cannot see
    them (#348).

    **An AST rather than a second regex, and it is strictly more precise than the one it joins.** A
    renderer is importable Python, so every branch is visible at once — the scan does not have to
    take one. That matters here specifically: :func:`~seqforge.workflows.starsolo_args.cb_umi_geometry`
    reads four keys on its simple arm and two on its Complex arm, and a reader that followed
    execution would see whichever the sample happened to be.

    A **subscript** is a key the composer owes; a ``.get`` is one only the chemistry that has it
    emits, and is deliberately not returned — the same line `keys_read_by` draws, drawn exactly here
    rather than by pattern. The block is found by parameter name (:data:`_PARAM_BLOCKS`), so a
    renderer states which block it renders by taking it as an argument, not by declaring it twice.
    """
    keys: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if not isinstance(node, ast.Subscript):
            continue
        block, key = node.value, node.slice
        if (
            isinstance(block, ast.Name)
            and block.id in _PARAM_BLOCKS
            and isinstance(key, ast.Constant)
            and isinstance(key.value, str)
        ):
            keys.add(f"{block.id}.{key.value}")
    return frozenset(keys)


#: The parse-param namespace a ``map/starsolo`` backend may declare — every key says how to **parse**
#: reads, and each is decided by bytes. The line is parse vs. count: what to COUNT (``soloFeatures``,
#: ``quantMode``) is *intent* and belongs to the processing manifest, where a user may instruct it and a
#: gate may check it. ``soloFeatures`` once sat here and cost a measured **40.7 % of a nuclear library**
#: — 10x 3' v3.1 chemistry is byte-identical for cells and nuclei, so counting was never a chemistry
#: property. Keeping this namespace **per pipeline** (a ``Pipeline.parse_keys`` field, not one global
#: set) is what lets a second aligner declare its own parse knobs without widening STARsolo's, so
#: "a user instruction contradicts the observed bytes" stays structurally inexpressible per namespace.
_STARSOLO_PARSE_KEYS: frozenset[str] = frozenset(
    {
        "soloType",
        "soloCBstart",
        "soloCBlen",
        "soloUMIstart",
        "soloUMIlen",
        "soloCBwhitelist",
        "soloCBposition",
        "soloUMIposition",
        "soloStrand",
        "soloAdapterSequence",
        "soloBarcodeReadLength",
        # How a read barcode is matched back to the whitelist — a parse knob by the same test as the
        # rest: it is decided by the chemistry's own vendor pipeline, never by what a user wants
        # counted. It was a `soloType` branch in starsolo.smk (`1MM` for Complex, STAR's default for
        # Simple) until #198, and a two-way branch cannot express what the KB already needs: Parse
        # Evercode and BD Rhapsody are BOTH `CB_UMI_Complex` and take different values (`EditDist_2`
        # vs `1MM`). Three answers, so it moves to the artifact that has one row per chemistry. Which
        # values are legal depends on the soloType (compose's params gate enforces the pairing).
        "soloCBmatchWLtype",
        # Which read-preprocessing STAR runs before alignment, by the SAME test as the key above and
        # with the same history: a module literal (`CellRanger4`, always) until #355, because "chosen
        # for CellRanger parity" was read as evidence about ownership. It is not — it is a reason to
        # pick a value, and the correct value moves between chemistries — the argument is stated on
        # `Backend` in `kb/schema.py` and the per-vendor evidence in
        # `docs/research/starsolo-read-preprocessing-per-family.md`. REQUIRED of all eleven rather
        # than optional-with-a-default, deliberately: whichever group stayed silent would be defined
        # by silence, and a new spec would join it by accident.
        "clipAdapterType",
        # The five-prime override, and only `CellRanger4` takes one — it REPLACES that mode's
        # hardcoded 10x TSO rather than adding to it, which is what lets a chemistry with its own TSO
        # keep the clip and fix the sequence. Declared by SPLiT-seq alone today. A parse key beside
        # its trimmer rather than a top-level term, because a top-level term is what a fact TWO
        # pipelines consume earns (`read_through`); a five-prime override is starsolo-only until a
        # second module can honour one, and promoting it early would be a term with one reader.
        "clip5pAdapterSeq",
    }
)

#: chromap's parse namespace — the byte-decided knobs a ``map/chromap`` backend may declare. Just the
#: barcode whitelist: chromap corrects the cell barcode against it (like STARsolo's ``soloCBwhitelist``),
#: and it resolves through the same ``{onlist:<alias>}`` mechanism to a registry name. Everything
#: else chromap needs is either a fixed module detail (the ``--preset atac`` mode, hardcoded in
#: chromap.smk the way star.smk hardcodes ``--outSAMtype``) or read geometry the manifest already states
#: (which file is the barcode read arrives via ``read_files_in``, not a parse param). The namespace is
#: DISJOINT from STARsolo's, which is what keeps "a user instruction contradicts the bytes" inexpressible
#: per pipeline: a chromap backend is policed against exactly this set, a starsolo backend against ``solo*``.
_CHROMAP_PARSE_KEYS: frozenset[str] = frozenset({"barcode_whitelist"})

#: How a module wants its reads handed to the aligner — a CLOSED vocabulary, extended deliberately:
#:
#: - ``barcoded``      — ``{cdna, barcode}``, chosen by ROLE (a barcoded single-cell RNA chemistry).
#: - ``mates``         — ``{mate1}`` or ``{mate1, mate2}``, chosen by ORDER (a bulk library, single-
#:   or paired-end).
#: - ``atac_barcoded`` — ``{gdna1, gdna2, barcode}``, chosen by ROLE (scATAC: two genomic mates and a
#:   separate barcode read — chromap's ``-1``/``-2``/``-b`` shape).
#: - ``umi_tagged``    — ``{umi_cdna, cdna}``, chosen by ROLE (a pre-demultiplexed plate assay: one
#:   mate opens with a tag + UMI + motif and the other is plain cDNA, with NO barcode read at all).
#:
#: A typed, visible choice rather than the old ``spec.backend.module == "map/starsolo"`` string
#: compare, in which every module that was not starsolo silently fell into the bulk mate1/mate2
#: branch and emitted a wrong command line. A third module must pick a kind, or add one.
#:
#: **``umi_tagged`` is not cosmetic, and ``mates`` is the reason.** ``barcoded`` refuses a plate
#: correctly — no ``CB`` element exists anywhere in the layout, so the lookup returns nothing — but
#: ``mates`` accepts it and picks by ORDER, and a plate's two mates are *not* symmetric: one carries
#: the tag. A layout listing the plain mate first hands the untagged read to the extractor, nothing
#: is tagged, and the run ends in an empty matrix at **exit 0**. That is exactly the silent
#: fall-through this field exists to kill, reappearing in the kind that looks like the safe default.
#: The kind also **tolerates one read** structurally — no minimum-of-two assert to undo later — so a
#: single-end plate needs a wider extractor rather than a fifth kind. Named for the READ property
#: rather than for the sample axis: ``plate``/``cell_per_file`` would be a second place to say what
#: the KB's ``identity.sample_is_cell`` already says, and ``tagged``/``untagged`` is the per-read
#: vocabulary the counting design uses *inside* one read.
#:
#: **``mates`` is 1..2 and not exactly 2, and the name moved for that reason** (ADR-0029). A kind is a
#: property of the MODULE, so ``map/star`` cannot be ``paired`` for one dataset and ``single`` for the
#: next; a ``single`` kind would buy a second hand-written module for one aligner. Widening the one
#: kind is what avoids that, and then the old name would have been a dispatch key that lies about what
#: it selects — the exact defect this field exists to remove.
#:
#: Named here rather than written inline on the field, because the composer's dispatch and the params
#: gate's cross-check are two independent re-derivations of one mapping and must be annotated against
#: the SAME set. Two separately spelled ``Literal``s agree right up until one of them gains a kind.
ReadLayoutKind = Literal["barcoded", "mates", "atac_barcoded", "umi_tagged"]


@dataclass(frozen=True)
class WorkflowModule:
    """One selectable pipeline: its id, version, runtime env, Snakefile, and per-pipeline contract.

    The pipeline registry's citizen. Beyond identity (``name``/``version``/``snakefile``) it declares
    the properties that used to be STARsolo-hardwired globals scattered across ``compose``/``policy``:
    the runtime ``env``, the ``read_layout_kind``, the ``parse_keys`` namespace a KB backend may declare,
    and the ``serves_modalities`` the assay↔pipeline adapter (:func:`~seqforge.workflows.resolve_pipeline`) checks. The
    aligner name and the config block are *derived*, never declared twice.
    """

    name: str
    version: str
    env: RuntimeEnv
    snakefile: Path
    #: How this module wants its reads handed to the aligner — see :data:`ReadLayoutKind`.
    read_layout_kind: ReadLayoutKind
    #: The Python module that renders this pipeline's aligner argv, where it has one. Scanned for
    #: config reads exactly as the Snakefile is (:func:`argv_keys_read_by`), because a module whose
    #: command line moved out of its ``shell:`` block still owes the composer the same keys — the
    #: reads moved, the contract did not. ``None`` for a module that renders its own argv inline.
    argv_source: Path | None = None
    #: The parse-param namespace this pipeline's KB backends may declare (byte-decided knobs). Empty for
    #: a bulk pipeline that takes no parse params. Per pipeline, so a chromap backend declares chromap's
    #: parse knobs and a starsolo backend declares ``solo*`` — each gated against its own namespace.
    parse_keys: frozenset[str] = frozenset()
    #: Which assay modalities this pipeline serves. The adapter refuses to bind a spec whose modality is
    #: not here, so an RNA chemistry can never be composed against an ATAC-only pipeline (or vice versa).
    serves_modalities: frozenset[str] = frozenset({"rna"})
    #: The DATASET-scoped deliverable this pipeline fans in to, if it has one — one filename under the
    #: results directory, produced once for the whole deposit rather than once per sample. ``None``
    #: for a pipeline that is per-sample end to end, which is the three shipped ones and the default.
    #:
    #: **Declared rather than inferred, and minimal on purpose.** A plate assay counts every cell in
    #: one job and writes one combined object, so "this module produces something the sample axis
    #: does not reach" is a fact about the module — and this repo has already paid three times for
    #: the implicit version of exactly that shape: ``read_layout_kind`` exists because a module-name
    #: compare made every non-starsolo module silently mean bulk, ``param_block`` raises rather than
    #: guessing which config block a module reads, and the stats registry names the modules that
    #: report nothing so a fourth aligner cannot be silently absent from every page. Left implicit
    #: here, the per-sample assumption baked into a pipeline directory's layout and its report holds
    #: forever and the next cross-sample module discovers it the way those three were discovered.
    #:
    #: It NAMES the artifact and stops there — not a scope algebra, and not the rule's resource class
    #: (which the module's own arithmetic owns, because only the module knows its rule graph).
    fan_in_artifact: str | None = None
    #: This pipeline's chimera-aware twin: the module id to compose instead when the recipe names a
    #: **chimera** — one reference built from several component assemblies. ``None`` for a pipeline
    #: that has no such twin, which is every shipped one, and a recipe naming a chimera against one of
    #: those is a compose refusal rather than a run that merges two organisms into a single count
    #: table with nothing in it saying which reads were whose.
    #:
    #: **Declared rather than inferred, by the argument ``fan_in_artifact`` makes directly above and
    #: by one more that is specific to this fact.** Whether a pipeline can keep the components apart
    #: downstream of the aligner is a property of its rule graph, and every way of inferring it reads
    #: something that is not: a module-id compare is ``read_layout_kind``'s dead ancestor, in which
    #: everything that was not starsolo silently meant bulk; a lookup in the shipped assembly table is
    #: worse *here* than that pattern is anywhere else, because nothing folds the `liulab-genome` pin
    #: into ``run_id`` — a table-derived answer would let one recipe compile to a DIFFERENT module at
    #: the SAME ``run_id``, into the directory the first compile's alignments are already in.
    #:
    #: It NAMES the twin and stops there. Which components a chimera has is the recipe's statement
    #: (`compose.chimera`), and what the twin then does with them is the twin's own contract.
    chimeric_variant: str | None = None

    @property
    def aligner(self) -> str:
        """The aligner name recorded in ``processing.aligner`` — derived from the module id.

        ``map/starsolo`` → ``starsolo``, ``map/star`` → ``star``, ``map/chromap`` → ``chromap``. This
        was a ``_ALIGNER_FOR_MODULE`` dict whose every entry equalled this ``rsplit`` fallback — a
        mirror of the module ids that could only ever drift from them. One rule, read off the id.
        """
        return self.name.rsplit("/", 1)[-1]

    @property
    def required_config(self) -> tuple[str, ...]:
        """Dotted config keys the module reads — the composer must emit every one.

        **Computed from the module source, never declared.** This was a hand-written tuple, checked in
        one direction against a scanner that lived in the test suite. It under-declared the four
        soloCB/UMI keys `starsolo.smk` has always dereferenced (a `KeyError` on a compute node, long
        after compose exited 0), and it over-declared `primary_feature` and `env`, which no rule
        reads. Both directions now close by construction: there is one list, and the module source is
        it. A hand-maintained list of what the code does is a comment with a tuple's syntax.

        Deriving is only safe because the module now *executes*: `kb e2e` runs this Snakefile against
        real reads and a ground-truth matrix, so a key this scanner misses fails loudly there. The two
        are complementary — `kb e2e` exercises one chemistry's branch, this covers both statically.

        **Two sources, because the reads live in two files now.** A module whose argv renderer moved
        into Python (`argv_source`) is scanned there too, and by AST rather than by regex — see
        :func:`argv_keys_read_by`. Deriving from wherever the reads actually are is the same rule as
        before; what changed is that "wherever" stopped being one file.
        """
        keys = keys_read_by(self.snakefile)
        if self.argv_source is not None:
            keys |= argv_keys_read_by(self.argv_source)
        return tuple(sorted(keys))

    @property
    def param_block(self) -> str:
        """Which config block carries this module's aligner params. **Read off the module source**.

        `starsolo.smk` dereferences `config["solo"]`; `star.smk` dereferences `config["bulk"]`. That
        is not a preference anyone declares — it is what the file does — so it is derived from
        `required_config`, which is itself scanned out of the module.

        It was `"solo" if spec.backend.module == "map/starsolo" else "bulk"`, the last surviving
        string compare against a module name, and it is the same bug `read_layout_kind` was created
        to kill: every module that is not starsolo silently means bulk. A third module would have had
        its params written into a `bulk:` block it never reads, and the params gate — which uses this
        same function — would have agreed with the composer, because both were wrong in the same
        direction. Two things wrong identically is what a shared bug looks like from inside a test.

        A module that reads neither block, or both, raises. That is a module whose config contract we
        do not understand, and guessing would be how the wrong params reach an aligner.
        """
        blocks = sorted({k.split(".")[0] for k in self.required_config} & _PARAM_BLOCKS)
        if len(blocks) != 1:
            raise ValueError(
                f"{self.name} reads {blocks or 'no'} aligner-param block(s) in its config; expected "
                f"exactly one of solo/bulk/chromap/umi. A module whose contract is unreadable must "
                f"not be guessed at — add the block it reads, or teach `param_block` the new shape."
            )
        return blocks[0]


#: What ``map/star-umi`` fans in to: ONE ``.h5ad`` over every cell of the deposit, under the results
#: directory and named for nothing in particular — there is no sample to name it after, which is the
#: whole point of the artifact.
#:
#: Here rather than in the counter, because the counter does not choose it: ``write_umi_counts``
#: writes wherever it is pointed, so the two places that decide this name are the registry entry
#: in the entry file (which DECLARES the deliverable) and the rule that produces it. Both read this constant, so
#: the declaration and the rule cannot come apart — the same one-owner-two-readers discipline the
#: QC-suffix constants get, applied to the one artifact that has no ``{sample}`` in it.
PLATE_H5AD = "combined.h5ad"

#: What ``map/star-umi-chimera`` fans in to: one ``.h5ad`` **per Component** over every cell, the same
#: artifact one arity out. The ``{component}`` placeholder is what makes it that: inserted into the
#: rule's f-string it survives as a snakemake wildcard, so the counting rule fans in once per
#: Component and the registry's declaration and the rule that produces it still read one constant. The
#: precedent for a placeholder in a declared artifact name is the stats registry's ``{sample}``
#: templates; the reason it is here rather than a second field is that a fan-in artifact's NAME has
#: exactly one owner and an arity is part of a name.
#:
#: Two objects and not one hstacked one, deliberately: a merged matrix would make the two organisms
#: tellable apart only by a gene-name prefix, forever, downstream of everything.
PLATE_COMPONENT_H5AD = "combined.{component}.h5ad"
