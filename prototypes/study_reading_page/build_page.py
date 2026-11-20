"""PROTOTYPE, throwaway: lay out each study's reading for a person to look over.

Reads a folder holding one subfolder per study and includes every study that has a
``reading.yaml``, with ``study.yaml``, ``reading.verdict.yaml`` and ``reading.notes.md`` beside it.
Writes one self-contained, view-only HTML page that shows one study at a time: the design, the
sample table with the words behind each cell, what the reading implies between its samples, the
second model's verdict, and the notes on what the format could not hold.

    python prototypes/study_reading_page/build_page.py FOLDER OUT.html [--title TEXT]

The page carries record and paper text: write it outside the repo, or to a folder git ignores.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from html import escape as e
from pathlib import Path
from typing import Any

import yaml

from seqforge.study.reading import LABELS, Factor, Reading, SampleClaim, read_reading
from seqforge.study.relations import relations

HERE = Path(__file__).parent
#: A claim with a probability under this stands out on the page.
LOW = 0.8
READ = {
    "records": "the GEO records",
    "abstract": "the paper's abstract",
    "full_text": "the paper's full text",
}
#: Each backing mark: its sign in a cell, and its name in plain words.
BACKING = {
    "quoted": ("❝", "quoted"),
    "inferred": ("≈", "worked out"),
    "unknown": ("?", "not stated"),
    "na": ("–", "does not apply"),
}
RELATIONS = {
    "replicates": (
        "Replicate groups",
        "Samples of the same kind of library at the same level of every factor.",
    ),
    "contrasts": (
        "Comparisons",
        "Two groups that differ in one factor only. Control libraries are left out.",
    ),
    "series": ("Series", "Samples in order along a factor whose levels have an order."),
    "shared_biosample": (
        "Made from the same material",
        "Libraries that came from one piece of material.",
    ),
    "signal_to_control": (
        "Pull-down and its control",
        "Each pull-down library with the control library made from the same material.",
    ),
}

Add = Callable[[dict[str, Any]], int]


def load(path: Path) -> dict[str, Any]:
    """One YAML file, or an empty dict when it is not there."""
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def words(label: str) -> str:
    """A label as a reader would say it."""
    return label.replace("_", " ")


def backing(claim: SampleClaim | Factor) -> str:
    """Which backing mark a claim carries."""
    for name in BACKING:
        if getattr(claim, name, None) is not None:
            return name
    return "unknown"


def amount(claim: SampleClaim) -> str:
    """A claim's number with its unit, a range as low to high."""
    if claim.value is None:
        return ""
    ends = claim.value if isinstance(claim.value, tuple) else (claim.value,)
    return " to ".join(f"{end:g}" for end in ends) + (f" {claim.unit}" if claim.unit else "")


def said(claim: SampleClaim) -> str:
    """What a cell shows first: the source's words, the file's own name, or which gap it is."""
    parts = [part for part in (claim.id, claim.text) if part]
    if not parts and claim.value is not None:
        parts = [amount(claim)]
    if not claim.says:
        parts.append(BACKING[backing(claim)][1])
    return " · ".join(parts) or "(no words given)"


def source(passage: Any) -> str:
    """Where a passage was read, in words."""
    if passage.paper:
        return f"Paper {passage.paper}, {passage.section}"
    record = passage.record
    if record == "each_sample":
        where = "Every sample record"
    elif isinstance(record, list):
        where = "Records " + ", ".join(record)
    else:
        where = f"Record {record}"
    return f"{where}, field {passage.field}"


def evidence(
    title: str, who: str, claim: SampleClaim | Factor, facts: list[tuple[str, str]], dis: list[int]
) -> dict[str, Any]:
    """What the evidence panel shows for one claim. The page script reads it as data."""
    mark = backing(claim)
    return {
        "title": title,
        "who": who,
        "facts": [[name, value] for name, value in facts if value],
        "mark": mark,
        "note": None if mark == "quoted" else getattr(claim, mark, None),
        "quoted": claim.quoted or [],
        "see": claim.see or [],
        "p": claim.p,
        "dis": dis,
    }


