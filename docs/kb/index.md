# Knowledge base

The knowledge base is how seqforge knows what a sequencing library is. It holds one entry per
technology: the layout of each read, where the barcodes sit, and which barcode lists they come from.
seqforge scores your files against every entry and picks the one the bytes match. That entry
decides how your data is aligned.

You do not choose an entry. The bytes do. This page answers "is my assay supported?", and helps you
read seqforge's answer when it tells you what your data is.

## Supported assays

These are the technologies seqforge can compile today, from `seqforge kb list`. A barcode read is
the read that carries the cell barcode and the UMI (unique molecular identifier, a random tag on
each molecule). A barcode list is the published set of barcodes a kit can contain.

### Droplet single-cell RNA-seq from 10x Genomics

| Assay | Entry | How seqforge tells it apart |
| --- | --- | --- |
| [Chromium 3' v3 and v3.1](10x-3p-gex-v3.md) | `10x-3p-gex-v3`, `10x-3p-gex-v3.1` | A 28 bp barcode read (16 bp barcode, 12 bp UMI) that hits the v3 barcode list. v3 and v3.1 are processed the same way, so seqforge records both names |
| [Chromium 3' v2](10x-3p-gex-v2.md) | `10x-3p-gex-v2` | A 26 bp barcode read (16 bp barcode, 10 bp UMI) and its own barcode list |
| GEM-X 3' v4 | `10x-gemx-3p-v4` | The v3 read layout, with its own barcode list |
| Chromium 5' v1 and v2 | `10x-5p-gex-v2` | The same reads and barcode list as 3' v2. The paper or archive record must say 5' or 3' |
| GEM-X 5' v3 | `10x-5p-gex-v3` | The v3 read layout, with its own barcode list |
| Multiome, gene expression arm | `10x-multiome-gex` | The v3 read layout, with the Multiome barcode list |
| Multiome, chromatin (ATAC) arm | `10x-multiome-atac` | Three reads: two from the genome, and a barcode read of up to 24 bp holding a 16 bp barcode and no UMI. It gives a fragments file, not a count matrix |

### Other single-cell RNA-seq

| Assay | Entry | How seqforge tells it apart |
| --- | --- | --- |
| [BD Rhapsody WTA, original bead](bd-rhapsody-wta.md) | `bd-rhapsody-wta` | Two fixed linker sequences at fixed places in the barcode read, and three barcode lists |
| [BD Rhapsody WTA, Enhanced bead](bd-rhapsody-wta.md#enhanced-beads-2022) | `bd-rhapsody-wta-enhanced-v1`, `bd-rhapsody-wta-enhanced-v2` | Two short linkers after a 0 to 3 bp insert. The barcode lists tell the 96-label bead from the 384-label bead |
| [SPLiT-seq](splitseq.md) | `splitseq` | Two fixed 30 bp linkers in the barcode read, and three barcode lists, one per round |
| Smart-seq3 | `smartseq3` | One cell per file pair. Some reads start with an 11 bp tag, then an 8 bp UMI |

### Bulk RNA-seq

| Assay | Entry | How seqforge tells it apart |
| --- | --- | --- |
| [Bulk RNA-seq](bulk-rnaseq.md) | `bulk-rnaseq` | No barcode read at all. Paired-end or single-end |

Each linked assay has its own page with its read layout and what goes wrong with it. The other
entries have no page on this site yet. `seqforge kb show <entry>` prints any entry in full.

## How seqforge picks an entry

seqforge reads a bounded head of each file, 2,000 reads by default, and checks the cheapest evidence
first. The number of reads and their lengths come first. Fixed sequences such as linkers come next.
Barcode-list matches come last.

- **A family narrows to its members.** Related kits form a family. The 10x 3' family is one loose
  pattern: a 16 bp barcode at the start of a 26 to 28 bp read. seqforge first places the data in the
  family, then picks the exact kit by read length and barcode list. So a vague mention in a paper,
  such as "10x 3'", still ends in an exact kit chosen from the bytes.
- **Two kits processed the same way are both recorded.** 10x 3' v3 and v3.1 look the same in the
  reads and are aligned with the same settings. seqforge records both names and asks nothing.
- **Two kits processed differently must be told apart.** Usually the barcode list settles it. When
  nothing in the reads can, as for 10x 3' v2 against 5' v1 and v2, seqforge turns to the paper or
  the archive record. If those do not settle it, it stops and asks you (exit code 4) rather than
  guessing.

A paper's claim about the kit is only a hint. It can break a tie the bytes cannot. If the paper and
the bytes disagree, seqforge asks you.

## How an entry is aligned

Each entry names the pipeline that aligns it:

| Pipeline | Used for | What it writes per sample |
| --- | --- | --- |
| STARsolo, the single-cell mode of the STAR aligner | 10x RNA, BD Rhapsody, SPLiT-seq | A count matrix (`.h5ad`) |
| STAR with gene counts | Bulk RNA-seq | Gene counts |
| STAR with UMI counting | Smart-seq3 | One count matrix for the whole plate |
| chromap, a fast aligner for chromatin data | Multiome ATAC | A fragments file (`fragments.tsv.gz`) |

## Add a technology

Every entry is executable. It declares its chemistry precisely enough that seqforge can write
synthetic reads from it, read them back, and check that it recovers what it declared. To teach
seqforge a new chemistry, follow [Add a technology](../tutorials/adding-a-technology.md).

For drawings of each library's structure, the Teichmann Lab's
[scg_lib_structs](https://teichlab.github.io/scg_lib_structs/) is a good reference. Each assay page
links to its entry there where one exists.
