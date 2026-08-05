# SMART-seq3 read structure: what the bytes actually look like

Research note for [#226](https://github.com/liuhlab/seqforge/issues/226), under the plate-based map
[#225](https://github.com/liuhlab/seqforge/issues/225). **No spec is authored here.** Every value
below is either quoted from a primary source with its URL, or measured from named public data with
the accession and the method written down, per `docs/agents/kb.md` ("a chemistry fact is looked up
against a live source and pinned by URL plus checksum, or it does not go in a `spec.yaml`").

Measurements were taken 2026-08-04. Where sources disagree, both are recorded and labelled — the
`splitseq` linker episode is why.

## The one-sentence answer

A SMART-seq3 library is **two read populations in one FASTQ pair**. A minority-to-majority share of
read 1 begins with a constant 11 bp tag, an 8 bp UMI and `GGG`; the rest of read 1, and effectively
all of read 2, are ordinary Nextera-tagmented cDNA fragments that are **structurally identical to
bulk paired-end RNA-seq**. The cell barcode is not in either read.

## The layout, element by element

### Read 1 — the 5′ (UMI-carrying) population

| element | type | coordinates (1-based, inclusive) | fixed or floating |
|---|---|---|---|
| `ATTGCGCAATG` | `linker` / tag | 1–11 | fixed at offset 0 |
| UMI | `umi`, 8 bp | 12–19 | fixed |
| `GGG` | `fixed` (template-switch Gs) | 20–22 | fixed |
| cDNA | `cdna` | 23–end | open-ended |

Sources, all agreeing:

- The authors' own zUMIs configuration in their pipeline repo declares exactly this:
  `find_pattern: ATTGCGCAATG`, `UMI(12-19)`, `cDNA(23-150)` —
  [sandberg-lab/Smart-seq3 `allele_level_expression/mouse_cross.yaml`](https://raw.githubusercontent.com/sandberg-lab/Smart-seq3/master/allele_level_expression/mouse_cross.yaml).
- The paper's own Methods: "To extract and identify the UMI-containing reads in zUMIs,
  `find_pattern: ATTGCGCAATG` was specified for file1 as well as `base_definition: cDNA(23-75;
  Single-end), (23-150bp, paired-end)` and `UMI(12-19)` in the YAML file" —
  [Hagemann-Jensen et al., bioRxiv 817924v1, Methods](https://www.biorxiv.org/content/10.1101/817924v1.full)
  (published as [Nat Biotechnol 38:708–714, doi:10.1038/s41587-020-0497-0](https://doi.org/10.1038/s41587-020-0497-0)).
- The TSO that creates it: `5'-Biotin-AGAGACAGATTGCGCAATGNNNNNNNNrGrGrG-3'` — same preprint Methods,
  and repeated verbatim in the xpress paper's Methods
  ([PMC9546772](https://europepmc.org/article/MED/35637418), "2 µM TSO
  (5′-Biotin-AGAGACAGATTGCGCAATGNNNNNNNNrGrGrG-3′; IDT)").
- [scg_lib_structs, SMART-seq family page](https://teichlab.github.io/scg_lib_structs/methods_html/SMART-seq_family.html)
  (CC-BY) draws the final library and annotates the same blocks: `... ME | ATTGCGCAATG (11 bp 5'
  fragment tag) | NNNNNNNN (8 bp UMI) | GGG | cDNA | ME ...`.
- [seqspec `docs/examples/assays/smart_seq3.spec.yaml`](https://raw.githubusercontent.com/pachterlab/seqspec/main/docs/examples/assays/smart_seq3.spec.yaml)
  declares regions `five_prime_fragment_tag` (`ATTGCGCAATG`, 11 bp), `umi` (8 bp random),
  `triple_G` (`GGG`), `cDNA`.

**Measured on raw reads.** Per-cycle base composition over read 1 of
[SRR19884922](https://www.ebi.ac.uk/ena/browser/view/SRR19884922) (GSE207085, a third-party
SMART-seq3 dataset), restricted to reads whose first 11 bases match the tag within 2 mismatches
(n = 7 349 of the first 12 302 reads):

```text
cycle  1..11  ATTGCGCAATG      each position 99.6–100.0% pure
cycle 12..19  near-random      A 21–32%, C 19–28%, G 33–41%, T 12–16%; 5 018 distinct 8-mers / 7 349 reads
cycle 20..22  G G G            95.0%, 96.7%, 96.9%
cycle 23      G-enriched       59.9% G — the first cDNA base, carrying template-switch G bleed
cycle 24+     unstructured     no consensus above ~31%
```

Two consequences for a future spec: the UMI window is G-biased rather than uniform (a `random`
`has_segment` test should expect that), and cycle 23 is not clean cDNA — the run of Gs is not always
exactly three.

### Read 1 — the internal population, and read 2

`cdna` from base 1 to the end. Nothing else. Read 2 is `cdna` from base 1 in **both** populations —
the tag lives only on the s5 end, so it can only ever appear on read 1.

Measured on the same run: read 2 carries the tag at offset 0 in 2 of 8 825 reads (0.0%).

## The tag fraction — the load-bearing number

**Verdict: the fraction is not a chemistry constant, it is a tunable protocol parameter, and across
real libraries it ranges from ~7% to ~70%. It straddles the majority line, so the tag cannot be a
`requires` gate under today's `has_segment: constant` evaluator** (`docs/agents/resolve.md`: "the
share of reads carrying the window's modal consensus to within a per-base slack, gated at a
majority").

The published statement is explicitly that it is tunable, not that it is a number: "The proportions
of 5′ to internal reads could be tuned by altering the Tn5-based tagmentation reaction (Figure 1c)"
— [bioRxiv 817924v1, Results](https://www.biorxiv.org/content/10.1101/817924v1.full). Figure 1c is
titled "Effect of tagmentation conditions on the fraction of UMI-containing reads". The xpress paper
repeats the design point ("the inability to modulate the ratio of UMI-containing and internal reads
by Tn5 amounts (Fig. 1j)") and reports the fraction only as figure panels
([PMC9546772](https://europepmc.org/article/MED/35637418)). **No numeric fraction appears in the body
text of either paper** — the number lives in figures, so it had to be measured.

### Measured fractions

| library | accession | shape | read 1 carrying the tag |
|---|---|---|---|
| HCA benchmark (SS3, authors) | [ERR3835347](https://www.ebi.ac.uk/ena/browser/view/ERR3835347) | zUMIs BAM | **6.9%** |
| HEK293T diySpike (SS3, authors) | [ERR3835349](https://www.ebi.ac.uk/ena/browser/view/ERR3835349) | zUMIs BAM | **13.1%** |
| Mouse fibroblast NovaSeq (SS3, authors) | [ERR3835351](https://www.ebi.ac.uk/ena/browser/view/ERR3835351) | zUMIs BAM | **41.4%** |
| GSE207085 nasal cell, 5 runs (SS3, third party) | SRR19884922, SRR19885818, SRR19885048, SRR19885873, SRR19885085 | raw FASTQ | **56.2 – 69.4%** |
| PBMC atlas run 5 (SS3xpress, authors) | [ERR8607756](https://www.ebi.ac.uk/ena/browser/view/ERR8607756) | zUMIs BAM | **70.5%** |

Method, raw FASTQ: bounded range request over the first ~4–6 MB of `<run>_1.fastq.gz` from ENA
(`https://ftp.sra.ebi.ac.uk/vol1/fastq/...`), first ~3 000–12 000 reads, counting reads whose first
11 bases match `ATTGCGCAATG` within 2 mismatches — the authors' own criterion ("UMI-containing reads
parsed by detection of the pattern (ATTGCGCAATG) while allowing up to two mismatches",
[PMC9546772](https://europepmc.org/article/MED/35637418) Methods).

Method, ENA BAM: the authors deposited **zUMIs-processed unmapped BAMs**, not FASTQ
([PRJEB36367](https://www.ebi.ac.uk/ena/browser/view/PRJEB36367) = ArrayExpress E-MTAB-8735;
[PRJEB50980](https://www.ebi.ac.uk/ena/browser/view/PRJEB50980) = E-MTAB-11488 for the xpress PBMC
atlas). In those files the tag+UMI+GGG has already been cut off read 1 and the UMI moved to the `RX`
aux tag, which makes the fraction directly countable and unambiguous: a read 1 of length
`raw − 22` carrying an 8 bp `RX` is a tagged read, a read 1 at full length with an empty `RX` is an
internal read. The two classes partition the data exactly (e.g. ERR3835349: 2 751 × (128 bp, `RX`=8)
against 18 296 × (150 bp, `RX`=∅), zero mixed cases). A BGZF prefix of the first 4 MB was decoded
directly over HTTP range requests.

### Why this matters to the DSL

- Three of the five libraries measured are **below** a majority; two are above. Choosing a
  `requires` gate on the tag would refuse the authors' own reference libraries.
- The `has_segment: constant` proportion is calibrated at a majority and counts junk in the
  denominator on purpose (`docs/agents/resolve.md`). Here the "junk" is half the library **by
  design** — the internal reads are the point of the assay, not contamination.
- So either the tag becomes a `supports` test (positive evidence, no gate), or a new evaluator is
  needed whose passing band is a *minority* proportion — "present in 5–80% of reads, at a fixed
  offset, with the same consensus" is the real claim, and today's DSL cannot say it.

A second, softer signal is available and worth measuring before designing that evaluator: the tag's
offset histogram is extremely clean. In SRR19884922, exact hits at offset 0 number 5 772 while the
next most common offset in the first 40 bp scores 54. A motif that is *either* at offset 0 *or*
absent, never elsewhere, is a stronger discriminator than its bare frequency.

## Are the internal reads distinguishable from bulk RNA-seq?

**No.** This is the strongest negative result here, and it is citable verbatim rather than argued.

[scg_lib_structs](https://teichlab.github.io/scg_lib_structs/methods_html/SMART-seq_family.html)
prints the final library structure for both assays on one page. SMART-seq2's final library and
SMART-seq3's internal fragments are **character-for-character the same string**:

```text
AATGATACGGCGACCACCGAGATCTACACNNNNNNNNTCGTCGGCAGCGTCAGATGTGTATAAGAGACAGXXXXXXXX...XXXXXXXXCTGTCTCTTATACACATCTCCGAGCCCACGAGACNNNNNNNNATCTCGTATGCCGTCTTCTGCTTG
P5           i5      s5          ME              cDNA               ME        s7      i7      P7
```

(that line occurs at six places in the rendered page: in the SMART-seq2 section and again under
"(9.2) Internal fragments — All three methods are the same".)

Everything a probe can see about an internal read pair — both mates start at base 1 of cDNA, both are
full length, no fixed sequence anywhere, the same Nextera ME/s5/s7/i5/i7 flanks — is what a
Nextera-tagmented bulk paired-end RNA-seq read pair looks like. The paper says the same thing from
the biology side: SMART-seq3 yields "strand-specific 5′ UMI-containing reads and **unstranded**
internal reads spanning the full-transcript without UMIs in the same sequencing reaction"
([bioRxiv 817924v1](https://www.biorxiv.org/content/10.1101/817924v1.full)).

seqspec reaches the same conclusion structurally: it models SMART-seq3 as **two modalities in one
assay**, `RNA_end` and `RNA_internal`, and the `RNA_internal` region list has no tag, no UMI and no
`GGG`
([smart_seq3.spec.yaml](https://raw.githubusercontent.com/pachterlab/seqspec/main/docs/examples/assays/smart_seq3.spec.yaml)).

Consequence for #225's constraint 2: a SMART-seq3 library whose tag fraction is low is, to a
byte-only probe, a bulk paired-end RNA-seq library with a minority of reads carrying an unexplained
5′ motif. The separation is real but it is a *minority-proportion* separation, which is exactly what
the current evaluator set cannot gate on.

## Where the cell identity lives

**In the i5/i7 index pair — never in read 1 or read 2.** scg_lib_structs states it directly: "add
Index 2 sequencing primer to sequence the second index (i5) (top strand as template, **single cells
can be identified by the combination of i5 and i7**)"
([SMART-seq family page](https://teichlab.github.io/scg_lib_structs/methods_html/SMART-seq_family.html)).

The authors' zUMIs config confirms the mechanics: `file3` = `I1`, `BC(1-8)`; `file4` = `I2`,
`BC(1-8)`, giving a 16 bp composite barcode, with `barcode_file: expected_barcodes.txt` and
`demultiplex: yes`
([mouse_cross.yaml](https://raw.githubusercontent.com/sandberg-lab/Smart-seq3/master/allele_level_expression/mouse_cross.yaml)).

**Index width is 8 or 10 bp and varies between experiments.** "Library amplification of the
tagmented samples was performed using either 1.5 uL Nextera XT index primers (Illumina) or 1.5 uL
custom designed Nextera index primers containing either 8 or 10 bp indexes"
([bioRxiv 817924v1, Methods](https://www.biorxiv.org/content/10.1101/817924v1.full)). Confirmed in
the deposited data: the `CR` tag is 16 bp in ERR3835349 and ERR3835351 (8+8) and 20 bp in ERR3835347
and the xpress runs (10+10). The xpress paper specifies "custom Nextera index primers (0.5 μM)
carrying 10-bp dual indexes" ([PMC9546772](https://europepmc.org/article/MED/35637418)).

**In the SRA-deposited shape the index is gone entirely and the cell is the FILE.** GSE207085
deposits one run per cell (1 440 runs under
[PRJNA853582](https://www.ebi.ac.uk/ena/browser/view/PRJNA853582), matching the 1 440 GEO samples),
already demultiplexed ("Demultiplexed fastq files were aligned to mm10", [GSM6276900](
https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSM6276900)), and the FASTQ headers are
SRA-normalized with no index field:

```text
@SRR19884922.1 ST-E00114:1220:HGL5HCCX2:7:1101:16498:1731/1
```

So both shapes exist in the wild and they are not the same problem:

| shape | where the cell is | example |
|---|---|---|
| authors' own, undemultiplexed | `I1`+`I2` index FASTQs, 8+8 or 10+10 bp | `mouse_cross.yaml`'s `Undetermined_S0_L003_{R1,R2,I1,I2}` |
| public archive, demultiplexed | the file / run / BioSample — nowhere in the reads | GSE207085, 1 440 runs |

## Read lengths, and whether R1 and R2 are symmetric

**Symmetric in length, asymmetric in content.** Both mates are sequenced to the same cycle count;
only read 1 can carry the tag.

- SMART-seq3 as published: "Libraries were sequenced at 75 bp single-end, or 150 bp paired-end on a
  high output flow cell using the Illumina NextSeq500 instrument, or on a NovaSeq S4 flow cell 150 bp
  paired-end" ([bioRxiv 817924v1, Methods](https://www.biorxiv.org/content/10.1101/817924v1.full)).
  The matching zUMIs definitions are `cDNA(23-75)` single-end and `cDNA(23-150)` paired-end.
- GSE207085 as deposited: 2 × 151, HiSeq X Ten
  ([ENA](https://www.ebi.ac.uk/ena/portal/api/filereport?accession=PRJNA853582&result=read_run&fields=run_accession,read_count,base_count,instrument_model&format=tsv);
  measured — every read 151 bp in both mates).
- SMART-seq3xpress: "sequenced using SE100, PE100 or PE150 cartridges" on MGI DNBSEQ-G400RS, or
  NextSeq 500 ([PMC9546772](https://europepmc.org/article/MED/35637418)). Measured on ERR8607752 /
  ERR8607756: read 1 is 100 bp raw.

A single-end SMART-seq3 run is legal and published. Any read-count gate must tolerate one biological
read, not require two.

## SMART-seq3xpress

**Same tag, same UMI, same coordinates for both — the only documented difference is a two-base
spacer between the UMI and the `GGG`, and it does not move the tag or the UMI.**

| | SMART-seq3 | SMART-seq3xpress | FLASH-seq |
|---|---|---|---|
| TSO | `AGAGACAGATTGCGCAATG[8bp UMI]rGrGrG` | `AGAGACAGATTGCGCAATG[8bp UMI]WWrGrGrG` | `AGAGACAGATTGCGCAATG[8bp UMI]CTAACrGrGrG` |
| tag | 1–11 | 1–11 | 1–11 |
| UMI | 12–19 | 12–19 | 12–19 |
| spacer | `GGG`, 20–22 | `WWGGG`, 20–24 | `CTAACGGG`, 20–27 |
| cDNA from | 23 | 25 | 28 |

Sources: the xpress Methods lists both oligos side by side — "Original Smartseq3 TSO
(5′-Biotin-AGAGACAGATTGCGCAATGNNNNNNNNrGrGrG-3′; IDT). Improved TSO
(5′-Biotin-AGAGACAGATTGCGCAATGNNNNNNNNWWrGrGrG-3′; IDT)"
([PMC9546772](https://europepmc.org/article/MED/35637418));
[scg_lib_structs](https://teichlab.github.io/scg_lib_structs/methods_html/SMART-seq_family.html)
prints all three TSOs and all three final library structures, and states "The library generation
procedures are the same, and the final libraries are ALMOST the same. The only differences are that
they used different variation of TSO, which makes the 5′ fragment a bit different"; seqspec's
[smartseq3-express.rgn.yaml](https://raw.githubusercontent.com/pachterlab/seqspec/main/docs/examples/regions/smartseq3-express.rgn.yaml)
declares `linker-1: ATTGCGCAATG`, `umi: NNNNNNNN`, `linker-2: WWGGG` (5 bp), `cDNA`.

**But the authors processed their own xpress data with the SMART-seq3 offsets.** Measured on the
xpress PBMC atlas (ERR8607752, ERR8607756): raw read 1 is 100 bp and tagged read 1 is 78 bp, i.e.
trimmed by 22 — `cDNA(23-100)`, not 25. And the first base after the trim is 50.0% G (against 57.6%
G for the SMART-seq3 fibroblast library), with no A/T enrichment at the first two positions: the
`WW` signature is absent. So the deposited xpress atlas was either built with the original TSO or
processed as if it were, and in either case the tag/UMI window — the only part a chemistry probe
reads — is identical between the two assays.

The xpress Methods gives one processing setting and it is the SMART-seq3 one: "UMI-containing reads
parsed by detection of the pattern (ATTGCGCAATG) while allowing up to two mismatches"
([PMC9546772](https://europepmc.org/article/MED/35637418)); no `base_definition` is published for
xpress.

**Recommendation for #225's open question:** xpress is not a second read structure. It is the same
parse with an optional two-base spacer, and the difference is invisible to every test that looks at
bases 1–19.

## How the tag is matched, in the authors' own code

Relevant because it fixes both the tolerance and the anchor, and because it defines what happens to
untagged reads.

zUMIs special-cases the literal string. From
[`fqfilter_v2.pl`](https://raw.githubusercontent.com/sdparekh/zUMIs/main/fqfilter_v2.pl):

```perl
if($p2 eq "ATTGCGCAATG"){
    $a = substr($mcrseq,0,length($p2));
    if(Approx::amatch($checkpattern, [ $mm ],$a)){
      $ss3 = "yespattern";
```

`$mm` defaults to 1 and is overridable as `find_pattern: PATTERN;N` (the papers use 2). The match is
against `substr(read, 0, 11)` — **anchored at offset 0, not searched**. And
[`distilReads.pm`](https://raw.githubusercontent.com/sdparekh/zUMIs/main/distilReads.pm) carries the
comment `# especially to check if it is smart-seq3 pattern to retain reads without pattern as PE`,
with `if($ss3 eq "nopattern"){ $c[0] = 1; }` — an untagged read is kept, its cDNA start forced to 1,
its UMI emptied. That is the mechanism behind the exact two-class partition measured above.

## Ontology term, checked live

**`EFO:0022488`, label `Smart-seq3`.** Verified 2026-08-04 against the live EBI OLS4 API, against an
EFO snapshot the service reports as loaded `2026-08-03T00:23:12`:

```console
$ curl -s --get https://www.ebi.ac.uk/ols4/api/v2/entities \
    --data-urlencode 'search=Smart-seq3' --data-urlencode 'size=10'
EFO:0022488 | ['Smart-seq3'] | definedBy ['efo'] | iri http://www.ebi.ac.uk/efo/EFO_0022488
```

- IRI: `http://www.ebi.ac.uk/efo/EFO_0022488`; direct parent `EFO:0010184` ("Smart-like").
- The class's own definition cites `PMID:32518404` (the SMART-seq3 paper) and `PMID:37953195`.
- Neighbours, so nobody files a spec under the wrong one: `EFO:0008930` Smart-seq, `EFO:0008931`
  Smart-seq2, `EFO:0008442` Smart-seq2 protocol, `EFO:0700016` Smart-seq v4.
- **There is no EFO term for SMART-seq3xpress.** `search=Smart-seq3xpress` returns
  `totalElements: 0`, and `search=xpress` returns six unrelated classes (an MS term, an MI tag, three
  CIDO assays, a ROR org). A future xpress leaf would have to reuse `EFO:0022488` or leave the field
  unset — it may not invent one.

**Gotcha worth recording.** The OLS4 *v1* search endpoint that `docs/agents/kb.md` implies
(`https://www.ebi.ac.uk/ols4/api/search?q=...`) returns `HTTP 500 {"status":500,"message":"Raw search
query failed"}` for any query containing a hyphen, quoted or not — `q=Smart-seq3`, `q="Smart-seq3"`
and `q=Smart-seq` all fail while `q=diabetes` succeeds. Use
`https://www.ebi.ac.uk/ols4/api/v2/entities?search=...` for hyphenated assay names.

## Where two sources disagree

Recorded rather than resolved, per the `splitseq` linker precedent.

1. **Index widths.** seqspec's `smart_seq3.spec.yaml` declares `i5` at **10 bp** and `i7` at **8 bp**
   in the same assay. No other source describes an asymmetric pair: the paper says "either 8 or 10 bp
   indexes", scg_lib_structs draws both as 8 bp (Nextera XT `N/S5xx` / `N7xx`), the authors' config
   reads `BC(1-8)` from both index files, and the deposited data measures 8+8 or 10+10, never 10+8.
   Treat seqspec's asymmetry as an artefact of an example file. Nothing here depends on it —
   seqforge does not read index FASTQs for this assay.
2. **cDNA start for xpress.** seqspec and scg_lib_structs both say 25 (`WWGGG` spacer); the authors'
   own deposited xpress data was trimmed at 23. Both are recorded above. Since the divergence is
   entirely downstream of the UMI, it changes counting, not recognition.
3. **The trailing G run.** All sources write `GGG`, exactly three. Measured, cycle 23 is 59.9% G in
   the tagged population of SRR19884922, so a fourth G is common. A `fixed`-sequence test over
   20–22 will pass; a test that asserts cycle 23 is *not* G will not.

## What could NOT be established from a primary source

- **A published numeric tag fraction.** Neither paper prints one in text; both put it in a figure
  panel. The numbers in this note are measurements, clearly labelled as such, not citations.
- **The published (Nature Biotechnology) Methods text.** `doi:10.1038/s41587-020-0497-0` is
  paywalled — `www.nature.com` returns the abstract plus "Access through your institution", and the
  article is not in PMC or Europe PMC (`isOpenAccess: N`, `inEPMC: N`). Every quotation attributed to
  the SMART-seq3 paper above is from the authors' own bioRxiv preprint (817924v1, 2019-10-25,
  CC BY-NC-ND, listed by bioRxiv as published as this DOI). **The preprint's Methods were not
  re-verified against the peer-reviewed version.** The three claims that matter — the tag string, the
  zUMIs coordinates, the 8/10 bp indexes — are each independently corroborated by the authors' own
  code, the open-access xpress paper, or the deposited data, so nothing here rests on the preprint
  alone.
- **The protocols.io text.** `dx.doi.org/10.17504/protocols.io.7dnhi5e` resolves (HTTP 200) to
  <https://www.protocols.io/view/smart-seq3-protocol-36wgq5rjxgk5/v1>, and the xpress protocol is at
  <https://www.protocols.io/view/smart-seq3xpress-bwh4pb8w>, but the site serves a JavaScript shell
  to plain HTTP clients and its v3/v4 API demands `Authorization: Bearer <access_token>`. The records
  are confirmed to exist; their bodies were not read. If a spec ever needs a protocols.io-only value,
  it needs a token first.
- **scg_lib_structs has no dedicated SMART-seq3 page.** The URL pattern used for other assays
  (`methods_html/Smart-seq3.html`) is a 404. The content is on
  `methods_html/SMART-seq_family.html`, which covers SMART-seq, SMART-seq2, SMART-seq3, SMART-seq3xpress
  and FLASH-seq together. Cite the family page.
- **Whether GSE207085's tag fractions are representative.** Five of 1 440 runs were measured, chosen
  pseudo-randomly among runs with >200 000 reads. They cluster tightly (56–69%), but no full-dataset
  sweep was run.

## Reproducing the measurements

The three throwaway scripts used (`probe.py` — bounded FASTQ range read and tag count; `bampeek2.py`
— BGZF-prefix decode and read-length × `RX`-length cross-tab; `bampeek3.py` — post-trim per-cycle
composition) were not committed; they are ~100 lines each and are described precisely enough above to
rewrite. Everything they touch is a public HTTPS range request against `ftp.sra.ebi.ac.uk`, bounded
to 4–6 MB per file, so re-running costs nothing and reads no whole FASTQ.