def probability(p: float | None) -> tuple[str, str]:
    """A probability as cell markup and as spoken words."""
    if p is None:
        return '<span class="p">no probability yet</span>', "no probability yet"
    low = '<b class="lowtag">low</b>' if p < LOW else ""
    bar = f'<span class="bar"><i style="width:{p * 100:.0f}%"></i></span>'
    return f'<span class="p">{bar}{p:g}{low}</span>', f"probability {p:g}"


def cell(
    n: int, name: str, head: str, small: list[str], claim: SampleClaim | Factor, flag: bool
) -> str:
    """One claim as a button: its words, how it is backed, and how sure the model is."""
    mark = backing(claim)
    sign, word = BACKING[mark]
    classes = ["claim", f"m-{mark}"]
    if claim.p is not None and claim.p < LOW:
        classes.append("low")
    if flag:
        classes.append("flagged")
    shown, spoken = probability(claim.p)
    disputed = '<span class="flag" title="The second model disagrees">!</span>' if flag else ""
    spoken_flag = ", the second model disagrees" if flag else ""
    return (
        f'<button type="button" class="{" ".join(classes)}" data-c="{n}" '
        f'aria-label="{e(name)}: {e(head)}, {word}, {spoken}{spoken_flag}">'
        f'<span class="mk" title="{word}">{sign}</span>'
        f'<span class="val">{e(head)}</span>{"".join(small)}{shown}{disputed}</button>'
    )


def claim_cell(label: str, who: str, claim: SampleClaim, dis: list[int], add: Add) -> str:
    """One label of one sample, as a cell that opens its evidence."""
    facts = [
        ("In the source's words", claim.text or ""),
        ("Name given in this file", claim.id or ""),
        ("Number", amount(claim)),
        ("Level of the factor", claim.level or ""),
        ("Ontology term", claim.term or ""),
    ]
    n = add(evidence(words(label), who, claim, facts, dis))
    small = []
    if claim.level:
        small.append(f'<span class="lv">level: {e(claim.level)}</span>')
    if claim.value is not None and (claim.text or claim.id):
        small.append(f'<span class="sub">{e(amount(claim))}</span>')
    if claim.term:
        small.append(f'<span class="term">{e(claim.term)}</span>')
    return cell(n, f"{words(label)}, {who}", said(claim), small, claim, bool(dis))


def kind_words(assay: str | None, target: str | None, control: str | None) -> str:
    """A kind of library in words: the assay, what it pulls down, or which control it is."""
    out = assay or "assay not stated"
    if target:
        out += f", pull-down of {target}"
    if control:
        out += f", control library ({control})"
    return out


def ids(samples: list[str]) -> str:
    """Sample ids as one small run of text."""
    return f'<span class="ids">{e(", ".join(samples))}</span>'


def levels_words(levels: dict[str, str | None]) -> str:
    """The level on each factor, in words; empty when there is no factor."""
    return ", ".join(
        f"{words(factor)}: {level}" if level is not None else f"not placed on {words(factor)}"
        for factor, level in levels.items()
    )


def disputes(
    reading: Reading, verdict: dict[str, Any]
) -> tuple[dict[tuple[str, str], list[int]], list[str]]:
    """Which claims each disagreement names, and the disagreements that name no claim on the page.

    A claim is keyed ``(sample, label)``, a factor ``("factors", label)``. A disagreement about a
    shared claim covers every sample whose own row does not replace it.
    """
    marked: dict[tuple[str, str], list[int]] = {}
    nowhere: list[str] = []
    for n, item in enumerate(verdict.get("disagreements") or []):
        where = str(item.get("where") or "")
        hits: list[tuple[str, str]] = []
        for sample, label in re.findall(r"samples\.([\w.-]+?)\.(\w+)", where):
            if label in reading.rows().get(sample, {}):
                hits.append((sample, label))
        for label in re.findall(r"shared\.(\w+)", where):
            if label in reading.shared:
                hits += [(s, label) for s, own in reading.samples.items() if label not in own]
        hits += [
            ("factors", f) for f in re.findall(r"factors\.(\w+)", where) if f in reading.factors
        ]
        for hit in hits:
            marked.setdefault(hit, []).append(n)
        if not hits:
            nowhere.append(where)
    return marked, nowhere


