"""The record set on disk: one loader for both dialects, and the draft a human edits.

**Top-level for the reason ``pipeline.py`` is.** ``records new`` writes this file and three verbs read
one — ``manifest fill --records``, ``run --records``, ``harvest extract --records`` — so the module
that writes the artifact owns reading it, and no caller re-implements either half. Putting the reader
under ``resolve/`` would file it inside the one stage that consumes it and charge every reader the
scoring engine's import surface for what is a YAML parse.

**Two dialects, one loader, no extension dispatch.** A record set arrives either as a cache
``seqforge io records`` wrote — JSON, four archive levels, attributes and prose — or as a file a human
typed about data that never went near an archive. A safe YAML load reads both, because YAML is a
superset of JSON, so ``.json`` and ``.yaml`` are one code path and renaming a file cannot change how
it parses. ``CSafeLoader`` rather than ``safe_load``, for the reason the KB's loader gives: same safe
semantics, libyaml underneath. What the two do not share is what they may *declare*, and ``source`` decides that:

- **anything but ``user``** is an archive transcript, validated exactly as it always was. Tolerant on
  purpose — those files are written by us and are already on disk, so tightening them here would
  refuse caches nobody can re-type.
- **``user``** is structure and nothing else: ``level``, ``id``, ``parent``, ``filenames``. Every
  other key is refused at parse, and ``attributes`` / ``free_text`` are refused loudest. The one
  *value* constrained here is the ``id``, because it is the only one that leaves as something other
  than data: a hand-written id is a grouping key, and a grouping key becomes a filename
  (:data:`_TYPEABLE_ID`).

**Why that strictness is load-bearing and not tidiness.** The metadata resolver grants a record's
typed slot the ``asserted`` basis, on one sentence: a record's typed slot for a sample is a
declaration *about that sample*. That is true of an archive and false of a line a human typed into a
YAML file this morning. Let one attribute through and it silently outranks a harvested claim that
carries a quote which greps back and entails its value — and ``experiment`` is inside
``dataset_hash``, which is never rewritten, so the wrong value is permanent. A lab that does know its
genotypes writes them into a README and harvests them; that path exists and it keeps the span
(ADR-0034). So a loader that accepts an attribute has not widened a schema, it has broken what
``asserted`` means, and this module is where that is stopped.

**Every refusal is a ``Blocker``, never a traceback.** A ``--records`` file is user input, and user
input that cannot be used is the exact shape this compiler already has an exit code for. Callers
catch :class:`RecordSetError`, print its blockers and exit 3, so a mistyped record set reads like
every other refusal and carries a remedy that says what to type.

**The draft makes no guess.** :func:`draft_record_set` writes one run record per run, keyed exactly as
``run_key`` keys it and parented to nothing — so applying it unedited yields the identical samples the
filenames alone would have produced, down to the ``accession`` being ``None``. What it adds is
comments: the run pairs differing only in a sample-sheet ``_S<n>`` token, or only in a flowcell id,
are the two shapes a filename provably cannot resolve, and each one is named beside the record whose
``parent`` would resolve it. It points at the decision; it never takes it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

from .models.blocker import Blocker, BlockerCode, BlockerSubject
from .models.records import (
    USER_SOURCE,
    ArchiveRecord,
    ArchiveRecordSet,
    RecordLevel,
    SubmittedFile,
)

if TYPE_CHECKING:
    from .models.resolve import ValidationReport

#: The only keys a ``source: user`` set may carry. ``io_version`` is deliberately not among them: it
#: is the stamp the transcriber puts on what it fetched, and a hand-written file carrying one would
#: forge the signature the staleness check reads.
_USER_SET_KEYS = ("source", "query", "records")

#: The only keys a ``source: user`` record may carry — the shape of the table in ADR-0034.
_USER_RECORD_KEYS = ("level", "id", "parent", "filenames")

#: The two levels the join reads. ``experiment`` and ``project`` are archive levels: nothing walks an
#: experiment except to map down to a sample, and asking a human to type a level nothing reads is
#: ceremony, which rots.
_USER_LEVELS = ("run", "sample")

#: The two keys whose refusal is the whole point of the dialect. Named separately from "unknown key"
#: so the message can say *why* rather than "not one of four".
_FACT_KEYS = ("attributes", "free_text")

#: What a hand-written id may be made of: an ASCII letter or digit, then any of letters, digits, dot,
#: underscore and hyphen. **Chosen from what consumes the id, not from taste**, and every clause below
#: is a consumer rather than a preference.
#:
#: A user set's id is a GROUPING KEY, and a run with no sample above it is its own sample — so any id
#: here can become a ``ResolvedSample.sample_id``, which is a plain ``str`` all the way down. From
#: there it is written into a ``units.tsv`` cell, read back by the workflow as ``{sample}``, and used
#: as **both** a results directory and a file stem (``<outdir>/<sample>/<sample>.h5ad``). Each of
#: those is a way to be wrong that nothing downstream would catch:
#:
#: - a tab or a newline splits the units row it is written into, so the table silently gains a column
#:   or a record;
#: - a ``/`` makes one sample two path components, and ``.`` or ``..`` names the results directory's
#:   own parent — a compile that writes outside where it said it would;
#: - the workflow interpolates the wildcard into shell commands **unquoted**, so whitespace and shell
#:   metacharacters become argument boundaries and operators;
#: - a leading ``-`` is read as an option by the very commands it is passed to.
#:
#: An allowlist rather than a list of the characters above, because the failure is open-ended: the
#: next consumer of a sample id inherits whatever this admits, and a denylist protects only against
#: the consumers that already exist.
#:
#: **Deliberately no length bound.** The filesystem's limit is on the whole name, and the id is only
#: part of it — the results directory is the caller's, and the suffix the workflow appends is the
#: module's — so a number here would be a guess at a budget this module cannot see.
#:
#: Applied to a ``source: user`` set alone. An archive set's ids are accessions: already well formed,
#: written by us into content-addressed caches nobody can re-type, and tightening them would refuse
#: work that is finished.
_TYPEABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: What a fact-key refusal tells the author to do instead. One sentence, because there is one answer:
#: the path that keeps a span already exists.
_HARVEST_INSTEAD = (
    "Delete it. A fact about a sample enters through `seqforge harvest` — write it in a README or a "
    "methods paragraph and harvest that, so the value arrives with the quote it came from. This file "
    "declares which files compile together and nothing else."
)


class RecordSetError(ValueError):
    """A record set that is not one, carrying the refusals that say why.

    ``blockers`` is the shape that matters: every caller turns it into a ``ValidationReport`` and
    exits on it, so a mistyped ``--records`` file produces the same legible object a failed manifest
    validation does instead of a stack trace out of a YAML parser. A ``ValueError`` because that is
    what a value which cannot be parsed into its type is, and because a caller that has not been
    taught this class still catches something sensible.

    Raised both when reading a file and when :func:`draft_record_set` is asked for a directory it
    cannot write a legal set for — a draft that would not load is a defect, so it is refused where it
    is written rather than where it is read.
    """

    def __init__(self, blockers: Sequence[Blocker]) -> None:
        self.blockers: list[Blocker] = list(blockers)
        super().__init__(" ".join(b.message for b in self.blockers))

    @property
    def report(self) -> ValidationReport:
        """The refusal in the shape ``exit_code_for_report`` reads, so a caller needs no glue."""
        from .models.resolve import ValidationReport

        return ValidationReport(ok=False, blockers=self.blockers, conflicts=[])

    @property
    def envelope(self) -> dict[str, Any]:
        """The refusal as the JSON object every stage prints — the tag, then the report inline.

        ``manifest fill``, ``run`` and ``harvest extract`` each refuse a bad ``--records`` file, and
        each had spelled this dict out by hand. Three copies of one wire shape is three chances for a
        caller's ``error == "records_invalid"`` check to hold on two stages and not the third, and
        the divergence would be invisible from here: nothing in this module fails when a caller
        spells its own envelope differently. The exception already owns the report, so it owns the
        object built from it too.

        Not shared with ``records validate``, which is the one verb whose result IS the verdict: it
        answers with a :class:`~seqforge.models.resolve.RecordSetResult` on stdout, because it was
        asked for the report rather than stopped by it.
        """
        return {"error": "records_invalid", **self.report.model_dump(mode="json")}


def load_record_set(path: Path) -> ArchiveRecordSet:
    """Read a record set — an ``io records`` cache or a hand-written file — or refuse with blockers.

    One function for both because ``source`` and not the extension decides which dialect a file is,
    and a caller holding a ``--records`` path has no business knowing which it was handed.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-unreadable",
                    kind="file",
                    ref=path.name,
                    message=f"--records {path} could not be read: {exc.strerror or exc}.",
                    remedy=(
                        "Check the path. `seqforge io records <accession>` writes one under "
                        "`seqforge/records/`; `seqforge records new <fastq dir>` drafts one for data "
                        "that has no accession."
                    ),
                )
            ]
        ) from exc
    try:
        payload = yaml.load(text, Loader=yaml.CSafeLoader)
    except yaml.YAMLError as exc:
        # The one place a parser message is quoted verbatim: PyYAML names the line and column, which
        # is the only thing that locates a mistyped indent, and no rewording of ours improves on it.
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-unparsable",
                    kind="file",
                    ref=path.name,
                    message=f"{path.name} is not valid YAML or JSON: {exc}",
                    remedy=(
                        "Fix the syntax at the line above. A record set is a mapping with `source`, "
                        "`query` and `records`; every list item under `records` starts with `- `."
                    ),
                )
            ]
        ) from exc
    if not isinstance(payload, Mapping):
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-unparsable",
                    kind="file",
                    ref=path.name,
                    message=(
                        f"{path.name} parsed as {type(payload).__name__}, not a mapping. A record "
                        f"set is one object with `source`, `query` and `records`."
                    ),
                    remedy=(
                        "Start the file with `source: user` and put the records under a `records:` "
                        "key, or re-fetch it with `seqforge io records <accession>`."
                    ),
                )
            ]
        )
    if payload.get("source") == USER_SOURCE:
        return _load_user(payload, path)
    return _load_archive(payload, path)


