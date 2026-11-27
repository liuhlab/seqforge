# Architecture decision records

One decision per file: `# N. <the decision as a claim>` and a short body on what was there, what was
decided and why the obvious reading lost, with a `**Status.**` line only where a record was amended or
superseded. The one length cap is the word cap that `pixi run vale` checks, `Lab.LengthAdr`, explained
in [`docs/agents/writing.md`](../agents/writing.md). The bar for writing a new one at all is stated in
[`AGENTS.md`](../../AGENTS.md).

The records here are system-wide. The rest sit beside the code they govern, in
`src/seqforge/<context>/docs/adr/` — so the records governing `resolve/` are the ones under
`resolve/`, and no index has to say so. Filenames stay globally unique, so `find . -name '0034-*.md'`
locates any record wherever it lives.

**Numbers are permanent and gaps stay.** Take the next number not present anywhere in the tree, never
the one after the highest file in this directory. There is no `0019-*.md` and none was ever
committed — two branches took the next number on the same day and the reserved draft never landed.
Numbers are not reused, so the gap stays.

Cite a record by number (`ADR-0034`), never by path: records move between contexts and numbers do not.