def design(reading: Reading, marked: dict[tuple[str, str], list[int]], add: Add) -> str:
    """The factors with their levels, and one small grid of sample counts per kind of library."""
    rows = reading.rows()
    names = list(reading.factors)
    if not names:
        return "<p>The reading names no factor: nothing was varied on purpose.</p>"
    cards = []
    for label, factor in reading.factors.items():
        dis = marked.get(("factors", label), [])
        order = "in order" if factor.ordered else "no order"
        base = (
            f"baseline: {factor.baseline}"
            if factor.baseline is not None
            else "no baseline: the levels are compared with each other"
        )
        facts = [
            ("Levels", (" → " if factor.ordered else ", ").join(factor.levels)),
            ("Baseline", factor.baseline or "none: the levels are compared with each other"),
            ("Do the levels have an order?", "yes" if factor.ordered else "no"),
        ]
        n = add(evidence(f"factor: {words(label)}", "the study's design", factor, facts, dis))
        items = []
        for level in factor.levels:
            claims = [row[label] for row in rows.values() if label in row]
            texts = list(dict.fromkeys(c.text for c in claims if c.level == level and c.text))
            count = sum(c.level == level for c in claims)
            tag = '<span class="tag">baseline</span>' if level == factor.baseline else ""
            src = "; ".join(texts[:3]) + (" …" if len(texts) > 3 else "")
            items.append(
                f'<li><b>{e(level)}</b>{tag} <span class="n">{count} samples</span>'
                + (f'<span class="src">{e(src)}</span>' if src else "")
                + "</li>"
            )
        head = f"{len(factor.levels)} levels, {order}"
        small = [f'<span class="sub">{e(base)}</span>']
        cards.append(
            f'<div class="factor"><h4>{e(words(label))}</h4>'
            + cell(n, f"factor {words(label)}", head, small, factor, bool(dis))
            + f'<ol class="levels{" ordered" if factor.ordered else ""}">{"".join(items)}</ol></div>'
        )

    def level(sample: str, factor: str) -> str | None:
        return rows[sample][factor].level if factor in rows[sample] else None

    def at(factor: str, value: str | None) -> int:
        order = reading.factors[factor].levels
        return order.index(value) if value is not None and value in order else len(order)

    across = max(names, key=lambda f: (len(reading.factors[f].levels), reading.factors[f].ordered))
    down = [f for f in names if f != across]
    kinds: dict[tuple[str | None, ...], list[str]] = {}
    for sample, row in rows.items():
        kind = tuple(
            row[k].text if row[k].says else None for k in ("assay", "target", "control_type")
        )
        kinds.setdefault(kind, []).append(sample)
    grids = []
    for kind, members in kinds.items():
        caption = f'{e(kind_words(*kind))} <span class="n">{len(members)} samples</span>'
        counts: dict[tuple[tuple[str | None, ...], str | None], int] = {}
        for sample in members:
            key = (tuple(level(sample, f) for f in down), level(sample, across))
            counts[key] = counts.get(key, 0) + 1
        if all(c is None and not any(r) for r, c in counts):
            grids.append(f'<p class="outside">{caption}: not placed on any factor.</p>')
            continue
        cols: list[str | None] = list(reading.factors[across].levels)
        if any(c is None for _, c in counts):
            cols.append(None)
        lines = sorted(
            {r for r, _ in counts}, key=lambda r: [at(f, v) for f, v in zip(down, r, strict=True)]
        )
        head = "".join(
            f'<th scope="col">{e(c) if c is not None else "not placed"}'
            + (
                '<span class="tag">baseline</span>'
                if c == reading.factors[across].baseline and c
                else ""
            )
            + "</th>"
            for c in cols
        )
        body = ""
        for line in lines:
            name = levels_words(dict(zip(down, line, strict=True))) or "samples"
            body += f'<tr><th scope="row">{e(name)}</th>'
            for c in cols:
                count = counts.get((line, c), 0)
                body += f'<td class="{"some" if count else "none"}">{count or "·"}</td>'
            body += "</tr>"
        arrow = " →" if reading.factors[across].ordered else ""
        grids.append(
            f'<figure class="grid"><figcaption>{caption}</figcaption><div class="scroll"><table>'
            f'<thead><tr><th class="corner">{e(words(across))}{arrow}</th>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div></figure>"
        )
    return (
        f'<div class="factors">{"".join(cards)}</div>'
        '<p class="hint">Samples at each level, counted. One grid per kind of library.</p>'
        + "".join(grids)
    )


