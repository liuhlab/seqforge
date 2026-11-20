"""PROTOTYPE, throwaway: draw each study of the standard set as one small picture.

Reads a folder holding one subfolder per study (<ID>/study.yaml, draft.yaml, verdict.yaml) and
writes one self-contained, view-only HTML page with two views of the same pictures: one study at
a time, and all of them as a gallery.

    python prototypes/standard_set_page/build_page.py FOLDER OUT.html [--lookup FILE] [--notice TEXT]

Titles, assays and sample counts are record text the study files never hold. They are looked up
when the page is built, from liulab-data's stores named by LABDATA_STORE and LABDATA_PAPERS. So
the page carries record text: write it outside the repo, or to a folder git ignores. `--lookup`
names a YAML file to read them from instead, which is how the made-up example page is built.
"""

from __future__ import annotations

import argparse
import textwrap
from html import escape
from pathlib import Path

import yaml

HERE = Path(__file__).parent
GEO = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="
DIRECTION = {"aging": "Aging", "neurodegeneration": "Neurodegeneration", "c_elegans": "C. elegans"}
SHAPE = {
    "recites_older_series": "paper reuses an older dataset",
    "paper_over_several_series": "one paper, several datasets",
    "superseries": "SuperSeries",
    "series_with_two_studies": "one dataset shared by two studies",
    "series_without_paper": "dataset with no paper",
}
# The picture's geometry, in SVG units: papers in a left column, Series in a right one.
WIDTH, PAPER_X, PAPER_W, SERIES_X, SERIES_W, GAP = 580, 4, 180, 240, 326, 14
MARK = '<span class="q">?</span>'


def load(path: Path) -> dict:
    """One YAML file, or an empty dict when it is not there."""
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def strs(items: object) -> list[str]:
    """A YAML list as strings, PMIDs without their prefix, duplicates dropped."""
    out: list[str] = []
    for item in items or []:
        text = str(item).strip()
        text = text[5:] if text.lower().startswith("pmid:") else text
        if text not in out:
            out.append(text)
    return out


def wrap(text: str, width: int, lines: int) -> list[str]:
    """Break text to fit a box, since SVG text does not wrap by itself."""
    return textwrap.wrap(text, width, max_lines=lines, placeholder="…") if text else []


def text_lines(lines: list[str], x: float, y: float, step: int, cls: str) -> str:
    """Stacked lines of SVG text, the first baseline at y."""
    return "".join(
        f'<text class="{cls}" x="{x}" y="{y + i * step:g}">{escape(line)}</text>'
        for i, line in enumerate(lines)
    )


def link(label: str, url: str, x: float, y: float, anchor: str = "end") -> str:
    """A small identifier that opens GEO or PubMed."""
    return (
        f'<a href="{escape(url)}" target="_blank" rel="noopener">'
        f'<text class="id" x="{x}" y="{y:g}" text-anchor="{anchor}">{escape(label)}</text></a>'
    )


