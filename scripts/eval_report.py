#!/usr/bin/env python
"""Render an ``seqforge eval run`` report to a single self-contained HTML file.

**Why this is a script and not a CLI verb.** `seqforge eval run` emits machine JSON on stdout and
nothing else — [ADR-0013](../docs/adr/0013-cli-is-a-machine-interface.md) makes that the contract and
forbids a `--json` switch, because a stream that changes shape is not a pipe. So the renderer is a
*consumer* of that stream rather than a second output mode: the CLI stays a machine interface, and a
human-readable artifact is one pipe away.

    seqforge eval run --no-llm --cases evals/benchmark > report.json
    python scripts/eval_report.py report.json -o report.html

It reads the report the CLI already produces and writes one HTML file with no external asset, so it
opens from disk or from a CI artifact download with nothing to fetch. `--title` labels the run;
`--source` records the command that produced it, because a report without its command is a number
whose provenance you have to reconstruct.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

#: Grades the corpus can assign, worst first. `false_accept` leads because it is the one failure the
#: corpus never recovers from: a confident wrong manifest that nobody looks at again (evals/README).
GRADE_ORDER = [
    "false_accept",
    "mis_triage",
    "false_refuse",
    "wrong_reason",
    "over_ask",
    "correct",
]
GRADE_BLURB = {
    "false_accept": "decided wrong, or decided at all when it should have stopped",
    "mis_triage": "refused when it should have asked, or vice versa",
    "false_refuse": "blocked on something it should have decided or asked",
    "wrong_reason": "right outcome, wrong blocker code or conflict",
    "over_ask": "asked what code could settle",
    "correct": "as expected",
}


def _e(value: Any) -> str:
    """Escape for HTML text. Every value here is archive prose or a case id, so none of it is ours."""
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value: Any) -> str:
    """A field's expected/actual value, flattened. Lists are the `experiment.samples.*` multisets."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def render(report: dict[str, Any], *, title: str, source: str | None) -> str:
    cases = report.get("per_case", [])
    graded = [c for c in cases if not c.get("skipped")]
    skipped = [c for c in cases if c.get("skipped")]
    by_grade: dict[str, int] = {}
    for c in graded:
        by_grade[c.get("grade") or "?"] = by_grade.get(c.get("grade") or "?", 0) + 1

    accuracy = report.get("field_accuracy")
    false_accept = report.get("false_accept_rate")
    cost = report.get("cost", {})

    # A false accept is the only metric with a hard threshold: `eval run` exits 3 on any, and that is
    # deliberately not on a --fail-under slider, because no threshold makes one tolerable.
    verdict_ok = not false_accept
    tiles = [
        (
            "cases graded",
            f"{len(graded)}",
            f"{len(skipped)} skipped" if skipped else "none skipped",
        ),
        ("field accuracy", "—" if accuracy is None else f"{accuracy:.1%}", "expected vs actual"),
        (
            "false accepts",
            "—" if false_accept is None else f"{false_accept:.0%}",
            "exit 3 on any" if verdict_ok else "CORPUS-POISONING",
        ),
        (
            "false refuses",
            "—"
            if report.get("false_refuse_rate") is None
            else f"{report['false_refuse_rate']:.0%}",
            "throughput cost only",
        ),
        ("LLM calls", f"{cost.get('llm_calls', 0):.0f}", "harvest stage"),
        ("wall time", f"{cost.get('seconds', 0):.0f}s", "including package pulls"),
    ]

    rows = []
    for c in sorted(
        cases,
        key=lambda c: (
            GRADE_ORDER.index(c["grade"]) if c.get("grade") in GRADE_ORDER else 98,
            c["case"],
        ),
    ):
        if c.get("skipped"):
            rows.append(
                f'<tr class="skip"><td class="case">{_e(c["case"])}</td>'
                f'<td><span class="pill skip">skipped</span></td><td></td><td></td>'
                f'<td class="why">{_e(c["skipped"])}</td></tr>'
            )
            continue
        grade = c.get("grade") or "?"
        bad = [f for f in c.get("fields", []) if not f.get("ok")]
        detail = ""
        if bad:
            items = "".join(
                f"<li><code>{_e(f['path'])}</code> expected <b>{_e(_fmt(f['expected']))}</b>, "
                f"got <b>{_e(_fmt(f['actual']))}</b></li>"
                for f in bad
            )
            detail = f"<ul class='bad'>{items}</ul>"
        notes = " ".join(_e(n) for n in c.get("notes", []))
        rows.append(
            f'<tr class="{_e(grade)}"><td class="case">{_e(c["case"])}</td>'
            f'<td><span class="pill {_e(grade)}">{_e(grade)}</span></td>'
            f"<td>{_e(c.get('expected'))} &rarr; {_e(c.get('actual'))}</td>"
            f"<td>{len(c.get('fields', [])) - len(bad)}/{len(c.get('fields', []))}</td>"
            f'<td class="why">{notes}{detail}</td></tr>'
        )

    legend = "".join(
        f"<dt><span class='pill {g}'>{g}</span></dt><dd>{_e(GRADE_BLURB[g])}</dd>"
        for g in GRADE_ORDER
        if g in by_grade
    )
    tile_html = "".join(
        f'<div class="tile"><div class="k">{_e(label)}</div>'
        f'<div class="v">{_e(value)}</div><div class="s">{_e(sub)}</div></div>'
        for label, value, sub in tiles
    )
    src = f"<p class='src'><code>{_e(source)}</code></p>" if source else ""

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#16181d;--dim:#5b6472;--line:#e3e6ea;--card:#f7f8fa;
--ok:#1a7f4b;--warn:#9a6700;--bad:#b42318;--accent:#2f5fd0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14161a;--fg:#e8eaed;--dim:#98a2b3;--line:#2a2e35;
--card:#1c1f25;--ok:#3fb950;--warn:#d9a441;--bad:#f85149;--accent:#6ea8fe}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:2rem 1.25rem 4rem;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto}}
h1{{font-size:1.5rem;margin:0 0 .2rem}}
.sub{{color:var(--dim);margin:0 0 1.5rem}}
.src code{{font-size:.82rem;color:var(--dim);word-break:break-all}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin:1.5rem 0}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.8rem .9rem}}
.tile .k{{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}}
.tile .v{{font-size:1.6rem;font-weight:650;margin:.15rem 0;font-variant-numeric:tabular-nums}}
.tile .s{{font-size:.75rem;color:var(--dim)}}
.scroll{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%;min-width:720px;font-size:.9rem}}
th,td{{text-align:left;padding:.55rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);
background:var(--card);position:sticky;top:0}}
tr:last-child td{{border-bottom:0}}
td.case{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem;white-space:nowrap}}
td.why{{color:var(--dim);font-size:.85rem}}
.pill{{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:.75rem;font-weight:600;
white-space:nowrap;border:1px solid currentColor}}
.pill.correct{{color:var(--ok)}} .pill.over_ask,.pill.wrong_reason{{color:var(--warn)}}
.pill.false_accept,.pill.false_refuse,.pill.mis_triage{{color:var(--bad)}}
.pill.skip{{color:var(--dim)}}
ul.bad{{margin:.35rem 0 0;padding-left:1.1rem}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:.35rem .8rem;align-items:baseline;
margin:1.25rem 0 0;font-size:.85rem;color:var(--dim)}}
dt,dd{{margin:0}}
.verdict{{margin:1.5rem 0 0;padding:.8rem 1rem;border-radius:10px;border:1px solid var(--line);
background:var(--card);font-size:.9rem}}
.verdict b{{color:{"var(--ok)" if verdict_ok else "var(--bad)"}}}
</style>
<div class="wrap">
<h1>{_e(title)}</h1>
<p class="sub">{len(graded)} graded, {len(skipped)} skipped &middot;
{" &middot; ".join(f"{n} {g}" for g, n in sorted(by_grade.items()))}</p>
{src}
<div class="tiles">{tile_html}</div>
<div class="verdict"><b>{"No false accepts." if verdict_ok else "FALSE ACCEPT PRESENT."}</b>
A false accept is the only failure with no tolerable rate &mdash; <code>eval run</code> exits 3 on any,
deliberately not on a threshold, because a confident wrong manifest is the one thing a corpus never
recovers from. A refusal costs attention; a false accept costs the corpus.</div>
<div class="scroll"><table>
<thead><tr><th>case</th><th>grade</th><th>outcome</th><th>fields</th><th>notes</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table></div>
<dl>{legend}</dl>
</div>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("report", type=Path, help="JSON from `seqforge eval run` (use - for stdin)")
    ap.add_argument("-o", "--out", type=Path, required=True, help="HTML file to write")
    ap.add_argument("--title", default="seqforge eval report")
    ap.add_argument("--source", default=None, help="the command that produced the report")
    args = ap.parse_args(argv)

    raw = sys.stdin.read() if str(args.report) == "-" else args.report.read_text()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(json.loads(raw), title=args.title, source=args.source))
    print(args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
