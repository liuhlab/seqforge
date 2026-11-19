"""PROTOTYPE, throwaway: draw each study of the standard set as one small picture.

Reads a folder of study files (<ID>.yaml, with <ID>.draft.yaml and <ID>.verdict.yaml beside it) and
writes one self-contained, view-only HTML page: a card per study, the paper on the left, its GEO
Series on the right.

    python prototypes/standard_set_page/build_page.py FOLDER OUT.html [--lookup FILE] [--notice TEXT]

Titles, assays and sample counts are record text the study files never hold. `--lookup` names a
YAML file that stands in for reading them from the record store at build time.
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
WIDTH, PAPER_X, PAPER_W, SERIES_X, SERIES_W = 580, 4, 178, 238, 328


def load(path: Path) -> dict:
    """One YAML file, or an empty dict when it is not there."""
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def strs(items: object) -> list[str]:
    """A YAML list as strings, PMIDs without their prefix, duplicates dropped."""
    out: list[str] = []
    for item in items or []:
        text = str(item).removeprefix("PMID:").strip()
        if text not in out:
            out.append(text)
    return out


def wrap(text: str, width: int, lines: int) -> list[str]:
    """Break a title to fit a box, since SVG text does not wrap by itself."""
    return textwrap.wrap(text, width, max_lines=lines, placeholder="…")


def paper_link(paper: str) -> tuple[str, str]:
    """The small label and the outside link of one paper identifier."""
    if paper.startswith("10."):
        return paper, f"https://doi.org/{paper}"
    return f"PMID {paper}", f"https://pubmed.ncbi.nlm.nih.gov/{paper}/"


def text_lines(lines: list[str], x: float, y: float, step: int, cls: str) -> str:
    """Stacked lines of SVG text, the first baseline at y."""
    return "".join(
        f'<text class="{cls}" x="{x}" y="{y + i * step}">{escape(line)}</text>'
        for i, line in enumerate(lines)
    )


def link(label: str, url: str, x: float, y: float, anchor: str = "end") -> str:
    """A small identifier that opens GEO or PubMed."""
    return (
        f'<a href="{escape(url)}" target="_blank" rel="noopener">'
        f'<text class="id" x="{x}" y="{y}" text-anchor="{anchor}">{escape(label)}</text></a>'
    )


def diagram(study: dict, lookup: dict, disputed: list[str]) -> str:
    """The picture of one study, as inline SVG."""
    records, bounds = study.get("records") or {}, study.get("bounds") or {}
    known = lookup.get("series") or {}
    inside = strs(bounds.get("series"))
    split = {str(k): strs(v) for k, v in (bounds.get("split_samples") or {}).items()}
    why_out = {str(k): str(v) for k, v in (bounds.get("left_out") or {}).items()}
    papers_in = strs(bounds.get("papers"))

    # Right column: the Series, the ones in the study first.
    series = []
    for gse in strs(inside + strs(records.get("series"))):
        info = known.get(gse) or {}
        total, part = info.get("n_samples"), len(split.get(gse, [])) or None
        count = (
            f"{part} of {total or '?'} samples" if part else (f"{total} samples" if total else "")
        )
        reason = why_out.get(gse, "")
        box = {
            "id": gse,
            "in": gse in inside,
            "frac": (part / total if total else 0.5) if part else None,
            "title": wrap(info.get("title") or f"GEO Series {gse}", 46, 2),
            "detail": " · ".join(x for x in (info.get("assay"), count) if x),
            "reason": wrap(("The rest: " if part else "") + reason, 56, 2) if reason else [],
            "pmids": strs(info["pmids"]) if "pmids" in info else None,
        }
        box["h"] = 16 * len(box["title"]) + 14 * len(box["reason"]) + 36
        series.append(box)

    # Left column: the papers, or one empty box when there is none.
    papers = []
    for pmid in strs(papers_in + strs(records.get("pmids"))):
        title = ((lookup.get("papers") or {}).get(pmid) or {}).get("title") or "Paper"
        reason = wrap(why_out.get(pmid, ""), 27, 4) if pmid not in papers_in else []
        lines = wrap(title, 22, 5)
        height = 15 * len(lines) + 13 * len(reason) + 46
        papers.append(
            {"id": pmid, "in": pmid in papers_in, "title": lines, "reason": reason, "h": height}
        )
    if not papers:
        papers.append({"id": None, "in": False, "title": [], "reason": [], "h": 56})

    superseries = records.get("superseries")
    top = 30 if superseries else 6
    right_h = sum(b["h"] for b in series) + 10 * (len(series) - 1)
    left_h = sum(p["h"] for p in papers) + 14 * (len(papers) - 1)
    height = top + max(left_h, right_h) + (14 if superseries else 6)
    y = top + (max(left_h, right_h) - right_h) / 2
    for box in series:
        box["y"], y = y, y + box["h"] + 10
    y = top + (max(left_h, right_h) - left_h) / 2
    for paper in papers:
        paper["y"], y = y, y + paper["h"] + 14

    out = [
        f'<svg viewBox="0 0 {WIDTH} {height:g}" role="img" aria-label="{escape(study.get("summary") or str(study.get("id")))}">'
    ]

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

    if superseries and series:
        first, last = series[0], series[-1]
        out.append(
            f'<rect class="ss" x="{SERIES_X - 10}" y="{first["y"] - 24:g}" width="{SERIES_W + 20}" '
            f'height="{last["y"] + last["h"] - first["y"] + 34:g}" rx="12"/>'
            f'<text class="sslabel" x="{SERIES_X}" y="{first["y"] - 8:g}">SuperSeries: GEO lists these together</text>'
            + link(str(superseries), GEO + str(superseries), SERIES_X + SERIES_W, first["y"] - 8)
        )

    for paper in papers:
        x, y, h = PAPER_X, paper["y"], paper["h"]
        if paper["id"] is None:
            out.append(
                f'<g class="out"><rect class="box nopaper" x="{x}" y="{y:g}" width="{PAPER_W}" height="{h}" rx="8"/>'
                f'<text class="d" x="{x + PAPER_W / 2}" y="{y + h / 2 + 4:g}" text-anchor="middle">No paper</text></g>'
            )
            continue
        label, url = paper_link(paper["id"])
        title_end = y + 34 + 15 * len(paper["title"])
        out.append(
            f'<g class="{"in" if paper["in"] else "out"}"><title>{escape(" ".join(paper["title"]))}</title>'
            f'<rect class="box paper" x="{x}" y="{y:g}" width="{PAPER_W}" height="{h}" rx="8"/>'
            f'<text class="kind" x="{x + 12}" y="{y + 17:g}">PAPER</text>'
            + link(label, url, x + PAPER_W - 12, y + 17)
            + text_lines(paper["title"], x + 12, y + 37, 15, "t")
            + text_lines(paper["reason"], x + 12, title_end + 8, 13, "r")
            + "</g>"
        )

    for n, box in enumerate(series):
        x, y, h = SERIES_X, box["y"], box["h"]
        detail_y = y + 22 + 16 * len(box["title"])
        shape = f'<rect class="box" x="{x}" y="{y:g}" width="{SERIES_W}" height="{h}" rx="8"/>'
        if (
            box["frac"] is not None
        ):  # a Series split between two studies: filled only as far as its share
            clip, cut = f"clip-{escape(str(study.get('id')))}-{n}", x + SERIES_W * box["frac"]
            shape = (
                f'<clipPath id="{clip}"><rect x="{x}" y="{y:g}" width="{SERIES_W}" height="{h}" rx="8"/></clipPath>'
                f'<rect class="rest" x="{x}" y="{y:g}" width="{SERIES_W}" height="{h}" rx="8"/>'
                f'<rect class="share" x="{x}" y="{y:g}" width="{cut - x:g}" height="{h}" clip-path="url(#{clip})"/>'
                f'<rect class="box edge" x="{x}" y="{y:g}" width="{SERIES_W}" height="{h}" rx="8"/>'
            )
        out.append(
            f'<g class="{"in" if box["in"] else "out"}">{shape}'
            + text_lines(box["title"], x + 12, y + 21, 16, "t")
            + f'<text class="d" x="{x + 12}" y="{detail_y:g}">{escape(box["detail"])}</text>'
            + link(box["id"], GEO + box["id"], x + SERIES_W - 12, detail_y)
            + text_lines(box["reason"], x + 12, detail_y + 16, 14, "r")
            + "</g>"
        )
        if box["id"] in disputed:
            cx = x + SERIES_W - 2
            out.append(
                f'<g class="mark"><title>The two models disagreed about this dataset</title>'
                f'<circle cx="{cx}" cy="{y + 2:g}" r="9"/><text x="{cx}" y="{y + 6:g}" text-anchor="middle">?</text></g>'
            )
    return "".join(out) + "</svg>"


def flag(study: dict, lookup: dict) -> tuple[str, str, list[str]]:
    """('differ' | 'unsure' | '', one plain sentence as HTML, the Series the two models disagreed about)."""
    draft, verdict = study.get("draft") or {}, study.get("verdict") or {}
    reason = escape(str(verdict.get("reason") or "").strip())
    known = lookup.get("series") or {}

    def name(gse: str) -> str:
        return escape(f"“{(known.get(gse) or {}).get('title') or gse}”")

    if verdict.get("agrees") is False:
        first, second = draft.get("bounds") or {}, verdict.get("bounds") or {}
        had, has = strs(first.get("series")), strs(second.get("series"))
        parts, disputed = [], []
        for gse in strs(had + has):
            share = len(strs((second.get("split_samples") or {}).get(gse)))
            if gse in had and gse not in has:
                parts.append(f"{name(gse)} (only the first put it in)")
            elif gse in has and gse not in had:
                parts.append(f"{name(gse)} (only the checking model put it in)")
            elif share and not (first.get("split_samples") or {}).get(gse):
                parts.append(
                    f"{name(gse)} (the first took all of it, the checking model {share} samples)"
                )
            else:
                continue
            disputed.append(gse)
        what = " and ".join(parts) or "which paper belongs"
        also = " It was also not sure." if verdict.get("unsure") else ""
        text = f"<b>The two models disagreed</b> about {what}. The picture follows the checking model.{also} {reason}"
        return "differ", text, disputed
    if verdict.get("unsure") or study.get("status") == "unsure":
        return "unsure", f"<b>The checking model was not sure.</b> {reason}", []
    return "", "", []


def card(study: dict, lookup: dict) -> tuple[int, str]:
    """One study's card and its place in the order: unsettled studies first."""
    kind, sentence, disputed = flag(study, lookup)
    tags = [DIRECTION.get(study.get("direction"), study.get("direction"))]
    if study.get("hard_shape"):
        tags.append(SHAPE.get(study["hard_shape"], study["hard_shape"]))
    html = (
        f'<article class="card {kind}" id="{escape(str(study["id"]))}"><header>'
        f'<h2>{escape(study.get("summary") or str(study["id"]))}</h2><div class="tags">'
        + "".join(f"<span>{escape(str(tag))}</span>" for tag in tags if tag)
        + f"<code>{escape(str(study['id']))}</code></div></header>"
        + diagram(study, lookup, disputed)
        + (f'<p class="note">{sentence}</p>' if sentence else "")
        + "</article>"
    )
    return {"differ": 0, "unsure": 1}.get(kind, 2), html


