"""The 18 labels of a reading, one row each: what a label holds and every rule that hangs on it.

The format check, the writer's instructions, Jev's questions, the term lookup and the report's
sample tables all read this table, so a label is added, or the vocabulary of its terms changed, in
one row. A row's ``holds`` and ``na`` are the words the writer and Jev read. A label of the study's
own, beyond the 18, takes :data:`OWN`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

#: The topics a label is shown under, one sample table each, in the order the report shows them.
Topic = Literal["Assay", "Organism", "Material", "Treatment & time", "Other"]
TOPICS: tuple[Topic, ...] = ("Assay", "Organism", "Material", "Treatment & time", "Other")


@dataclass(frozen=True)
class LabelRow:
    """One label, and what the reading format and the reading job do with it."""

    name: str
    #: What the label holds, as the writer and Jev are told.
    holds: str
    #: The sample table the report shows the label in. Every row names one, so a new label
    #: cannot be left without one.
    topic: Topic = field(kw_only=True)
    #: Who writes its values: the writing model lists them, or code writes them on its own.
    written_by: Literal["writer", "code"] = "writer"
    #: Whether its claim may carry a number and a unit.
    number: bool = False
    #: Whether it may be a factor.
    factor: bool = True
    #: Whether its claim may say only which one it is, by an id made up in the file.
    id_alone: bool = False
    #: What the id every claim of it carries names, such as the material; ``None`` when a claim
    #: may carry none.
    id_names: str | None = None
    #: Whether every claim of it is quoted or inferred, never unknown or na.
    always_known: bool = False
    #: Whether its term may be a list: one sample can be given several compounds.
    many_terms: bool = False
    #: The vocabulary its terms come from, for a sample of any species not in ``by_species``.
    vocabulary: str | None = None
    #: The vocabulary for a sample of one species, by NCBI Taxonomy id.
    by_species: Mapping[str, str] = field(default_factory=dict)
    #: The words of the format's own its term is one of, which is no ontology id.
    words: tuple[str, ...] = ()
    #: A text, folded to lower case, whose id code maps on its own.
    ids: Mapping[str, str] = field(default_factory=dict)
    #: A text, folded to lower case, whose term code finds by names of its own, the first
    #: whole-text match taken.
    named_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: An example of a sample it does not apply to, as Jev is told.
    na: str | None = None

    def vocabulary_for(self, taxid: str | None) -> str | None:
        """The vocabulary its terms come from for a sample of species ``taxid``."""
        return self.by_species.get(str(taxid), self.vocabulary)


#: The labels every sample has once the shared claims are applied, in the order the file's check
#: and the writer list them.
LABELS: tuple[LabelRow, ...] = (
    LabelRow(
        "biosample",
        "the piece of material the library was made from",
        topic="Material",
        written_by="code",
        id_alone=True,
        id_names="the material",
    ),
    LabelRow(
        "donor",
        "the person or animal the material came from",
        topic="Material",
        written_by="code",
        id_alone=True,
    ),
    LabelRow(
        "assay",
        "the kind of sequencing experiment, such as RNA-Seq, ChIP-Seq or ATAC-seq",
        topic="Assay",
        vocabulary="efo",
    ),
    LabelRow(
        "target",
        "what a pull-down enriches: a histone mark or a protein; na for an assay with no "
        "pull-down and for a control library",
        topic="Assay",
        # The relations already keep samples with different targets apart, and a target has no
        # baseline to compare the others with.
        factor=False,
        na="an assay with no pull-down, or a control library",
    ),
    LabelRow(
        "control_type",
        "the kind of background-control library, such as input or IgG; na for every other library",
        topic="Assay",
        na="a library that is not a background control",
    ),
    LabelRow(
        "species",
        "the species",
        topic="Organism",
        written_by="code",
        vocabulary="ncbitaxon",
        always_known=True,
    ),
    LabelRow(
        "sex",
        "the sex: female, male, hermaphrodite or mixed",
        topic="Organism",
        words=("female", "male", "hermaphrodite", "mixed"),
    ),
    LabelRow("age", "the age, with a number and a unit", topic="Organism", number=True),
    LabelRow(
        "development_stage",
        "the stage of life, such as embryo, larval stage or adult",
        topic="Organism",
        # A rat stage has no stage list.
        by_species={"9606": "hsapdv", "10090": "mmusdv", "6239": "wbls", "7227": "fbdv"},
        # The mouse's adult stage is named neither: its adult stages share the parent mature stage.
        named_by={"adult": ("adult stage", "adult", "mature stage")},
    ),
    LabelRow(
        "tissue",
        "the tissue or organ",
        topic="Material",
        vocabulary="uberon",
        # C. elegans names its tissues in its own anatomy list.
        by_species={"6239": "wbbt"},
    ),
    LabelRow(
        "cell_type",
        "the cell type",
        topic="Material",
        vocabulary="cl",
        na="whole tissue with no cell type isolated",
    ),
    LabelRow(
        "cell_line",
        "the cell line",
        topic="Material",
        vocabulary="cellosaurus",
        na="tissue taken from an animal",
    ),
    LabelRow(
        "disease",
        "the disease or disease model, or healthy",
        topic="Material",
        vocabulary="mondo",
        ids={"healthy": "PATO:0000461"},
    ),
    LabelRow("genotype", "the genotype", topic="Organism"),
    LabelRow(
        "strain", "the strain", topic="Organism", na="a species with no strains, such as human"
    ),
    LabelRow(
        "treatment",
        "what was applied to the sample: a drug, a diet, a vector, an infection or a procedure",
        topic="Treatment & time",
        many_terms=True,
        vocabulary="chebi",
        na="no treatment applied in the study",
    ),
    LabelRow(
        "dose",
        "how much of the treatment",
        topic="Treatment & time",
        number=True,
        na="no treatment",
    ),
    LabelRow(
        "duration",
        "how long the treatment itself was applied",
        topic="Treatment & time",
        number=True,
        na="no treatment",
    ),
)

#: A label of the study's own, which the writer lists under its own name.
OWN = LabelRow("", "a label of the study's own", topic="Other")

_BY_NAME = {r.name: r for r in LABELS}


def label_row(label: str) -> LabelRow:
    """The row of ``label``; :data:`OWN` for a label of the study's own."""
    return _BY_NAME.get(label, OWN)


def vocabulary(label: str, taxid: str | None) -> str | None:
    """The vocabulary a label's terms come from for a sample of species ``taxid``.

    ``None`` when the label takes no ontology term there: one with no vocabulary, such as a
    genotype or a sex, or a stage of a species with no stage list, such as the rat.
    """
    return label_row(label).vocabulary_for(taxid)
