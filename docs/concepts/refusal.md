# When it refuses

The most useful thing seqforge does is stop. A tool that always produces an answer is only
trustworthy if you check the answer, and at ten thousand datasets nobody checks. So it is built to
fail loudly where a normal pipeline fails silently.

## Three ways it can end

```mermaid
flowchart TD
    START["a dataset"] --> Q{"can code decide it?"}
    Q -->|yes| OK["<span style='color:#fff'><b>manifest</b><br/>exit 0</span>"]
    Q -->|"something is structurally wrong"| BLK["<span style='color:#fff'><b>blocker</b><br/>exit 3<br/><i>this cannot be processed</i></span>"]
    Q -->|"a real question only<br/>a human can settle"| ASK["<span style='color:#fff'><b>question</b><br/>exit 4<br/><i>tell me, and I'll continue</i></span>"]

    %% The inline white <span> is required on dark fills — see the comment in docs/index.md.
    classDef artifact fill:#00695c,stroke:#004d40
    classDef blocked fill:#b71c1c,stroke:#7f0000
    classDef ask fill:#bf360c,stroke:#7f2400
    class OK artifact
    class BLK blocked
    class ASK ask
```

A refusal is an **exit code**, not a warning in a log. Code decides whether processing may proceed;
the language model never gets a vote, and at most helps phrase the question.

Every blocker carries a reason and an actionable remedy. "Could not process" is not a remedy. "The
barcode read is missing — re-fetch including technical reads, or pull the submitter's original files"
is.

## The failures worth catching are the quiet ones

Loud failures take care of themselves: a corrupt file explodes and you notice. These do not.

**The wrong strand.** About half the reads land unassigned, and the matrix just looks like a thin
dataset. Public metadata essentially never states the strand.

**A trimmed barcode file.** A trimming tool ran before upload. Most reads are still the right length,
so the geometry checks pass — but some shifted, so the barcode is read from the wrong position and
those cells are dropped.

**The wrong genome.** A worm dataset against the human genome barely maps: loud, therefore fine. The
same mistake between two *similar* genomes is silent, and yields a plausible matrix in the wrong
coordinate space.

**Counting only exons on a nuclear sample.** Nuclei are full of unspliced RNA sitting in introns;
count only exons and you throw it away. We measured **40.7%** of a nuclear library, gone. The
chemistry is byte-identical to the whole-cell version, so the reads cannot tell you which you have.

Every one of them exits 0.

## Never ask a question you don't need answered

Refusing is expensive too: a system that interrogates you constantly is one you route around, and
then it protects nobody.

**Don't ask if the answer can't change anything.** Two versions of the 10x chemistry may be
indistinguishable from the reads. If they produce *identical* settings, record both names and move
on. This is computed, not assumed: a check over every pair asserts that "indistinguishable" and
"identical settings" agree.

**Don't ask if you can afford every answer.** Nuclear-versus-whole-cell is unanswerable from the
reads — so count **all five ways at once**. One alignment, five counting rules, one pass. Download
and alignment dominate the cost so completely that the extra counting is close to free.

```mermaid
flowchart LR
    Q["'is this cells or nuclei?'<br/>— unanswerable from the reads"]
    Q --> BAD["<span style='color:#fff'>ask the human<br/><i>exit 4</i></span>"]
    Q --> GOOD["<span style='color:#fff'>count all five ways<br/><i>exit 0</i></span>"]

    %% The inline white <span> is required on dark fills — see the comment in docs/index.md.
    classDef ask fill:#bf360c,stroke:#7f2400
    classDef artifact fill:#00695c,stroke:#004d40
    class BAD ask
    class GOOD artifact
```

Save the interruptions for things that are genuinely exclusive — a genome, an aligner — where you
really do have to choose one.