def _load_archive(payload: Mapping[str, Any], path: Path) -> ArchiveRecordSet:
    """The archive spelling, unchanged — every cache already on disk still loads.

    Deliberately no stricter than the model: unknown keys are dropped here as they always have been,
    because these files are ours, they are content-addressed caches nobody can re-type, and a rule
    that refused one written by an older transcriber would refuse work that is finished. The strict
    dialect is the one a human types, where the cost of a silently-dropped key is a fact nobody can
    trace.
    """
    from pydantic import ValidationError

    try:
        return ArchiveRecordSet.model_validate(payload)
    except ValidationError as exc:
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-invalid",
                    kind="file",
                    ref=path.name,
                    message=(
                        f"{path.name} declares `source: {payload.get('source')!r}`, so it is read as "
                        f"an archive transcript, and it is not a valid one: "
                        f"{'; '.join(_pydantic_lines(exc))}"
                    ),
                    remedy=(
                        "If a human wrote this file, its first line must be `source: user` — that "
                        "is the dialect that declares structure only (`level`, `id`, `parent`, "
                        "`filenames`). If a machine wrote it, re-fetch with `seqforge io records "
                        "<accession>`."
                    ),
                    evidence=_pydantic_lines(exc),
                )
            ]
        ) from exc


def _pydantic_lines(exc: Any) -> list[str]:
    """A validation error as one readable line per problem — ``loc: msg``, no repr of the input."""
    return [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
    ]


