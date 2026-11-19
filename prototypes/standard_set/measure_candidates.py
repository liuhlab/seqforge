"""Measure the candidate studies of the standard set on one build of liulab-data's store.

A throwaway measurement, not part of seqforge: it reads the store's Parquet tables with DuckDB
and prints JSON lines. It reads the store and writes nothing there. The only thing it puts on
disk is DuckDB's spill folder under ``--tmp``, which it deletes.

A **candidate** is a held target Series that fits a direction:

- *C. elegans*: a ``Series_sample_taxid`` line says 6239.
- aging, neurodegeneration: a term list matches the Series title, summary or overall design.

A **linked group** is a connected set of Series joined by a SuperSeries link, a shared
BioProject (or BioProjects under one umbrella), or a shared paper. A group's samples are counted
once, so a SuperSeries does not count its SubSeries' samples twice.

Output, one JSON object a line, ``kind`` first: ``provenance``, then one ``series`` per
candidate, one ``group`` per linked group that holds a candidate, one ``paper`` per paper a
candidate links, and ``summary`` last. No record text is written: accessions, typed fields and
counts only.

Run it with a Python that has ``duckdb``, such as liulab-data's own environment::

    python measure_candidates.py --store <store root> --papers <paper store root> > out.jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import time
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, TextIO

import duckdb

#: The NCBI taxonomy id of Caenorhabditis elegans.
C_ELEGANS_TAXID = "6239"

#: The Series fields a direction's term list is matched against.
TEXT_FIELDS = ("Series_title", "Series_summary", "Series_overall_design")

#: Term lists, as RE2 patterns matched without regard to case. "aged" is left out on purpose:
#: it matches "mice aged 8 weeks".
AGING = (
    r"\b(aging|ageing|lifespan|life[- ]span|longevity|healthspan|senescen\w*|geriatric|elderly"
    r"|centenarian\w*|progeri\w*|rejuvenat\w*|age[- ](related|associated|dependent)"
    r"|old(er)? (age|adults?|mice|animals|individuals|donors))\b"
)
NEURODEGENERATION = (
    r"\b(neurodegenerat\w*|alzheimer\w*|parkinson\w*|huntington\w*|amyotrophic lateral sclerosis"
    r"|frontotemporal|dementia\w*|tauopath\w*|synucleinopath\w*|lewy bod\w*|prion"
    r"|spinocerebellar ataxia\w*|motor neuron disease|multiple system atrophy"
    r"|amyloid[- ]beta|beta[- ]amyloid)\b"
)
#: Disease acronyms, matched with case. "AD" and "PD" are left out: too many other meanings.
NEURODEGENERATION_ACRONYMS = r"\b(ALS|FTD|FTLD)\b"
#: A weak hint that a Series is single-cell; no store column says so.
SINGLE_CELL = (
    r"\b(single[- ](cell|nucle\w+)|sc(rna|atac)[- ]?seq|sn(rna|atac)[- ]?seq|10x genomics"
    r"|chromium|drop[- ]seq|smart[- ]seq\d?|cel[- ]seq\d?|split[- ]seq|sci[- ]rna)"
)

#: The link relations that can tie Series into one group, plus the paper and reanalysis links.
TIE_RELATIONS = ("SuperSeries of", "BioProject", "umbrella of", "PubMed", "DOI", "Reanalyzed by")

#: A PMID below this that only BioProject XML states is a placeholder, as liulab-data treats it.
PLACEHOLDER_BELOW = 100_000

DIRECTIONS = ("aging", "neurodegeneration", "c_elegans")
#: The columns of ``series`` read back for each candidate, after its accession.
FLAGS = (
    "published",
    *DIRECTIONS,
    "aging_in_title",
    "neurodegeneration_in_title",
    "n_taxids",
    "mentions_single_cell",
    "types",
)
#: The counts of one Series or group that has no sample link.
EMPTY: dict[str, int | None] = {
    "n_samples": 0,
    "n_experiments": 0,
    "n_runs": 0,
    "n_runs_sized": 0,
    "read_bytes": None,
    "bases": None,
    "spots": None,
}
QUANTILES = (0, 10, 25, 50, 75, 90, 99, 100)


def lit(text: str) -> str:
    """Return ``text`` as a quoted SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


def log(message: str, since: float) -> None:
    """Print one progress line, with the seconds since the run started, to standard error."""
    print(f"[{time.monotonic() - since:7.1f}s] {message}", file=sys.stderr, flush=True)


