# AGENTS.md

This repository's agent guidance lives in [`CLAUDE.md`](CLAUDE.md) — read it in full. It is
tool-agnostic (the only Claude Code-specific parts are the hook names `PreToolUse` / `PostToolUse` /
`Stop`; the rules R1–R15 and conventions apply to any coding agent).

Kept as a single canonical document on purpose: do **not** generate this file by find-replacing
"Claude"→"Codex" across `CLAUDE.md` — that corrupts real paths like `.seqforge/` and would misstate
the rules. Point here instead.