def _load_user(payload: Mapping[str, Any], path: Path) -> ArchiveRecordSet:
    """The strict dialect. Collects EVERY refusal before raising, so one edit fixes the whole file."""
    blockers: list[Blocker] = []

    unknown = sorted(k for k in payload if k not in _USER_SET_KEYS)
    if unknown:
        blockers.append(
            _refusal(
                "blk-record-set-unknown-key",
                kind="file",
                ref=path.name,
                message=(
                    f"{path.name} declares {_quoted(unknown)} at the top level. A `source: user` "
                    f"record set carries {_quoted(_USER_SET_KEYS)} and nothing else."
                ),
                remedy=(
                    f"Delete {_quoted(unknown)}. `io_version` in particular is the stamp the "
                    f"transcriber puts on records it fetched; a file you wrote has not been fetched, "
                    f"and carrying one would make a hand-written set read as a machine's transcript."
                ),
                evidence=unknown,
            )
        )

    query = payload.get("query")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        blockers.append(
            _refusal(
                "blk-record-set-invalid",
                kind="file",
                ref=path.name,
                message=f"`query` must be a non-empty string; got {query!r}.",
                remedy=(
                    "Give it the name you call this dataset, or drop the key — it then defaults to "
                    "the file's own stem, since there is no accession a human typed."
                ),
            )
        )
        query = None

    raw = payload.get("records")
    if not isinstance(raw, list) or not raw:
        blockers.append(
            _refusal(
                "blk-record-set-invalid",
                kind="file",
                ref=path.name,
                message=f"{path.name} declares no records, so it groups nothing.",
                remedy=(
                    "Add a `records:` list with one `- level: run` entry per run, each carrying the "
                    "`filenames` it wrote. `seqforge records new <fastq dir>` drafts exactly that."
                ),
            )
        )
        raise RecordSetError(blockers)

    declared: dict[str, str] = {}
    parents: list[tuple[str, str, str]] = []
    filenames_seen: dict[str, str] = {}
    built: list[ArchiveRecord] = []

    for index, item in enumerate(raw):
        ref = f"records[{index}]"
        if not isinstance(item, Mapping):
            blockers.append(
                _refusal(
                    "blk-record-set-invalid",
                    kind="field",
                    ref=ref,
                    message=f"{ref} is a {type(item).__name__}, not a record.",
                    remedy="Each entry under `records:` is a mapping: `- level: run`, then `id:`.",
                )
            )
            continue

        ok = True
        facts = [k for k in _FACT_KEYS if k in item]
        if facts:
            ok = False
            blockers.append(
                _refusal(
                    "blk-record-set-invalid",
                    kind="field",
                    ref=f"{ref}.{facts[0]}",
                    message=(
                        f"{ref} declares {_quoted(facts)}. A `source: user` record set declares "
                        f"structure — which files compile together — and never a fact about what a "
                        f"sample was. A value typed here has no document to grep and no span to "
                        f"verify, yet it would outrank a claim that has both, permanently: sample "
                        f"attributes are hashed into the dataset and the manifest is never rewritten."
                    ),
                    remedy=_HARVEST_INSTEAD,
                    evidence=facts,
                )
            )
        stray = sorted(k for k in item if k not in _USER_RECORD_KEYS and k not in _FACT_KEYS)
        if stray:
            ok = False
            blockers.append(
                _refusal(
                    "blk-record-set-unknown-key",
                    kind="field",
                    ref=f"{ref}.{stray[0]}",
                    message=(
                        f"{ref} declares {_quoted(stray)}. A record carries "
                        f"{_quoted(_USER_RECORD_KEYS)} and nothing else."
                    ),
                    remedy=(
                        f"Delete {_quoted(stray)}. If it was meant to say what the sample IS, "
                        f"{_spliced(_HARVEST_INSTEAD)}"
                    ),
                    evidence=stray,
                )
            )

        level = item.get("level")
        if level not in _USER_LEVELS:
            ok = False
            blockers.append(
                _refusal(
                    "blk-record-set-level",
                    kind="field",
                    ref=f"{ref}.level",
                    message=(
                        f"{ref} declares `level: {level!r}`. A `source: user` record set has two "
                        f"levels, `run` and `sample`, and the join reads exactly those."
                    ),
                    remedy=(
                        "Declare each run, and a `sample` for every group of runs that is one "
                        "library. `experiment` and `project` are the archive's levels: nothing here "
                        "reads an experiment except to walk down to its sample, and a project record "
                        "with no attributes says nothing at all — so writing either is ceremony."
                    ),
                )
            )

        ident = item.get("id")
        if not isinstance(ident, str) or not ident.strip():
            ok = False
            blockers.append(
                _refusal(
                    "blk-record-set-id",
                    kind="field",
                    ref=f"{ref}.id",
                    message=f"{ref} declares no usable `id` (got {ident!r}).",
                    remedy=(
                        "Give every record an id. A run's id names the run; a sample's id is the "
                        "grouping key, and it is the name its matrix will be written under."
                    ),
                )
            )
        else:
            # Two independent refusals over one string, deliberately not an `elif` chain: an id can
            # be both untypeable and a duplicate, and an author fixing one and then being refused
            # for the other is the loop this module collects blockers to avoid.
            if _TYPEABLE_ID.fullmatch(ident) is None:
                ok = False
                blockers.append(_untypeable_id(ref, ident))
            if ident in declared:
                ok = False
                blockers.append(
                    _refusal(
                        "blk-record-set-id",
                        kind="field",
                        ref=f"{ref}.id",
                        message=(
                            f"`{ident}` is declared twice. An id is how `parent` reaches a record, "
                            f"so two records answering to one id makes the level above ambiguous — "
                            f"and the walk up from a run would stop at whichever came first."
                        ),
                        remedy=(
                            f"Rename one of them. A run and the sample it belongs to need DIFFERENT "
                            f"ids: if `{ident}` is the sample you want the matrix named after, give "
                            f"the run its own id and point its `parent` at `{ident}`."
                        ),
                    )
                )
            else:
                declared[ident] = str(level)

        named = ident if isinstance(ident, str) and ident.strip() else ref

        filenames = item.get("filenames")
        names: list[str] = []
        if filenames is not None:
            if not isinstance(filenames, list) or not all(
                isinstance(n, str) and n.strip() for n in filenames
            ):
                ok = False
                blockers.append(
                    _refusal(
                        "blk-record-set-filenames",
                        kind="field",
                        ref=f"{ref}.filenames",
                        message=f"`{named}`: `filenames` must be a list of file names.",
                        remedy=(
                            "Write them as a YAML list of BASENAMES — `filenames: [a_R1.fastq.gz, "
                            "a_R2.fastq.gz]`. Never a path: a manifest is machine-independent, and "
                            "the join is by name."
                        ),
                    )
                )
            else:
                names = list(filenames)
        if level == "run" and not names:
            ok = False
            blockers.append(
                _refusal(
                    "blk-record-set-filenames",
                    kind="field",
                    ref=f"{ref}.filenames",
                    message=(
                        f"run `{named}` declares no filenames, so no file on disk can reach it. A "
                        f"run that claims no file leaves those files unclaimed, and a set that does "
                        f"not account for every file is refused at the join rather than half-applied."
                    ),
                    remedy=(
                        "List the basenames this run wrote, every mate and every lane: "
                        "`filenames: [x_L001_R1_001.fastq.gz, x_L001_R2_001.fastq.gz]`."
                    ),
                )
            )
        if level == "sample" and names:
            ok = False
            blockers.append(
                _refusal(
                    "blk-record-set-filenames",
                    kind="field",
                    ref=f"{ref}.filenames",
                    message=(
                        f"sample `{named}` declares filenames. A sample is reached THROUGH its runs "
                        f"— the files hang off the run, and the sample is what several runs point at."
                    ),
                    remedy=(
                        f"Move those names onto a `- level: run` record and give it "
                        f"`parent: {named}`. Two runs pointing at one sample is how you say they are "
                        f"one library."
                    ),
                )
            )
        for name in names:
            if name in filenames_seen and filenames_seen[name] != named:
                ok = False
                blockers.append(
                    _refusal(
                        "blk-record-set-filenames",
                        kind="field",
                        ref=f"{ref}.filenames",
                        message=(
                            f"`{name}` is declared by both `{filenames_seen[name]}` and `{named}`. "
                            f"One file belongs to one run, and the join keeps whichever it reads "
                            f"last — so the second claim would silently move the file's sample."
                        ),
                        remedy=(
                            "Delete the duplicate. To say two runs are one library, give them the "
                            "same `parent`; never the same file."
                        ),
                    )
                )
            else:
                filenames_seen[name] = named

        parent = item.get("parent")
        if parent is not None:
            if not isinstance(parent, str) or not parent.strip():
                ok = False
                blockers.append(
                    _refusal(
                        "blk-record-set-parent",
                        kind="field",
                        ref=f"{ref}.parent",
                        message=f"`{named}`: `parent` must be the id of another record, or absent.",
                        remedy="Write `parent: <the sample's id>`, or delete the key.",
                    )
                )
            elif level == "sample":
                ok = False
                blockers.append(
                    _refusal(
                        "blk-record-set-parent",
                        kind="field",
                        ref=f"{ref}.parent",
                        message=(
                            f"sample `{named}` declares a parent. The hierarchy here is one hop deep "
                            f"— a run points at its sample, and a sample points at nothing."
                        ),
                        remedy="Delete the `parent` line from the sample record.",
                    )
                )
            else:
                parents.append((ref, named, parent))

        if ok:
            built.append(
                ArchiveRecord(
                    level=cast(RecordLevel, level),
                    accession=str(ident),
                    parent=parent if isinstance(parent, str) else None,
                    submitted_files=[SubmittedFile(filename=n) for n in names],
                )
            )

    for ref, named, parent in parents:
        if parent not in declared:
            blockers.append(
                _refusal(
                    "blk-record-set-parent",
                    kind="field",
                    ref=f"{ref}.parent",
                    message=(
                        f"run `{named}` points at `{parent}`, which no record in this set declares. "
                        f"The walk up would stop at the run, and the run's own id would silently "
                        f"become the sample id — one typo, and a library quietly splits in two."
                    ),
                    remedy=(
                        f"Add the record it names — `- level: sample` with `id: {parent}` — or fix "
                        f"the spelling to match one of: {_quoted(sorted(declared))}."
                    ),
                    evidence=sorted(declared),
                )
            )
        elif declared[parent] != "sample":
            blockers.append(
                _refusal(
                    "blk-record-set-parent",
                    kind="field",
                    ref=f"{ref}.parent",
                    message=(
                        f"run `{named}` points at `{parent}`, which is a {declared[parent]} and not "
                        f"a sample. A run's parent is the sample it belongs to; the walk up looks "
                        f"for a sample and would find none."
                    ),
                    remedy=(
                        "Point it at a `- level: sample` record. To fuse two runs into one library, "
                        "declare a sample and give BOTH runs `parent: <that sample's id>`."
                    ),
                )
            )

    if blockers:
        raise RecordSetError(blockers)
    return ArchiveRecordSet(
        source=USER_SOURCE,
        # No accession was ever typed, so the file's own name is what the set was asked for. Kept
        # non-empty because it is what refusals downstream print when they name the set.
        query=query if isinstance(query, str) else (path.stem or USER_SOURCE),
        records=built,
    )


