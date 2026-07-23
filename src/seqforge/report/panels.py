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

from .flow import FlowStep, flow_steps
from .model import (
    ArtifactEmbed,
    AssayReport,
    AttributeView,
    DecisionField,
    EvidenceRef,
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
    "index": "sample index",
}

_TABS: list[tuple[str, str]] = [
    ("overview", "Overview"),
    ("flow", "Flow"),
    ("samples", "Samples"),
    ("evidence", "Evidence"),
    ("pipeline", "Pipeline"),
]

#: How each provenance basis reads to someone who has never seen the manifest vocabulary.
_BASIS_PHRASE: dict[str, str] = {
    "observed": "measured directly from your files",
    "asserted": "stated in the records or paper",
    "inferred": "inferred from the surrounding context",
    "user_confirmed": "confirmed by you",
}

#: A verdict glyph for the restated hero card.
_VERDICT_GLYPH: dict[str, str] = {
    "compiled": "✓",
    "ir_ready": "•",
    "blocker": "✗",
    "question": "?",
}


# ---- helpers ------------------------------------------------------------------------------------


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def _basis_phrase(basis: str) -> str:
    return _BASIS_PHRASE.get(basis, basis.replace("_", " "))


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _panel(title: str, body: str, *, sub: str = "", cls: str = "") -> str:
    sub_html = f'<p class="sub">{esc(sub)}</p>' if sub else ""
    klass = f"panel {cls}".strip()
    return f'<div class="{klass}"><h2>{esc(title)}</h2>{sub_html}{body}</div>'