def diagram(study: dict, lookup: dict, disputed: list[str]) -> str:
    """The picture of one study, as inline SVG."""
    records, bounds = study.get("records") or {}, study.get("bounds") or {}
    known = lookup.get("series") or {}
    inside, papers_in = strs(bounds.get("series")), strs(bounds.get("papers"))
    split = {str(k): strs(v) for k, v in (bounds.get("split_samples") or {}).items()}
    why_out = {strs([k])[0]: str(v) for k, v in (bounds.get("left_out") or {}).items()}
    superseries = str(records.get("superseries") or "")
    # A SuperSeries is the outline around its parts, never a box of its own.
    every = [g for g in strs(inside + strs(records.get("series"))) if g != superseries]
    members = strs((lookup.get("superseries_members") or {}).get(superseries)) or every

    # Right column: the Series. A SuperSeries' own sub-Series come first, so one outline holds them.
    series = []
    for gse in sorted(every, key=lambda g: not (superseries and g in members)):
        info = known.get(gse) or {}
        total, part = info.get("n_samples"), len(split.get(gse, [])) or None
        count = f"{part} of {total or '?'} samples" if part else f"{total} samples" if total else ""
        reason = why_out.get(gse, "")
        if reason and part:
            reason = "The rest: " + reason[0].lower() + reason[1:]
        box = {
            "id": gse,
            "in": gse in inside,
            "member": bool(superseries) and gse in members,
            "frac": (part / total if total else 0.5) if part else None,
            "full": info.get("title") or gse,
            "title": wrap(info.get("title") or f"GEO Series {gse}", 44, 2),
            "detail": " · ".join(x for x in (info.get("assay"), count) if x),
            "reason": wrap(reason, 56, 1),
            "pmids": strs(info["pmids"]) if "pmids" in info else None,
        }
        box["h"] = 16 * len(box["title"]) + 15 * len(box["reason"]) + 38
        series.append(box)

    # Left column: the papers, or one empty box when there is none.
    papers = []
    for pmid in strs(papers_in + strs(records.get("pmids"))):
        full = ((lookup.get("papers") or {}).get(pmid) or {}).get("title") or "Paper"
        reason = wrap(why_out.get(pmid, ""), 26, 2) if pmid not in papers_in else []
        lines = wrap(full, 21, 4)
        papers.append(
            {
                "id": pmid,
                "in": pmid in papers_in,
                "full": full,
                "title": lines,
                "reason": reason,
                "h": 15 * len(lines) + 13 * len(reason) + 42,
            }
        )
    if not papers:
        papers.append({"id": None, "in": False, "h": 56})

    # Stack both columns, then centre the shorter one against the taller.
    y = 0.0
    for i, box in enumerate(series):
        box["y"], y = y, y + box["h"] + GAP
        if box["member"] and (i + 1 == len(series) or not series[i + 1]["member"]):
            y += 12  # room for the bottom edge of the SuperSeries outline
    right_h = y - GAP
    left_h = sum(p["h"] for p in papers) + GAP * (len(papers) - 1)
    top = 32 if superseries else 8
    height = top + max(left_h, right_h) + 8
    for box in series:
        box["y"] += top + (max(left_h, right_h) - right_h) / 2
    y = top + (max(left_h, right_h) - left_h) / 2
    for paper in papers:
        paper["y"], y = y, y + paper["h"] + GAP

    label = escape(study.get("summary") or str(study.get("id")))
    out = [f'<svg viewBox="0 0 {WIDTH} {height:g}" role="img" aria-label="{label}">']

    # Lines first, so the boxes sit on top of them.
    for paper in papers:
        for box in series:
            if paper["id"] is None:
                cls = "none"
            elif (box["pmids"] is None and not paper["in"]) or (
                box["pmids"] is not None and paper["id"] not in box["pmids"]
            ):
                continue
            else:
                cls = "in" if paper["in"] and box["in"] else "out"
            x1, y1 = PAPER_X + PAPER_W, paper["y"] + paper["h"] / 2
            x2, y2 = SERIES_X, box["y"] + box["h"] / 2
            mid = (x1 + x2) / 2
            out.append(
                f'<path class="link {cls}" d="M{x1} {y1:g} C{mid} {y1:g} {mid} {y2:g} {x2} {y2:g}"/>'
            )

    inner = [b for b in series if b["member"]]
    if inner:
        first, last = inner[0], inner[-1]
        out.append(
            f'<rect class="ss" x="{SERIES_X - 10}" y="{first["y"] - 26:g}" width="{SERIES_W + 20}" '
            f'height="{last["y"] + last["h"] - first["y"] + 36:g}" rx="12"/>'
            f'<text class="sslabel" x="{SERIES_X}" y="{first["y"] - 9:g}">SuperSeries</text>'
            + link(superseries, GEO + superseries, SERIES_X + SERIES_W, first["y"] - 9)
        )

    for paper in papers:
        x, y, h, w = PAPER_X, paper["y"], paper["h"], PAPER_W
        if paper["id"] is None:
            out.append(
                f'<g class="out"><rect class="box" x="{x}" y="{y:g}" width="{w}" height="{h}" rx="3"/>'
                f'<text class="d" x="{x + w / 2}" y="{y + h / 2 + 4:g}" text-anchor="middle">No paper</text></g>'
            )
            continue
        doi = paper["id"].startswith("10.")
        url = (
            f"https://doi.org/{paper['id']}"
            if doi
            else f"https://pubmed.ncbi.nlm.nih.gov/{paper['id']}/"
        )
        after = y + 22 + 15 * len(paper["title"])
        out.append(  # a sheet with a folded corner, so a paper needs no label
            f'<g class="{"in" if paper["in"] else "out"}"><title>{escape(paper["full"])}</title>'
            f'<path class="box paper" d="M{x} {y:g}H{x + w - 14}L{x + w} {y + 14:g}V{y + h:g}H{x}Z"/>'
            f'<path class="fold" d="M{x + w - 14} {y:g}V{y + 14:g}H{x + w}"/>'
            + text_lines(paper["title"], x + 12, y + 22, 15, "t")
            + text_lines(paper["reason"], x + 12, after + 2, 13, "r")
            + link(paper["id"] if doi else f"PMID {paper['id']}", url, x + 12, y + h - 10, "start")
            + "</g>"
        )
        if paper["id"] in disputed:
            out.append(
                f'<g class="mark"><title>The two models disagreed about this paper</title>'
                f'<circle cx="{x + 2}" cy="{y + 2:g}" r="9"/>'
                f'<text x="{x + 2}" y="{y + 6:g}" text-anchor="middle">?</text></g>'
            )

    for n, box in enumerate(series):
        x, y, h, w = SERIES_X, box["y"], box["h"], SERIES_W
        detail_y = y + 23 + 16 * len(box["title"])
        shape = f'<rect class="box" x="{x}" y="{y:g}" width="{w}" height="{h}" rx="8"/>'
        if box["frac"] is not None:  # split between two studies: filled only as far as its share
            clip = f"clip-{escape(str(study.get('id')))}-{n}"
            shape = (
                f'<clipPath id="{clip}"><rect x="{x}" y="{y:g}" width="{w}" height="{h}" rx="8"/></clipPath>'
                f'<rect class="rest" x="{x}" y="{y:g}" width="{w}" height="{h}" rx="8"/>'
                f'<rect class="share" x="{x}" y="{y:g}" width="{w * box["frac"]:g}" height="{h}" clip-path="url(#{clip})"/>'
                f'<rect class="box edge" x="{x}" y="{y:g}" width="{w}" height="{h}" rx="8"/>'
            )
        out.append(
            f'<g class="{"in" if box["in"] else "out"}"><title>{escape(box["full"])}</title>{shape}'
            + text_lines(box["title"], x + 14, y + 22, 16, "t")
            + f'<text class="d" x="{x + 14}" y="{detail_y:g}">{escape(box["detail"])}</text>'
            + link(box["id"], GEO + box["id"], x + w - 14, detail_y)
            + text_lines(box["reason"], x + 14, detail_y + 16, 15, "r")
            + "</g>"
        )
        if box["id"] in disputed:
            out.append(
                f'<g class="mark"><title>The two models disagreed about this dataset</title>'
                f'<circle cx="{x + w - 2}" cy="{y + 2:g}" r="9"/>'
                f'<text x="{x + w - 2}" y="{y + 6:g}" text-anchor="middle">?</text></g>'
            )
    return "".join(out) + "</svg>"


