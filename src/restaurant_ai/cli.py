"""Command line interface."""

from __future__ import annotations

import typer

from restaurant_ai.logging_setup import configure_logging

app = typer.Typer(
    name="restaurant-ai",
    help="Autonomous restaurant operations: 13 agents across 6 departments.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    configure_logging()


@app.command()
def seed(
    history_days: int = typer.Option(56, help="Days of synthetic trading history to generate."),
    skip_stock: bool = typer.Option(False, help="Skip opening stock balances."),
) -> None:
    """Load the demo restaurant: menu, recipes, suppliers, staff, tables, SOPs."""
    from restaurant_ai.db.seed import seed_all

    counts = seed_all(history_days=history_days, with_stock=not skip_stock)
    typer.echo("Seeded:")
    for key, value in counts.items():
        typer.echo(f"  {key:22} {value}")


@app.command("reset-db")
def reset_db(
    yes: bool = typer.Option(False, "--yes", help="Confirm dropping every table."),
) -> None:
    """Drop the public schema. Destructive; requires --yes."""
    from sqlalchemy import text

    from restaurant_ai.config import get_settings
    from restaurant_ai.db.base import get_engine

    if not yes:
        typer.echo("Refusing to drop the schema without --yes.", err=True)
        raise typer.Exit(code=1)

    settings = get_settings()
    typer.echo(f"Dropping schema in {settings.postgres_db} @ {settings.postgres_host}...")
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    typer.echo("Schema dropped. Run `make migrate && make seed`.")


@app.command("menu-cost")
def menu_cost(sku: str = typer.Argument(None, help="Limit to one SKU.")) -> None:
    """Show plate cost and margin for the menu, exploded through the recipe BOM."""
    from sqlalchemy import select

    from restaurant_ai.db.base import session_scope
    from restaurant_ai.db.models import MenuItem
    from restaurant_ai.domain.costing import cost_breakdown

    with session_scope() as session:
        stmt = select(MenuItem).where(MenuItem.is_active)
        if sku:
            stmt = stmt.where(MenuItem.sku == sku)
        items = list(session.execute(stmt).scalars())
        if not items:
            typer.echo("No menu items found.", err=True)
            raise typer.Exit(code=1)

        typer.echo(f"{'SKU':14} {'Item':34} {'Price':>8} {'Cost':>8} {'Margin':>8} {'Food%':>7}")
        for item in sorted(items, key=lambda i: i.sku):
            b = cost_breakdown(session, item.id)
            typer.echo(
                f"{b.sku:14} {b.name[:34]:34} {b.price:>8} {b.total_cost:>8} "
                f"{b.gross_margin:>8} {b.food_cost_pct * 100:>6.1f}%"
            )
            if sku:
                for line in b.lines:
                    typer.echo(
                        f"    {line.name[:36]:36} {line.quantity:>12} {line.uom:4} {line.cost:>10}"
                    )


if __name__ == "__main__":  # pragma: no cover
    app()
