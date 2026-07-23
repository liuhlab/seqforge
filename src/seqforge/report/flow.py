"""Turn one :class:`AssayReport` into a Mermaid ``flowchart`` of the *actual* decision path.

The centrepiece of the Flow tab. Not a generic diagram of the pipeline — the real chain for THIS
dataset, with the chemistry it resolved to, the confidence and rung behind it, the role wiring the
bytes chose, the manifest hash, the recipe, and the terminal driven by how the compile actually ended.
Nodes appear only for stages that are present, so an IR-ready dataset stops at the manifest and a
refusal stops at a red terminal.

House style matches the docs (``classDef artifact/output/blocked/ask``); dark-filled nodes carry an
inline white ``<span>`` so the label is legible whatever the page or mermaid theme.
"""

from __future__ import annotations

from .model import AssayReport

#: The docs' palette, verbatim — the report and the site read as one system.
_CLASSDEFS = (
    "classDef step fill:#eceff1,stroke:#90a4ae,color:#263238;",
    "classDef artifact fill:#00695c,stroke:#004d40;",
    "classDef output fill:#37474f,stroke:#263238;",
    "classDef blocked fill:#b71c1c,stroke:#7f0000;",
    "classDef ask fill:#bf360c,stroke:#7f2400;",
)
#: Classes whose fill is dark, so their label text must be forced white.
_DARK = {"artifact", "output", "blocked", "ask"}


def _san(text: str) -> str:
    """Make a value safe inside a quoted mermaid label: drop the two characters that break it."""
    return str(text).replace('"', "").replace("#", "").replace("\n", " ").strip()


def _node(node_id: str, lines: list[str], cls: str) -> str:
    body = "<br/>".join(_san(x) for x in lines if x)
    if cls in _DARK:
        body = f"<span style='color:#fff'>{body}</span>"
    return f'    {node_id}["{body}"]:::{cls}'


def _dataset_hash(assay: AssayReport) -> str | None:
    for key, val in assay.provenance:
        if key == "dataset_hash":
            return val
    return None


def _wiring_summary(assay: AssayReport) -> str:
    """``R1: CB,UMI · R2: cDNA`` — each read and the ordered, de-duplicated roles packed into it."""
    parts: list[str] = []
    for read in assay.reads:
        roles: list[str] = []
        for el in read.elements:
            if el.role not in roles:
                roles.append(el.role)
        if roles:
            parts.append(f"{read.read_id}: {','.join(roles)}")
    return " · ".join(parts)


def flow_mermaid(assay: AssayReport) -> str:
    """The Mermaid source for one assay's decision chain (a full ``flowchart TD`` document)."""
    nodes: list[str] = []
    edges: list[str] = []
    chain: list[str] = []  # node ids, in order, to wire with arrows

    def add(node_id: str, lines: list[str], cls: str) -> None:
        nodes.append(_node(node_id, lines, cls))
        chain.append(node_id)

    n_files = assay.chemistry.n_files or assay.n_files
    add("probe", ["probe", f"{n_files} files · bytes only" if n_files else "bytes only"], "step")

    chem = assay.chemistry
    if chem.value:
        head = chem.value[0] + ("  +more" if len(chem.value) > 1 else "")
        conf = f"conf {chem.confidence:.2f}" if chem.confidence is not None else "conf n/a"
        add("resolve", ["resolve", head, f"{conf} · rung {chem.rung}"], "artifact")
    else:
        add("resolve", ["resolve", "no confident call"], "artifact")

    kind = assay.conclusion.kind

    if kind in ("compiled", "ir_ready"):
        wiring = _wiring_summary(assay)
        if wiring:
            add("wiring", ["role wiring", wiring], "step")
        h = _dataset_hash(assay)
        manifest_lines = ["manifest"]
        if h:
            manifest_lines.append(f"hash {h[:8]}")
        manifest_lines.append(f"{assay.n_samples} samples")
        add("manifest", manifest_lines, "artifact")

        if assay.plan is not None:
            genome = _plan_value(assay, "genome")
            genome_short = genome.split(" (")[0].split(" / ")[0] if genome else "genome"
            feature = assay.plan.primary_feature or _plan_value(assay, "quantification") or "counts"
            add("plan", ["plan", genome_short, _san(feature)], "artifact")

        if kind == "compiled":
            add("compose", ["compose"], "step")
            add("done", ["✓ Compiled", "Snakefile ready"], "output")
        else:
            add("done", ["Manifest ready", "not composed yet"], "output")
    elif kind == "blocker":
        add("done", ["✗ Blocked", "compiler refused"], "blocked")
    else:  # question
        add("done", ["? Needs a human", "an open question"], "ask")

    for a, b in zip(chain, chain[1:], strict=False):  # consecutive pairs: last has no successor
        edges.append(f"    {a} --> {b}")

    lines = ["flowchart TD", *nodes, *edges, "", *(f"    {c}" for c in _CLASSDEFS)]
    return "\n".join(lines)


def _plan_value(assay: AssayReport, label: str) -> str | None:
    if assay.plan is None:
        return None
    for field in assay.plan.fields:
        if field.label == label:
            return field.value
    return None


__all__ = ["flow_mermaid"]