def sample_table(reading: Reading, marked: dict[tuple[str, str], list[int]], add: Add) -> str:
    """The labels every sample shares, listed once, then one row per sample for the rest."""
    rows = reading.rows()
    seen = dict.fromkeys(label for row in rows.values() for label in row)
    order = [f for f in reading.factors if f in seen]
    order += [label for label in (*LABELS, *seen) if label in seen and label not in order]
    order = list(dict.fromkeys(order))
    first = next(iter(rows.values()))
    same = [
        label
        for label in order
        if all(label in row and row[label] == first[label] for row in rows.values())
    ]
    differ = [label for label in order if label not in same]
    chips = []
    for label in same:
        dis = sorted({n for sample in rows for n in marked.get((sample, label), [])})
        chips.append(
            f'<div class="chip"><span class="label">{e(words(label))}</span>'
            + claim_cell(label, "every sample", first[label], dis, add)
            + "</div>"
        )
    head = "".join(
        f'<th scope="col">{e(words(label))}'
        + ('<span class="tag">factor</span>' if label in reading.factors else "")
        + "</th>"
        for label in differ
    )
    body = ""
    for sample, row in rows.items():
        body += f'<tr data-sample="{e(sample)}"><th scope="row" class="sid">{e(sample)}</th>'
        for label in differ:
            if label in row:
                dis = marked.get((sample, label), [])
                body += f"<td>{claim_cell(label, sample, row[label], dis, add)}</td>"
            else:
                body += '<td class="absent">not written</td>'
        body += "</tr>"
    return (
        f"<h4>The same for every sample ({len(same)} labels)</h4>"
        f'<div class="shared">{"".join(chips)}</div>'
        f"<h4>What differs between the {len(rows)} samples ({len(differ)} labels)</h4>"
        '<div class="scroll tablebox" tabindex="0" role="group" aria-label="Sample table">'
        f'<table class="samples"><thead><tr><th scope="col" class="sid">sample</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def relation_lines(reading: Reading) -> str:
    """The five relations the reading implies, each in plain words."""
    rows = reading.rows()
    found = relations(reading)

    def kind(item: dict[str, Any]) -> str:
        return kind_words(item.get("assay"), item.get("target"), item.get("control_type"))

    def held(item: dict[str, Any]) -> str:
        fixed = levels_words(item["fixed"])
        return f" With {e(fixed)}." if fixed else ""

    lines: dict[str, list[str]] = {name: [] for name in RELATIONS}
    for item in found["replicates"]:
        at = levels_words(item["levels"])
        lines["replicates"].append(
            f"<b>{len(item['samples'])} samples</b>: {e(kind(item))}"
            + (f", {e(at)}" if at else "")
            + f". {ids(item['samples'])}"
        )
    for item in found["contrasts"]:
        one, other = item["levels"]
        sides = [
            f"<b>{e(lv)}</b> ({len(item['samples'][lv])}"
            + (", the baseline" if lv == item["baseline"] else "")
            + ")"
            for lv in (other, one)
        ]
        lines["contrasts"].append(
            f"{e(words(item['factor']))}: {sides[0]} against {sides[1]}. {e(kind(item))}."
            f'{held(item)} <span class="ids">{e(other)}: {e(", ".join(item["samples"][other]))}; '
            f"{e(one)}: {e(', '.join(item['samples'][one]))}</span>"
        )
    for item in found["series"]:
        steps = " → ".join(
            f'<span class="step"><b>{e(step["level"])}</b> {ids(step["samples"])}</span>'
            for step in item["steps"]
        )
        lines["series"].append(
            f"{e(words(item['factor']))}, {len(item['steps'])} steps. {e(kind(item))}.{held(item)}"
            f'<span class="steps">{steps}</span>'
        )
    for item in found["shared_biosample"]:
        lines["shared_biosample"].append(f"<b>{e(item['biosample'])}</b>: {ids(item['samples'])}")
    pairs: dict[tuple[str, str], list[str]] = {}
    for item in found["signal_to_control"]:
        pairs.setdefault((item["biosample"], item["control"]), []).append(item["signal"])
    for (biosample, control), signals in pairs.items():
        names = ", ".join(f"{s} ({rows[s]['target'].text or 'target'})" for s in signals)
        lines["signal_to_control"].append(
            f"<b>{e(biosample)}</b>: {e(names)} → control {e(control)} "
            f"({e(rows[control]['control_type'].text or 'control')})"
        )
    out = ""
    for name, (title, meaning) in RELATIONS.items():
        got = lines[name]
        count = len(found[name])
        items = "".join(f"<li>{line}</li>" for line in got) if got else ""
        listing = f"<ul>{items}</ul>" if got else '<p class="none">None in this study.</p>'
        out += (
            f'<div class="relation"><h4>{title} <span class="n">{count}</span></h4>'
            f'<p class="hint">{meaning}</p>{listing}</div>'
        )
    return out


def where_words(where: str) -> str:
    """A disagreement's place in the file, in words."""
    parts: dict[str, list[str]] = {}
    for sample, label in re.findall(r"samples\.([\w.-]+?)\.(\w+)", where):
        parts.setdefault(words(label), []).append(sample)
    out = [f"{label} of {', '.join(samples)}" for label, samples in parts.items()]
    out += [
        f"{words(label)}, a claim shared by the samples"
        for label in re.findall(r"shared\.(\w+)", where)
    ]
    out += [f"the factor {words(label)}" for label in re.findall(r"factors\.(\w+)", where)]
    out += [f"passage {number}" for number in re.findall(r"passages\.(\d+)", where)]
    return "; ".join(out) or where


def verdict_block(reading: Reading, verdict: dict[str, Any], nowhere: list[str]) -> str:
    """What the second model found: whether it agrees, each disagreement, and its doubts."""
    if not verdict:
        return '<p class="none">No second model has checked this reading.</p>'
    found = verdict.get("disagreements") or []
    agrees = verdict.get("agrees")
    if agrees is True:
        state, cls = "The second model agrees with the reading", "yes"
        if found:
            state += f", and raises {len(found)} point{'s' if len(found) > 1 else ''}"
    elif agrees is False:
        state, cls = "The second model does not agree with the reading", "no"
    else:
        state, cls = "The second model gave no overall answer", "no"
    quotes = verdict.get("quotes") or {}
    facts = [f"Checked by {verdict.get('model') or 'a model not named'}"]
    if verdict.get("when"):
        facts.append(f"on {verdict['when']}")
    if verdict.get("read"):
        facts.append("after reading " + ", ".join(READ.get(r, str(r)) for r in verdict["read"]))
    lines = []
    if quotes:
        lines.append(
            f"{quotes.get('found')} of {quotes.get('checked')} quotes were found in their sources."
        )
        if quotes.get("not_found"):
            lines.append("Not found: " + ", ".join(str(q) for q in quotes["not_found"]) + ".")
    if "samples_match_bounds" in verdict:
        match = "are" if verdict["samples_match_bounds"] else "are NOT"
        lines.append(f"The samples {match} the ones the study was drawn around.")
    out = (
        f'<p class="state {cls}">{e(state)}.</p><p class="hint">{e(" ".join(facts))}. '
        f"{e(' '.join(lines))}</p>"
    )
    if found:
        out += "<h4>Disagreements</h4>"
    for item in found:
        where = str(item.get("where") or "")
        backs = ""
        for number in item.get("passages") or []:
            passage = reading.passages.get(number) if isinstance(number, int) else None
            if passage:
                backs += (
                    f"<li>Passage {number}: <q>{e(passage.quote)}</q> "
                    f'<span class="src">{e(source(passage))}</span></li>'
                )
            else:
                backs += f"<li>{e(str(number))}</li>"
        off = (
            '<p class="hint">This one names no cell on this page, so nothing is marked for it.</p>'
            if where in nowhere
            else '<p class="hint">Marked <span class="flag">!</span> on this page.</p>'
        )
        out += (
            f'<div class="dis"><p class="where">{e(where_words(where))} <code>{e(where)}</code></p>'
            f"<dl><dt>The reading says</dt><dd>{e(str(item.get('says') or ''))}</dd>"
            f"<dt>It should be</dt><dd>{e(str(item.get('should_be') or ''))}</dd>"
            f"<dt>Why</dt><dd>{e(str(item.get('why') or ''))}</dd></dl>"
            + (f'<ul class="backs">{backs}</ul>' if backs else "")
            + off
            + "</div>"
        )
    for title, key in (
        ("What it was unsure about", "unsure"),
        ("What it fixed in the file's form before checking", "format_fixes"),
    ):
        if verdict.get(key):
            items = "".join(f"<li>{e(str(item))}</li>" for item in verdict[key])
            out += f'<h4>{title}</h4><ul class="plain">{items}</ul>'
    if verdict.get("reason"):
        out += f"<h4>In its own words</h4><p>{e(str(verdict['reason']).strip())}</p>"
    return out


def inline(text: str) -> str:
    """Escaped text, with `code` and **bold** kept."""
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", e(text))
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)


