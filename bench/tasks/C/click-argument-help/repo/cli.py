import click


@click.command()
@click.argument("name")
@click.argument("count", type=int, default=1)
@click.option("--shout", is_flag=True, help="Uppercase the greeting.")
def greet(name, count, shout):
    """Greet NAME, COUNT times."""
    for _ in range(count):
        text = f"Hello, {name}!"
        click.echo(text.upper() if shout else text)


if __name__ == "__main__":
    greet()
