# `IO_VERSION`: why each bump was made, to 2026-11-25

Moved on **2026-11-25**, word for word, from the comment above `IO_VERSION` in
`src/seqforge/io/__init__.py`. Each entry gives the reason for one bump, and some give
the measurement behind it. The stamp keeps one line that says what a bump means and points
here. From 2026-11-25 on, a bump's reason goes in its commit message, and this file is not
extended.

The text is the comment as it stood, with only the `#:` comment marker taken off each line.
The entries keep the comment's order, which is not always date order. Where an entry says
"this log", "above" or "below", it means this text.

```text
CalVer YYYY.M.PATCH; bump when onlist packing / registry semantics change.
2026.8.0 — the SRA package parser reads what it had been dropping: per run, one **Submitted file**
per uploaded original, carrying the provider md5, the declared size and the `sra-pub-src-*` URI
beside the filename it already read (ADR-0033). A record set now also carries the version that
wrote it, which is what lets an unstamped one say "written before this" rather than passing for a
deposit that publishes no originals — so the stamp only means anything if this constant moves.
2026.11.0 — a record set holds every experiment the accession resolved to. NCBI can answer an SRA
fetch with fewer packages than it was asked for and say nothing; the missing ones are now asked for
again and refused if still absent, where a set stamped earlier may have kept the short reply.
```