def flag(study: dict) -> tuple[str, str, list[str]]:
    """('differ' | 'unsure' | '', one short sentence as HTML, the Series the two models disagreed about)."""
    draft, verdict = study.get("draft") or {}, study.get("verdict") or {}
    if verdict.get("agrees") is False:
        first, second = draft.get("bounds") or {}, verdict.get("bounds") or {}
        had, has = strs(first.get("series")), strs(second.get("series"))
        said = {}
        for gse in strs(had + has):
            # An empty selection means the whole Series, so the two are compared as sets.
            mine = set(strs((first.get("split_samples") or {}).get(gse)))
            theirs = set(strs((second.get("split_samples") or {}).get(gse)))
            if gse in had and gse not in has:
                said[gse] = f"only the first put in the dataset marked {MARK}"
            elif gse in has and gse not in had:
                said[gse] = f"only the checking model put in the dataset marked {MARK}"
            elif mine == theirs:
                continue
            elif not mine:
                said[gse] = (
                    f"the first took all of the dataset marked {MARK}, "
                    f"the checking model only {len(theirs)} samples"
                )
            elif not theirs:
                said[gse] = (
                    f"the first took only {len(mine)} samples of the dataset marked {MARK}, "
                    "the checking model all of it"
                )
            else:
                said[gse] = f"the two took different samples of the dataset marked {MARK}"
        papers = sorted(set(strs(first.get("papers"))) ^ set(strs(second.get("papers"))))
        if len(said) == 1 and not papers:
            what = f": {next(iter(said.values()))}"
        elif said:
            what = f" about the {len(said) + len(papers)} records marked {MARK}"
        elif len(papers) == 1:
            what = f" about whether the paper marked {MARK} belongs"
        elif papers:
            what = f" about whether the {len(papers)} papers marked {MARK} belong"
        else:
            what = ", though they named the same datasets and papers"
        text = (
            f"<b>The two models disagreed</b>{what}; the picture shows the checking model's choice."
        )
        if verdict.get("unsure") or study.get("status") == "unsure":
            text += " <b>The checking model was not sure</b> of its own choice either."
        return "differ", text, [*said, *papers]
    if verdict.get("unsure") or study.get("status") == "unsure":
        return "unsure", "<b>The checking model was not sure</b> about this study.", []
    return "", "", []


