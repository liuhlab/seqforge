"""The Flow tab's narrative: what actually happened to THIS dataset, as an ordered list of steps.

Not a diagram of pipeline verbs (``probe``/``resolve``/``harvest``/``compose`` mean nothing to a
biologist) — a plain-language chain with the real values: the guess we started from, what the files
turned out to contain, which kit made the reads and how sure we are, which files belong to which
sample, and how it will be processed. ``panels.py`` renders these as responsive HTML cards (readable
at any width, wrapping on a narrow screen) — no scaled SVG, so the text never shrinks to nothing.

Steps appear only for stages that are present, so an IR-ready dataset stops before "compose" and a
refusal ends at a red "needs a human" step.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from .model import AssayReport

#: Which visual family a step belongs to — drives its card colour, mirroring the docs palette.
StepKind = Literal["guess", "measured", "done", "blocked", "ask"]


class FlowStep(NamedTuple):
    """One step of the narrative: a bold title, plain description lines, an italic cost/meaning note."""

    title: str
    desc: list[str]
    note: str
    kind: StepKind


def _chem_name(assay: AssayReport) -> str:
    """The kit's human name (EFO words) when we have it, else its id — never a bare code alone."""
    labels = assay.chemistry.assay_labels
    if labels and labels[0].name:
        return labels[0].name
    return assay.chemistry.value[0] if assay.chemistry.value else "an unknown kit"


def _genome_short(assay: AssayReport) -> str:
    genome = _plan_value(assay, "genome")
    if not genome:
        return "the reference genome"
    return genome.split(" (")[0].split(" / ")[0]


def flow_steps(assay: AssayReport) -> list[FlowStep]:
    """The ordered narrative steps for one assay's compile."""
    steps: list[FlowStep] = []
    chem = assay.chemistry
    chem_name = _chem_name(assay)
    chem_id = chem.value[0] if chem.value else ""
    n_files = chem.n_files or assay.n_files
    n_samples = assay.n_samples
    conf = f"{chem.confidence:.2f}" if chem.confidence is not None else "no single number"

    # 1. The starting guess — only when there was prose or records to guess from.
    if assay.has_prose or assay.has_records:
        source = "the paper and database records" if assay.has_prose else "the database records"
        steps.append(
            FlowStep(
                "What the humans said",
                [f"{source} describe {n_samples} sample(s)"],
                "a starting guess, never the answer",
                "guess",
            )
        )

    # 2. What the bytes actually contain.
    steps.append(
        FlowStep(
            "What the files actually contain",
            [
                f"we open {n_files} FASTQ file(s) and measure read"
                if n_files
                else "we read the raw sequence and measure",
                "lengths and which short barcodes repeat",
            ],
            "free — we are reading the bytes anyway",
            "measured",
        )
    )

    # 3. Which kit made the reads (the one evidenced decision). chem_id is kept verbatim so the exact
    # KB id stays greppable in the rendered page (a contract the flow test checks).
    if chem.value:
        match_desc = [
            "the read layout is matched to a known kit, then",
            f"confirmed against its barcode list → {chem_id}",
        ]
        match_note = f"{chem_name} — decided by the bytes, {conf} confidence"
    else:
        match_desc = ["no kit fit the read layout confidently"]
        match_note = "the bytes were ambiguous"
    steps.append(FlowStep("Which kit made these reads", match_desc, match_note, "measured"))

    kind = assay.conclusion.kind
    if kind in ("compiled", "ir_ready"):
        steps.append(
            FlowStep(
                "Which files belong to which sample",
                [
                    f"the files are grouped into {n_samples} sample(s)",
                    "and labelled with strain, tissue, stage, sex…",
                ],
                "from records and prose — disagreements left blank, never guessed",
                "measured",
            )
        )
        if assay.plan is not None:
            # scATAC counts open-chromatin fragments, not genes — keep the flow node truthful per modality.
            count_line = (
                "call chromatin fragments per cell"
                if assay.chemistry.modality.lower() == "atac"
                else "count genes per cell"
            )
            steps.append(
                FlowStep(
                    "How to process it",
                    [f"align to {_genome_short(assay)} and", count_line],
                    "our processing choices — change these freely",
                    "measured",
                )
            )
        if kind == "compiled":
            steps.append(
                FlowStep(
                    "✓ " + assay.conclusion.headline,
                    ["a runnable Snakefile is ready"],
                    "",
                    "done",
                )
            )
        else:
            steps.append(FlowStep(assay.conclusion.headline, ["not composed yet"], "", "done"))
    elif kind == "blocker":
        steps.append(
            FlowStep("✗ " + assay.conclusion.headline, ["the compiler refused"], "", "blocked")
        )
    else:  # question
        steps.append(
            FlowStep(
                "? " + assay.conclusion.headline,
                ["a human decision is needed"],
                "the bytes could not settle it",
                "ask",
            )
        )
    return steps


def _plan_value(assay: AssayReport, label: str) -> str | None:
    if assay.plan is None:
        return None
    for field in assay.plan.fields:
        if field.label == label:
            return field.value
    return None


__all__ = ["FlowStep", "StepKind", "flow_steps"]
