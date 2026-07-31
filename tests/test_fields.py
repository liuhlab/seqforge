"""Tests for ``harvest/fields.py`` — what the model may be asked, and what it may set.

The module is one closed vocabulary with two axes: **scope** (which record level a document describes)
and **role** (whether you handed us the document or we downloaded it). Everything here asserts against
that vocabulary directly.

The boundary against the two neighbouring files is the *subject*, not the import. A test that drives
``verify_drafts`` and happens to read ``PERMITTED_FIELDS`` as its oracle belongs in
``test_harvest.py``, because a defect in ``verify`` is what turns it red; the same goes for
``test_prompt_names_only_permitted_fields`` in ``test_extract.py``, whose subject is the prompt. Only
tests that go red when *this table* changes live here.
"""

from __future__ import annotations


def test_the_allowlist_is_exact_match_not_a_prefix_rule() -> None:
    """A prefix rule ("anything under experiment.") would re-open the hole it exists to close."""
    from seqforge.harvest.fields import is_permitted

    assert is_permitted("experiment.samples.tissue")
    assert not is_permitted("experiment.samples.tissue.extra")
    # `condition` was OURS, not NCBI's. It is gone -- see io/attributes.py.
    assert not is_permitted("experiment.samples.condition")
    assert not is_permitted("experiment.anything.you.can.name")
    assert not is_permitted("library.chemistry.value")


def test_the_ask_is_scoped_so_a_biosample_is_never_asked_for_a_chemistry() -> None:
    """A sample record has no opinion about the chemistry, so asking invites a guess from an alias.

    "single nucleus sequencing daf2 replicate 3" contains no chemistry, but it does contain words a
    model could pattern-match on. The cheapest defence is not asking.
    """
    from seqforge.harvest.fields import fields_for

    assert "library.chemistry" not in fields_for("sample", "reference")
    assert "experiment.samples.tissue" in fields_for("sample", "reference")
    # ...and the experiment's protocol paragraph is asked for the chemistry, plus `treatment` (and only
    # treatment): the GSM title carries the diet, which lives nowhere in the typed BioSample fields.
    assert fields_for("experiment", "reference") == (
        "library.chemistry",
        "experiment.samples.treatment",
    )
    # ...but NOT strain/age/tissue: those are the BioSample's own typed fields, and asking the title
    # for them would let "Day6" vs "day6" null a value the record already resolved.
    assert "experiment.samples.strain" not in fields_for("experiment", "reference")
    assert "experiment.samples.age" not in fields_for("experiment", "reference")
    # ...and the project level is asked nothing at all: "wild-type and daf-2 mutants" is true of the
    # study and false of every single sample in it.
    assert fields_for("project", "reference") == ()


def test_a_record_document_may_never_set_processing() -> None:
    """An archive field is an untrusted input. Prose reaching --soloStrand is precisely what we forbid."""
    from seqforge.harvest.fields import fields_for, permitted_for

    for scope in ("project", "sample", "experiment", "run"):
        assert not any(f.startswith("processing.") for f in fields_for(scope, "reference"))
        assert not permitted_for("processing.genome.assembly", scope, "reference")


def test_every_asked_attribute_is_one_ncbi_defines() -> None:
    """Derived, not typed twice. A name we invent here would sail past the manifest validator's
    key check only by being invented in both places -- which is exactly how `condition` survived."""
    from seqforge.harvest.fields import ASKED_SAMPLE_ATTRIBUTES
    from seqforge.io.attributes import is_attribute

    for name in ASKED_SAMPLE_ATTRIBUTES:
        assert is_attribute(name), f"{name!r} is not an NCBI harmonized BioSample attribute"
    assert "condition" not in ASKED_SAMPLE_ATTRIBUTES


def test_the_ask_carries_ncbis_own_definition_not_our_paraphrase() -> None:
    """The prompt is the worst place to keep a definition: nothing checks it, and it is exactly where
    the pilot's misfiling happened. So the text comes out of NCBI's list."""
    from seqforge.harvest.fields import describe_asked
    from seqforge.io.attributes import get_attribute

    text = describe_asked(("experiment.samples.dev_stage",))
    assert get_attribute("dev_stage").description in text
    assert "dev_stage" in text
