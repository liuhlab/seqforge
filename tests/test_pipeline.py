"""Tests for ``seqforge.pipeline`` — the one owner of a compiled pipeline directory's layout.

Tested **directly**, at its own seam, and that is deliberate rather than thorough-for-its-own-sake.
This module's entire justification is that one derivation exists once — which **Workflow module**
ran, which samples the run was contracted to produce, where its outputs went — and no consumer-level
test can show that the composer, the report, the gates and the ground-truth harness all go through
it. A test that reached the same answers through the report would pass just as happily against four
copies of the derivation, which is the state this module replaced.

So: compose a real directory and ask it every question it exists to answer, then ask the same
questions of the directories that are *not* whole — the never-composed workspace, the half-written
one, the one carrying a module this build does not know. The degradations are the interesting half,
because every one of them is a page a reader still has to be able to open.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from conftest import Built, _processing, declare_read_floor, one_run_each, plate_of
from seqforge.compose import compose
from seqforge.pipeline import (
    CONFIG_NAME,
    DEFAULT_OUTDIR,
    EXCLUSIONS_NAME,
    SNAKEFILE_NAME,
    UNITS_TSV_NAME,
    CompiledPipeline,
)
from seqforge.workflows import MODULES
from seqforge.workspace import pipeline_dir


def test_the_owner_answers_every_question_a_composed_directory_can_be_asked(
    built_v3: Built, tmp_path: Path
) -> None:
    """Compose one pipeline, open it through the owner, and take all five answers off it.

    The paths are checked against the ones ``compose`` *reports*, never against a layout rebuilt
    here: the directory name folds in the run id, and a test that spells it is testing its own
    arithmetic. What is being asserted is that the writer and the reader agree — which is the whole
    claim, since they are now the same three constants.
    """
    manifest, reg = built_v3
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)

    pipeline = CompiledPipeline.discover(tmp_path)
    assert pipeline is not None, "a workspace that just composed must have a pipeline to find"

    # 1. the three files, at the paths the composer reported writing them to
    assert pipeline.directory == (tmp_path / result.config_path).parent
    assert pipeline.snakefile == tmp_path / result.snakefile_path
    assert pipeline.config_path == tmp_path / result.config_path
    assert pipeline.units_path == tmp_path / result.units_path
    assert all(p.is_file() for p in (pipeline.snakefile, pipeline.config_path, pipeline.units_path))

    # 2. which Workflow module ran, off the .smk copied in beside the wrapper
    assert pipeline.module == result.modules[0].name == "map/starsolo"

    # 3. the config, as the composer wrote it. Checked against the manifest the composer was handed,
    #    never by re-reading the file with `.config`'s own body -- that cannot fail.
    assert pipeline.config["chemistry"] == list(manifest.library.chemistry.value)

    # 4. the samples the pipeline is contracted to produce -- the manifest's, via the config
    assert pipeline.samples == [s.sample_id for s in manifest.experiment.samples]

    # 5. where the outputs land, and where one sample's land. The subdirectory is spelled out rather
    #    than taken from `DEFAULT_OUTDIR`, because `results/` is a name a user reads off their own
    #    disk: renaming the constant should cost a red test, not pass silently on both sides.
    assert pipeline.results_dir == pipeline.directory / "results"
    assert pipeline.sample_dir("s1") == pipeline.directory / "results" / "s1"


def test_which_module_ran_is_inverted_out_of_the_registry_rather_than_matched_by_name(
    tmp_path: Path,
) -> None:
    """Every registered module is recognised from its own ``.smk``, and a stranger is not.

    Parametrized over the registry itself, so a fourth **Workflow module** is covered the day it is
    registered — which is the property a hardcoded ``{"starsolo.smk": "map/starsolo"}`` cannot have:
    the new module would simply be missing from it, would answer nothing, and nothing would fail.
    """
    for name, module in MODULES.items():
        directory = tmp_path / f"pipeline-for-{module.snakefile.stem}"
        directory.mkdir()
        shutil.copy2(module.snakefile, directory / module.snakefile.name)
        assert CompiledPipeline(directory).module == name

    stranger = tmp_path / "not-a-pipeline"
    stranger.mkdir()
    (stranger / "someone_elses.smk").write_text("rule all:\n    input: []\n")
    assert CompiledPipeline(stranger).module is None, (
        "a .smk this build does not ship names no module -- answering a guess would be worse"
    )


def test_a_directory_missing_its_config_still_answers_every_question(tmp_path: Path) -> None:
    """Absent, unreadable and unparseable configs are one fact to a reader: there is nothing to say.

    A half-written pipeline directory is a real state — a preempted compile, a partial copy — and the
    page that reports it must still render. Each degradation is asserted separately because they
    arrive by different routes and only the first is obvious.
    """
    # The one place the constant is bound to the literal the rest of this file spells, so renaming it
    # costs exactly one red line here rather than passing silently on both sides of every assertion.
    assert DEFAULT_OUTDIR == "results"

    empty = tmp_path / "empty"
    empty.mkdir()
    bare = CompiledPipeline(empty)
    assert bare.config == {}
    assert bare.samples == []
    assert bare.module is None
    # No config means no `outdir`, so the results directory falls back rather than becoming the
    # pipeline directory itself -- which would make every stray file in it look like a result. The
    # name is spelled out, not read off `DEFAULT_OUTDIR`: the fallback and the composer's default
    # have to be the same string, and a test that reads the constant proves only that it equals
    # itself.
    assert bare.results_dir == empty / "results"

    unparseable = tmp_path / "unparseable"
    unparseable.mkdir()
    (unparseable / CONFIG_NAME).write_text("outdir: [unclosed\n")
    assert CompiledPipeline(unparseable).config == {}

    not_a_mapping = tmp_path / "not-a-mapping"
    not_a_mapping.mkdir()
    (not_a_mapping / CONFIG_NAME).write_text("- outdir\n- results\n")
    assert CompiledPipeline(not_a_mapping).config == {}
    assert CompiledPipeline(not_a_mapping).samples == []


def test_the_config_is_read_from_disk_at_every_call(tmp_path: Path) -> None:
    """Not cached at open, and the ground-truth harness is why.

    ``kb e2e`` composes, patches the config it just read, and then reads it back to learn which
    sample to open. A value cached when the directory was opened would hand that second read the file
    the first one replaced -- a stale answer that looks exactly like a correct one.
    """
    directory = tmp_path / "run"
    directory.mkdir()
    pipeline = CompiledPipeline(directory)
    pipeline.config_path.write_text(yaml.safe_dump({"samples": ["s1"], "outdir": "results"}))
    assert pipeline.samples == ["s1"]

    pipeline.config_path.write_text(
        yaml.safe_dump({"samples": ["s1", "s2"], "outdir": "elsewhere"})
    )
    assert pipeline.samples == ["s1", "s2"]
    assert pipeline.results_dir == directory / "elsewhere"
    assert pipeline.sample_dir("s2") == directory / "elsewhere" / "s2"


def test_an_assay_finds_its_own_pipeline_and_an_uncomposed_workspace_finds_none(
    built_v3: Built, tmp_path: Path
) -> None:
    """``discover`` is per assay: a subdirectory compile is invisible to the flat lookup, and vice versa.

    The multi-assay layout puts one assay per subdirectory, so "where is the pipeline" has no answer
    that is not scoped to an assay. A lookup that fell back to searching the whole workspace would
    hand a heterogeneous project another assay's configuration and be right most of the time.
    """
    assert CompiledPipeline.discover(tmp_path) is None, "nothing composed, nothing to find"

    manifest, reg = built_v3
    compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path, subdir="assay-a")

    found = CompiledPipeline.discover(tmp_path, subdir="assay-a")
    assert found is not None
    assert found.directory.parent == pipeline_dir(tmp_path, subdir="assay-a")
    assert found.snakefile.is_file()
    assert CompiledPipeline.discover(tmp_path) is None, (
        "the flat lookup must not reach into an assay subdirectory"
    )


def test_the_three_filenames_are_the_ones_the_composer_writes(
    built_v3: Built, tmp_path: Path
) -> None:
    """The constants are not a second opinion about what is on disk -- they name what landed.

    Read off the composed directory rather than asserted against literals, so the check is that the
    writer and the names agree, not that the names still say what they said when this was written.
    """
    manifest, reg = built_v3
    compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    pipeline = CompiledPipeline.discover(tmp_path)
    assert pipeline is not None

    written = {item.name for item in pipeline.directory.iterdir()}
    assert {SNAKEFILE_NAME, CONFIG_NAME, UNITS_TSV_NAME} <= written


def test_the_exclusion_record_is_named_here_and_absent_when_nothing_was_excluded(
    built_v3: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sixth file: named by the layout's owner whether or not one is there, written only if it is.

    Absent is the honest reading of "nothing was excluded", so the property is a join like the other
    four and a caller asks the filesystem — the alternative, a `None` when the file is missing, would
    make every reader spell the same fallback. Composing without a floor and composing with one that
    drops a cell are the two states, and the name has to be the same in both or the reader looking for
    an explanation would not find the one that exists.
    """
    manifest, reg = built_v3
    compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path / "shipped")
    shipped = CompiledPipeline.discover(tmp_path / "shipped")
    assert shipped is not None
    assert shipped.exclusions_path == shipped.directory / EXCLUSIONS_NAME
    assert not shipped.exclusions_path.exists(), "no floor is declared, so nothing was excluded"

    plate = plate_of(manifest, one_run_each({"cell1": 4000, "cell2": 400}))
    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], 1000)
    compose(plate, _processing(plate), registry=reg, workspace=tmp_path / "plate")
    gated = CompiledPipeline.discover(tmp_path / "plate")
    assert gated is not None
    assert gated.exclusions_path.is_file()
    assert gated.samples == ["cell1"], "the contracted list is the post-drop one"
