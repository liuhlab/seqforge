"""HTML fragments — small typed helpers plus one function per tab.

Hand-rolled rather than templated: one self-contained page does not earn a templating dependency, and
keeping the fragments as functions means the types flow (an :class:`AssayReport` in, a string out) and
mypy checks the projection is used the way it was built. Every dynamic value goes through :func:`esc`;
the only structured untrusted inputs (a study title, a forbidden reason, a metadata value) are escaped
at the point they enter the string.

A later LLM-written summary would be one more function here returning a fragment — the shell in
``render.py`` would not change.
"""

from __future__ import annotations

from html import escape

from .flow import flow_mermaid
from .model import AssayReport, AttributeView, EvidenceRef, MatrixView, ProjectReport, SampleView

_TABS: list[tuple[str, str]] = [
    ("overview", "Overview"),
    ("flow", "Flow"),
    ("samples", "Samples"),
    ("evidence", "Evidence"),
    ("pipeline", "Pipeline"),
]


# ---- helpers ------------------------------------------------------------------------------------


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def _basis_badge(basis: str) -> str:
    return f'<span class="badge {esc(basis)}">{esc(basis.replace("_", " "))}</span>'


def _conf_badge(conf: float | None) -> str:
    if conf is None:
        return ""
    return f'<span class="badge conf">conf {conf:.2f}</span>'


def _rung_badge(rung: int) -> str:
    return f'<span class="badge rung">rung {esc(rung)}</span>'


def _panel(title: str, body: str, *, sub: str = "") -> str:
    sub_html = f'<p class="sub">{esc(sub)}</p>' if sub else ""
    return f'<div class="panel"><h2>{esc(title)}</h2>{sub_html}{body}</div>'


