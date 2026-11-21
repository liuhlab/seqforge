#!/usr/bin/env python
"""Gather what the full store links to each standard-set study, next to the pick's group.

    python -B gather_linked_groups.py --store <store root> --papers <paper store root> \
        --set-dir evals/standard-set --out results.json --summary summary.tsv

A one-off measurement. It runs in liulab-data's own environment, not seqforge's, and reads the
store only through ``labdata``: ``BuildReader.links`` for every link and ``PaperReader.papers`` for
each paper's abstract and files. It writes nothing in either store. DuckDB's spill folder follows
``XDG_CACHE_HOME``, so set that to a scratch folder.

For each study's Series (the folder names under ``--set-dir``) it follows four kinds of link:

- ``superseries``: a SuperSeries and its SubSeries. One hop from a Series reaches its
  SuperSeries, that SuperSeries' other SubSeries, and its own SubSeries.
- ``bioproject``: the Series that share a BioProject.
- ``umbrella``: the Series of the BioProjects one umbrella step away: the umbrella over the
  Series' BioProject and that umbrella's other BioProjects, or the BioProjects under it.
- ``paper``: the Series that link the same paper by PMID or DOI. A placeholder PMID is dropped,
  as liulab-data drops it.

A **rule** is a set of kinds and a depth: each kind alone at one hop and at two, all four at one
and at two, and all four until no Series is added (``closure``), which is what the pick's linked
group was. A paper, BioProject or SuperSeries is a **hub**; one linked from more Series than
``--hub-cap`` is counted and reported but not crossed, and a hop that would start from more than
``--frontier-cap`` Series keeps the first ones and says so. A BioProject's own paper links are
recorded and tie nothing, as in the pick.

It writes one JSON file (provenance, one object per study with each rule's group, and one per
paper) and a short TSV with one row per rule. Each output is written whole or not at all, so a
rerun after a kill starts over and leaves no half file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import resource
import statistics
import sys
import time
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from labdata import __version__ as labdata_version
from labdata import open_store
from labdata.papers.reader import PaperReader
from labdata.papers.root import is_placeholder, paper_key
from labdata.store.layout import sql_string
from labdata.store.reader import BuildReader

KINDS = ("superseries", "bioproject", "umbrella", "paper")
SUPER, PROJECT, UMBRELLA, REANALYZED = (
    "SuperSeries of",
    "BioProject",
    "umbrella of",
    "Reanalyzed by",
)
PAPER_RELATIONS = ("PubMed", "DOI")
SERIES_RELATIONS = (SUPER, PROJECT, *PAPER_RELATIONS, REANALYZED)
FULL_TEXT_KINDS = ("xml", "pdf")
#: (name, kinds, depth); a depth of None means until no Series is added.
RULES: tuple[tuple[str, tuple[str, ...], int | None], ...] = (
    *((f"{k}-{d}", (k,), d) for k in KINDS for d in (1, 2)),
    ("all-1", KINDS, 1),
    ("all-2", KINDS, 2),
    ("all-closure", KINDS, None),
)

HubKey = tuple[str, str]


@dataclass
class SeriesLinks:
    """What one Series links, read once."""

    parents: set[str] = field(default_factory=set)
    children: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    papers: dict[str, set[str]] = field(default_factory=dict)
    reanalysis: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class Hub:
    """A node that ties Series: its fan-out in Series, and its members unless it was not crossed."""

    fanout: int
    members: frozenset[str] | None


class Graph:
    """The links of the Series and hubs met, read in batches through liulab-data's reader."""

    def __init__(self, reader: BuildReader, hub_cap: int) -> None:
        self.reader = reader
        self.hub_cap = hub_cap
        self.series: dict[str, SeriesLinks] = {}
        self.hubs: dict[HubKey, Hub] = {}
        self.up: dict[str, set[str]] = {}
        self.down: dict[str, set[str]] = {}
        self.project_papers: dict[str, set[str]] = {}
        self.queries = 0

    def load_series(self, accessions: Iterable[str]) -> None:
        """Read the links of each Series not read yet, in one lookup."""
        todo = {a for a in accessions if a not in self.series}
        if not todo:
            return
        for a in todo:
            self.series[a] = SeriesLinks()
        self.queries += 1
        for src, dst, relation, source in self.reader.links(todo, SERIES_RELATIONS):
            if relation == SUPER:
                if dst in todo:
                    self.series[dst].parents.add(src)
                if src in todo:
                    self.series[src].children.add(dst)
            elif relation == PROJECT and src in todo:
                self.series[src].projects.add(dst)
            elif relation in PAPER_RELATIONS and src in todo:
                self.series[src].papers.setdefault(dst, set()).add(source)
            elif relation == REANALYZED:
                if src in todo:
                    self.series[src].reanalysis.add(("reanalyzed by", dst))
                if dst in todo:
                    self.series[dst].reanalysis.add(("reanalysis of", src))
        for a in todo:
            links = self.series[a]
            links.papers = {k: s for k, s in links.papers.items() if not _placeholder(k, s)}
        self.load_projects({p for a in todo for p in self.series[a].projects})

    def load_projects(self, projects: Iterable[str]) -> None:
        """Read each BioProject's umbrella links and its own paper links."""
        todo = {p for p in projects if p not in self.up}
        if not todo:
            return
        for p in todo:
            self.up[p], self.down[p], self.project_papers[p] = set(), set(), set()
        self.queries += 1
        papers: dict[tuple[str, str], set[str]] = {}
        for src, dst, relation, source in self.reader.links(todo, (UMBRELLA, *PAPER_RELATIONS)):
            if relation == UMBRELLA:
                if src in todo:
                    self.down[src].add(dst)
                if dst in todo:
                    self.up[dst].add(src)
            elif src in todo:
                papers.setdefault((src, dst), set()).add(source)
        for (project, key), sources in papers.items():
            if not _placeholder(key, sources):
                self.project_papers[project].add(key)

    def hub_keys(self, accession: str, kinds: Iterable[str]) -> list[HubKey]:
        """Return the hubs one hop of ``kinds`` crosses from a Series whose links are read."""
        links = self.series[accession]
        keys: list[HubKey] = []
        for kind in kinds:
            if kind == "superseries":
                keys += [(kind, p) for p in sorted(links.parents)]
                keys += [(kind, accession)] if links.children else []
            elif kind == "bioproject":
                keys += [(kind, p) for p in sorted(links.projects)]
            elif kind == "umbrella":
                for p in sorted(links.projects):
                    keys += [(kind, u) for u in sorted(self.up.get(p, ()))]
                    keys += [(kind, p)] if self.down.get(p) else []
            else:
                keys += [(kind, k) for k in sorted(links.papers)]
        return keys

    def load_hubs(self, keys: Iterable[HubKey]) -> None:
        """Count each new hub's fan-out, and read its members when it is under the cap."""
        todo = {k for k in keys if k not in self.hubs}
        by_kind = {kind: sorted(n for k, n in todo if k == kind) for kind in KINDS}
        umbrellas = by_kind["umbrella"]
        if not todo:
            return
        self.load_projects(umbrellas)
        plain = {
            "superseries": ("links_by_from.parquet", "from", "to", (SUPER,)),
            "bioproject": ("links_by_to.parquet", "to", "from", (PROJECT,)),
            "paper": ("links_by_to.parquet", "to", "from", PAPER_RELATIONS),
        }
        for kind, (table, end, far, relations) in plain.items():
            self._load_plain(kind, by_kind[kind], table, end, far, relations)
        # An umbrella's members are the Series of every BioProject under it, and its own.
        under = {u: sorted({u} | self.down.get(u, set())) for u in umbrellas}
        crossed = [u for u in umbrellas if len(under[u]) <= self.hub_cap]
        projects = sorted({p for u in crossed for p in under[u]})
        if projects:
            self.load_hubs([("bioproject", p) for p in projects])
        for u in umbrellas:
            if u not in crossed:
                self.hubs[("umbrella", u)] = Hub(len(under[u]), None)
                continue
            parts = [self.hubs[("bioproject", p)] for p in under[u]]
            whole = all(h.members is not None for h in parts)
            members = frozenset(m for h in parts for m in (h.members or ()))
            fanout = sum(h.fanout for h in parts)
            self.hubs[("umbrella", u)] = Hub(fanout, members if whole else None)

    def _load_plain(
        self,
        kind: str,
        nodes: Sequence[str],
        table: str,
        end: str,
        far: str,
        relations: Sequence[str],
    ) -> None:
        if not nodes:
            return
        rels = ", ".join(sql_string(r) for r in relations)
        node_list = ", ".join(sql_string(n) for n in nodes)
        self.queries += 1
        counts = dict(
            self.reader.connection.execute(
                f'SELECT "{end}", count(DISTINCT "{far}") '
                f"FROM read_parquet({sql_string(str(self.reader.path / table))}) "
                f'WHERE "{end}" IN ({node_list}) AND relation IN ({rels}) '
                f"AND \"{far}\" LIKE 'GSE%' GROUP BY 1"
            ).fetchall()
        )
        crossed = {n for n in nodes if counts.get(n, 0) <= self.hub_cap}
        members: dict[str, set[str]] = {n: set() for n in crossed}
        if crossed:
            self.queries += 1
            for src, dst, _relation, _source in self.reader.links(crossed, relations):
                hub, other = (src, dst) if end == "from" else (dst, src)
                if hub in crossed and other.startswith("GSE"):
                    members[hub].add(other)
        for n in nodes:
            if n in crossed:
                own = {n} if kind == "superseries" else set()
                self.hubs[(kind, n)] = Hub(int(counts.get(n, 0)), frozenset(members[n] | own))
            else:
                self.hubs[(kind, n)] = Hub(int(counts[n]), None)


