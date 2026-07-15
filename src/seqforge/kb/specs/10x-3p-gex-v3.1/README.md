# 10x Chromium Single Cell 3' Gene Expression v3.1

Prose context for the harvester (the machine-checkable truth is `spec.yaml`).

## How the assay works
Droplet single-cell 3' RNA-seq, on the Next GEM chip. **R1 = 28 bp** technical read: a **16 bp cell
barcode (CB)** from the `3M-february-2018` whitelist followed by a **12 bp UMI**. **R2 = cDNA**
(open-ended), sense to the mRNA (`soloStrand Forward`). Index reads (I1/I2) may or may not be
retained.

## Aliases seen in the wild
"10x 3' v3.1", "Chromium 3' v3.1", "SC3Pv3.1", "single cell 3-prime v3.1", "Next GEM 3' v3.1".
Papers very often write only "10x 3' v3" for a v3.1 run, or name the kit and not the version at all.
That is not sloppiness worth correcting — see below.

## Why this entry exists even though it is identical to v3

It is the **benign twin** (§12). v3 and v3.1 share read geometry and the same `3M-february-2018`
whitelist, so no probe can separate them — and none needs to, because they emit **identical**
STARsolo parameters. The resolver records both ids into the chemistry equivalence class and asks
**zero** questions.

The file exists because `backend_identical(v3, v3.1)` is a *computed* predicate and there was nothing
to compute it against: v3 declared the twin, the twin was never written, and so the canonical example
of the rule was the one pair CI could not check. A declaration whose subject does not exist is a
comment.

Everything outside `identity` is byte-for-byte v3, and that is the assertion, not an accident. If a
future edit makes these two specs diverge in `backend.params`, the §12 biconditional turns red —
which is the correct outcome, because at that point they would no longer be benign and the resolver
would owe the user a question.

## How to tell it apart from its siblings
- **v3** — you cannot, from bytes, and you do not need to. Record both.
- **v2** = 16 CB + **10** UMI = **26 bp** R1, whitelist `737K-august-2016`. Length alone separates it.
- **Multiome (ARC) GEX** and **GEM-X 3' v4** also produce a 28 bp / 16+12 R1 — **only the onlist**
  separates them (`737K-arc-v1` and the newer GEM-X list, respectively).
- **5'** shares CB/UMI geometry but reads antisense cDNA (`soloStrand Reverse`) — decidable by
  metadata (rung 0) or alignment (rung 6), never by geometry.

## Attribution
Read structure cross-checked against [scg_lib_structs](https://teichlab.github.io/scg_lib_structs/)
(CC-BY). Assay term `EFO:0022980` verified against live EFO via the EBI OLS API (FLAG-1: never
hardcode a CURIE from memory).