def _kv_rows(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return '<p class="empty">nothing recorded</p>'
    body = "".join(f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in rows)
    return f'<div class="tbl-wrap"><table class="kv"><tbody>{body}</tbody></table></div>'


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

    # chemistry, spelled human-first with the code as a quiet chip
    chem_name = chem.assay_labels[0].name if chem.assay_labels and chem.assay_labels[0].name else ""
    chem_id = chem.value[0] if chem.value else ""
    chem_line = ""
    if chem_id:
        name_html = f'<span class="chem-name">{esc(chem_name)}</span>' if chem_name else ""
        more = (
            f'<span class="chem-plus">+{len(chem.value) - 1} equivalent</span>'
            if len(chem.value) > 1
            else ""
        )
        chem_line = (
            f'<div class="chem-line">{name_html}'
            f'<code class="chem-id">{esc(chem_id)}</code>{more}</div>'
        )

    eyebrow = " · ".join(x for x in (study_acc, center) if x)
    c = assay.conclusion
    verdict_card = (
        f'<div class="verdict-card {esc(c.kind)}">'
        f'<span class="vc-icon">{esc(_VERDICT_GLYPH.get(c.kind, "•"))}</span>'
        f"<div><strong>{esc(c.headline)}</strong><span>{esc(c.detail)}</span></div></div>"
    )
    hero = (
        '<div class="hero"><div class="h-main">'
        + (f'<div class="eyebrow">{esc(eyebrow)}</div>' if eyebrow else "")
        + f"<h1>{esc(title)}</h1>"
        + f'<p class="organism">{org_html} · {esc(_assay_kind(assay))}</p>'
        + chem_line
        + "</div>"
        + verdict_card
        + "</div>"
    )

    # abstract — first-class, shown by default (only when a record actually carried one)
    if study and study.abstract:
        abstract = (
            '<section class="abstract"><div class="section-label">About this study</div>'
            f'<p class="abstract-body">{esc(study.abstract)}</p></section>'
        )
    else:
        abstract = ""

    # general-statistics strip — jargon-free, with a confidence meter
    conf = chem.confidence
    if conf is not None:
        pct = round(max(0.0, min(1.0, conf)) * 100)
        conf_dd = (
            f'<dd><span class="meter-line"><span class="meter">'
            f'<span style="width:{pct}%"></span></span> {conf:.2f}</span></dd>'
        )
    else:
        conf_dd = '<dd class="sm">n/a</dd>'
    genstats = (
        '<dl class="genstats">'
        f"<div><dt>Samples</dt><dd>{assay.n_samples}</dd></div>"
        f"<div><dt>FASTQ files</dt><dd>{assay.n_files}</dd></div>"
        f'<div><dt>Kit</dt><dd class="sm">{esc(chem_name or chem_id or "—")}</dd></div>'
        f'<div><dt>Organism</dt><dd class="sm">{org_html}</dd></div>'
        '<div class="genstats-conf"><dt>Confidence '
        '<span class="hint" title="How strongly the files’ own bytes point to this kit '
        '— 1.00 means certain.">i</span></dt>'
        f"{conf_dd}</div>"
        "</dl>"
    )

    return _panel("Overview", hero + abstract + genstats)


# ---- flow ---------------------------------------------------------------------------------------


def flow_pane(assay: AssayReport) -> str:
    steps = flow_steps(assay)
    cards = "".join(_flow_card(s, i) for i, s in enumerate(steps))
    legend = (
        '<div class="legend">'
        '<span><span class="sw" style="background:#eceff1;border:1px solid #90a4ae"></span>a guess to check</span>'
        '<span><span class="sw" style="background:#00695c"></span>measured / decided</span>'
        '<span><span class="sw" style="background:#37474f"></span>the deliverable</span>'
        '<span><span class="sw" style="background:#bf360c"></span>needs a human</span>'
        "</div>"
    )
    return _panel(
        "How seqforge read this dataset",
        f'<ol class="flow-strip">{cards}</ol>{legend}',
        sub="Read the steps in order: the guess we started from, what your files actually contain, and "
        "how it ends. Every step is decided from the sequence itself, not from what the paper claimed.",
    )


def _flow_card(step: FlowStep, index: int) -> str:
    desc = "<br>".join(esc(d) for d in step.desc if d)
    note = f'<div class="fs-note">{esc(step.note)}</div>' if step.note else ""
    arrow = '<div class="fs-arrow" aria-hidden="true">→</div>'
    return (
        f'<li class="flow-step kind-{esc(step.kind)}">'
        f'<span class="fs-num" aria-hidden="true">{index + 1}</span>'
        f'<div class="fs-title">{esc(step.title)}</div>'
        f'<div class="fs-desc">{desc}</div>{note}{arrow}</li>'
    )


# ---- samples ------------------------------------------------------------------------------------


def samples_pane(assay: AssayReport, index: int) -> str:
    if not assay.samples:
        return _panel("Samples", '<p class="empty">no samples resolved for this assay.</p>')

    columns = assay.attribute_columns
    read_map = {r.read_id: _read_structure(r) for r in assay.reads}
    file_read = {f.basename: f.read_id for f in assay.files if f.read_id}

    legend = (
        '<div class="legend-basis">Where each value came from:'
        '<span class="basis-observed"><span class="basis-dot"></span>your files</span>'
        '<span class="basis-asserted"><span class="basis-dot"></span>records / paper</span>'
        '<span class="basis-inferred"><span class="basis-dot"></span>inferred</span>'
        '<span class="basis-user_confirmed"><span class="basis-dot"></span>you</span>'
        "</div>"
    )

    head_cells = "".join(f"<th>{esc(c.replace('_', ' '))}</th>" for c in columns)
    header = (
        '<thead><tr><th class="col-sample">Sample</th>'
        f'{head_cells}<th class="num">Files</th></tr></thead>'
    )
    rows = "".join(
        _sample_rows(s, columns, read_map, file_read, index, i) for i, s in enumerate(assay.samples)
    )
    table = (
        '<div class="tbl-wrap tbl-sticky"><table class="samples">'
        f"{header}<tbody>{rows}</tbody></table></div>"
    )
    return _panel(
        "Samples",
        legend + table,
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
    acc = f'<span class="acc mono">{esc(sample.accession)}</span>' if show_acc else ""
    # The whole first cell is the toggle — a big, easy click target — not just the little caret.
    sample_cell = (
        f'<th scope="row" class="col-sample sample-toggle" data-target="{esc(detail_id)}" '
        'role="button" tabindex="0" aria-expanded="false" aria-label="Show this sample\'s files">'
        '<span class="row-toggle" aria-hidden="true">▸</span>'
        f'<span class="sid">{esc(sample.sample_id)}</span>{acc}</th>'
    )

    cells = "".join(_attr_cell(by_key.get(k)) for k in columns)
    summary = (
        f'<tr class="sample-row">{sample_cell}{cells}<td class="num">{sample.n_files}</td></tr>'
    )

    n_cols = len(columns) + 2
    detail = (
        f'<tr class="detail-row" id="{esc(detail_id)}" hidden><td colspan="{n_cols}">'
        f"{_sample_detail(sample, read_map, file_read)}</td></tr>"
    )
    return summary + detail


def _attr_cell(attr: AttributeView | None) -> str:
    """One metadata cell. Provenance rides in ``data-*`` attributes, not a native ``title``: the script
    turns a click into a pinned, selectable, copyable popover — a hover tooltip can be neither."""
    if attr is None:
        return '<td class="attr-cell empty">—</td>'
    if attr.withheld:
        note = (
            "left blank on purpose — two equally-trusted sources disagreed, "
            "so nothing was recorded rather than guess"
        )
        return (
            '<td class="attr-cell withheld" role="button" tabindex="0" '
            f'data-key="{esc(attr.key)}" data-value="withheld" '
            f'data-basis="{esc(note)}" data-source="" data-quote="">'
            '<span class="v">— withheld</span></td>'
        )
    return (
        f'<td class="attr-cell basis-{esc(attr.basis)}" role="button" tabindex="0" '
        f'data-key="{esc(attr.key)}" data-value="{esc(attr.value)}" '
        f'data-basis="{esc(_basis_phrase(attr.basis))}" '
        f'data-source="{esc(_evidence_source(attr.evidence))}" '
        f'data-quote="{esc(_evidence_quote(attr.evidence))}">'
        f'<span class="v">{esc(attr.value)}</span>'
        '<span class="basis-dot" aria-hidden="true"></span></td>'
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
    if not sample.file_names:
        return '<div class="detail-body"><h4>FASTQ files</h4><p class="empty">none listed</p></div>'
    items = ""
    for name in sample.file_names:
        role_id = file_read.get(name)
        struct = read_map.get(role_id, "") if role_id else ""
        desc = (
            f'<span class="rstruct">{esc(role_id)} · {esc(struct)}</span>'
            if role_id and struct
            else ""
        )
        items += f"<li><code>{esc(name)}</code>{desc}</li>"
    return (
        '<div class="detail-body"><h4>FASTQ files &amp; read structure</h4>'
        f'<ul class="file-list">{items}</ul></div>'
    )


# ---- evidence -----------------------------------------------------------------------------------


def evidence_pane(assay: AssayReport) -> str:
    chem = assay.chemistry
    chem_name = chem.assay_labels[0].name if chem.assay_labels and chem.assay_labels[0].name else ""
    winner_label = chem_name or (chem.value[0] if chem.value else "—")
    conf = f"{chem.confidence:.2f}" if chem.confidence is not None else "no single number"
    confirmed = "confirmed against the kit's published barcode list · " if assay.onlists else ""
    verdict_strip = (
        '<div class="verdict-strip">'
        f'<span class="win-chip">✓ {esc(winner_label)}</span>'
        f'<span class="vs-note">{esc(confirmed)}{esc(conf)} confidence</span></div>'
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
            sib_block = f'<div class="siblings">{siblings}</div>' if siblings else ""
            focus = (
                '<section class="family-focus"><h3>The winning kit'
                '<span class="fam-note"> — and its close variants</span></h3>'
                f"{winner_card}{sib_block}</section>"
            )
        ruled = _ruled_out(assay)
        body = verdict_strip + focus + ruled
    else:
        body = (
            verdict_strip
            + '<p class="notice">The scored side-by-side comparison was <b>not persisted</b> for '
            "this workspace (an older cache, or a resumed run). The winning kit above still holds — "
            "it is recorded in the manifest.</p>"
        )

    return _panel(
        "How the chemistry was decided",
        body,
        sub="Every kit whose read layout could plausibly fit was scored against your actual reads. "
        "One family fit; the rest were ruled out by the sequence itself.",
        cls="evidence",
    )


def _matrix_card(m: MatrixView) -> str:
    win = '<span class="win">winner</span>' if m.is_winner else ""
    score = f'<span class="score">score {m.score:.2f}</span>' if m.score is not None else ""
    caption = f'<figcaption><span class="tech">{esc(m.tech)}</span>{win}{score}</figcaption>'
    return f'<figure class="matrix-card is-winner">{caption}{_matrix_table(m)}</figure>'


def _sibling(m: MatrixView, winner_score: float | None, *, equivalent: bool = False) -> str:
    pct = round(max(0.0, min(1.0, m.score)) * 100) if m.score is not None else 0
    score = f'<span class="score">{m.score:.2f}</span>' if m.score is not None else ""
    why = (
        "processing-equivalent — identical result" if equivalent else _sibling_why(m, winner_score)
    )
    summary = (
        f'<summary><span class="tech">{esc(m.tech)}</span>'
        f'<span class="mini-bar" style="--w:{pct}%"></span>{score}'
        f'<span class="why">{esc(why)}</span></summary>'
    )
    return f'<details class="sibling">{summary}{_matrix_table(m)}</details>'


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
    cols = "".join(f"<th>{esc(label)}</th>" for label in m.file_labels)
    rows = ""
    for role in m.roles:
        cells = ""
        for cell in role.cells:
            if cell.status == "scored" and cell.value is not None:
                pct = round(max(0.0, min(1.0, cell.value)) * 100)
                bg = f"color-mix(in srgb, var(--heat-1) {pct}%, var(--heat-0))"
                cells += f'<td class="cell" style="background:{bg}">{cell.value:.2f}</td>'
            else:
                reason = esc(cell.reason or "forbidden")
                cells += f'<td class="cell forbidden" title="{reason}"></td>'
        rows += f'<tr><td class="mrole">{esc(role.role)}</td>{cells}</tr>'
    return (
        '<table class="matrix"><thead><tr><th>read role</th>'
        f"{cols}</tr></thead><tbody>{rows}</tbody></table>"
    )


def _ruled_out(assay: AssayReport) -> str:
    if not assay.ruled_out:
        return ""
    items = "".join(
        f'<li><span class="x">✕</span><b>{esc(r.tech)}</b>'
        f'<span class="reason">{esc(r.reason)}</span></li>'
        for r in assay.ruled_out
    )
    return (
        '<section class="ruled-out"><h3>Other kits considered'
        '<span class="fam-note"> — ruled out by the reads</span></h3>'
        f'<ul class="ruled-list">{items}</ul>'
        '<p class="ruled-foot">Scoring every kit that could plausibly fit and rejecting the wrong '
        "ones is the check doing its job — not noise.</p></section>"
    )


# ---- pipeline -----------------------------------------------------------------------------------

_STAGE_ICON: dict[str, str] = {
    "onlist": "⛬",
    "align": "⧉",
    "count": "▦",
    "package": "▦",
}


def pipeline_pane(assay: AssayReport) -> str:
    plan = assay.plan
    if plan is None:
        return _panel(
            "Pipeline",
            '<p class="notice">No processing recipe has been composed for this assay yet — it '
            "resolved to a validated manifest but was not planned.</p>",
        )

    stages_panel = _stages_panel(assay)
    recipe_panel = _recipe_panel(plan)
    files_panel = _artifacts_panel(assay)
    return stages_panel + recipe_panel + files_panel


def _stages_panel(assay: AssayReport) -> str:
    stages = assay.pipeline_stages
    if not stages:
        return ""
    first_sample = assay.samples[0].sample_id if assay.samples else "each sample"
    boxes: list[str] = []
    for st in stages:
        boxes.append(_stage_box(st))
    strip = '<div class="stage-arrow">→</div>'.join(boxes)
    return _panel(
        "What the pipeline will run",
        f'<div class="stage-flow">{strip}</div>',
        sub=f"The same stages run for every sample — shown here for {first_sample}. Running the "
        "composed Snakefile below ends in an .h5ad count matrix.",
    )


def _stage_box(stage: PipelineStage) -> str:
    icon = _STAGE_ICON.get(stage.key, "•")
    return (
        f'<div class="stage"><div class="stage-icon">{esc(icon)}</div>'
        f"<b>{esc(stage.title)}</b><span>{esc(stage.detail)}</span></div>"
    )


def _recipe_panel(plan: PlanView) -> str:
    rows = ""
    for f in plan.fields:
        rows += (
            f'<tr class="recipe-row"><td class="rk">{esc(f.label)}</td>'
            f'<td class="rv">{esc(f.value)}</td>'
            f'<td><span class="who"><span class="basis-dot bd-{esc(f.basis)}"></span>'
            f"{esc(_who(f))}</span></td></tr>"
        )
    table = (
        '<div class="tbl-wrap"><table><thead><tr><th>choice</th><th>value</th>'
        f"<th>who decided</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )
    if plan.primary_feature:
        table += (
            f'<p class="sub" style="margin-top:10px">Main count matrix: '
            f"<b>{esc(plan.primary_feature)}</b></p>"
        )
    res = ", ".join(f"{esc(k)} {esc(v)}" for k, v in plan.resources)
    if res:
        table += f'<p class="sub" style="margin-top:6px">Requested resources: {res}.</p>'
    return _panel(
        "Processing choices",
        table,
        sub="What to DO with this data — separate from what the data IS. Change any of these without "
        "touching the manifest.",
    )


def _who(field: DecisionField) -> str:
    for ref in field.evidence:
        if ref.kind == "policy":
            return "our default"
        if ref.kind == "cli":
            return "you specified"
        if ref.kind == "assertion":
            return "from the paper / records"
        if ref.kind == "accession":
            return "from the records"
    return {
        "observed": "measured from the files",
        "asserted": "from the records",
        "inferred": "inferred",
    }.get(field.basis, field.basis.replace("_", " "))


def _artifacts_panel(assay: AssayReport) -> str:
    if not assay.artifacts:
        return _panel(
            "Files",
            '<p class="empty">no text artifacts found on disk for this assay.</p>',
        )
    blocks = "".join(_artifact_block(a) for a in assay.artifacts)
    return _panel(
        "Files",
        blocks,
        sub="Everything needed to run, embedded in this page — view inline or download. No other "
        "files required; this report is self-contained.",
    )


def _artifact_block(a: ArtifactEmbed) -> str:
    n_lines = a.text.count("\n") + (1 if a.text and not a.text.endswith("\n") else 0)
    b64 = base64.b64encode(a.text.encode()).decode()
    href = f"data:{a.mime};base64,{b64}"
    head = (
        '<div class="artifact-head">'
        f"<code>{esc(a.name)}</code>"
        f'<span class="sz">{n_lines} lines · {esc(_human_size(a.size_bytes))}</span>'
        f'<a class="dl-btn" download="{esc(a.name)}" href="{href}">⭳ Download</a></div>'
    )
    view = f'<details><summary>View</summary><pre class="code">{esc(a.text)}</pre></details>'
    return f'<div class="artifact">{head}{view}</div>'


# ---- assay section + tab bar --------------------------------------------------------------------


def assay_section(assay: AssayReport, index: int) -> str:
    panes = [
        ("overview", overview_pane(assay)),
        ("flow", flow_pane(assay)),
        ("samples", samples_pane(assay, index)),
        ("evidence", evidence_pane(assay)),
        ("pipeline", pipeline_pane(assay)),
    ]
    body = "".join(
        f'<div class="pane{" active" if name == "overview" else ""}" data-tab="{name}">{html}</div>'
        for name, html in panes
    )
    return f'<section class="assay" data-assay="{index}">{body}</section>'


def tab_bar() -> str:
    tabs = "".join(
        f'<button class="tab{" active" if key == "overview" else ""}" data-tab="{key}">{esc(label)}</button>'
        for key, label in _TABS
    )
    return f'<nav class="tabs"><div class="tabs-row">{tabs}</div></nav>'


def assay_switcher(report: ProjectReport) -> str:
    if not report.is_multi_assay:
        return ""
    opts = "".join(
        f'<option value="{i}">{esc(a.label)} · {a.n_samples} sample(s)</option>'
        for i, a in enumerate(report.assays)
    )
    return (
        '<div class="assay-switch"><label for="assay-select" class="title-dim">assay</label>'
        f'<select id="assay-select">{opts}</select></div>'
    )


__all__ = ["assay_section", "tab_bar", "assay_switcher", "esc"]
