import click


@click.group()
def cli():
    """Example tool."""


@cli.command()
def hello():
    """Say hello."""
    click.echo("hello")


if __name__ == "__main__":
    cli()