def _untypeable_id(ref: str, ident: str) -> Blocker:
    """An id that cannot safely be a sample id, refused where the human's input arrives.

    **Refused here and nowhere else**, because this is the one module whose job is refusing what must
    not get through, and because every layer below has already stopped being able to. A ``source:
    user`` id is a grouping key; a run parented to nothing is its own sample; so this string becomes a
    ``ResolvedSample.sample_id``, which is a plain ``str``, then a ``units.tsv`` cell, then a
    directory, a file stem and an unquoted shell word. By the time a tab has split a units row or a
    ``/`` has made one sample two path components, the manifest is written, content-addressed and
    permanent, and what fails is a workflow several stages away naming none of this.

    The remedy carries the nearest legal spelling rather than only the rule. An author who must
    derive their own replacement from a character class is an author who will retype it wrong once,
    and this is a refusal they hit while typing the file rather than while reading a design doc.
    """
    return _refusal(
        "blk-record-set-id",
        kind="field",
        ref=f"{ref}.id",
        message=(
            f"`{ident}` cannot be an id. In a `source: user` set an id is the grouping key — a run "
            f"with no sample above it is its own sample — so it is written into `units.tsv`, and it "
            f"becomes both the results directory and the name of the `.h5ad` inside it. A tab or a "
            f"newline splits the units row; a `/` (or a leading `.`) makes one sample two path "
            f"components; a space or a shell character is an argument boundary where the pipeline "
            f"interpolates it; a leading `-` is read as an option."
        ),
        remedy=(
            f"Use letters, digits, `.`, `_` and `-`, starting with a letter or a digit — "
            f"`{_typeable_suggestion(ident)}` is the nearest spelling of what you typed. Change it "
            f"on the record and on every `parent` naming it."
        ),
        evidence=[ident],
    )


