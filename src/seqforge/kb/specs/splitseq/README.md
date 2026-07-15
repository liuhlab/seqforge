# SPLiT-seq (combinatorial split-pool barcoding)

Prose context for the harvester (the machine-checkable truth is `spec.yaml`).

**Scope: the original published SPLiT-seq only** (Rosenberg et al., *Science* 2018,
[doi:10.1126/science.aam8999](https://doi.org/10.1126/science.aam8999)). Parse Biosciences **Evercode**
is a separate, actively-versioned commercial descendant with different linkers and whitelists — it is
**out of scope for this pilot** and needs its own KB entry when we get to it; do not conflate the two.

Structure pinned from **scg_lib_structs** (CC-BY):
[methods page](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html) and
[issue #13](https://github.com/Teichlab/scg_lib_structs/issues/13) (the "Published Manuscript" read-2
variant, as distinct from the preprint and the Parse commercial variants).

## How the assay works
Combinatorial split-pool indexing: a cell's barcode is the concatenation of **three** round-specific
8 bp barcodes, each from a ~96-entry whitelist, joined by **two fixed linkers**. Sequencing:

- **Read 1 (66 cycles) = cDNA.**
- **Read 2 (94 cycles) = the barcode read**, 5'→3':

  ```
  [10 bp UMI][8 bp Round3] GTGGCCGATGTTTCGCATCGGCGTACGACT [8 bp Round2] ATCCACGTGCTTGAGAGGCCAGAGCATTCG [8 bp Round1]
      0..10     10..18            linker1 (18..48)             48..56            linker2 (56..86)             86..94
  ```

Round1 is the RT/first-round barcode (oligo-dT / randN primer); Round3 is nearest the sequencing
primer. The two 30 bp linker sequences above are the **verbatim Science-2018 sequences**.

## Why it is the pilot's generalization test
It stresses machinery the two 10x-family entries don't:
- **Width-generic onlist matching** — 8 bp round barcodes, not 16; the `onlist_hit_rate` evaluator
  reads the width from the registry (still a `uint32` pack), never a hardcoded 16 bp.
- **Fixed internal linkers** as the structural signature (`has_segment … constant`) rather than a
  single leading barcode block.
- **Combinatorial `CB_UMI_Complex`** backend: `soloCBposition` / `soloUMIposition` are **derived from
  the element coordinates at compose time** (FLAG-3), never hand-entered from memory.

## Still to pin before a real run
- **Whitelists.** `spec.yaml` declares registry names `splitseq-round{1,2,3}` (96 × 8 bp each). Register
  the actual barcode files from scg_lib_structs with URL + sha256; the KB never vendors them.
- **`soloStrand`.** Marked FLAG — confirm the cDNA (Read 1) strand for SPLiT-seq before trusting the
  count matrix (the `kb e2e` run is what will certify it).

`assay_ontology` is pinned to **`EFO:0009919`** ("SPLiT-seq"), verified against the EBI Ontology
Lookup Service (not memory). Parse Evercode's distinct EFO terms (`EFO:0022600/1/2`) confirm it is a
separate assay and stays out of this entry.

## Coverage caveat
Variable-length / anchored elements (inDrop-class floating linkers) are **not** exercised here — add
an inDrop entry before claiming the element model fully generalizes.
