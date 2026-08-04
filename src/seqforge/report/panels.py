"""HTML fragments — small typed helpers plus one function per tab.

Hand-rolled rather than templated: one self-contained page does not earn a templating dependency, and
keeping the fragments as functions means the types flow (an :class:`AssayReport` in, a string out) and
mypy checks the projection is used the way it was built. Every dynamic value goes through :func:`esc`;
the only structured untrusted inputs (a study title, a forbidden reason, a metadata value) are escaped
at the point they enter the string.

The audience is a wet-lab reader, so the surface is de-jargoned: the Overview leads with the study's
own words, the Flow narrates in plain language, and Samples is a metadata table with provenance on
hover. The technical artifacts (the recipe, the Snakefile, the config) live on the Pipeline tab,
embedded so the page stays self-contained. A later LLM-written summary would be one more function here.
"""

from __future__ import annotations

import base64
import re
from html import escape
from math import ceil, log10

from ..workflows.metrics import (
    SEVERITY_PHRASE,
    Alert,
    Level,
    Metric,
    MetricGroup,
    PipelineStats,
    SampleStats,
    Severity,
)
from .flow import FlowStep, flow_steps
from .model import (
    ArtifactEmbed,
    AssayReport,
    AttributeView,
    DecisionField,
    EvidenceRef,
    MatrixCellView,
    MatrixView,
    PipelineStage,
    PlanView,
    ProjectReport,
    ReadView,
    SampleView,
)

#: Role/region ids the read layout uses → plain words a wet-lab reader recognises. A read is described
#: by what it *contains* (a cell barcode, a molecule tag, the cDNA), which is always known from the
#: manifest — unlike a FASTQ's byte size, which is meaningless when the manifest came from head-slices.
_ROLE_NAME: dict[str, str] = {
    "cb": "cell barcode",
    "cell_barcode": "cell barcode",
    "barcode": "cell barcode",
    "umi": "UMI (molecule tag)",
    "cdna": "cDNA (gene reads)",
    "cdna_read": "cDNA (gene reads)",
    "gdna": "genomic DNA (open-chromatin reads)",
    "index": "sample index",
}

#: Results goes LAST, after Pipeline. Two reasons, both about not moving what a reader already knows:
#: the tabs read as the compiler's own order in time (what the data is → how we read it → what we will
#: run → what came out), and appending leaves every existing tab at the index a returning reader's
#: cursor already learned. Inserting Results earlier — where its importance might argue it belongs —
#: would shift Evidence and Pipeline sideways on every page, including the pages that have no results.
_TABS: list[tuple[str, str]] = [
    ("overview", "Overview"),
    ("flow", "Flow"),
    ("samples", "Samples"),
    ("evidence", "Evidence"),
    ("pipeline", "Pipeline"),
    ("results", "Results"),
]

#: How each provenance basis reads to someone who has never seen the manifest vocabulary — the
#: **how we know** voice, for a *dataset* field, where that is the question ``basis`` answers.
_BASIS_PHRASE: dict[str, str] = {
    "observed": "measured directly from your files",
    "asserted": "stated in the records or paper",
    "inferred": "inferred from the surrounding context",
    "user_confirmed": "confirmed by you",
}

#: The same four bases as the Samples legend says them: three or four words under a mark, not a
#: sentence. A third map rather than a shortening of :data:`_BASIS_PHRASE`, because a legend entry and
#: a popover line are read in different places and only one of them has room for a clause — and,
#: like the other two, it is asserted total over the ``Basis`` literal so a fifth basis cannot ship a
#: mark whose key says nothing.
_BASIS_LEGEND: dict[str, str] = {
    "observed": "your files",
    "asserted": "records / paper",
    "inferred": "we inferred it",
    "user_confirmed": "you",
}

#: The same four bases in the **who decided** voice, for a *recipe* field. Two maps, and they stay
#: two: on a dataset field ``basis`` answers how we know, on a recipe it answers who decided, and the
#: "Processing choices" table's third column is headed *who decided*. Merged, that column would read
#: "inferred from the surrounding context" as an answer to "who", which is not an answer at all.
#:
#: Each entry is the recipe ladder's own actor: a policy default is us, a flag or an instruction
#: document is you, reference prose is the paper. ``observed`` has no writer on a recipe today and is
#: still here, because a map that is total over the ``Basis`` literal cannot render a raw token — and
#: an exhaustiveness test over both maps derives the members from that literal, so a fifth basis goes
#: red here rather than shipping ``user confirmed`` onto a page.
_WHO_PHRASE: dict[str, str] = {
    "observed": "the files themselves",
    "asserted": "the records / paper",
    "inferred": "our default",
    "user_confirmed": "you specified",
}

# ---- helpers ------------------------------------------------------------------------------------


def esc(value: object) -> str:
    """Escape one value for HTML text or an attribute — and render a missing one as **nothing**.

    The display model is optional-heavy (a study's centre, an element's ``onlist_ref``, a sample's
    note), so ``None`` reaches here routinely. ``str(None)`` puts the five-letter English word
    ``None`` on the page, which a reader cannot tell apart from a value that was genuinely recorded —
    the one failure this page must never have. Guarding at every call site was the alternative and is
    the same fix written thirty times, each of which can be forgotten once. The eval report's sibling
    escaper (``evals/report.py``) has always mapped ``None`` to empty; this is that, here.

    Only ``None`` is blanked. ``0`` and ``False`` are answers, and a falsey test would erase them.
    """
    return escape("" if value is None else str(value), quote=True)


def _basis_phrase(basis: str) -> str:
    return _BASIS_PHRASE.get(basis, basis.replace("_", " "))


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _panel(title: str, body: str, *, sub: str = "") -> str:
    """The one box every tab composes into: a title, an optional lead sentence, and a body.

    A ``<section>`` and not a ``<div>``, because six panes of unlabelled ``div``s is what a screen
    reader currently gets and the heading is right there to name each one. The panel carries the
    page's only shadow (``.sf-panel``); everything nested inside it is flat and separated by a
    hairline, since a second shadow inside a box that already has one reads as a bug.

    Every panel is the same box. There used to be a ``cls`` hook for a caller that wanted a narrower
    one, and its last user went with the Evidence tab's redesign: a panel narrower than the panel
    above it is the loudest way a page reads as two products, and the reading cap belongs on the
    column *inside* the box.
    """
    sub_html = f'<p class="sf-panel-sub">{esc(sub)}</p>' if sub else ""
    return f'<section class="sf-panel"><h2 class="sf-panel-h">{esc(title)}</h2>{sub_html}{body}</section>'


# ---- overview -----------------------------------------------------------------------------------


def _assay_kind(assay: AssayReport) -> str:
    """A plain phrase for what kind of experiment this is, without exposing 'modality'."""
    modality = assay.chemistry.modality.lower()
    if assay.onlists:
        return "single-cell RNA-seq" if modality == "rna" else f"single-cell {modality.upper()}"
    if modality == "rna":
        return "bulk RNA-seq"
    return f"{modality.upper()}-seq"


def overview_pane(assay: AssayReport) -> str:
    chem = assay.chemistry
    study = assay.study
    title = (study.title if study and study.title else None) or assay.label
    study_acc = study.accession if study and study.accession else ""
    center = study.center if study and study.center else ""
    organism_name = assay.organism_name
    organism = organism_name or (
        f"taxid {assay.organism_taxid}" if assay.organism_taxid else "organism not declared"
    )
    org_html = f"<em>{esc(organism)}</em>" if organism_name else esc(organism)

    # chemistry, spelled human-first with the code as a quiet chip. The kit's name is the page's
    # second-biggest fact, so it is given weight and a hairline — never a hue: the colour law reserves
    # colour for exceptions, and the winning kit is the norm.
    chem_name = chem.assay_labels[0].name if chem.assay_labels and chem.assay_labels[0].name else ""
    chem_id = chem.value[0] if chem.value else ""
    chem_line = ""
    if chem_id:
        name_html = (
            f'<span class="rounded border border-line px-2 py-0.5 text-sm font-bold">'
            f"{esc(chem_name)}</span>"
            if chem_name
            else ""
        )
        more = (
            f'<span class="text-xs text-faint">+{len(chem.value) - 1} equivalent</span>'
            if len(chem.value) > 1
            else ""
        )
        chem_line = (
            '<div class="mt-3 flex flex-wrap items-center gap-2">'
            f'{name_html}<code class="font-mono text-xs text-dim">{esc(chem_id)}</code>{more}</div>'
        )

    eyebrow = " · ".join(x for x in (study_acc, center) if x)
    hero = (
        '<div class="flex flex-wrap items-start gap-6">'
        '<div class="min-w-0 flex-1 basis-80">'
        + (f'<div class="sf-sub-h mb-2">{esc(eyebrow)}</div>' if eyebrow else "")
        + f'<h1 class="mt-0 mb-2 text-2xl font-bold tracking-tight">{esc(title)}</h1>'
        + f'<p class="m-0 text-dim">{org_html} · {esc(_assay_kind(assay))}</p>'
        + chem_line
        + "</div>"
        + _verdict_card(assay)
        + "</div>"
    )

    # abstract — first-class, shown by default (only when a record actually carried one). Capped at
    # 70ch because a 1080px-wide paragraph is not a paragraph anyone finishes.
    if study and study.abstract:
        abstract = (
            '<div class="mt-6 border-t border-line pt-6"><div class="sf-sub-h mb-2">'
            "About this study</div>"
            '<p class="m-0 max-w-[70ch] border-l-2 border-line pl-4">'
            f"{esc(study.abstract)}</p></div>"
        )
    else:
        abstract = ""

    tiles = (
        '<dl class="mt-6 mb-0 grid grid-cols-2 gap-3 border-t border-line pt-6 '
        'sm:grid-cols-3 lg:grid-cols-5">'
        + _tile("Samples", str(assay.n_samples))
        + _tile("FASTQ files", str(assay.n_files))
        + _tile("Kit", esc(chem_name or chem_id or "—"), numeric=False)
        + _tile("Organism", org_html, numeric=False)
        + _confidence_tile(chem.confidence)
        + "</dl>"
    )

    return _panel("Overview", hero + abstract + tiles)


