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

    assert is_permitted("experiment.organism")
    assert not is_permitted("experiment.organism.extra")
    # What a sample WAS is a study reading's to say: no sample attribute is assertable at all.
    assert not is_permitted("experiment.samples.tissue")
    assert not is_permitted("experiment.anything.you.can.name")
    assert not is_permitted("library.chemistry.value")


def test_only_an_experiment_record_is_asked_anything() -> None:
    """A record document is asked only what its level can answer, and only one level can answer.

    An experiment's protocol paragraph names the chemistry. A sample or run record has no opinion
    about it -- "single nucleus sequencing daf2 replicate 3" contains no chemistry, but it does
    contain words a model could pattern-match on -- and a project's abstract is true of the study
    and of no single library, so the cheapest defence is not asking.
    """
    from seqforge.harvest.fields import fields_for

    assert fields_for("experiment", "reference") == ("library.chemistry",)
    for scope in ("project", "sample", "run"):
        assert fields_for(scope, "reference") == (), scope


def test_a_record_document_may_never_set_processing() -> None:
    """An archive field is an untrusted input. Prose reaching --soloStrand is precisely what we forbid."""
    from seqforge.harvest.fields import fields_for, permitted_for

    for scope in ("project", "sample", "experiment", "run"):
        assert not any(f.startswith("processing.") for f in fields_for(scope, "reference"))
        assert not permitted_for("processing.genome.assembly", scope, "reference")
