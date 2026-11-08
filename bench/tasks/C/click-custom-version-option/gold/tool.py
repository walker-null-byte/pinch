import os

import click


def show(ctx):
    return f"{ctx.command_path} 1.4.2 (config: {os.environ.get('TOOL_CONFIG', 'default')})"


@click.group()
@click.custom_version_option(show)
def cli():
    """Example tool."""


@cli.command()
def hello():
    """Say hello."""
    click.echo("hello")


if __name__ == "__main__":
    cli()