def insert(
    con: duckdb.DuckDBPyConnection, table: str, rows: Sequence[Sequence[str | bool]]
) -> None:
    """Append ``rows`` to ``table`` as literal ``VALUES``, a few thousand a statement."""
    for start in range(0, len(rows), 5000):
        values = ", ".join(
            "(" + ", ".join(str(v).upper() if isinstance(v, bool) else lit(v) for v in row) + ")"
            for row in rows[start : start + 5000]
        )
        con.execute(f"INSERT INTO {table} VALUES {values}")


class Groups:
    """Union-find over accessions and paper keys."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, node: str) -> str:
        """Return the representative of ``node``'s set."""
        parent = self._parent.setdefault(node, node)
        if parent == node:
            return node
        root = parent
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, a: str, b: str) -> None:
        """Join the sets of ``a`` and ``b``."""
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def series_number(accession: str) -> int:
    """Return the number in a ``GSE`` accession, for sorting."""
    return int(accession[3:])


def quantiles(values: Iterable[int | None]) -> dict[str, int | None]:
    """Return the count, the sum and the chosen percentiles of the values that are not None."""
    held = sorted(v for v in values if v is not None)
    out: dict[str, int | None] = {"n": len(held), "sum": sum(held)}
    for q in QUANTILES:
        out[f"p{q}"] = held[min(len(held) - 1, len(held) * q // 100)] if held else None
    return out


def size_bin(n: int) -> str:
    """Return the bin a group of ``n`` Series falls in."""
    return str(n) if n <= 2 else "3-5" if n <= 5 else "6-20" if n <= 20 else ">20"


def scan_series(con: duckdb.DuckDBPyConnection, records: Path) -> None:
    """Make ``series``: one row per held Series with its direction flags, and no text."""
    text = ", ".join(lit(f) for f in TEXT_FIELDS)
    wanted = ", ".join(lit(f) for f in (*TEXT_FIELDS, "Series_type", "Series_sample_taxid"))
    aging = f"regexp_matches(value, {lit(AGING)}, 'i')"
    neuro = (
        f"(regexp_matches(value, {lit(NEURODEGENERATION)}, 'i') "
        f"OR regexp_matches(value, {lit(NEURODEGENERATION_ACRONYMS)}))"
    )
    con.execute(
        f"""CREATE TEMP TABLE series AS
        SELECT accession,
               any_value(published) AS published,
               bool_or(name IN ({text}) AND {aging}) AS aging,
               bool_or(name = 'Series_title' AND {aging}) AS aging_in_title,
               bool_or(name IN ({text}) AND {neuro}) AS neurodegeneration,
               bool_or(name = 'Series_title' AND {neuro}) AS neurodegeneration_in_title,
               bool_or(name = 'Series_sample_taxid' AND trim(value) = {lit(C_ELEGANS_TAXID)})
                 AS c_elegans,
               count(DISTINCT value) FILTER (WHERE name = 'Series_sample_taxid') AS n_taxids,
               bool_or(name IN ({text}) AND regexp_matches(value, {lit(SINGLE_CELL)}, 'i'))
                 AS mentions_single_cell,
               list(DISTINCT value ORDER BY value) FILTER (WHERE name = 'Series_type') AS types
        FROM (SELECT accession, published, f.name AS name, f.value AS value
              FROM (SELECT accession, published, unnest(fields) AS f
                    FROM read_parquet({lit(str(records / "geo-series.parquet"))})))
        WHERE name IN ({wanted})
        GROUP BY accession"""
    )


def measure(con: duckdb.DuckDBPyConnection, scope: str) -> dict[str, dict[str, int | None]]:
    """Return samples, experiments, runs and read sizes per key of ``scope`` (key, gse).

    Each sample, experiment and run is counted once per key, however many of the key's Series
    list it.
    """
    rows = con.execute(
        f"""WITH t AS (SELECT s.key, r.gsm, r.srx, r.srr
                       FROM {scope} s JOIN series_runs r ON r.gse = s.gse),
             runs AS (SELECT DISTINCT key, srr FROM t WHERE srr IS NOT NULL),
             sized AS (SELECT runs.key, count(*) AS n_runs, count(z.srr) AS n_runs_sized,
                              sum(z.bytes) AS read_bytes, sum(z.bases) AS bases,
                              sum(z.spots) AS spots
                       FROM runs LEFT JOIN run_size z ON z.srr = runs.srr GROUP BY runs.key),
             n AS (SELECT key, count(DISTINCT gsm) AS n_samples,
                          count(DISTINCT srx) AS n_experiments FROM t GROUP BY key)
        SELECT n.key, n.n_samples, n.n_experiments, coalesce(sized.n_runs, 0),
               coalesce(sized.n_runs_sized, 0), sized.read_bytes, sized.bases, sized.spots
        FROM n LEFT JOIN sized ON sized.key = n.key"""
    ).fetchall()
    names = ("n_samples", "n_experiments", "n_runs", "n_runs_sized", "read_bytes", "bases", "spots")
    return {row[0]: dict(zip(names, row[1:], strict=True)) for row in rows}


def paper_states(
    con: duckdb.DuckDBPyConnection, papers: Path | None, keys: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Return each paper key's status and whether the paper store holds an abstract for it."""
    if papers is None or not keys:
        return {}
    table = lit(str(papers / "papers.parquet"))
    con.execute("CREATE TEMP TABLE wanted_paper (key VARCHAR)")
    insert(con, "wanted_paper", [(k,) for k in keys])
    rows = con.execute(
        f"""WITH ids AS (
              SELECT 'PMID:' || pmid AS key, paper, status FROM read_parquet({table})
              WHERE pmid IS NOT NULL
              UNION ALL
              SELECT 'DOI:' || doi, paper, status FROM read_parquet({table}) WHERE doi IS NOT NULL
              UNION ALL
              SELECT 'DOI:' || d, paper, status
              FROM (SELECT unnest(other_dois) AS d, paper, status FROM read_parquet({table})))
        SELECT ids.key, any_value(ids.status),
               bool_or(i.abstract IS NOT NULL AND length(trim(i.abstract)) > 0)
        FROM ids JOIN wanted_paper w ON w.key = ids.key
        LEFT JOIN read_parquet({lit(str(papers / "paper_info.parquet"))}) i ON i.paper = ids.paper
        GROUP BY ids.key"""
    ).fetchall()
    return {
        key: {"status": status, "has_abstract": bool(abstract)} for key, status, abstract in rows
    }


def run(args: argparse.Namespace, out: TextIO, spill: str) -> None:
    """Measure the candidates and write the JSON lines to ``out``."""
    start = time.monotonic()
    store = Path(args.store)
    with (store / "labdata-store.toml").open("rb") as fh:
        marker = tomllib.load(fh)
    build = store / "builds" / str(marker["build"])
    records, links = build / "records", lit(str(build / "links_by_from.parquet"))
    papers = Path(args.papers) if args.papers else None
    paper_status: dict[str, Any] = {}
    if papers is not None:
        paper_status = json.loads((papers / "paper-status.json").read_text())
    con = duckdb.connect(
        ":memory:",
        config={"threads": args.threads, "memory_limit": args.memory, "temp_directory": spill},
    )

    def emit(kind: str, **fields: Any) -> None:
        out.write(json.dumps({"kind": kind, **fields}, default=str) + "\n")

    emit(
        "provenance",
        measured=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        build=marker["build"],
        switched=str(marker.get("switched")),
        paper_store={
            k: paper_status.get(k)
            for k in ("last_daily_success", "last_pmc_sync_success", "finalized")
        }
        if papers is not None
        else None,
        duckdb=duckdb.__version__,
        limit=args.limit,
        terms={
            "aging": AGING,
            "neurodegeneration": NEURODEGENERATION,
            "neurodegeneration_acronyms": NEURODEGENERATION_ACRONYMS,
            "single_cell": SINGLE_CELL,
            "c_elegans_taxid": C_ELEGANS_TAXID,
        },
    )

    scan_series(con, records)
    fits = " OR ".join(DIRECTIONS)
    limit = (
        f" ORDER BY CAST(substr(accession, 4) AS BIGINT) LIMIT {args.limit}" if args.limit else ""
    )
    flags: dict[str, dict[str, Any]] = {
        row[0]: dict(zip(FLAGS, row[1:], strict=True))
        for row in con.execute(
            f"SELECT accession, {', '.join(FLAGS)} FROM series WHERE {fits}{limit}"
        ).fetchall()
    }
    held_series = {row[0] for row in con.execute("SELECT accession FROM series").fetchall()}
    log(f"{len(held_series)} Series held, {len(flags)} candidates", start)

    # The links that tie Series together, and the papers each Series and BioProject links.
    groups = Groups()
    subseries: dict[str, set[str]] = defaultdict(set)
    superseries: dict[str, set[str]] = defaultdict(set)
    projects: dict[str, set[str]] = defaultdict(set)
    project_series: dict[str, set[str]] = defaultdict(set)
    umbrella: list[tuple[str, str]] = []
    series_papers: dict[str, set[str]] = defaultdict(set)
    paper_series: dict[str, set[str]] = defaultdict(set)
    project_papers: dict[str, set[str]] = defaultdict(set)
    reanalysis: set[str] = set()
    relations = ", ".join(lit(r) for r in TIE_RELATIONS)
    for src, dst, relation, sources in con.execute(
        f"""SELECT "from", "to", relation, list(DISTINCT source) FROM read_parquet({links})
            WHERE relation IN ({relations}) AND ("from" LIKE 'GSE%' OR "from" LIKE 'PRJ%')
            GROUP BY ALL"""
    ).fetchall():
        if relation == "SuperSeries of":
            subseries[src].add(dst)
            superseries[dst].add(src)
            groups.union(src, dst)
        elif relation == "BioProject" and src.startswith("GSE"):
            projects[src].add(dst)
            project_series[dst].add(src)
            groups.union(src, dst)
        elif relation == "umbrella of":
            umbrella.append((src, dst))
            groups.union(src, dst)
        elif relation in ("PubMed", "DOI"):
            number = dst.removeprefix("PMID:")
            small = number.isdigit() and int(number) < PLACEHOLDER_BELOW
            if small and set(sources) <= {"BioProject XML"}:
                continue
            if src.startswith("GSE"):
                series_papers[src].add(dst)
                paper_series[dst].add(src)
                groups.union(src, dst)
            else:
                project_papers[src].add(dst)
        elif relation == "Reanalyzed by":
            reanalysis.update((src, dst))
    members: dict[str, set[str]] = defaultdict(set)
    for accession in held_series | set(subseries) | set(superseries):
        members[groups.find(accession)].add(accession)
    kept = {groups.find(a) for a in flags}
    under_umbrella = {groups.find(a) for a, _ in umbrella}
    group_id = {root: min(members[root], key=series_number) for root in kept}
    log(f"{len(kept)} linked groups hold a candidate", start)

    con.execute("CREATE TEMP TABLE member (series VARCHAR, grp VARCHAR, candidate BOOLEAN)")
    insert(con, "member", [(s, group_id[r], s in flags) for r in kept for s in sorted(members[r])])
    for table, columns, relation, source in (
        ("gse_gsm", "gse, gsm", "sample", "SELECT series FROM member"),
        ("gsm_srx", "gsm, srx", "SRA", "SELECT gsm FROM gse_gsm"),
        ("srx_srr", "srx, srr", "run", "SELECT srx FROM gsm_srx"),
    ):
        first, second = columns.split(", ")
        con.execute(
            f"""CREATE TEMP TABLE {table} AS
            SELECT DISTINCT "from" AS {first}, "to" AS {second} FROM read_parquet({links})
            WHERE relation = {lit(relation)} AND "from" IN ({source})"""
        )
    con.execute(
        """CREATE TEMP TABLE series_runs AS
        SELECT DISTINCT g.gse, g.gsm, x.srx, r.srr
        FROM gse_gsm g LEFT JOIN gsm_srx x ON x.gsm = g.gsm LEFT JOIN srx_srr r ON r.srx = x.srx"""
    )
    log("sample, experiment and run links read", start)
    con.execute(
        f"""CREATE TEMP TABLE run_size AS
        SELECT accession AS srr,
               max(TRY_CAST(f.value AS BIGINT)) FILTER (WHERE f.name = 'RUN@size') AS bytes,
               max(TRY_CAST(f.value AS BIGINT)) FILTER (WHERE f.name = 'RUN@total_bases') AS bases,
               max(TRY_CAST(f.value AS BIGINT)) FILTER (WHERE f.name = 'RUN@total_spots') AS spots
        FROM (SELECT accession, unnest(fields) AS f
              FROM read_parquet({lit(str(records / "sra-run.parquet"))})
              WHERE accession IN (SELECT srr FROM srx_srr))
        WHERE f.name IN ('RUN@size', 'RUN@total_bases', 'RUN@total_spots')
        GROUP BY accession"""
    )
    log("run sizes read", start)
    con.execute(
        f"""CREATE TEMP TABLE sample_assay AS
        SELECT accession AS gsm,
               max(f.value) FILTER (WHERE f.name = 'Sample_library_strategy') AS strategy,
               max(f.value) FILTER (WHERE f.name = 'Sample_library_source') AS source
        FROM (SELECT accession, unnest(fields) AS f
              FROM read_parquet({lit(str(records / "geo-sample.parquet"))})
              WHERE accession IN (SELECT g.gsm FROM gse_gsm g JOIN member m ON m.series = g.gse
                                  WHERE m.candidate))
        WHERE f.name IN ('Sample_library_strategy', 'Sample_library_source')
        GROUP BY accession"""
    )
    assays: dict[str, Counter[str]] = defaultdict(Counter)
    for gse, strategy, source, n in con.execute(
        """SELECT g.gse, coalesce(a.strategy, '(none)'), coalesce(a.source, '(none)'), count(*)
           FROM (SELECT DISTINCT g.gse, g.gsm FROM gse_gsm g JOIN member m ON m.series = g.gse
                 WHERE m.candidate) g
           LEFT JOIN sample_assay a ON a.gsm = g.gsm GROUP BY ALL"""
    ).fetchall():
        assays[gse][f"{strategy} / {source}"] += n
    log("sample assays read", start)

    con.execute(
        "CREATE TEMP VIEW by_series AS SELECT series AS key, series AS gse FROM member WHERE candidate"
    )
    con.execute("CREATE TEMP VIEW by_group AS SELECT grp AS key, series AS gse FROM member")
    per_series, per_group = measure(con, "by_series"), measure(con, "by_group")
    linked = sorted({p for s in flags for p in series_papers.get(s, ())})
    states = paper_states(con, papers, linked)
    log("counts and paper states read", start)

    summary_groups: dict[str, Any] = {"by_size": Counter(), "by_ties": Counter(), "largest": []}
    singles: Counter[int] = Counter()
    group_of = {s: group_id[groups.find(s)] for s in flags}
    for root in sorted(kept, key=lambda r: series_number(group_id[r])):
        inside = members[root]
        shared_project = any(
            len(project_series[p] & inside) > 1 for s in inside for p in projects.get(s, ())
        )
        shared_paper = any(
            len(paper_series[p] & inside) > 1 for s in inside for p in series_papers.get(s, ())
        )
        present = {
            "superseries": any(subseries.get(s, set()) & inside for s in inside),
            "bioproject": shared_project,
            "bioproject-umbrella": len(inside) > 1 and root in under_umbrella,
            "paper": shared_paper,
        }
        ties = sorted(name for name, found in present.items() if found)
        group_papers = sorted({p for s in inside for p in series_papers.get(s, ())})
        emit(
            "group",
            group=group_id[root],
            series=sorted(inside, key=series_number),
            n_series=len(inside),
            n_candidates=sum(s in flags for s in inside),
            n_not_held=sum(s not in held_series for s in inside),
            ties=ties,
            papers=group_papers,
            **per_group.get(group_id[root], EMPTY),
        )
        summary_groups["by_size"][size_bin(len(inside))] += 1
        summary_groups["by_ties"]["+".join(ties) or "(none)"] += 1
        summary_groups["largest"].append((len(inside), group_id[root], ties))
        if len(inside) == 1:
            singles[min(len(group_papers), 2)] += 1
    summary_groups["largest"] = sorted(summary_groups["largest"], reverse=True)[:20]

    for accession in sorted(flags, key=series_number):
        own = sorted(series_papers.get(accession, ()))
        emit(
            "series",
            series=accession,
            group=group_of[accession],
            **flags[accession],
            is_superseries=bool(subseries.get(accession)),
            is_subseries=bool(superseries.get(accession)),
            reanalysis_link=accession in reanalysis,
            bioprojects=sorted(projects.get(accession, ())),
            papers=own,
            papers_with_abstract=sum(states.get(p, {}).get("has_abstract", False) for p in own),
            bioproject_papers=sorted(
                {q for p in projects.get(accession, ()) for q in project_papers.get(p, ())}
            ),
            assays=dict(assays.get(accession, {})),
            **per_series.get(accession, EMPTY),
        )
    for key in linked:
        state = states.get(key)
        emit(
            "paper",
            paper=key,
            n_series=len(paper_series[key]),
            n_candidates=sum(s in flags for s in paper_series[key]),
            in_paper_store=state is not None if papers is not None else None,
            status=state["status"] if state else None,
            has_abstract=state["has_abstract"]
            if state
            else (False if papers is not None else None),
        )

    def describe(chosen: Sequence[str]) -> dict[str, Any]:
        counts = [per_series.get(s, EMPTY) for s in chosen]
        return {
            "n_series": len(chosen),
            "n_with_experiments": sum((c["n_experiments"] or 0) > 0 for c in counts),
            "in_title": sum(
                bool(flags[s].get("aging_in_title") or flags[s].get("neurodegeneration_in_title"))
                for s in chosen
            ),
            "mentions_single_cell": sum(bool(flags[s]["mentions_single_cell"]) for s in chosen),
            "superseries": sum(bool(subseries.get(s)) for s in chosen),
            "subseries": sum(bool(superseries.get(s)) for s in chosen),
            "reanalysis_link": sum(s in reanalysis for s in chosen),
            "papers_per_series": dict(
                Counter(min(len(series_papers.get(s, ())), 3) for s in chosen)
            ),
            "series_type": dict(
                Counter(" + ".join(flags[s]["types"] or ["(none)"]) for s in chosen)
            ),
            "sample_assays": dict(
                sum((assays.get(s, Counter()) for s in chosen), Counter()).most_common(40)
            ),
            "series_assay_sets": dict(
                Counter(
                    " + ".join(sorted(assays.get(s, {"(no samples)": 0}))) for s in chosen
                ).most_common(40)
            ),
            **{
                name: quantiles(c[name] for c in counts)
                for name in ("n_samples", "n_experiments", "n_runs", "read_bytes", "bases")
            },
        }

    emit(
        "summary",
        all=describe(sorted(flags)),
        by_direction={d: describe(sorted(s for s in flags if flags[s][d])) for d in DIRECTIONS},
        overlap=dict(Counter("+".join(d for d in DIRECTIONS if flags[s][d]) for s in flags)),
        groups={
            "n": len(kept),
            "by_size": dict(summary_groups["by_size"]),
            "by_ties": dict(summary_groups["by_ties"]),
            "largest": summary_groups["largest"],
            "one_series_by_papers": {"0": singles[0], "1": singles[1], "2+": singles[2]},
        },
        papers={
            "n_linked": len(linked),
            "in_paper_store": sum(k in states for k in linked) if papers is not None else None,
            "with_abstract": sum(states[k]["has_abstract"] for k in states)
            if papers is not None
            else None,
            "by_status": dict(Counter(states[k]["status"] for k in states)),
            "doi_keys": sum(k.startswith("DOI:") for k in linked),
        },
        seconds=round(time.monotonic() - start, 1),
    )
    log("done", start)


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line; the store roots fall back to labdata's environment variables."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--store", default=os.environ.get("LABDATA_STORE"), help="GEO/SRA store root"
    )
    parser.add_argument(
        "--papers",
        default=os.environ.get("LABDATA_PAPERS"),
        help="paper store root; without it no abstract is looked up",
    )
    parser.add_argument("--out", default="-", help="output file; - is standard output")
    parser.add_argument(
        "--tmp", default=None, help="folder for DuckDB's spill files, off the store"
    )
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--memory", default="16GB", help="DuckDB's memory limit")
    parser.add_argument(
        "--limit", type=int, default=0, help="keep the first N candidates, for a try"
    )
    args = parser.parse_args(argv)
    if not args.store:
        parser.error("give --store or set LABDATA_STORE")
    return args


@contextlib.contextmanager
def opened(path: str) -> Iterator[TextIO]:
    """Yield the output stream, closing it if it is a file."""
    if path == "-":
        yield sys.stdout
    else:
        with Path(path).open("w") as fh:
            yield fh


def main(argv: Sequence[str] | None = None) -> None:
    """Run the measurement with a spill folder that is deleted at the end."""
    args = arguments(argv)
    spill_folder = tempfile.TemporaryDirectory(prefix="standard-set-", dir=args.tmp)
    with spill_folder as spill, opened(args.out) as out:
        run(args, out, spill)


if __name__ == "__main__":
    main()
