# Commands

Every `seqforge` command, one line each, grouped by what you want to do. For a command's options,
run it with `--help`, for example `seqforge study run --help`. The help is always current.

The **Needs** column names what a command uses beyond your own files:

- **Gateway**: the lab's model gateway, at `$LABLLM_GATEWAY`. The command spends money and takes a
  `--ceiling-usd`.
- **Stores**: `liulab-data`'s record store (`$LABDATA_STORE`), paper store (`$LABDATA_PAPERS`) and
  ontology store (`$LABDATA_ONTOLOGIES`).
- **Network**: the public archives (NCBI, the US archive, and ENA, the European one) or a web
  address.
- **Genome**: `liulab-genome`, for the genome and its gene annotation.
- **Cluster**: a step the compiled pipeline runs on the cluster, next to the aligner. You do not
  run it by hand.

A blank cell means the command needs none of these.

## Read studies

| Command | What it does | Needs |
| --- | --- | --- |
| `seqforge study run` | Bound and then read each study of a folder, under one spending limit | Gateway, Stores |
| `seqforge pass start` | Run one job, `study-bounds` or `study-reading`, over its studies | Gateway, Stores |
| `seqforge pass status` | Say where a job stands: the count of each ending, the cost, and each study not done | |
| `seqforge study write-bounds` | Write each study's bounds file from the bounding job's answers | |
| `seqforge study write-reading` | Write each study's reading file from the reading job's answers | `liulab-data` installed |
| `seqforge study check-bounds` | Check one bounds file against its format | |
| `seqforge study check-reading` | Check one reading file against its format | `liulab-data` installed |
| `seqforge study relations` | Print the relations between the samples of one reading: replicates, contrasts, series and controls | `liulab-data` installed |
| `seqforge standard-set list` | List the 50 studies of the standard set, from each study's files | |

The few options you need for `study run` are `--studies` (the folder), `--ceiling-usd` (the most
it may spend, required) and `--keys` (a file of Series ids, one per line).
[Read your own studies](../guides/read-studies.md) walks through them.

## Compile a dataset

| Command | What it does | Needs |
| --- | --- | --- |
| `seqforge run` | FASTQ files and notes in, manifest and pipeline out, in one command. `seqforge compile` is the same command | Gateway unless `--no-llm`; Network or Stores with `--accession`; Genome |
| `seqforge probe` | Read the start of each FASTQ file and report what the reads look like | |
| `seqforge resolve score` | Decide which technology the reads are, from their bytes and the knowledge base | |
| `seqforge io records` | Fetch what the archive says about an accession: project, samples, experiments and runs | Network, or the record store |
| `seqforge io resolve` | Expand an accession into its runs, and flag a read the archive dropped | Network |
| `seqforge records new` | Draft a record set: which FASTQ files in a folder compile together | |
| `seqforge records validate` | Check a record set and say what it declares | |
| `seqforge harvest normalize` | Turn each document, such as a PDF or a spreadsheet, into the text that quotes are checked against | |
| `seqforge harvest extract` | Ask a model to read claims out of a document, each with a quote | Gateway |
| `seqforge harvest verify` | Find each quote in the document again and check that it backs its value | |
| `seqforge manifest fill` | Write the dataset manifest: what the data is | Network or the record store with `--accession` |
| `seqforge manifest validate` | Check a manifest | |
| `seqforge manifest hash` | Print a manifest's content hash, and whether it matches the one recorded | |
| `seqforge processing new` | Write a recipe: what to do with the data, such as the genome to align to | |
| `seqforge processing validate` | Check a recipe | |
| `seqforge processing hash` | Print a recipe's content hash, and whether it matches the one recorded | |
| `seqforge compose` | Compile a manifest and a recipe into a Snakefile, its config and its table of input files | Genome |
| `seqforge project metadata` | Rewrite the sample table and the project file from each assay's manifest | |

[Compile step by step](../tutorials/step-by-step.md) runs these in order on a real dataset.

## Check the knowledge base

The knowledge base is the list of sequencing technologies seqforge knows. Each entry describes its
reads and tests itself.