def _placeholder(target: str, sources: set[str]) -> bool:
    try:
        return is_placeholder(paper_key(target), sources)
    except ValueError:
        return True


def gather(
    graph: Graph, seed: str, kinds: Sequence[str], depth: int | None, args: argparse.Namespace
) -> dict[str, Any]:
    """Follow ``kinds`` from ``seed`` for ``depth`` hops and return the group and its ties."""
    limit = depth if depth is not None else args.closure_depth
    reached = {seed: 0}
    ties: list[dict[str, Any]] = []
    met: dict[HubKey, Hub] = {}
    frontier, frontier_cut, hop = [seed], False, 0
    while frontier and hop < limit:
        hop += 1
        if len(frontier) > args.frontier_cap:
            frontier, frontier_cut = frontier[: args.frontier_cap], True
        graph.load_series(frontier)
        keys = {s: graph.hub_keys(s, kinds) for s in frontier}
        graph.load_hubs(k for ks in keys.values() for k in ks)
        nxt: set[str] = set()
        for s in frontier:
            for key in keys[s]:
                hub = met[key] = graph.hubs[key]
                for m in sorted(hub.members or ()):
                    if m == s or reached.get(m, hop) < hop:
                        continue
                    if m not in reached:
                        reached[m] = hop
                        nxt.add(m)
                    ties.append({"series": m, "hop": hop, "kind": key[0], "via": key[1], "from": s})
        frontier = sorted(nxt)
    graph.load_series(reached)
    series = sorted(reached, key=_number)
    papers = sorted({p for s in series for p in graph.series[s].papers})
    projects = sorted({p for s in series for p in graph.series[s].projects})
    fanout: dict[str, dict[str, Any]] = {}
    for (kind, node), hub in met.items():
        if hub.fanout > fanout.get(kind, {}).get("fanout", -1):
            fanout[kind] = {"node": node, "fanout": hub.fanout}
    return {
        "series": series,
        "n_series": len(series),
        "papers": papers,
        "n_papers": len(papers),
        "bioprojects": projects,
        "bioproject_papers": sorted(
            {k for p in projects for k in graph.project_papers.get(p, ())} - set(papers)
        ),
        "ties": ties,
        "tie_kinds": sorted({t["kind"] for t in ties}),
        "largest_fanout": fanout,
        "not_crossed": sorted(
            (
                {"kind": k, "node": n, "fanout": h.fanout}
                for (k, n), h in met.items()
                if h.members is None
            ),
            key=lambda d: (-d["fanout"], d["node"]),
        ),
        "frontier_cut": frontier_cut,
        "hops": hop,
        "closed": not frontier if depth is None else None,
    }