def _verdict_card(assay: AssayReport) -> str:
    """The compile verdict, restated in the header pill's own badge — and never in the run state's.

    Deliberately the *same* component as the header's (``sf-verdict``/``sf-v-{kind}``): this and the
    pill answer one question — did the compiler produce a Snakefile — so they are one badge shown
    twice, and a reader who learns the shape up top reads it again here for free. What must never
    look like either is the Results tab's pipeline-run state (``.lvl-state``): a left-ruled,
    tinted banner answering "did that Snakefile *finish*", which disagrees with this exactly when it
    matters. Two facts, two forms — the word in the badge, not a glyph, is the non-colour channel.
    """
    c = assay.conclusion
    return (
        '<div class="w-full shrink-0 rounded-lg border border-line p-4 sm:w-72">'
        f'<span class="sf-verdict sf-v-{esc(c.kind)}">'
        '<span class="size-2 rounded-full bg-current"></span>'
        f"{esc(c.headline)}</span>"
        f'<p class="mt-3 mb-0 text-sm text-dim">{esc(c.detail)}</p></div>'
    )


def _tile(key: str, value_html: str, *, numeric: bool = True) -> str:
    """One headline figure. ``value_html`` is pre-escaped — ``Organism`` carries an ``<em>``."""
    size = "text-2xl tabular-nums" if numeric else "text-lg"
    return (
        f'<div class="rounded-lg border border-line p-3"><dt class="sf-sub-h">{esc(key)}</dt>'
        f'<dd class="mt-2 mb-0 ms-0 {size} font-bold tracking-tight">{value_html}</dd></div>'
    )


def _confidence_tile(conf: float | None) -> str:
    """How strongly the bytes point at the winning kit: a single-ink bar and the number beside it.

    The bar is drawn in the text ink, not in the accent and not in a verdict hue. 0.94 is not a pass
    and 0.61 is not a failure — the Evidence tab is where that judgement lives — so a green bar here
    would be the page asserting a verdict it has not made.
    """
    if conf is None:
        value = '<dd class="mt-2 mb-0 ms-0 text-lg font-bold text-faint">n/a</dd>'
    else:
        pct = round(max(0.0, min(1.0, conf)) * 100)
        value = (
            '<dd class="mt-2 mb-0 ms-0 flex items-center gap-2 text-2xl font-bold '
            'tracking-tight tabular-nums">'
            '<span class="inline-block h-1.5 w-12 shrink-0 overflow-hidden rounded-full bg-line">'
            f'<span class="block h-full rounded-full bg-dim" style="width:{pct}%"></span></span>'
            f"{conf:.2f}</dd>"
        )
    return (
        '<div class="rounded-lg border border-line p-3"><dt class="sf-sub-h">Confidence '
        '<span class="cursor-help text-faint" title="How strongly the files’ own bytes point to '
        'this kit — 1.00 means certain.">ⓘ</span></dt>'
        f"{value}</div>"
    )


# ---- flow ---------------------------------------------------------------------------------------


def flow_pane(assay: AssayReport) -> str:
    """The narrative, as cards that reflow — a grid whose column count follows the viewport.

    Reflowing is the whole reason this tab is HTML and not a diagram: a scaled SVG cannot, and its
    text shrank to nothing on a wide dataset (``render.py``, ``VENDOR.md``). So the layout is a plain
    CSS grid and the ordinal badge carries the sequence. The absolutely-positioned arrow that used to
    sit in the gutter is gone: it was already wrong on every wrapped row, and pointing at a card that
    is somewhere else is worse than not pointing.

    There is no legend any more. It named four saturated fills that no longer exist, and what is left
    needs none — a card is tinted only when it is asking for a human, and it says so in words.
    """
    cards = "".join(_flow_card(s, i) for i, s in enumerate(flow_steps(assay)))
    return _panel(
        "How seqforge read this dataset",
        f'<ol class="m-0 grid list-none gap-4 p-0 sm:grid-cols-2 lg:grid-cols-3">{cards}</ol>',
        sub="Read the steps in order: the guess we started from, what your files actually contain, and "
        "how it ends. Every step is decided from the sequence itself, not from what the paper claimed.",
    )


def _flow_card(step: FlowStep, index: int) -> str:
    """One step. ``flow-{kind}`` is computed from :data:`~seqforge.report.flow.StepKind`, so all five
    members are declared components — and only two of them paint anything.

    ``desc`` joins on a **space**, not on ``<br>``. Its lines are sentence fragments written to read
    as one sentence ("…and measure read" + "lengths and which short barcodes repeat"); the break was
    there to steer a fixed-width strip's wrapping, and inside a card that reflows it is a hard break
    landing wherever the author happened to split the string.
    """
    desc = " ".join(esc(d) for d in step.desc if d)
    note = f'<p class="mt-2 mb-0 text-xs text-dim italic">{esc(step.note)}</p>' if step.note else ""
    return (
        f'<li class="flow-{esc(step.kind)}">'
        '<div class="mb-2 flex items-start gap-2">'
        '<span class="inline-grid size-5 shrink-0 place-items-center rounded-full border '
        f'border-line text-xs font-bold text-dim" aria-hidden="true">{index + 1}</span>'
        f'<span class="text-sm leading-5 font-bold">{esc(step.title)}</span></div>'
        f'<p class="m-0 text-sm">{desc}</p>{note}</li>'
    )


# ---- samples ------------------------------------------------------------------------------------


def _basis_legend() -> str:
    """The key for the provenance marks, iterated over :data:`_BASIS_LEGEND`.

    Over the map and never over a hand-listed tuple, for the reason the level legend gives: a fifth
    basis would otherwise be carried by the manifest, drawn as a mark by the stylesheet and silently
    absent from the key that says what the mark means. Insertion order is display order.
    """
    marks = "".join(
        f'<span class="inline-flex items-center gap-2">'
        f'<span class="basis-mark basis-{esc(key)}" aria-hidden="true"></span>{esc(label)}</span>'
        for key, label in _BASIS_LEGEND.items()
    )
    return (
        '<p class="mt-0 mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-dim">'
        f"<span>Where each value came from:</span>{marks}</p>"
    )


def samples_pane(assay: AssayReport, index: int) -> str:
    if not assay.samples:
        return _panel("Samples", '<p class="sf-empty">no samples resolved for this assay.</p>')

    columns = assay.attribute_columns
    read_map = {r.read_id: _read_structure(r) for r in assay.reads}
    file_read = {f.basename: f.read_id for f in assay.files if f.read_id}

    head_cells = "".join(f'<th scope="col">{esc(c.replace("_", " "))}</th>' for c in columns)
    header = (
        '<thead><tr><th scope="col" class="sf-col-sticky">Sample</th>'
        f'{head_cells}<th scope="col" class="text-right">Files</th></tr></thead>'
    )
    rows = "".join(
        _sample_rows(s, columns, read_map, file_read, index, i) for i, s in enumerate(assay.samples)
    )
    # `w-auto min-w-full`: fill the panel when a dataset declares three attributes, and grow past it —
    # letting the region, never the page, scroll — once it declares fifteen. The minimum width that
    # forces that is stated once, on `.basis-cell`, so it cannot drift column to column.
    table = (
        '<div class="sf-scroll-x"><table class="w-auto min-w-full text-sm">'
        f"{header}<tbody>{rows}</tbody></table></div>"
    )
    return _panel(
        "Samples",
        _basis_legend() + table,
        sub=f"{assay.n_samples} sample(s). Click any value to see — and copy — what supports it; open "
        "a row (▸) for its files, their read structure, and the exact quotes.",
    )