def markdown(text: str) -> str:
    """Notes as simple markup: headings, paragraphs, flat lists, `code` and **bold**. No more."""
    out: list[str] = []
    para: list[str] = []
    items: list[list[str]] = []
    tag = "ul"

    def flush() -> None:
        nonlocal para, items
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
        if items:
            body = "".join(f"<li>{inline(' '.join(item))}</li>" for item in items)
            out.append(f"<{tag}>{body}</{tag}>")
        para, items = [], []

    blank = False
    for line in text.splitlines():
        if not line.strip():
            blank = True
            continue
        heading = re.match(r"(#+)\s+(.*)", line)
        item = re.match(r"\s*(-|\d+\.)\s+(.*)", line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 3, 6)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif item:
            kind = "ul" if item.group(1) == "-" else "ol"
            if para or (items and kind != tag):
                flush()
            tag = kind
            items.append([item.group(2).strip()])
        elif items and (line[0].isspace() or not blank):
            items[-1].append(line.strip())
        else:
            if items or blank:
                flush()
            para.append(line.strip())
        blank = False
    flush()
    return "".join(out)


def study_section(folder: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One study's screen, the data its evidence panel reads, and a line for the build report."""
    name = folder.name
    study = load(folder / "study.yaml")
    verdict = load(folder / "reading.verdict.yaml")
    notes_path = folder / "reading.notes.md"
    reading, problems = read_reading(yaml.safe_load((folder / "reading.yaml").read_text()))
    summary = str(study.get("summary") or "")
    head = f'<h2>{e(name)}</h2><p class="summary">{e(summary)}</p>'
    report: dict[str, Any] = {"study": name, "problems": problems}
    if reading is None or problems:
        listing = "".join(
            f"<li><code>{e(p['field'])}</code>: {e(p['reason'])}</li>" for p in problems
        )
        html = (
            f'<section class="study" id="{e(name)}" data-title="{e(summary)}">{head}'
            f'<p class="state no">This reading does not pass the format check, so it is not shown.</p>'
            f"<ul>{listing}</ul></section>"
        )
        return html, {"claims": [], "passages": {}, "dis": []}, report

    claims: list[dict[str, Any]] = []

    def add(entry: dict[str, Any]) -> int:
        claims.append(entry)
        return len(claims) - 1

    marked, nowhere = disputes(reading, verdict)
    read = ", ".join(READ[r] for r in reading.read)
    models = [f"Written by {reading.models.reading or 'a model not named'}"]
    models.append(f"probabilities by {reading.models.probabilities or 'no model yet'}")
    models.append(f"checked by {verdict.get('model') or 'no second model'}")
    notes = markdown(notes_path.read_text()) if notes_path.exists() else ""
    html = (
        f'<section class="study" id="{e(name)}" data-title="{e(summary)}">{head}'
        '<p class="banner">Written by a model, checked by a second model. Not checked by a person.</p>'
        f'<dl class="meta"><dt>What was read</dt><dd>{e(read)}.'
        + (f' <span class="detail">{e(reading.read_detail)}</span>' if reading.read_detail else "")
        + f"</dd><dt>Models</dt><dd>{e('; '.join(models))}.</dd></dl>"
        f"<section><h3>The design at a glance</h3>{design(reading, marked, add)}</section>"
        f"<section><h3>The samples</h3>{LEGEND}{sample_table(reading, marked, add)}</section>"
        f'<section><h3>What follows from the reading</h3><p class="hint">Worked out from the table '
        f"above each time the page is built. No file stores these.</p>"
        f'<div class="relations">{relation_lines(reading)}</div></section>'
        f'<section class="verdict"><h3>The second model\'s verdict</h3>'
        f"{verdict_block(reading, verdict, nowhere)}</section>"
        f'<section class="notes"><h3>What the format could not hold</h3>'
        '<p class="hint">The notes the writing model kept beside the reading, whole.</p>'
        + (notes or '<p class="none">No notes were kept.</p>')
        + "</section></section>"
    )
    data: dict[str, Any] = {
        "claims": claims,
        "passages": {
            str(number): {"quote": passage.quote, "source": source(passage)}
            for number, passage in reading.passages.items()
        },
        "dis": [
            {key: str(item.get(key) or "") for key in ("says", "should_be", "why")}
            for item in verdict.get("disagreements") or []
        ],
    }
    report |= {
        "samples": len(reading.samples),
        "claims_on_page": len(claims),
        "disagreements": len(data["dis"]),
        "disagreements_marking_no_cell": nowhere,
        "has_notes": bool(notes),
        "has_verdict": bool(verdict),
    }
    return html, data, report


LEGEND = (
    '<p class="legend"><span><b class="mk m-quoted">❝</b> quoted: a source says it</span>'
    '<span><b class="mk m-inferred">≈</b> worked out: no passage says it outright</span>'
    '<span><b class="mk m-unknown">?</b> not stated in what was read</span>'
    '<span><b class="mk m-na">–</b> does not apply to this sample</span>'
    '<span><span class="bar"><i style="width:70%"></i></span> the model\'s own probability that '
    f'the claim is right; under {LOW:g} is marked <b class="lowtag">low</b></span>'
    '<span><span class="flag">!</span> the second model disagrees</span>'
    "<span>Choose a cell to see the words behind it.</span></p>"
)


def main() -> None:
    """Build the page from the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder holding one subfolder per study")
    parser.add_argument("out", type=Path, help="the HTML file to write")
    parser.add_argument("--title", default="Five study readings")
    args = parser.parse_args()

    folders = sorted(p.parent for p in args.folder.glob("*/reading.yaml"))
    sections: list[str] = []
    data: dict[str, Any] = {}
    reports: list[dict[str, Any]] = []
    for folder in folders:
        html, entry, report = study_section(folder)
        sections.append(html)
        data[folder.name] = entry
        reports.append(report)
    tabs = "".join(f'<a class="tab" href="#{e(f.name)}">{e(f.name)}</a>' for f in folders)
    # The data sits in a script element as JSON; these three escapes keep record text from ending it.
    blob = json.dumps(data, ensure_ascii=False)
    blob = blob.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    slots = {"TITLE": e(args.title), "TABS": tabs, "STUDIES": "\n".join(sections), "DATA": blob}
    # One pass over the template alone, so a slot's name inside record text is never filled.
    template = (HERE / "template.html").read_text()
    page = re.sub(r"\{\{(\w+)\}\}", lambda found: slots[found.group(1)], template)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(
        json.dumps(
            {"out": str(args.out), "bytes": len(page.encode()), "studies": reports}, indent=1
        )
    )


if __name__ == "__main__":
    main()