| Command | What it does | Needs |
| --- | --- | --- |
| `seqforge kb list` | List every technology | |
| `seqforge kb show` | Print one technology's entry as JSON | |
| `seqforge kb lint` | Check every entry against its schema | |
| `seqforge kb roundtrip` | Make fake reads for one technology and check that seqforge recognizes them | |
| `seqforge kb e2e` | Run fake reads through the aligner and check the count table against what went in | Genome, and the STAR aligner |
| `seqforge kb e2e-introns` | The same, with reads from introns, to check that the two ways of counting differ as they should | Genome, and the STAR aligner |
| `seqforge kb e2e-cost` | Measure the aligner's peak memory against the number of reads | Genome, and the STAR aligner |
| `seqforge kb e2e-fit` | Fit one line across cost runs measured separately | |

[Add a technology](../tutorials/adding-a-technology.md) shows how an entry is written and tested.

## Look at results

| Command | What it does | Needs |
| --- | --- | --- |
| `seqforge report` | Write one HTML page and its JSON data for a compile, a study, or both | `liulab-data` installed for a study |
| `seqforge eval report` | Write an HTML page from an `eval run` result | |

`seqforge report --study <folder>/<ID>` alone makes a study's report in its folder. Without
`--study`, it reports on the compile in the current folder, or in the folder `--workspace` names.

## Network and files

| Command | What it does | Needs |
| --- | --- | --- |
| `seqforge io peek` | Read the start of a remote compressed FASTQ file without downloading it | Network |
| `seqforge io probe-remote` | Report what a remote FASTQ file's reads look like, without downloading it | Network |
| `seqforge io probe-sra` | Report what an SRA run's reads look like, without downloading it | Network |
| `seqforge preflight` | Pack the start of each FASTQ file and the notes into one small file that compiles the same way | Network with `--accession` |
| `seqforge strip-fingerprint` | Repack that file without the paper and its figures, so it can be shared | |
| `seqforge io publish-package` | Publish that file to the public benchmark collection | Network |
| `seqforge io onlist list` | List the barcode lists seqforge ships | |
| `seqforge io onlist show` | Show one barcode list's entry as JSON | |
| `seqforge io onlist write` | Write a barcode list as the text the aligner reads | Cluster |
| `seqforge io onlist pack` | Pack a barcode list into the shipped form. For maintainers | |

## Steps inside the compiled pipeline

The Snakefile calls these after alignment. Each needs the cluster.

| Command | What it does |
| --- | --- |
| `seqforge io h5ad` | Package the aligner's count tables as an `.h5ad` file, the format single-cell tools read |
| `seqforge io h5ad-labels` | Add the sample table's labels to one sample's `.h5ad` file |
| `seqforge io qc-bundle` | Bundle one sample's statistics and logs into one file |
| `seqforge io fragments` | Sort, compress and index the fragments file of an ATAC library (open chromatin) |
| `seqforge io fragments-qc` | Summarize a fragments file |
| `seqforge io cram` | Store the aligned reads as a CRAM file, a smaller form of a BAM file, the standard file of aligned reads |
| `seqforge io umi-extract` | Move a plate assay's UMI, the molecule tag, out of the first read |
| `seqforge io umi-count` | Count every cell of a plate into one `.h5ad` file |
| `seqforge io split-chimera` | Split reads aligned to two genomes at once into one file per genome |

## Development

| Command | What it does | Needs |
| --- | --- | --- |
| `seqforge version` | Print the version | |
| `seqforge schema list` | List every file format whose JSON Schema can be printed | |
| `seqforge schema export` | Print the JSON Schema of one file format, or of every one | |
| `seqforge eval list` | List the cases of the test collection, with what each should give | |
| `seqforge eval plan` | Say what a run with models would ask and cost, with nothing spent | |
| `seqforge eval run` | Run the test collection and report how often seqforge was right | Gateway with `--llm` |
| `seqforge hook install` | Add seqforge's guards for Claude Code to `.claude/settings.json` | |
| `seqforge hook check` | Check that each installed guard fires | |
| `seqforge hook pre-tool-use` | The guard that stops a whole-file FASTQ read or an absolute path in a manifest | |
| `seqforge hook post-tool-use` | The guard that checks a manifest again after each edit | |
| `seqforge hook stop` | The guard that keeps Claude from ending its turn while a question is open | |

## Exit codes

Every command reports its result through its exit code.

| Code | Meaning |
| --- | --- |
| `0` | It worked |
| `1` | Something broke, such as a download or a tool that failed |
| `2` | It could not start: a wrong option, a file it cannot read, or a missing gateway or store. Nothing was spent |
| `3` | Blocked or stopped: the data cannot go on as it is, a file failed its check, or a pass reached its spending limit |
| `4` | A person must decide: two sources disagree, or a quote did not check out |

A command's own `--help` says which of these it can return.