def _sample_rows(
    sample: SampleView,
    columns: list[str],
    read_map: dict[str, str],
    file_read: dict[str, str],
    assay_index: int,
    row_index: int,
) -> str:
    by_key = {a.key: a for a in sample.attributes}
    detail_id = f"detail-{assay_index}-{row_index}"

    show_acc = sample.accession and sample.accession != sample.sample_id
    acc = (
        f'<span class="ml-2 font-mono text-xs text-dim">{esc(sample.accession)}</span>'
        if show_acc
        else ""
    )
    # The whole first cell is the toggle — a big, easy click target — not just the little caret.
    # `whitespace-nowrap` because this cell is one identifier: caret, id, accession. Row headers wrap
    # by default, which is right where one carries a sentence (the Results table's per-row note) and
    # wrong here — it put the caret on a line of its own above the id it belongs to.
    sample_cell = (
        f'<th scope="row" class="sf-col-sticky smp-toggle whitespace-nowrap" '
        f'data-target="{esc(detail_id)}" '
        'role="button" tabindex="0" aria-expanded="false" aria-label="Show this sample\'s files">'
        '<span class="smp-caret" aria-hidden="true">▸</span>'
        f'<span class="smp-sid">{esc(sample.sample_id)}</span>{acc}</th>'
    )

    cells = "".join(_attr_cell(by_key.get(k)) for k in columns)
    summary = (
        f'<tr>{sample_cell}{cells}<td class="text-right tabular-nums">{sample.n_files}</td></tr>'
    )

    n_cols = len(columns) + 2
    detail = (
        f'<tr id="{esc(detail_id)}" hidden><td colspan="{n_cols}">'
        f"{_sample_detail(sample, read_map, file_read)}</td></tr>"
    )
    return summary + detail


def _attr_cell(attr: AttributeView | None) -> str:
    """One metadata cell. Provenance rides in ``data-*`` attributes, not a native ``title``: the script
    turns a click into a pinned, selectable, copyable popover — a hover tooltip can be neither.

    The mark rides *beside* the value in a flex row rather than absolutely positioned into a reserved
    right-hand gutter. Same picture, but the gutter was padding stated in the stylesheet, and a value
    long enough to be clamped ran under the mark the moment that padding lost a cascade argument. A
    flex row cannot overlap.

    Three shapes, and all three say something. A withheld attribute is a real answer — "two equally
    trusted sources disagreed, so nothing was recorded" — and reads as `— withheld`, never as the
    empty cell that means nobody ever mentioned it. The empty one is the only cell with no
    ``data-key``, which is exactly how the script knows not to offer a popover with nothing in it.
    """
    if attr is None:
        return '<td class="basis-cell text-faint">—</td>'
    if attr.withheld:
        note = (
            "left blank on purpose — two equally-trusted sources disagreed, "
            "so nothing was recorded rather than guess"
        )
        return (
            '<td class="basis-cell" role="button" tabindex="0" '
            f'data-key="{esc(attr.key)}" data-value="withheld" '
            f'data-basis="{esc(note)}" data-source="" data-quote="">'
            '<span class="basis-v basis-withheld">— withheld</span></td>'
        )
    return (
        '<td class="basis-cell" role="button" tabindex="0" '
        f'data-key="{esc(attr.key)}" data-value="{esc(attr.value)}" '
        f'data-basis="{esc(_basis_phrase(attr.basis))}" '
        f'data-source="{esc(_evidence_source(attr.evidence))}" '
        f'data-quote="{esc(_evidence_quote(attr.evidence))}">'
        '<span class="flex items-start justify-between gap-2">'
        f'<span class="basis-v">{esc(attr.value)}</span>'
        f'<span class="basis-mark basis-{esc(attr.basis)} mt-1" aria-hidden="true"></span>'
        "</span></td>"
    )


def _evidence_source(evidence: list[EvidenceRef]) -> str:
    for ref in evidence:
        if ref.kind == "assertion" and ref.document:
            return _humanize_document(ref.document) + (f" p.{ref.page}" if ref.page else "")
        if ref.kind == "accession" and ref.accession:
            return f"record {ref.accession}"
    return ""


def _humanize_document(name: str) -> str:
    """A rendered-document stem → words a reader recognises, not an archive filename.

    ``mmc2`` is a journal's auto-name for a supplementary table, ``experiment-SRX…`` is an archive
    record — neither means anything to a biologist, so each maps to a plain phrase.
    """
    low = name.lower()
    if re.match(r"^mmc\d", low) or "supp" in low:
        return "a supplementary file"
    if low.startswith("experiment-"):
        return "the experiment record"
    if low.startswith("run-"):
        return "the run record"
    if low.startswith(("project-", "study-", "sample-", "biosample-")):
        return "the record"
    if "et-al" in low or "et al" in low or re.search(r"\b(19|20)\d{2}\b", name):
        return "the paper"
    return name


def _evidence_quote(evidence: list[EvidenceRef]) -> str:
    for ref in evidence:
        if ref.kind == "assertion" and ref.quote:
            return ref.quote
    return ""


def _role_human(role: str, region_type: str) -> str:
    for key in (role.lower(), (region_type or "").lower()):
        if key in _ROLE_NAME:
            return _ROLE_NAME[key]
    return role


def _read_structure(read: ReadView) -> str:
    """A plain description of what one sequencing read holds — cell barcode, UMI, cDNA — with lengths.

    This is what belongs in the sample drawer instead of a byte count: it is always known (it is the
    manifest's read layout) and it is the thing a biologist actually wants to confirm about a FASTQ.
    """
    if not read.elements:
        return "sequence reads"
    parts: list[str] = []
    for el in read.elements:
        name = _role_human(el.role, el.region_type)
        parts.append(f"{name} {el.length} bp" if el.length else name)
    return " · ".join(parts)


def _sample_detail(sample: SampleView, read_map: dict[str, str], file_read: dict[str, str]) -> str:
    # Just the files + their read structure. The per-attribute quotes used to be repeated here, but the
    # click popover already carries each value's source and quote, so a second copy was pure redundancy.
    head = '<h4 class="sf-sub-h mb-2">FASTQ files &amp; read structure</h4>'
    if not sample.file_names:
        return f'<div class="border-l-2 border-line py-1 pl-4">{head}<p class="sf-empty">none listed</p></div>'
    items = ""
    for name in sample.file_names:
        role_id = file_read.get(name)
        struct = read_map.get(role_id, "") if role_id else ""
        desc = (
            f'<span class="text-xs text-dim">{esc(role_id)} · {esc(struct)}</span>'
            if role_id and struct
            else ""
        )
        items += (
            '<li class="flex flex-wrap items-baseline gap-3 py-0.5">'
            f'<code class="break-words">{esc(name)}</code>{desc}</li>'
        )
    return (
        '<div class="border-l-2 border-line py-1 pl-4">'
        f'{head}<ul class="m-0 list-none p-0 text-sm">{items}</ul></div>'
    )


# ---- evidence -----------------------------------------------------------------------------------


def evidence_pane(assay: AssayReport) -> str:
    chem = assay.chemistry
    chem_name = chem.assay_labels[0].name if chem.assay_labels and chem.assay_labels[0].name else ""
    winner_label = chem_name or (chem.value[0] if chem.value else "—")
    conf = f"{chem.confidence:.2f}" if chem.confidence is not None else "no single number"
    confirmed = "confirmed against the kit's published barcode list · " if assay.onlists else ""
    # Outline and a hairline, no fill. This is the page's good news, and good news gets no tint: a
    # page whose "it worked" is as loud as its "it did not" has stopped using colour to say anything.
    verdict_strip = (
        '<p class="mt-0 mb-5 flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg '
        'border border-line px-4 py-3 text-sm">'
        f'<span class="font-bold">✓ {esc(winner_label)}</span>'
        f'<span class="text-dim">{esc(confirmed)}{esc(conf)} confidence</span></p>'
    )

    if assay.matrices or assay.ruled_out:
        # One full grid for the winner; every other member of the family — including a
        # processing-equivalent co-winner (v3 vs v3.1, identical scores) — collapses to a score bar,
        # so the reader sees one grid, not two identical ones.
        winners = [m for m in assay.matrices if m.is_winner]
        non_winners = [m for m in assay.matrices if not m.is_winner]
        primary = winners[0] if winners else (assay.matrices[0] if assay.matrices else None)
        winner_score = primary.score if primary and primary.score is not None else None
        winner_card = _matrix_card(primary) if primary else ""
        sib_models = [(m, True) for m in winners[1:]] + [(m, False) for m in non_winners]
        siblings = "".join(_sibling(m, winner_score, equivalent=eq) for m, eq in sib_models)
        focus = ""
        if winner_card or siblings:
            focus = (
                '<section class="mb-6"><h3 class="sf-sub-h mb-3">The winning kit — and its close '
                f"variants</h3>{winner_card}{siblings}</section>"
            )
        body = verdict_strip + focus + _ruled_out(assay)
    else:
        body = (
            verdict_strip
            + '<p class="sf-notice">The scored side-by-side comparison was <b>not persisted</b> for '
            "this workspace (an older cache, or a resumed run). The winning kit above still holds — "
            "it is recorded in the manifest.</p>"
        )

    # The reading cap sits on the column INSIDE the panel, not on the panel: a panel narrower than the
    # panel above it is the loudest way a page reads as two products.
    return _panel(
        "How the chemistry was decided",
        f'<div class="max-w-3xl">{body}</div>',
        sub="Every kit whose read layout could plausibly fit was scored against your actual reads. "
        "One family fit; the rest were ruled out by the sequence itself.",
    )