def _typeable_suggestion(ident: str) -> str:
    """The nearest id the rule admits: every refused character to ``_``, and a bad lead dropped.

    A suggestion and never an application. Renaming a sample is the author's decision — the id is
    what the matrix will be called and what the dataset is grouped by — and quietly rewriting it here
    would move a grouping on their behalf, in the one file that exists so a human can decide it.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", ident).lstrip("._-")
    return cleaned or "sample1"


def _refusal(
    blocker_id: str,
    *,
    kind: Literal["file", "field", "dataset"],
    ref: str,
    message: str,
    remedy: str,
    evidence: Sequence[str] = (),
) -> Blocker:
    """One refusal, in the one shape. Every code here is the same; the id says which rule was hit."""
    return Blocker(
        id=blocker_id,
        code=BlockerCode.RECORD_SET_INVALID,
        message=message,
        remedy=remedy,
        subject=BlockerSubject(kind=kind, ref=ref),
        evidence=list(evidence),
    )


def _quoted(items: Sequence[str]) -> str:
    return ", ".join(f"`{i}`" for i in items)


def _spliced(sentence: str) -> str:
    """A standalone sentence, joined onto the end of another: its first letter lowered, nothing else.

    :data:`_HARVEST_INSTEAD` opens one remedy and finishes another, and the only difference between
    the two uses is the case of one character. Reaching into the constant by index at the call site
    states a fact about that string's first byte in a place that cannot say why it is being stated —
    and it silently stops being right the day the sentence is reworded to open with an acronym.
    """
    return sentence[:1].lower() + sentence[1:]


# ================================================================================================
# the draft
# ================================================================================================

#: The sample-sheet entry bcl2fastq writes — `_S1`, `_S12`. Never stripped from a run key, because it
#: is the one token separating two libraries on one flowcell; it is ALSO what a library resequenced
#: for depth comes back with, and no filename tells those two apart. Hence a comment and not a guess.
_SAMPLE_SHEET_TOKEN = re.compile(r"S\d+")

#: What a flowcell id looks like in a filename — `HJ7L2BGXX`, `22GVYKLT3`. Deliberately narrow: eight
#: or more UPPERCASE alphanumerics carrying at least one letter and one digit. A false negative costs
#: a comment nobody reads; a false positive costs a line of noise in every draft a lab ever opens, and
#: nothing here decides anything, so the pattern errs toward saying less.
_FLOWCELL_TOKEN = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]{8,}")

#: Filename token separators, kept in the split so a blanked comparison also compares punctuation.
_SEPARATORS = re.compile(r"([._-])")


def draft_record_set(fastq_dir: Path) -> str:
    """The YAML `seqforge records new <dir>` writes: one run per run, and no guess anywhere.

    Every record is a run keyed exactly as the grouping keys it, parented to nothing — so a run is its
    own sample and applying this file unedited produces the samples the filenames already produced,
    with the same ids and the same absent accession. That is the property that makes it safe to write
    a file into somebody's dataset directory: the draft cannot change an answer, only record one.

    The comments are the whole value. Two shapes are invisible to a filename and expensive to get
    wrong — a library resequenced under a new `_S<n>`, and a library split across two flowcells — and
    both compile as two samples at partial depth, at exit 0, with nothing downstream disagreeing. So
    each candidate is named beside the record whose `parent` would resolve it, phrased as the question
    only the person who ran the sequencer can answer.

    Raises :class:`RecordSetError` on the two directories it cannot draft a **loadable** set for —
    one holding no FASTQ, and one whose run keys would not pass :data:`_TYPEABLE_ID`. A draft nothing
    can load is a defect of ours rather than of the caller's data, and the caller is the one who would
    be told so: ``records new -o`` reads its own output back and reports a failure there as a bug in
    seqforge. Refusing here keeps that report honest and costs one pass over names already in hand.
    """
    # Local: reading a record set must not pay for importing the scoring engine, and the package
    # `__init__` behind `resolve.group` pulls the whole of it.
    from .resolve.group import group_runs, is_fastq_name

    if not fastq_dir.is_dir():
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-no-fastq",
                    kind="file",
                    ref=fastq_dir.name,
                    message=f"{fastq_dir} is not a directory.",
                    remedy="Point `records new` at the directory holding this dataset's FASTQ files.",
                )
            ]
        )
    names = sorted(
        # The predicate belongs to the module that STRIPS the extension: a file this filter admitted
        # and `run_key` could not strip would be keyed by a name still carrying its suffix.
        entry.name
        for entry in fastq_dir.iterdir()
        if entry.is_file() and is_fastq_name(entry.name)
    )
    if not names:
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-no-fastq",
                    kind="file",
                    ref=fastq_dir.name,
                    message=f"{fastq_dir} holds no FASTQ files, so there are no runs to declare.",
                    remedy=(
                        "Point `records new` at the directory the reads are in. A record set groups "
                        "files by name, so the files have to be there to be named."
                    ),
                )
            ]
        )

    groups = group_runs(names)
    # A run key is a filename with its extension and its mate/lane tokens taken off, so a directory
    # of oddly-named reads yields an id the loader above will not take — and the draft would then be
    # written, read back by `records new -o`, and reported as a bug in seqforge, which is exactly
    # what it is not. Refused here, naming the files, so the "a draft always loads" promise holds by
    # construction rather than by the coincidence that most FASTQ are named sanely.
    untypeable = sorted(key for key in groups if _TYPEABLE_ID.fullmatch(key) is None)
    if untypeable:
        raise RecordSetError(
            [
                _refusal(
                    "blk-record-set-id",
                    kind="file",
                    ref=fastq_dir.name,
                    message=(
                        f"{len(untypeable)} of the runs in {fastq_dir} key to a name that cannot be "
                        f"a sample id ({_quoted(untypeable[:6])}"
                        f"{', ...' if len(untypeable) > 6 else ''}). With no record set above them "
                        f"these runs ARE the samples, and a sample id becomes a `units.tsv` cell, a "
                        f"results directory and the name of an `.h5ad`."
                    ),
                    remedy=(
                        "Rename those files so the part before the mate token is letters, digits, "
                        "`.`, `_` and `-`, starting with a letter or a digit — then re-run `records "
                        "new`. The same names would compile with no record set at all, and break "
                        "much later, where nothing names the file that caused it."
                    ),
                    evidence=untypeable,
                )
            ]
        )
    notes = _candidate_notes(list(groups))
    query = fastq_dir.resolve().name or USER_SOURCE

    lines = [
        "# A record set: which files compile together, and nothing else.",
        "#",
        "# Drafted by `seqforge records new`. As written it declares one sample per run — the same",
        "# grouping seqforge already derives from these filenames — so applying it unedited changes",
        "# nothing. Edit it to say the one thing a filename cannot: that two runs are ONE library.",
        "#",
        "#   - level: sample",
        "#     id: my_library          # the name its matrix will be written under",
        "# ...then add `parent: my_library` to each run that belongs to it.",
        "#",
        "# It declares STRUCTURE ONLY. There is no place here for a strain, a tissue or a genotype:",
        "# a line typed here has no document to grep and no span to verify, and it would outrank a",
        "# claim that has both. Write those in a README and run `seqforge harvest` over it.",
        "#",
        _scan_line(notes),
        yaml.safe_dump({"source": USER_SOURCE, "query": query}, sort_keys=False).rstrip("\n"),
        "records:",
    ]
    for key, paths in groups.items():
        lines.extend(notes.get(key, []))
        lines.append(_item({"level": "run", "id": key, "filenames": [p.name for p in paths]}))
    return "\n".join(lines) + "\n"


def _scan_line(notes: Mapping[str, list[str]]) -> str:
    """State what the scan found, including when it found nothing.

    An absent comment and a check that ran and came back empty look identical, and only one of them
    means "these runs are unambiguous". So the draft always says which it was.
    """
    if not notes:
        return (
            "# No two runs here differ only in an `_S<n>` token or only in a flowcell id, so there "
            "is\n# nothing this draft has left for you to decide."
        )
    return (
        f"# {len(notes)} group(s) below are marked with a decision only you can make. Nothing has\n"
        f"# been applied — a draft you ignore leaves the grouping exactly as it is."
    )


def _item(mapping: dict[str, Any]) -> str:
    """One record as a YAML list item, dumped rather than formatted so odd names stay quoted."""
    body = yaml.safe_dump(mapping, sort_keys=False, allow_unicode=True, width=10_000).rstrip("\n")
    head, *rest = body.split("\n")
    return "\n".join([f"  - {head}", *(f"    {line}" for line in rest)])


def _candidate_notes(keys: Sequence[str]) -> dict[str, list[str]]:
    """Comment lines for each candidate group, keyed by the run they are written above."""
    notes: dict[str, list[str]] = {}
    for members, tokens in _differ_only_at(keys, _SAMPLE_SHEET_TOKEN):
        notes.setdefault(members[0], []).extend(
            _comment(
                f"{_and(members)} differ only in {_slashed(tokens)} — the sample-sheet entry. That "
                f"token separates two libraries loaded on one flowcell, and it is also what a "
                f"library resequenced for depth comes back with; a filename cannot tell those "
                f"apart, and seqforge will not guess. If they are ONE library, declare a sample and "
                f"give each of these runs the same `parent`. If they are two, change nothing."
            )
        )
    for members, tokens in _differ_only_at(keys, _FLOWCELL_TOKEN):
        notes.setdefault(members[0], []).extend(
            _comment(
                f"{_and(members)} differ only in {_slashed(tokens)}, which reads as a flowcell id — "
                f"the shape of one library split across two flowcells. If it is one library, give "
                f"these runs the same `parent`; each would otherwise be its own sample at part of "
                f"the depth. If they are two libraries that share a name, change nothing."
            )
        )
    return notes


def _differ_only_at(
    keys: Sequence[str], token: re.Pattern[str]
) -> list[tuple[list[str], list[str]]]:
    """Run keys identical but for ONE token matching ``token``, grouped, with those tokens.

    Grouped rather than paired on purpose: a 96-well plate named `plate_S1` … `plate_S96` is one
    candidate a reader can act on, and 4560 pairwise comments nobody would read.
    """
    buckets: dict[tuple[int, tuple[str, ...]], dict[str, str]] = {}
    for key in keys:
        parts = _SEPARATORS.split(key)
        for index, part in enumerate(parts):
            if token.fullmatch(part):
                blanked = (index, tuple(parts[:index] + [""] + parts[index + 1 :]))
                buckets.setdefault(blanked, {})[key] = part
    groups = [
        (list(members), list(members.values())) for members in buckets.values() if len(members) > 1
    ]
    return sorted(groups, key=lambda group: group[0][0])


def _comment(text: str) -> list[str]:
    """Wrap prose into indented `#` lines that sit above the record they are about."""
    import textwrap

    return [f"  # {line}" for line in textwrap.wrap(text, width=94)]


def _and(items: Sequence[str]) -> str:
    quoted = [f"`{i}`" for i in items]
    if len(quoted) == 2:
        return " and ".join(quoted)
    return ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


def _slashed(items: Sequence[str]) -> str:
    return " / ".join(f"`{i}`" for i in items)


__all__ = ["RecordSetError", "draft_record_set", "load_record_set"]