def _number(accession: str) -> tuple[str, int]:
    digits = "".join(c for c in accession if c.isdigit())
    return accession.rstrip("0123456789"), int(digits or 0)


def _pmid(text: str) -> str:
    """Return a paper id from the pick or the bounds as the store writes it, ``PMID:<n>``."""
    text = str(text).strip()
    low = text.lower()
    if low.startswith("pmid:"):
        return "PMID:" + text[5:]
    if text.isdigit():
        return "PMID:" + text
    return "DOI:" + low.removeprefix("doi:") if low.startswith(("doi:", "10.")) else text


def read_pick(set_dir: Path) -> list[dict[str, Any]]:
    """Return each study's pick group, records and bounds, from the standard-set files."""
    pick = yaml.safe_load((set_dir / "pick.yaml").read_text())
    picked = {str(s["id"]): s for s in pick["studies"]}
    studies = []
    for folder in sorted(p for p in set_dir.iterdir() if p.is_dir() and p.name.startswith("GSE")):
        study = yaml.safe_load((folder / "study.yaml").read_text())
        bounds = yaml.safe_load((folder / "bounds.yaml").read_text())
        entry = picked.get(folder.name, {})
        records = study.get("records") or {}
        left_out = [str(x["id"]) for x in bounds.get("left_out") or ()]
        studies.append(
            {
                "id": folder.name,
                "direction": study.get("direction"),
                "hard_shape": study.get("hard_shape"),
                "pick": {
                    "linked_series": [str(s) for s in entry.get("linked_series") or ()],
                    "series": sorted(
                        {str(s) for s in entry.get("linked_series") or ()}
                        | {str(s) for s in records.get("series") or ()}
                    ),
                    "papers": sorted(
                        {_pmid(p) for p in entry.get("pmids") or ()}
                        | {_pmid(p) for p in records.get("pmids") or ()}
                    ),
                    "bioprojects": sorted(str(p) for p in records.get("bioprojects") or ()),
                    "store_build": study.get("store_build"),
                },
                "bounds": {
                    "series": [str(s["id"]) for s in bounds.get("series") or ()],
                    "papers": [_pmid(p) for p in bounds.get("papers") or ()],
                    "left_out": [_pmid(x) if not x.startswith("GSE") else x for x in left_out],
                },
            }
        )
    return studies