def _matrix_card(m: MatrixView) -> str:
    """The winner's grid: a hairline card, a caption, and the scores. No border colour, no shadow.

    "Winner" is said in the section heading, in the caption and by the ✓ in the strip above; saying it
    a fourth time in green would spend the verdict palette on a fact that is not a verdict.
    """
    win = (
        '<span class="rounded-full border border-line px-2 py-0.5 text-xs font-bold '
        'tracking-[0.07em] text-dim uppercase">winner</span>'
        if m.is_winner
        else ""
    )
    score = (
        f'<span class="ml-auto tabular-nums text-dim">score {m.score:.2f}</span>'
        if m.score is not None
        else ""
    )
    caption = (
        '<figcaption class="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 '
        f'text-sm"><span class="font-mono font-semibold">{esc(m.tech)}</span>{win}{score}'
        "</figcaption>"
    )
    return f'<figure class="sf-card m-0 mb-3 overflow-hidden">{caption}{_matrix_table(m)}</figure>'


def _sibling(m: MatrixView, winner_score: float | None, *, equivalent: bool = False) -> str:
    pct = round(max(0.0, min(1.0, m.score)) * 100) if m.score is not None else 0
    score = (
        f'<span class="tabular-nums text-dim">{m.score:.2f}</span>' if m.score is not None else ""
    )
    why = (
        "processing-equivalent — identical result" if equivalent else _sibling_why(m, winner_score)
    )
    summary = (
        '<summary class="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">'
        f'<span class="min-w-32 font-mono font-semibold">{esc(m.tech)}</span>'
        f'<span class="mx-bar" style="--mx-w:{pct}%"></span>{score}'
        f'<span class="text-xs text-faint">{esc(why)}</span></summary>'
    )
    return f'<details class="mx-sib">{summary}{_matrix_table(m)}</details>'


def _sibling_why(m: MatrixView, winner_score: float | None) -> str:
    has_forbidden = any(c.status == "forbidden" for r in m.roles for c in r.cells)
    if has_forbidden:
        return "some reads don't fit this variant's barcode list"
    if m.score is not None and winner_score is not None and abs(m.score - winner_score) < 1e-6:
        return "processing-equivalent — same result"
    if m.score is not None:
        return f"scored {m.score:.2f}, below the winner"
    return "a close variant"


