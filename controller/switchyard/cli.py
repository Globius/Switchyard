"""``syctl``: the Switchyard command-line client."""

import typer

from switchyard import __version__

app = typer.Typer(help="Switchyard command-line client.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Switchyard command-line client."""


@app.command()
def version() -> None:
    """Print the Switchyard version."""
    typer.echo(__version__)