def paper_facts(reader: PaperReader, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Return, for each paper key, whether the paper store holds it, an abstract and a full text."""
    out: dict[str, dict[str, Any]] = {}
    found = reader.papers(keys)
    for key in keys:
        paper = found.get(paper_key(key))
        if paper is None:
            out[key] = {"in_paper_store": False}
            continue
        held = sorted(
            {
                f.kind
                for f in paper.files
                if f.kind in FULL_TEXT_KINDS
                and f.path is not None
                and f.gone is None
                and f.path.exists()
            }
        )
        abstract = paper.info.abstract if paper.info else None
        out[key] = {
            "in_paper_store": True,
            "status": paper.status,
            "reason": paper.reason,
            "has_abstract": bool(abstract and abstract.strip()),
            "has_full_text": bool(held),
            "full_text_kinds": held,
            "pmcid": paper.pmcid,
        }
    return out


def provenance(store_root: Path, paper_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Return what was read: the store build and marker, the paper store's state, the code."""
    with (store_root / "labdata-store.toml").open("rb") as fh:
        marker = tomllib.load(fh)
    status_file = paper_root / "paper-status.json"
    status = json.loads(status_file.read_text()) if status_file.is_file() else {}
    made_by = next(
        (
            line.strip()
            for line in (store_root / "README.md").read_text().splitlines()
            if "labdata store build" in line and "commit" in line
        ),
        None,
    )
    return {
        "measured": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "store": store_root.name,
        "build": marker.get("build"),
        "switched": str(marker.get("switched")),
        "build_made_by": made_by,
        "paper_store": {
            k: status.get(k)
            for k in ("last_pass", "last_daily_success", "last_pmc_sync_success", "finalized")
        },
        "labdata": labdata_version,
        "liulab_data_commit": os.environ.get("LIULAB_DATA_COMMIT"),
        "seqforge_commit": os.environ.get("SEQFORGE_COMMIT"),
        "slurm_job": os.environ.get("SLURM_JOB_ID"),
        "caps": {
            "hub_cap": args.hub_cap,
            "frontier_cap": args.frontier_cap,
            "closure_depth": args.closure_depth,
        },
        "rules": [{"rule": n, "kinds": list(k), "depth": d} for n, k, d in RULES],
    }


def compare(group: dict[str, Any], study: dict[str, Any]) -> None:
    """Add to ``group`` what it holds over the pick's group and the bounds, and what it lacks."""
    pick, bounds = study["pick"], study["bounds"]
    named = set(bounds["series"]) | set(bounds["papers"]) | set(bounds["left_out"])
    for name, mine, theirs in (
        ("series", group["series"], pick["series"]),
        ("papers", group["papers"], pick["papers"]),
        ("bioprojects", group["bioprojects"], pick["bioprojects"]),
    ):
        group.setdefault("added", {})[name] = sorted(set(mine) - set(theirs), key=_number)
        group.setdefault("missing", {})[name] = sorted(set(theirs) - set(mine), key=_number)
    group["added_not_in_bounds"] = sorted(
        (set(group["series"]) | set(group["papers"])) - named, key=_number
    )
    group["same_as_pick"] = set(group["series"]) == set(pick["series"])


def summarize(studies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one row per rule over the 50 studies."""
    rows = []
    for name, _kinds, _depth in RULES:
        groups = [s["rules"][name] for s in studies]
        n_series = [g["n_series"] for g in groups]
        n_papers = [g["n_papers"] for g in groups]
        fan = [f["fanout"] for g in groups for f in g["largest_fanout"].values()]
        rows.append(
            {
                "rule": name,
                "studies": len(groups),
                "same_series_as_pick": sum(g["same_as_pick"] for g in groups),
                "adds_series": sum(bool(g["added"]["series"]) for g in groups),
                "adds_papers": sum(bool(g["added"]["papers"]) for g in groups),
                "misses_pick_series": sum(bool(g["missing"]["series"]) for g in groups),
                "series_median": statistics.median(n_series),
                "series_max": max(n_series),
                "papers_median": statistics.median(n_papers),
                "papers_max": max(n_papers),
                "largest_fanout": max(fan, default=0),
                "studies_not_crossed": sum(bool(g["not_crossed"]) for g in groups),
                "studies_frontier_cut": sum(g["frontier_cut"] for g in groups),
            }
        )
    return rows


def write_whole(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file and a rename, so no half file is left."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Gather every rule for every study and write the outputs."""
    start = time.monotonic()
    store = open_store(args.store)
    reader = store.reader
    paper_root = Path(args.papers)
    prov = provenance(Path(store.root), paper_root, args)
    graph = Graph(reader, args.hub_cap)
    studies = read_pick(Path(args.set_dir))
    if args.only:
        studies = [s for s in studies if s["id"] in set(args.only)]
    log(f"{len(studies)} studies, build {reader.build_id}", start)
    held = reader.held({s["id"] for s in studies})
    for i, study in enumerate(studies, 1):
        study["held"] = study["id"] in held
        study["rules"] = {}
        for name, kinds, depth in RULES:
            group = gather(graph, study["id"], kinds, depth, args)
            compare(group, study)
            study["rules"][name] = group
        closure = study["rules"]["all-closure"]
        study["reanalysis"] = sorted(
            [rel, other, s] for s in closure["series"] for rel, other in graph.series[s].reanalysis
        )
        if i % 10 == 0 or i == len(studies):
            log(f"{i}/{len(studies)} studies, {graph.queries} lookups", start)
    keys = sorted(
        {p for s in studies for g in s["rules"].values() for p in g["papers"]}
        | {p for s in studies for g in s["rules"].values() for p in g["bioproject_papers"]}
        | {p for s in studies for p in s["pick"]["papers"]}
    )
    papers = paper_facts(PaperReader(paper_root, reader), keys)
    for key, facts in papers.items():
        hub = graph.hubs.get(("paper", key))
        facts["series_linking"] = hub.fanout if hub else None
    log(f"{len(keys)} papers looked up", start)
    rows = summarize(studies)
    result = {
        "provenance": prov,
        "summary": rows,
        "studies": studies,
        "papers": papers,
        "lookups": graph.queries,
        # Linux reports kilobytes; this runs on the cluster.
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
        "seconds": round(time.monotonic() - start, 1),
    }
    write_whole(Path(args.out), json.dumps(result, indent=1, sort_keys=False, default=str) + "\n")
    header = list(rows[0])
    lines = ["\t".join(header)] + ["\t".join(str(r[h]) for h in header) for r in rows]
    write_whole(Path(args.summary), "\n".join(lines) + "\n")
    log("done", start)
    return rows


def log(message: str, since: float) -> None:
    """Print one progress line, with the seconds since the start, to standard error."""
    print(f"[{time.monotonic() - since:7.1f}s] {message}", file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments, run, and print the summary table."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--store", default=os.environ.get("LABDATA_STORE"), required=False)
    parser.add_argument("--papers", default=os.environ.get("LABDATA_PAPERS"), required=False)
    parser.add_argument("--set-dir", default="evals/standard-set")
    parser.add_argument("--out", default="results.json")
    parser.add_argument("--summary", default="summary.tsv")
    parser.add_argument("--hub-cap", type=int, default=200, help="most Series a hub may tie")
    parser.add_argument(
        "--frontier-cap", type=int, default=2000, help="most Series one hop starts from"
    )
    parser.add_argument("--closure-depth", type=int, default=10, help="most hops of the closure")
    parser.add_argument("--only", nargs="*", default=None, help="these study ids only, for a try")
    args = parser.parse_args(argv)
    if not args.store or not args.papers:
        parser.error("give --store and --papers, or set LABDATA_STORE and LABDATA_PAPERS")
    rows = run(args)
    for row in rows:
        print("\t".join(f"{k}={v}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