def main() -> None:
    """Write the page."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder of study files")
    parser.add_argument("out", type=Path, help="the HTML file to write")
    parser.add_argument(
        "--lookup", type=Path, help="YAML of titles, assays, sample counts and paper titles"
    )
    parser.add_argument("--title", default="The standard set of studies")
    parser.add_argument(
        "--notice", default="", help="one line across the top, e.g. that the data is made up"
    )
    args = parser.parse_args()

    lookup = load(args.lookup) if args.lookup else {}
    cards = []
    for path in sorted(args.folder.glob("*.yaml")):
        if path.name.startswith("_") or path.stem.rsplit(".", 1)[-1] in (
            "draft",
            "verdict",
            "answer",
        ):
            continue
        study = load(path)
        study.setdefault("id", path.stem)
        study["draft"] = load(args.folder / f"{path.stem}.draft.yaml")
        study["verdict"] = load(args.folder / f"{path.stem}.verdict.yaml")
        cards.append((*card(study, lookup), str(study["id"])))
    cards.sort(key=lambda c: (c[0], c[2]))
    unsettled = sum(1 for c in cards if c[0] < 2)

    count = f"{len(cards)} studies"
    if unsettled:
        count += f", {unsettled} with a coloured edge shown first: the two models disagreed there, or one was not sure"
    page = (HERE / "template.html").read_text()
    for slot, value in {
        "{{TITLE}}": escape(args.title),
        "{{NOTICE}}": f'<div class="notice">{escape(args.notice)}</div>' if args.notice else "",
        "{{COUNT}}": escape(count),
        "{{CARDS}}": "\n".join(c[1] for c in cards),
    }.items():
        page = page.replace(slot, value)
    args.out.write_text(page)
    print(f"wrote {args.out}: {len(cards)} studies, {len(page) // 1024} KB")


if __name__ == "__main__":
    main()