def _kv_rows(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return '<p class="empty">nothing recorded</p>'
    body = "".join(f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in rows)
    return f'<div class="tbl-wrap"><table class="kv"><tbody>{body}</tbody></table></div>'


def _tiles(items: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div class="tile"><div class="k">{esc(k)}</div><div class="v small">{esc(v)}</div></div>'
        for k, v in items
    )
    return f'<div class="tiles">{cells}</div>'


# ---- overview -----------------------------------------------------------------------------------


def overview_pane(assay: AssayReport) -> str:
    chem = assay.chemistry
    badges = (
        "".join(
            f'<span class="chem-badge">{esc(c)}'
            + (f" <small>{esc(_curie_for(assay, c))}</small>" if _curie_for(assay, c) else "")
            + "</span>"
            for c in chem.value
        )
        or '<span class="chem-badge">no confident chemistry call</span>'
    )

    organism = assay.organism_name or (
        f"taxid {assay.organism_taxid}" if assay.organism_taxid else "organism not declared"
    )
    study = assay.study
    title = (study.title if study and study.title else None) or assay.label
    study_acc = study.accession if study and study.accession else ""

    hero = (
        '<div class="hero"><div class="h-main">'
        f"<h1>{esc(title)}</h1>"
        f'<div class="organism">{esc(organism)}'
        + (f" · {esc(study_acc)}" if study_acc else "")
        + "</div>"
        f'<div class="chem-badges">{badges}</div>'
        "</div></div>"
    )

    conf = f"{chem.confidence:.3f}" if chem.confidence is not None else "n/a"
    tiles = _tiles(
        [
            ("Samples", str(assay.n_samples)),
            ("FASTQ files", str(assay.n_files)),
            ("Modality", chem.modality),
            ("Confidence", conf),
            ("Rung", str(chem.rung)),
            ("Organism", organism),
        ]
    )

    c = assay.conclusion
    banner = (
        f'<div class="banner {esc(c.kind)}"><b>{esc(c.headline)}.</b>'
        f"<span>{esc(c.detail)}</span></div>"
    )
    if study and study.abstract:
        abstract = (
            "<details style='margin-top:12px'><summary>Study abstract</summary>"
            f"<p style='color:var(--text-dim);margin:8px 0 0'>{esc(study.abstract)}</p></details>"
        )
    else:
        abstract = ""

    return _panel("Overview", hero + banner + tiles + abstract)


def _curie_for(assay: AssayReport, chemistry: str) -> str:
    for label in assay.chemistry.assay_labels:
        if label.chemistry == chemistry:
            return label.name
    return ""


# ---- flow ---------------------------------------------------------------------------------------


def flow_pane(assay: AssayReport, flow_id: str) -> str:
    source = flow_mermaid(assay)
    legend = (
        '<div class="legend">'
        '<span><span class="sw" style="background:#00695c"></span>evidenced artifact</span>'
        '<span><span class="sw" style="background:#37474f"></span>deliverable</span>'
        '<span><span class="sw" style="background:#b71c1c"></span>blocked</span>'
        '<span><span class="sw" style="background:#bf360c"></span>needs a human</span>'
        "</div>"
    )
    body = (
        f'<div class="flow-box"><div class="mermaid-target" id="{esc(flow_id)}">'
        '<p class="empty">rendering…</p></div></div>' + legend
    )
    panel = _panel(
        "Decision flow",
        body,
        sub="How the compiler moved from bytes to a deliverable, with this dataset's real values.",
    )
    # The source rides in a script block the inlined JS renders; it never contains </script>.
    return panel + f'<script type="text/x-mermaid" data-target="{esc(flow_id)}">{source}</script>'


# ---- samples ------------------------------------------------------------------------------------


def samples_pane(assay: AssayReport) -> str:
    if not assay.samples:
        return _panel("Samples", '<p class="empty">no samples resolved for this assay.</p>')
    cards = "".join(_sample_card(s) for s in assay.samples)
    return _panel(
        "Samples",
        cards,
        sub=f"{assay.n_samples} sample(s). Each attribute carries how it was known and where from.",
    )


def _sample_card(sample: SampleView) -> str:
    # Only show the accession when it adds something — for a record-joined sample the id IS the
    # accession, and printing it twice is noise.
    show_acc = sample.accession and sample.accession != sample.sample_id
    acc = f'<span class="acc">{esc(sample.accession)}</span>' if show_acc else ""
    head = (
        '<summary class="sample-head">'
        f'<span class="sid">{esc(sample.sample_id)}</span>{acc}'
        f'<span class="count">{sample.n_files} file(s) · {len(sample.attributes)} attribute(s)</span>'
        "</summary>"
    )
    if sample.attributes:
        rows = "".join(_attr_row(a) for a in sample.attributes)
        body = (
            '<div class="tbl-wrap"><table><thead><tr>'
            "<th>attribute</th><th>value</th><th>provenance</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    else:
        body = '<p class="empty" style="padding:12px">no attributes declared.</p>'
    files = (
        f'<p style="color:var(--text-dim);font-size:0.82rem;padding:8px 12px 0">'
        f"files: {esc(', '.join(sample.file_names))}</p>"
        if sample.file_names
        else ""
    )
    return f'<details class="sample-card">{head}{body}{files}</details>'


def _attr_row(attr: AttributeView) -> str:
    badges = _basis_badge(attr.basis) + _conf_badge(attr.confidence) + _rung_badge(attr.rung)
    if attr.withheld:
        badges = '<span class="badge withheld">withheld</span>' + badges
    value = f'<span class="val">{esc(attr.value)}</span>'
    prov = _provenance(attr.evidence)
    return (
        f'<tr class="attr-row"><td class="mono">{esc(attr.key)}</td>'
        f'<td>{value}<div class="pill-row" style="margin-top:5px">{badges}</div></td>'
        f"<td>{prov}</td></tr>"
    )


def _provenance(evidence: list[EvidenceRef]) -> str:
    if not evidence:
        return '<span class="empty">—</span>'
    parts: list[str] = []
    for ref in evidence:
        if ref.kind == "assertion" and ref.quote:
            page = f" · p.{ref.page}" if ref.page else ""
            doc = f" · {esc(ref.document)}" if ref.document else ""
            parts.append(
                "<details><summary>quoted claim</summary>"
                f'<div class="prov"><span class="quote">{esc(ref.quote)}</span>{page}{doc}</div>'
                "</details>"
            )
        elif ref.kind == "accession":
            parts.append(f'<span class="mono">record {esc(ref.accession)}</span>')
        elif ref.kind == "policy":
            parts.append(f'<span class="mono" title="policy default">{esc(ref.raw)}</span>')
        elif ref.kind == "cli":
            parts.append(f'<span class="mono" title="a CLI flag">{esc(ref.raw)}</span>')
        elif ref.kind == "assertion":
            parts.append('<span class="mono">harvested claim</span>')
        else:
            parts.append(f'<span class="mono">{esc(ref.raw[:16])}</span>')
    return "<div>" + "<br>".join(parts) + "</div>"


# ---- evidence -----------------------------------------------------------------------------------


def evidence_pane(assay: AssayReport) -> str:
    chem = assay.chemistry
    decision = _kv_rows(
        [
            ("chemistry", ", ".join(chem.value) or "—"),
            ("basis", _basis_badge(chem.basis)),
            ("confidence", f"{chem.confidence:.4f}" if chem.confidence is not None else "n/a"),
            ("rung", str(chem.rung)),
            ("files scored", str(chem.n_files)),
            ("evidence", f"{len(chem.evidence_shas)} file fingerprint(s)"),
        ]
    )
    decision_panel = _panel(
        "Chemistry decision",
        decision,
        sub="The single evidenced call the whole library follows from — decided from bytes.",
    )

    if assay.matrices:
        matrices = "".join(_matrix(m) for m in assay.matrices)
        matrix_panel = _panel(
            "Evidence matrix",
            matrices,
            sub="Per technology: each read role scored against each file. Green = support, ✕ = a "
            "gate forbade it (hover for the reason).",
        )
    else:
        matrix_panel = _panel(
            "Evidence matrix",
            '<p class="empty">The scored matrix was not persisted for this workspace (an older '
            "cache, or a resumed run). The chemistry decision above still holds — it lives in the "
            "manifest.</p>",
        )
    return decision_panel + matrix_panel


def _matrix(m: MatrixView) -> str:
    win = '<span class="win">winner</span>' if m.is_winner else ""
    score = f'<span class="badge conf">score {m.score:.3f}</span>' if m.score is not None else ""
    head = f"<h3>{esc(m.tech)} {win} {score}</h3>"
    cols = "".join(f"<th>{esc(label)}</th>" for label in m.file_labels)
    rows = ""
    for role in m.roles:
        cells = ""
        for cell in role.cells:
            if cell.status == "scored" and cell.value is not None:
                pct = round(max(0.0, min(1.0, cell.value)) * 100)
                bg = f"color-mix(in srgb, var(--scored-1) {pct}%, var(--scored-0))"
                cells += f'<td class="cell" style="background:{bg}">{cell.value:.2f}</td>'
            else:
                reason = esc(cell.reason or "forbidden")
                cells += f'<td class="cell forbidden" title="{reason}"></td>'
        rows += f'<tr><td class="mrole">{esc(role.role)}</td>{cells}</tr>'
    table = (
        '<div class="tbl-wrap"><table class="matrix"><thead><tr><th>role</th>'
        f"{cols}</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    return f'<div class="matrix-tech">{head}{table}</div>'


# ---- pipeline -----------------------------------------------------------------------------------


def pipeline_pane(assay: AssayReport) -> str:
    plan = assay.plan
    if plan is None:
        return _panel(
            "Pipeline",
            '<p class="empty">No processing recipe has been composed for this assay yet — it '
            "resolved to a manifest but was not planned.</p>",
        )

    field_rows = ""
    for f in plan.fields:
        badges = _basis_badge(f.basis) + _conf_badge(f.confidence) + _rung_badge(f.rung)
        ev = ", ".join(esc(r.raw) for r in f.evidence) or "—"
        field_rows += (
            f'<tr><td class="mono">{esc(f.label)}</td><td><b>{esc(f.value)}</b>'
            f'<div class="pill-row" style="margin-top:5px">{badges}</div></td>'
            f'<td class="mono" style="font-size:0.8rem">{ev}</td></tr>'
        )
    recipe = (
        '<div class="tbl-wrap"><table><thead><tr><th>decision</th><th>value</th><th>who decided</th>'
        f"</tr></thead><tbody>{field_rows}</tbody></table></div>"
    )
    if plan.primary_feature:
        recipe += f'<p class="sub" style="margin-top:10px">primary matrix: <b>{esc(plan.primary_feature)}</b></p>'
    recipe_panel = _panel(
        "Recipe", recipe, sub="What to DO with this assay. Precedence: flag ▸ instruction ▸ policy."
    )

    resources = _panel("Resources", _kv_rows([(k, esc(v)) for k, v in plan.resources]))

    links = _artifact_links(assay)
    config = (
        "<details><summary>composed config.yaml ("
        f"{len(plan.config)} keys)</summary>"
        + _kv_rows([(k, esc(v)) for k, v in plan.config])
        + "</details>"
        if plan.config
        else '<p class="empty">no composed config on disk.</p>'
    )
    artifacts_panel = _panel("Artifacts", links + config)

    return recipe_panel + resources + artifacts_panel


def _artifact_links(assay: AssayReport) -> str:
    plan = assay.plan
    prefix = f"{assay.subdir}/" if assay.subdir else ""
    links: list[tuple[str, str | None]] = [
        ("manifest.yaml", f"{prefix}manifest.yaml"),
        ("processing.yaml", f"{prefix}processing.yaml"),
        ("Snakefile", plan.snakefile_rel if plan else None),
        ("config.yaml", plan.config_rel if plan else None),
        ("units.tsv", plan.units_rel if plan else None),
    ]
    items = ""
    for label, href in links:
        if href:
            items += f'<a href="{esc(href)}"><span class="badge rung">{esc(label)}</span></a> '
        else:
            items += f'<span class="badge inferred" style="opacity:0.5">{esc(label)}</span> '
    return f'<div class="pill-row" style="margin-bottom:10px">{items}</div>'


# ---- assay section + tab bar --------------------------------------------------------------------


def assay_section(assay: AssayReport, index: int) -> str:
    flow_id = f"flow-{index}"
    panes = [
        ("overview", overview_pane(assay)),
        ("flow", flow_pane(assay, flow_id)),
        ("samples", samples_pane(assay)),
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