def _matrix_table(m: MatrixView) -> str:
    """One technology's role × file grid, inside its own scroll region.

    The card is already a box, so the region loses its border and its radius rather than drawing a
    second one inside the first — the whole page separates with a hairline and nests nothing.
    """
    cols = "".join(f'<th scope="col">{esc(label)}</th>' for label in m.file_labels)
    labels = list(m.file_labels)
    rows = ""
    for role in m.roles:
        # A row wider than `file_labels` is not a reason to drop a cell: the cell still renders, it
        # just cannot name its column.
        cells = "".join(
            _matrix_cell(cell, role.role, labels[i] if i < len(labels) else "")
            for i, cell in enumerate(role.cells)
        )
        rows += (
            '<tr><td class="font-semibold whitespace-nowrap text-dim">'
            f"{esc(role.role)}</td>{cells}</tr>"
        )
    return (
        '<div class="sf-scroll-x rounded-none border-0"><table class="text-sm">'
        f'<thead><tr><th scope="col">read role</th>{cols}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _matrix_cell(cell: MatrixCellView, role: str, file_label: str) -> str:
    """One matrix cell, and every one of the three states it can be in says which it is.

    A forbidden cell used to render as an empty ``<td>`` whose ``✕`` was drawn by the stylesheet and
    whose reason was a native ``title=`` — so on a page with no stylesheet it was blank, and its
    reason could be neither selected nor copied. Worse, a cell that was *scored* but carried no number
    fell down the same branch and was labelled forbidden, which is a different claim: "this kit
    forbids that" and "nobody scored that" are not the same sentence. Three branches, three tagged
    statuses, and the reason rides the same pinnable popover the Samples grid uses.
    """
    if cell.status == "scored" and cell.value is not None:
        pct = round(max(0.0, min(1.0, cell.value)) * 100)
        # Single ink, alpha ramp — the same cyan the provenance marks use, because a matrix cell and
        # an `observed` mark are the same claim: the bytes support this. Written as an inline style
        # because a per-cell percentage cannot be a class the purge could ever see.
        bg = f"color-mix(in srgb, var(--mx-heat) {pct}%, transparent)"
        return f'<td class="mx-cell mx-scored" style="background:{bg}">{cell.value:.2f}</td>'
    if cell.status == "forbidden":
        reason = cell.reason or "this read cannot carry this role for this kit"
        return (
            '<td class="mx-cell mx-forbidden" role="button" tabindex="0" '
            f'data-key="{esc(role)}" data-value="{esc(file_label) or "ruled out"}" '
            f'data-basis="{esc(reason)}" data-source="" data-quote="">✕</td>'
        )
    reason = cell.reason or "no score was recorded for this read and this role"
    return (
        '<td class="mx-cell mx-absent" role="button" tabindex="0" '
        f'data-key="{esc(role)}" data-value="{esc(file_label) or "not scored"}" '
        f'data-basis="{esc(reason)}" data-source="" data-quote="">not scored</td>'
    )


def _ruled_out(assay: AssayReport) -> str:
    if not assay.ruled_out:
        return ""
    # The ✕ is text, not a hue. Ruling a kit out is this check working, and the paragraph below says
    # so — tinting it red would make the page's normal, healthy behaviour look like a wall of errors.
    items = "".join(
        '<li class="flex flex-wrap items-baseline gap-3 border-b border-line py-2 last:border-b-0">'
        f'<span class="text-faint" aria-hidden="true">✕</span>'
        f'<span class="font-semibold">{esc(r.tech)}</span>'
        f'<span class="text-dim">{esc(r.reason)}</span></li>'
        for r in assay.ruled_out
    )
    return (
        '<section class="mt-6 border-t border-line pt-5">'
        '<h3 class="sf-sub-h mb-3">Other kits considered — ruled out by the reads</h3>'
        f'<ul class="m-0 list-none p-0 text-sm">{items}</ul>'
        '<p class="mt-3 mb-0 text-xs text-faint italic">Scoring every kit that could plausibly fit '
        "and rejecting the wrong ones is the check doing its job — not noise.</p></section>"
    )


# ---- pipeline -----------------------------------------------------------------------------------


def pipeline_pane(assay: AssayReport) -> str:
    plan = assay.plan
    if plan is None:
        return _panel(
            "Pipeline",
            '<p class="sf-notice">No processing recipe '
            "has been composed for this assay yet — it resolved to a validated manifest but was not "
            "planned.</p>",
        )

    stages_panel = _stages_panel(assay)
    recipe_panel = _recipe_panel(plan)
    files_panel = _artifacts_panel(assay)
    return stages_panel + recipe_panel + files_panel


def _stages_panel(assay: AssayReport) -> str:
    """What will run, in order — as boxes that share the width rather than a strip with arrows.

    The arrows were `<div>`s *between* the boxes in a wrapping flex row, so on any width where the
    row wrapped one of them ended a line pointing at nothing. The ordinal carries the order instead,
    exactly as the Flow tab's cards do: one ornament vocabulary for "these happen in this sequence",
    used twice, rather than two. It also retires `_STAGE_ICON`, which drew `count` and `package` with
    the same glyph — two boxes claiming to be the same kind of step.
    """
    stages = assay.pipeline_stages
    if not stages:
        return ""
    first_sample = assay.samples[0].sample_id if assay.samples else "each sample"
    boxes = "".join(_stage_box(st, i) for i, st in enumerate(stages))
    # The deliverable depends on the modality: scATAC ends in a fragments file, not a count matrix.
    deliverable = (
        "a tabix-indexed fragments file (fragments.tsv.gz)"
        if assay.chemistry.modality.lower() == "atac"
        else "an .h5ad count matrix"
    )
    return _panel(
        "What the pipeline will run",
        f'<ol class="m-0 flex list-none flex-wrap gap-3 p-0">{boxes}</ol>',
        sub=f"The same stages run for every sample — shown here for {first_sample}. Running the "
        f"composed Snakefile below ends in {deliverable}.",
    )


def _stage_box(stage: PipelineStage, index: int) -> str:
    """One stage. ``flex-1 basis-48`` is why two stages fill the row and four quarter it, and why a
    narrow viewport stacks them instead of scrolling."""
    return (
        '<li class="flex-1 basis-48 rounded-lg border border-line p-4">'
        '<span class="inline-grid size-5 place-items-center rounded-full border border-line '
        f'text-xs font-bold text-dim" aria-hidden="true">{index + 1}</span>'
        f'<b class="mt-2 mb-1 block text-sm font-bold">{esc(stage.title)}</b>'
        f'<span class="block text-xs text-dim">{esc(stage.detail)}</span></li>'
    )


def _recipe_panel(plan: PlanView) -> str:
    """The recipe, one row per decision, with *who decided* as a plain phrase and nothing else.

    The phrase used to be preceded by a basis-coloured dot, and the two could disagree: :func:`_who`
    prefers the field's evidence token over its basis, so a field with ``basis="inferred"`` and a
    ``cli:`` token rendered "you specified" beside the grey dot that means inferred. A hue that
    restates a phrase adds nothing when it agrees and lies when it does not — and four provenance
    hues on a tab that otherwise carries none is exactly what the colour law calls noise. The dot
    stays where it earns its place: the Samples table, where the phrase is not shown.
    """
    rows = ""
    for f in plan.fields:
        rows += (
            f'<tr><td class="whitespace-nowrap text-dim">{esc(f.label)}</td>'
            f'<td class="font-semibold">{esc(f.value)}</td>'
            f'<td class="text-dim">{esc(_who(f))}</td></tr>'
        )
    table = (
        '<div class="sf-scroll-x"><table class="text-sm"><thead><tr><th>choice</th><th>value</th>'
        f"<th>who decided</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )
    if plan.primary_feature:
        table += (
            '<p class="mt-3 mb-0 text-sm">Main count matrix: '
            f'<b class="font-bold">{esc(plan.primary_feature)}</b></p>'
        )
    res = ", ".join(f"{esc(k)} {esc(v)}" for k, v in plan.resources)
    if res:
        table += f'<p class="mt-2 mb-0 text-sm text-dim">Requested resources: {res}.</p>'
    return _panel(
        "Processing choices",
        table,
        sub="What to DO with this data — separate from what the data IS. Change any of these without "
        "touching the manifest.",
    )


def _who(field: DecisionField) -> str:
    """Who decided this recipe field: its evidence token when there is one, its basis otherwise.

    The token is the sharper answer — it distinguishes a policy rule from a flag from a quoted paper,
    which the basis alone cannot — so it wins; the basis is the fallback for a field carrying no
    evidence. That fallback was an inline three-entry dict missing ``user_confirmed``, which is the
    basis a recipe almost always carries, so the commonest answer in this column rendered as the raw
    token "user confirmed" beside "our default" and "you specified".
    """
    for ref in field.evidence:
        if ref.kind == "policy":
            return "our default"
        if ref.kind == "cli":
            return "you specified"
        if ref.kind == "assertion":
            return "from the paper / records"
        if ref.kind == "accession":
            return "from the records"
    return _WHO_PHRASE.get(field.basis, field.basis.replace("_", " "))


def _artifacts_panel(assay: AssayReport) -> str:
    if not assay.artifacts:
        return _panel(
            "Files",
            '<p class="sf-empty">no text artifacts found on disk for this assay.</p>',
        )
    blocks = "".join(_artifact_block(a) for a in assay.artifacts)
    return _panel(
        "Files",
        blocks,
        sub="Everything needed to run, embedded in this page — view inline or download. No other "
        "files required; this report is self-contained.",
    )


def _artifact_block(a: ArtifactEmbed) -> str:
    """One compiled artifact, carried *in* the page rather than linked out of it.

    The download is a ``data:`` URI of the artifact's own bytes: a relative ``href`` to
    ``pipeline/.../Snakefile`` breaks the moment the HTML is moved off the workspace, which is the
    whole point of a one-file report (``model.ArtifactEmbed``). The inline view scrolls inside its
    own box — capped, so a 2000-line Snakefile cannot turn this panel into the page.
    """
    n_lines = a.text.count("\n") + (1 if a.text and not a.text.endswith("\n") else 0)
    b64 = base64.b64encode(a.text.encode()).decode()
    href = f"data:{a.mime};base64,{b64}"
    head = (
        '<div class="flex flex-wrap items-center gap-3 border-b border-line px-3 py-2">'
        f'<code class="font-mono text-xs font-bold">{esc(a.name)}</code>'
        f'<span class="text-xs text-dim tabular-nums">{n_lines} lines · '
        f"{esc(_human_size(a.size_bytes))}</span>"
        '<a class="ms-auto rounded border border-line px-3 py-1 text-xs font-semibold text-accent" '
        f'download="{esc(a.name)}" href="{href}">⭳ Download</a></div>'
    )
    view = (
        '<details><summary class="px-3 py-2 text-sm text-accent">View</summary>'
        '<pre class="m-0 max-h-96 overflow-auto border-t border-line bg-sunk p-4 font-mono '
        f'text-xs">{esc(a.text)}</pre></details>'
    )
    return f'<div class="mb-3 rounded-lg border border-line last:mb-0">{head}{view}</div>'


# ---- results ------------------------------------------------------------------------------------

#: How the four :data:`~seqforge.workflows.metrics.Level` verdicts read in words, and the mark that
#: carries each one **without colour** — a tint is invisible to a colour-blind reader and gone in a
#: printout, so a graded cell is marked as well as tinted and the legend spells both out. ``ok`` and
#: ``none`` get no mark on purpose: marking the majority of cells is the same as marking none of them.
_LEVEL_PHRASE: dict[str, str] = {
    "ok": "within the expected range",
    "warn": "outside the expected range — worth a look",
    "bad": "far outside the expected range",
    "none": "no defensible threshold exists for this number — reported as-is",
}
_LEVEL_FLAG: dict[str, str] = {"warn": "!", "bad": "!!"}


def _level_mark(level: str) -> str:
    """The non-colour mark for a graded value, carrying its verdict as an accessible name."""
    flag = _LEVEL_FLAG.get(level)
    if not flag:
        return ""
    return (
        f'<span class="lvl-flag ml-1" role="img" '
        f'aria-label="{esc(_LEVEL_PHRASE[level])}">{flag}</span>'
    )


#: The band each :data:`~seqforge.workflows.metrics.MetricGroup` renders as, and — because insertion
#: order **is** display order — the order the bands appear in. That order is the pipeline's own order
#: in time: what went in, whether the barcodes worked, where the reads landed, what they were counted
#: into, how much of it was a repeat, what cell-calling produced. The same argument :data:`_TABS`
#: makes for tab order, one level down.
#:
#: Total over the literal, and an exhaustiveness test derived from ``get_args`` holds it there: a
#: seventh group otherwise reaches the page as a span of columns under no heading at all, which is
#: worse than no grouping. The labels are one wet-lab word each for the same reason this whole module
#: is de-jargoned — *Counts*, not *quantification*.
_GROUP_LABEL: dict[MetricGroup, str] = {
    "input": "Input",
    "barcode": "Barcodes",
    "alignment": "Alignment",
    "counts": "Counts",
    "duplication": "Duplication",
    "cells": "Cells",
}

#: Where a column with no metric behind it in any sample would land. Unreachable while
#: ``PipelineStats.columns`` is the union across ``samples`` its own docstring says it is — but a
#: fallback and not an assertion, because such a column is a column of gaps and a reader is still
#: entitled to see its header. Losing one band assignment beats dropping a column silently.
_ORPHAN_GROUP: MetricGroup = "input"

#: At or above this many metric columns the table folds to its headline set behind one control.
#: Sample id plus seven columns is a table a human reads; sample id plus sixteen is a spreadsheet. So
#: the fold engages where there is something to fold and nowhere else: ``map/starsolo`` (16 columns)
#: folds to 7, while ``map/star`` (5) and ``map/chromap`` (6) show everything and render no
#: disclosure at all — which is the fix for a bulk page whose two headline metrics would otherwise
#: have hidden its own read count behind a click.
#:
#: **Why a threshold is admissible here when the sample-count threshold this replaces was not**, and
#: it is the whole reason this number is allowed to exist: ``columns`` is the *module's* declared
#: column set, a static property of which Workflow module ran — identical on the first sample and on
#: the ninety-sixth, and known before a single artifact lands. ``_STRIP_MAX_SAMPLES`` keyed off how
#: many samples were *found*, so one dataset changed layout depending on how much of its pipeline had
#: finished and the same page reread an hour later was a different page. A threshold over a run's
#: outcome is a bug; a threshold over a module's shape is a design.
_FOLD_MIN_COLUMNS = 8

#: How many knee panels reach the page. Each is ~3.5 KB of polyline (200 points, capped upstream by
#: ``knee_points``) and the whole report has a 500 KB budget, so an unbounded per-sample plot is the
#: one thing on this tab that can blow it — a 96-well plate would spend 340 KB drawing curves nobody
#: compares by eye past the first two dozen. Truncation is stated on the page, never silent.
_KNEE_MAX_FIGURES = 24

#: The knee figure's drawing box in SVG user units. The page never sets a pixel width — the CSS grid
#: sizes each figure and the ``viewBox`` scales it — but the aspect and the label margins are fixed
#: here so every small multiple has identical geometry and the panels compare by eye.
_KNEE_W, _KNEE_H = 320.0, 190.0
#: The right margin is wide enough for HALF the last x tick label ("100K"), because that label is
#: centred on the last gridline and the browser clips an ``<svg>``'s overflow by default.
_KNEE_L, _KNEE_R, _KNEE_T, _KNEE_B = 42.0, 16.0, 10.0, 34.0


def results_pane(assay: AssayReport) -> str:
    """What the composed pipeline actually produced — or an honest note that it has not run.

    Every assay gets one of these even when there is nothing to show, so switching tabs in a
    multi-assay report never lands on a section that silently does not exist for assay 2.

    **The table leads, always.** Sample-by-metric was always a grid, and it used to sit one
    disclosure below a wall of per-sample tiles — so the view that answers the reader's first
    question ("how do my samples compare?") was the one view they had to ask for.

    The one thing that leads it is an **alert**, when there is one: it is a claim about the numbers
    in that table, so it sits directly under the run state and directly above the legend that says
    how those numbers grade. On a healthy run there is nothing there at all.
    """
    stats = assay.pipeline_stats
    if stats is None:
        return _panel(
            "Results",
            '<p class="sf-notice">This assay\'s '
            "pipeline has <b>not been run yet</b> — no per-sample QC artifact has been written for "
            "it. Submit the composed Snakefile from the Pipeline tab; this section fills itself in "
            "from what that pipeline writes, and nothing here is computed by "
            '<span class="font-mono">seqforge report</span> itself.</p>',
        )

    if not stats.samples:
        # Every artifact that landed was unreadable, so the state block's notes ARE the section, and
        # the caption may not invite a click on a number that does not exist.
        body = ""
        sub = (
            f"{stats.module} wrote a QC artifact for every sample below and none of them could be "
            "read. The pipeline ran; what it produced is unparseable."
        )
    else:
        body = _stats_table(stats)
        sub = (
            f"Read back from the finished pipeline's own QC artifacts by {stats.module} — one row "
            "per sample, one column per metric. Click a column header for what that metric measures "
            "and what a bad value would mean."
        )

    head = _pipeline_state(stats) + _alerts_block(assay.alerts)
    return _panel("Results", head + body, sub=sub) + _knee_panel(stats)


def _pipeline_state(stats: PipelineStats) -> str:
    """Did the *pipeline* finish — deliberately not the header's compile verdict, and never as it.

    The pill up top answers "did the compiler produce a Snakefile". This answers "did that Snakefile
    finish", and the two disagree exactly when it matters: a workspace stays ``compiled`` and green
    while three of twenty samples are still missing. One badge for both facts is how that goes unseen.

    Three states, not two. Nothing readable at all is its own — it is reached only when artifacts
    landed and every one of them was corrupt, so "what landed is below" would point at an empty
    section, and a partial-run tint would understate a pipeline that produced nothing usable.

    A finished run is drawn in **no colour at all**, like an ``ok`` cell: the page tints what wants
    looking at, and "everything worked" does not. That leaves exactly two tinted things on this tab,
    which is what makes either of them mean something.
    """
    if stats.complete:
        level, icon, text = "ok", "✓", f"all {stats.n_found} sample(s) finished"
    elif stats.n_found == 0:
        level, icon, text = (
            "bad",
            "✗",
            f"<b>No readable result for any of the {stats.n_expected} contracted samples.</b> "
            "The artifacts below were written and could not be parsed.",
        )
    else:
        level, icon, text = (
            "warn",
            "◐",
            f"<b>{stats.n_found} of {stats.n_expected} samples finished.</b> What landed is below; "
            "the rest were contracted by the composed config and have not been written.",
        )
    state = (
        f'<div class="lvl-{level} lvl-state mb-4 flex items-start gap-3 rounded-lg py-3 pr-4 pl-3 '
        f'text-sm"><span class="lvl-flag" aria-hidden="true">{icon}</span>'
        f"<span>{text}</span></div>"
    )
    if not stats.notes:
        return state
    notes = "".join(f"<li>{esc(n)}</li>" for n in stats.notes)
    return f'{state}<ul class="mt-0 mb-4 list-disc pl-5 text-xs text-dim">{notes}</ul>'


# ---- the cross-check's alerts -------------------------------------------------------------------

#: Which verdict a severity wears, and the whole reason this PR adds **no hue, no token and no
#: component**. An alert exists only because something looks wrong, so it is an exception by
#: construction — the verdict palette is not being borrowed for a second meaning here, it is being
#: used for its own, and ``lvl-state`` already draws exactly this shape for the run-state block.
#:
#: Total over :data:`~seqforge.workflows.metrics.Severity`, and an exhaustiveness test derived from
#: ``get_args`` holds it there: a third severity would otherwise render an untinted, unmarked box
#: whose only difference from the two real ones is that nobody can see it.
_SEVERITY_LEVEL: dict[Severity, Level] = {"likely": "bad", "possible": "warn"}

#: How many firing samples an alert spells out before it summarises the rest. The count is already
#: stated in words above the list ("14 of 96"), so what the list adds is the *shape of the numbers* —
#: enough rows to judge the claim rather than trust it. Ninety-six of them would be a wall, and a
#: wall inside a box whose job is to be read first is the fastest way to stop being read.
_ALERT_MAX_SAMPLES = 6


def _alerts_block(alerts: list[Alert]) -> str:
    """Every alert this assay's cross-checks raised, directly under the pipeline's own state.

    Here, on Results, and nowhere else: an alert is a claim about the numbers in the table below it,
    and a reader has to be able to check the claim against the evidence without navigating away. It
    is above the table rather than beside a cell because the claim is about a *decision*, and the
    decision is not in any one column.

    **A healthy run renders nothing at all** — not an empty block, not "no alerts". Seeing one has to
    mean something, and a section that is present on every page is a section the eye stops reading.
    """
    if not alerts:
        return ""
    return (
        '<div class="mb-4"><h3 class="sf-sub-h mb-2">What looks wrong — and which decision it '
        f"points at</h3>{''.join(_alert_card(a) for a in alerts)}</div>"
    )


def _alert_card(alert: Alert) -> str:
    """One alert: the claim, who it fired on with their numbers, the decisions, and what to change.

    The severity mark (``!``/``!!``) carries the same grade as the tint, for the reason every graded
    cell on this tab carries one: a hue is invisible to a colour-blind reader and gone in a greyscale
    printout, and this box is the one thing on the page that must never be missed.

    The **stable id is on the page**, quietly, in the last line. A reader who wants to suppress or
    track one across runs needs to be able to read it off — it is the identifier the JSON payload
    carries, and a page that showed only the sentence would leave them grepping for prose.
    """
    level = _SEVERITY_LEVEL[alert.severity]
    scope = (
        f"every sample that finished ({alert.n_samples} of {alert.n_samples})"
        if alert.scope == "systematic"
        else f"{len(alert.samples)} of {alert.n_samples} samples that finished"
    )
    # `strict=True` because the pairing is guaranteed upstream: `Alert` refuses to exist with the two
    # lists out of step, so there is nothing here to be lenient about. `strict=False` would have this
    # renderer silently absorb a defect the model already makes unconstructable.
    shown = list(zip(alert.samples, alert.measured, strict=True))[:_ALERT_MAX_SAMPLES]
    measured = "".join(
        f'<li><span class="font-mono">{esc(sample)}</span> — {esc(text)}</li>'
        for sample, text in shown
    )
    if len(alert.samples) > len(shown):
        measured += f"<li>and {len(alert.samples) - len(shown)} more, measured the same way</li>"

    # Dropped entirely when nothing resolved, rather than rendered as a heading over an empty list:
    # a field name with no value beside it reads as a value of nothing.
    points = "".join(
        f"<li>{esc(ref.label)} — currently <b>{esc(ref.value)}</b>"
        + (f"; the alternative is <b>{esc(ref.change_to)}</b>" if ref.change_to else "")
        + "</li>"
        for ref in alert.implicates
    )
    implicates = (
        '<div class="sf-sub-h mt-3 mb-1">Points at</div>'
        f'<ul class="mt-0 mb-0 list-disc pl-5 text-xs">{points}</ul>'
        if points
        else ""
    )
    return (
        f'<div class="lvl-{level} lvl-state mb-3 flex items-start gap-3 rounded-lg py-3 pr-4 pl-3 '
        f'text-sm last:mb-0"><span class="lvl-flag" role="img" '
        f'aria-label="{esc(SEVERITY_PHRASE[alert.severity])}">{esc(_LEVEL_FLAG[level])}</span><div>'
        f'<div class="flex flex-wrap items-baseline gap-2"><b>{esc(alert.title)}</b>'
        f'<span class="text-xs text-dim">{esc(SEVERITY_PHRASE[alert.severity])} · {esc(scope)}'
        "</span></div>"
        f'<ul class="mt-1 mb-0 list-disc pl-5 text-xs text-dim">{measured}</ul>'
        f'{implicates}<div class="mt-2 text-xs">{esc(alert.remedy)}</div>'
        f'<div class="mt-1 font-mono text-xs text-faint">{esc(alert.id)}</div></div></div>'
    )


#: The level legend, once per table. It is what makes the per-cell mark and the tint mean something,
#: and it is the reason neither has to be repeated as prose in every cell.
_LEVEL_LEGEND = (
    '<div class="mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-dim">'
    "How each number reads against its own thresholds:"
    + "".join(
        f'<span class="lvl-{key} inline-flex items-center gap-2">'
        f'<span class="lvl-chip">{_LEVEL_FLAG.get(key, "")}</span>'
        f"{escape(phrase.split(' — ')[0])}</span>"
        # Over the map, not over a hand-listed tuple: a fifth verdict would otherwise be graded by an
        # adapter, tinted by the stylesheet and silently absent from the key that says what the tint
        # means. Insertion order IS the display order, which is why the map is written worst-last.
        for key, phrase in _LEVEL_PHRASE.items()
    )
    + "</div>"
)

#: One column of the metrics table, as the header and the body both need it: ``(key, label, headline,
#: hint)``. ``PipelineStats.columns`` carries only the first two, because it is a union across
#: samples; the rest are properties of the metric and are byte-identical the whole way down.
_Column = tuple[str, str, bool, str]


def _column_metrics(stats: PipelineStats) -> dict[str, Metric]:
    """The first :class:`Metric` seen under each column key — one pass, first-seen wins.

    A column's group, headline flag and hint describe the *metric*, so they are identical in every
    row and there is exactly one answer to look up. Collected once here rather than re-scanned per
    header, which was columns × samples × metrics: quadratic work on a plate for a constant.
    """
    found: dict[str, Metric] = {}
    for sample in stats.samples:
        for metric in sample.metrics:
            found.setdefault(metric.key, metric)
    return found


def _banded_columns(stats: PipelineStats) -> list[tuple[MetricGroup, list[_Column]]]:
    """``stats.columns`` regrouped into the bands the header row draws, in :data:`_GROUP_LABEL` order.

    Two stable orders composed: the groups in the order the pipeline reaches them in time, and inside
    each band the adapter's own declared column order, untouched. Reordering for display is a
    presentation decision and it is taken here — the model still hands over exactly what the adapter
    declared.

    A band with no columns on this module is not rendered at all. That is the point rather than an
    optimisation: an scATAC page must have no heading reading *Cells* over metrics that were never
    about cells, and a bulk page none reading *Barcodes*.
    """
    grouped: dict[MetricGroup, list[_Column]] = {group: [] for group in _GROUP_LABEL}
    exemplar = _column_metrics(stats)
    for key, label in stats.columns:
        metric = exemplar.get(key)
        group = metric.group if metric is not None else _ORPHAN_GROUP
        headline = metric is not None and metric.headline
        grouped[group].append((key, label, headline, metric.hint if metric is not None else ""))
    return [(group, columns) for group, columns in grouped.items() if columns]


def _stats_table(stats: PipelineStats) -> str:
    """Rows are samples, columns are ``columns`` grouped into bands, verdict is on the cell.

    The column set is a union across samples, so the lookup is by key and a sample that never reported
    one leaves that cell blank. Never a zero: a zero here is a number a reader would act on, and the
    tool did not write it.

    **Group structure is rule and label, never a second hue.** The bands are a header row of
    ``colspan`` cells separated by a hairline; the group also rides on ``data-group`` so the page
    carries it verbatim, as an attribute rather than as a ``grp-{group}`` class. A computed class
    that styles nothing would be six empty rules whose only job is to satisfy the class-coverage
    guard, and a standing invitation to tint one later — at which point a coloured cell would mean
    "this is a barcode metric" instead of "this number is wrong".

    **The fold is one control for the whole table, not one per band** (:data:`_FOLD_MIN_COLUMNS`),
    and it is a checkbox and its label rather than a ``<details>`` or a script: ``<details>`` cannot
    wrap a subset of a row's ``<td>``s, two tables would duplicate every headline column, and
    ``report.js`` is another ticket's file. The header's band cells are emitted twice when the table
    folds — once at the headline ``colspan`` and once at the full one — because a ``colspan`` is a
    count and a band cannot span columns that are not being shown.
    """
    bands = _banded_columns(stats)
    n_columns = sum(len(columns) for _group, columns in bands)
    n_headline = sum(1 for _group, columns in bands for column in columns if column[2])
    folds = n_columns >= _FOLD_MIN_COLUMNS and 0 < n_headline < n_columns

    band_row, first_folded = "", True
    for index, (group, columns) in enumerate(bands):
        rule = "" if index == 0 else " border-l border-line"
        heads = sum(1 for column in columns if column[2])
        if folds and heads:
            band_row += _band_cell(
                group, heads, "grp-folded" + ("" if first_folded else " border-l border-line")
            )
            first_folded = False
        band_row += _band_cell(group, len(columns), ("grp-extra" if folds else "") + rule)

    head_row = "".join(
        _metric_head(label, hint, extra=folds and not headline)
        for _group, columns in bands
        for _key, label, headline, hint in columns
    )
    rows = ""
    for sample in stats.samples:
        by_key = {m.key: m for m in sample.metrics}
        cells = "".join(
            _metric_cell(by_key.get(key), extra=folds and not headline)
            for _group, columns in bands
            for key, _label, headline, _hint in columns
        )
        # Per row, not once for the table: two samples can be counted off different features, and a
        # single footnote would silently claim they were counted the same way.
        note = (
            f'<span class="block text-xs italic text-faint">{esc(sample.note)}</span>'
            if sample.note
            else ""
        )
        # Nothing but the sticky column. The seven undo-utilities that used to sit here — left, ink,
        # 14px, normal case, normal tracking, wrapping — were all arguing with a `.sf-scroll-x th`
        # that meant to style column heads; that rule now says `thead`.
        rows += (
            f'<tr><th scope="row" class="sf-col-sticky">{esc(sample.sample_id)}{note}</th>'
            f"{cells}</tr>"
        )

    table = (
        '<div class="sf-scroll-x"><table class="w-full text-sm tabular-nums">'
        '<thead><tr><th scope="col" rowspan="2" class="sf-col-sticky align-bottom">Sample</th>'
        f"{band_row}</tr><tr>{head_row}</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    if not folds:
        return _LEVEL_LEGEND + table
    control = (
        '<label class="grp-btn mt-3 inline-flex cursor-pointer items-center gap-2 text-sm '
        'font-semibold text-accent"><input type="checkbox" class="grp-fold sr-only">'
        f'<span class="grp-folded"><span aria-hidden="true">▸ </span>Show all {n_columns} '
        f'metrics</span><span class="grp-extra"><span aria-hidden="true">▾ </span>Show the '
        f"{n_headline} headline metrics</span></label>"
    )
    return f'{_LEVEL_LEGEND}<div class="grp-scope">{table}{control}</div>'


def _band_cell(group: MetricGroup, span: int, cls: str) -> str:
    """One group's header band: a label over its own columns, and never a fill."""
    return (
        f'<th scope="colgroup" colspan="{span}" data-group="{esc(group)}" '
        f'class="text-center {cls}">{esc(_GROUP_LABEL[group])}</th>'
    )


def _metric_head(label: str, hint: str, *, extra: bool) -> str:
    """A column header that reaches its metric's ``hint`` through the samples table's own popover.

    Not a native ``title=``: a hint is a whole sentence of domain knowledge ("a near-zero valid-barcode
    rate means the wrong kit was identified") that a reader wants to select, copy and paste into an
    email — which a transient tooltip can be neither, and which never appears at all on touch. Sharing
    the popover costs one selector in ``report.js`` and gives both tables one behaviour.

    The hint hangs off the header and not the cell, for two reasons that agree. It describes the
    metric and is byte-identical down the whole column, so per cell it is a lie about where the
    information lives; and repeating a ~200-character sentence in every cell of a 96-sample ×
    15-metric table is ~300 KB against a 500 KB page budget — the one thing on this tab that could
    break it.

    ``role="button"`` sits on a span *inside* the ``<th>``, never on the ``<th>``: a column header that
    announces itself as a button has stopped being a column header, and screen-reader table navigation
    is the thing a wide metrics table needs most.
    """
    cls = "align-bottom text-right" + (" grp-extra" if extra else "")
    if not hint:
        return f'<th scope="col" class="{cls}">{esc(label)}</th>'
    return (
        f'<th scope="col" class="{cls}"><span class="metric-head" role="button" tabindex="0" '
        f'data-key="{esc(label)}" data-value="" data-basis="{esc(hint)}" '
        f'data-source="" data-quote="">{esc(label)}</span></th>'
    )


def _metric_cell(metric: Metric | None, *, extra: bool) -> str:
    """One graded number: the value, tinted **only** when its verdict is an exception, and marked.

    ``ok`` and ``none`` get no tint whatever — not a pale green, nothing — so that the few cells that
    do carry one are the ones the eye lands on, and so that a metric which is fine is indistinguishable
    from one nobody could set a defensible bar for. Colour is never the whole signal: the ``!``/``!!``
    mark carries the same verdict for a colour-blind reader, a greyscale printout and a bad projector.

    Deliberately lean — no ``data-*``, no handler. What a reader needs *per cell* is the number and
    whether it is off; what the metric MEANS is one click away on the column header, where it is
    stored once instead of once per sample. A missing metric is a gap and never a zero.
    """
    fold = " grp-extra" if extra else ""
    if metric is None:
        return f'<td class="text-right whitespace-nowrap text-faint{fold}">—</td>'
    return (
        f'<td class="lvl-{esc(metric.level)} lvl-cell text-right whitespace-nowrap{fold}">'
        f"{esc(metric.display)}{_level_mark(metric.level)}</td>"
    )


def _knee_panel(stats: PipelineStats) -> str:
    """Barcode-rank curves as **small multiples over one shared axis range**, not an overlay.

    An overlay needs one distinguishable colour per sample plus a legend, and past about six series a
    legend stops being a key and becomes a lookup table — the reader is matching hues instead of
    reading curves. Small multiples tile instead, and the thing that makes them comparable is that
    every panel is drawn on the same log domain, computed once across the whole pipeline here.
    """
    kneed = [s for s in stats.samples if s.knee]
    if not kneed:
        return ""
    shown = kneed[:_KNEE_MAX_FIGURES]
    # ceil() to the enclosing decade so the gridlines land on the panel edge, and max(1.0, …) so a
    # degenerate one-barcode vector cannot divide by zero.
    x_max = max(1.0, ceil(log10(max(r for s in shown for r, _v in s.knee))))
    y_max = max(1.0, ceil(log10(max((v for s in shown for _r, v in s.knee if v >= 1), default=1))))
    figures = "".join(_knee_figure(s, x_max, y_max) for s in shown)
    if not figures:
        return ""
    trunc = (
        f" Showing {len(shown)} of {len(kneed)} samples — the page has a size budget, and two dozen "
        "panels is already past what anyone compares by eye."
        if len(shown) < len(kneed)
        else ""
    )
    return _panel(
        "Barcode rank ('knee') plot",
        f'<div class="grid gap-4 grid-cols-[repeat(auto-fill,minmax(240px,1fr))]">{figures}</div>',
        sub="Every barcode ranked by how many molecules it captured, on log axes. A real cell "
        "population shows a plateau and then a cliff — the cliff is where cells stop and empty "
        f"droplets start. All panels share one axis range, so they compare directly.{trunc}",
    )


def _knee_figure(sample: SampleStats, x_max: float, y_max: float) -> str:
    """One sample's curve as hand-built inline SVG — no plotting library, so no network request.

    Single-ink on purpose (see :func:`_knee_panel`), and the ink is the accent, which on this page
    means *chrome* and never a verdict — a curve drawn in the warn or the critical hue would be the
    one thing on the tab claiming a judgement nobody made.

    Geometry rides in **presentation attributes** and colour in utility classes: a literal
    ``stroke-width`` on the element is what a browser needs before any stylesheet loads, and it is the
    lowest-priority styling there is, so it can never fight a rule. ``<title>`` rather than
    ``aria-labelledby``: an id would have to be unique across every figure on the page, and a
    generated id is one more thing that has to stay deterministic across two renders for the
    byte-identity test to hold.
    """
    points = [(r, v) for r, v in sample.knee if r >= 1 and v >= 1]
    if not points:
        return ""
    plot_w = _KNEE_W - _KNEE_L - _KNEE_R
    plot_h = _KNEE_H - _KNEE_T - _KNEE_B
    base = _KNEE_T + plot_h

    def px(rank: int) -> float:
        return _KNEE_L + (log10(rank) / x_max) * plot_w

    def py(value: int) -> float:
        return base - (log10(value) / y_max) * plot_h

    grid, ticks = "", ""
    for e in range(int(x_max) + 1):
        x = px(10**e)
        grid += (
            f'<line class="stroke-line" stroke-width="0.5" x1="{x:.1f}" y1="{_KNEE_T:.1f}" '
            f'x2="{x:.1f}" y2="{base:.1f}"/>'
        )
        ticks += (
            f'<text class="fill-faint font-sans" font-size="9" x="{x:.1f}" y="{base + 12:.1f}" '
            f'text-anchor="middle">{_decade(e)}</text>'
        )
    for e in range(int(y_max) + 1):
        y = py(10**e)
        grid += (
            f'<line class="stroke-line" stroke-width="0.5" x1="{_KNEE_L:.1f}" y1="{y:.1f}" '
            f'x2="{_KNEE_L + plot_w:.1f}" y2="{y:.1f}"/>'
        )
        ticks += (
            f'<text class="fill-faint font-sans" font-size="9" x="{_KNEE_L - 5:.1f}" '
            f'y="{y + 3:.1f}" text-anchor="end">{_decade(e)}</text>'
        )
    poly = " ".join(f"{px(r):.1f},{py(v):.1f}" for r, v in points)
    mid_y = _KNEE_T + plot_h / 2
    axis = (
        f'<line class="stroke-faint" stroke-width="1" x1="{_KNEE_L:.1f}" y1="{_KNEE_T:.1f}" '
        f'x2="{_KNEE_L:.1f}" y2="{base:.1f}"/>'
        f'<line class="stroke-faint" stroke-width="1" x1="{_KNEE_L:.1f}" y1="{base:.1f}" '
        f'x2="{_KNEE_L + plot_w:.1f}" y2="{base:.1f}"/>'
    )
    return (
        f'<figure class="sf-card m-0 p-2"><svg class="block h-auto w-full" '
        f'viewBox="0 0 {_KNEE_W:.0f} {_KNEE_H:.0f}" role="img" preserveAspectRatio="xMidYMid meet">'
        f"<title>{esc(sample.sample_id)}: barcodes ranked by molecule count, log-log</title>"
        f"<g>{grid}</g>"
        f'<polyline class="fill-none stroke-accent" stroke-width="1.8" stroke-linejoin="round" '
        f'points="{poly}"/>'
        f"{axis}{ticks}"
        f'<text class="fill-dim font-sans" font-size="9" x="{_KNEE_L + plot_w / 2:.1f}" '
        f'y="{_KNEE_H - 4:.1f}" text-anchor="middle">barcodes, ranked</text>'
        f'<text class="fill-dim font-sans" font-size="9" x="11" y="{mid_y:.1f}" '
        f'text-anchor="middle" transform="rotate(-90 11 {mid_y:.1f})">molecules</text>'
        f'</svg><figcaption class="pt-2 text-center text-xs break-words">'
        f"{esc(sample.sample_id)}</figcaption></figure>"
    )


def _decade(exponent: int) -> str:
    """``10**exponent`` as a tick label a human reads: 1, 10, 100, 1K, 10K, 1M."""
    if exponent >= 6:
        return f"{10 ** (exponent - 6)}M"
    if exponent >= 3:
        return f"{10 ** (exponent - 3)}K"
    return str(10**exponent)


# ---- assay section + tab bar --------------------------------------------------------------------


def assay_section(assay: AssayReport, index: int) -> str:
    """One assay's six panes, of which the script shows one.

    ``assay``, ``pane`` and ``active`` are not styling classes and must not become utilities:
    ``report.js`` selects on the first two and toggles the third, and there is no markup for a
    toggled state to hang a utility off. ``.pane``/``.pane.active`` are declared components for that
    reason, and the section's own visibility is an inline ``style`` the script writes — which is also
    why swapping it for ``hidden`` would break it, an inline style beating any class.
    """
    panes = [
        ("overview", overview_pane(assay)),
        ("flow", flow_pane(assay)),
        ("samples", samples_pane(assay, index)),
        ("evidence", evidence_pane(assay)),
        ("pipeline", pipeline_pane(assay)),
        ("results", results_pane(assay)),
    ]
    body = "".join(
        f'<div class="pane{" active" if name == "overview" else ""}" data-tab="{name}">{html}</div>'
        for name, html in panes
    )
    return f'<section class="assay" data-assay="{index}">{body}</section>'


def tab_bar(report: ProjectReport) -> str:
    """The tab strip, minus any tab this report has nothing behind.

    Results is dropped entirely when no assay has pipeline stats, rather than rendered as a tab
    leading to "not run yet". A workspace that has only been compiled then renders exactly the page it
    rendered before this tab existed — the reader is never offered a door into an empty room, and the
    tab's presence becomes the signal that *something* here has results.
    """
    has_results = any(a.pipeline_stats is not None for a in report.assays)
    tabs = "".join(
        f'<button class="tab{" active" if key == "overview" else ""}" data-tab="{key}">{esc(label)}</button>'
        for key, label in _TABS
        if key != "results" or has_results
    )
    # `overflow-x-auto` on the strip and not on the page: at a narrow viewport the tabs scroll
    # sideways, which is the one horizontal scroll this page is allowed to have.
    return f'<nav><div class="sf-page flex gap-1 overflow-x-auto">{tabs}</div></nav>'


def assay_switcher(report: ProjectReport) -> str:
    if not report.is_multi_assay:
        return ""
    opts = "".join(
        f'<option value="{i}">{esc(a.label)} · {a.n_samples} sample(s)</option>'
        for i, a in enumerate(report.assays)
    )
    return (
        '<div class="flex shrink-0 items-center gap-2">'
        '<label for="assay-select" class="text-xs font-bold uppercase tracking-[0.07em] text-faint">'
        "assay</label>"
        f'<select id="assay-select" class="sf-select">{opts}</select></div>'
    )


__all__ = ["assay_section", "tab_bar", "assay_switcher", "esc"]