def card(study: dict, lookup: dict) -> tuple[int, str]:
    """One study's card and its place in the order: unsettled studies first."""
    kind, sentence, disputed = flag(study)
    sid = escape(str(study["id"]))
    tags = [DIRECTION.get(study.get("direction"), study.get("direction"))]
    shape = study.get("hard_shape")
    if shape == "series_with_two_studies" and not (study.get("bounds") or {}).get("split_samples"):
        # Picked on the sign of two studies in one dataset; drawn, it turned out to be one.
        tags.append("one dataset, several papers")
    elif shape:
        tags.append(SHAPE.get(shape, shape))
    if "stand-in" in str((study.get("verdict") or {}).get("model")):
        tags.append("written by hand, not by the models")
    note = ""
    if sentence:
        reason = escape(str((study.get("verdict") or {}).get("reason") or "").strip())
        why = f"<details><summary>Why</summary>{reason}</details>" if reason else ""
        note = f'<div class="note"><p>{sentence}</p>{why}</div>'
    html = (
        f'<article class="card {kind}" id="{sid}" data-kind="{kind}">'
        f'<h2>{escape(study.get("summary") or str(study["id"]))}</h2><div class="tags">'
        + "".join(f"<span>{escape(str(tag))}</span>" for tag in tags if tag)
        + f"<code>{sid}</code></div>"
        + diagram(study, lookup, disputed)
        + note
        + "</article>"
    )
    return {"differ": 0, "unsure": 1}.get(kind, 2), html


def assay(libraries: list[str]) -> str:
    """A Series' assay in a few words, from its samples' library strategy and source."""
    names = []
    for library in libraries:
        strategy, _, source = library.partition(" / ")
        strategy = {"RNA-Seq": "RNA-seq", "ATAC-seq": "ATAC-seq", "ChIP-Seq": "ChIP-seq"}.get(
            strategy, strategy
        )
        name = f"single-cell {strategy}" if "single cell" in source else strategy
        if name and name not in names:
            names.append(name)
    return " + ".join(names)


def store_lookup(studies: list[dict]) -> dict:
    """Titles, assays, sample counts and record links of every record the studies name, from the stores.

    A record the store does not hold is left out, and its box shows the identifier alone.
    """
    from seqforge.standard_set.material import open_sources, read_group

    store, papers = open_sources()
    lookup: dict = {"series": {}, "superseries_members": {}, "papers": {}}
    for study in studies:
        records = study.get("records") or {}
        entry = {
            "id": study["id"],
            "linked_series": strs(records.get("series")),
            "pmids": strs(records.get("pmids")),
        }
        group = read_group(store, papers, entry)
        for s in group["series"]:
            if not s["held"]:
                continue
            lookup["series"][s["id"]] = {
                "title": " ".join(s["fields"]["Title"]),
                "assay": assay([sample["library"] for sample in s["samples"]]),
                "n_samples": len(s["samples"]),
                "pmids": s["pmids"],
            }
            if s["superseries_of"]:
                lookup["superseries_members"][s["id"]] = s["superseries_of"]
        for paper in group["papers"]:
            if paper["title"]:
                lookup["papers"][paper["pmid"]] = {"title": paper["title"].rstrip(".")}
    return lookup


def main() -> None:
    """Write the page."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder with one subfolder per study")
    parser.add_argument("out", type=Path, help="the HTML file to write")
    parser.add_argument(
        "--lookup",
        type=Path,
        help="YAML of titles, assays, sample counts and record links, read instead of the stores",
    )
    parser.add_argument("--title", default="The standard set of studies")
    parser.add_argument(
        "--notice", default="", help="one line across the top, e.g. that the data is made up"
    )
    args = parser.parse_args()

    studies = []
    for path in sorted(args.folder.glob("*/study.yaml")):
        study = load(path)
        study.setdefault("id", path.parent.name)
        study["draft"] = load(path.with_name("draft.yaml"))
        study["verdict"] = load(path.with_name("verdict.yaml"))
        studies.append(study)
    lookup = load(args.lookup) if args.lookup else store_lookup(studies)
    cards = [(*card(study, lookup), str(study["id"])) for study in studies]
    cards.sort(key=lambda c: (c[0], c[2]))

    page = (HERE / "template.html").read_text()
    for slot, value in {
        "{{TITLE}}": escape(args.title),
        "{{NOTICE}}": f'<div class="notice">{escape(args.notice)}</div>' if args.notice else "",
        "{{CARDS}}": "\n".join(c[1] for c in cards),
    }.items():
        page = page.replace(slot, value)
    args.out.write_text(page)
    print(f"wrote {args.out}: {len(cards)} studies, {len(page) // 1024} KB")


if __name__ == "__main__":
    main()
