"""The root Typer app and every command group's sub-Typer, wired together.

Defined in one place so a command module can import exactly the group it registers onto without
pulling in its siblings. The ``add_typer`` wiring here fixes the CLI's shape; the verbs attach when
each command module is imported (see this package's ``__init__``). Introspected by
``test_skills.py`` -- a renamed verb goes red there, not here.
"""

from __future__ import annotations

import typer

from .. import __version__

app = typer.Typer(
    name="seqforge",
    help="Compile FASTQ + metadata into a validated library manifest and a Snakemake config.",
    no_args_is_help=True,
    add_completion=False,
)

schema_app = typer.Typer(help="Export JSON Schema from the Pydantic models (the source of truth).")
app.add_typer(schema_app, name="schema")

kb_app = typer.Typer(help="The executable, self-testing knowledge base.")
app.add_typer(kb_app, name="kb")

io_app = typer.Typer(help="The network + onlist surface (pooch-cached, sha256-verified).")
app.add_typer(io_app, name="io")

onlist_app = typer.Typer(help="Barcode-whitelist (onlist) registry.")
io_app.add_typer(onlist_app, name="onlist")

resolve_app = typer.Typer(help="Score bytes + KB into a ranked, escalated chemistry decision.")
app.add_typer(resolve_app, name="resolve")

manifest_app = typer.Typer(
    help="The DATASET manifest: what the data IS. Immutable, one per dataset."
)
app.add_typer(manifest_app, name="manifest")
processing_app = typer.Typer(
    help="The PROCESSING manifest: what to DO with a dataset. Many per dataset."
)
app.add_typer(processing_app, name="processing")

harvest_app = typer.Typer(
    help="Prose/metadata -> span-verified Assertions (the one LLM touchpoint)."
)
app.add_typer(harvest_app, name="harvest")

eval_app = typer.Typer(help="The evals harness: measure what unit tests cannot (brief §9).")
app.add_typer(eval_app, name="eval")

hook_app = typer.Typer(help="Agent hooks: the rules as mechanism, not aspiration (design §4.2).")
app.add_typer(hook_app, name="hook")

project_app = typer.Typer(
    help="Project-level views over a multi-assay compile (sample_metadata.tsv + project.yaml)."
)
app.add_typer(project_app, name="project")


@app.command()
def version() -> None:
    """Print the seqforge version."""
    typer.echo(__version__)
