"""Command line interface."""

from __future__ import annotations

import json
from decimal import Decimal

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


@app.command("agents")
def list_agents() -> None:
    """List the 13 agents by department."""
    from restaurant_ai.kernel.registry import all_agents, departments

    agents = all_agents()
    typer.echo(f"{len(agents)} agents across {len(departments())} departments\n")
    for department in departments():
        typer.echo(f"  {department.replace('_', ' ').title()}")
        for spec in sorted(
            (a for a in agents.values() if a.department == department), key=lambda a: a.name
        ):
            gated = (
                f"  [approval: {', '.join(t.name for t in spec.gated_tools)}]"
                if spec.gated_tools
                else ""
            )
            typer.echo(f"    {spec.person:10} {spec.name:20} {spec.title}{gated}")
        typer.echo("")


@app.command("models")
def list_models() -> None:
    """List the models the configured key can actually see.

    Model ids move — Gemini Flash went 3.0 to 3.7 inside a year — and the
    `-latest` aliases are not safe to pin to. This is the answer to a 404.
    """
    from restaurant_ai.kernel import llm

    try:
        names = llm.available_models()
    except Exception as exc:
        typer.echo(f"  {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    configured = {llm.model_name("reasoning"), llm.model_name("conversational")}
    typer.echo(f"\n  {len(names)} model(s) available to this key\n")
    for name in names:
        typer.echo(f"    {'*' if name in configured else ' '} {name}")
    missing = configured - set(names)
    if missing:
        typer.echo(f"\n  configured but NOT available: {', '.join(sorted(missing))}", err=True)
        raise typer.Exit(code=1)
    typer.echo("\n  (* = configured for a tier)")


@app.command("live-check")
def live_check(
    prompt: str = typer.Option(
        "Reply with the single word: ready.", help="What to ask. Keep it small."
    ),
) -> None:
    """Make one real call on each model tier and report what came back.

    Worth spending a few cents on before a full pass: it settles whether the
    credentials work and whether the request shape is one the configured models
    actually accept, rather than discovering both thirteen agents into a run.
    """
    from langchain_core.messages import HumanMessage

    from restaurant_ai.kernel import llm

    described = llm.describe_provider()
    typer.echo(f"\n  provider   {described['provider']}")
    typer.echo(f"  key        {'set' if described['has_key'] else 'NOT SET'}")
    typer.echo(f"  thinking   {described['thinking']}")
    typer.echo(f"  max tokens {described['max_tokens']}\n")

    if described["provider"] == "fake":
        typer.echo(
            "  LLM_PROVIDER=fake — nothing to check. Set it to 'anthropic' or 'google'.",
            err=True,
        )
        raise typer.Exit(code=1)

    failures = 0
    totals = {"input": 0, "output": 0}
    for tier in ("reasoning", "conversational"):
        typer.echo(f"  {tier} ({described[tier]})")
        try:
            response = llm.get_model(tier).invoke([HumanMessage(content=prompt)])
        except Exception as exc:
            failures += 1
            typer.echo(f"    FAILED  {type(exc).__name__}: {exc}", err=True)
            if "404" in str(exc) or "not found" in str(exc).lower():
                # The likeliest first failure by a distance: model ids move, and
                # a bare 404 says nothing about what you should have used.
                typer.echo("    try:    restaurant-ai models", err=True)
            typer.echo("", err=True)
            continue

        from restaurant_ai.kernel.graph import _message_text

        usage = getattr(response, "usage_metadata", None) or {}
        totals["input"] += usage.get("input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
        typer.echo(f"    said    {_message_text(response)!r}")
        typer.echo(
            f"    tokens  {usage.get('input_tokens', '?')} in, "
            f"{usage.get('output_tokens', '?')} out\n"
        )

    typer.echo(f"  total     {totals['input']} in, {totals['output']} out")
    if failures:
        raise typer.Exit(code=1)


@app.command("run-agent")
def run_one(
    name: str = typer.Argument(..., help="Agent name, e.g. stock_reorder."),
    business_date: str = typer.Option(None, help="ISO date; defaults to today."),
    approve: bool = typer.Option(False, help="Auto-approve anything the agent proposes."),
    path: str = typer.Option(
        "auto",
        help="Which planner: auto (follow LLM_PROVIDER), model, or deterministic.",
    ),
    payload: str = typer.Option(
        None,
        help='What triggered the run, as JSON. e.g. \'{"guest_message": "any nut-free mains?"}\'',
    ),
    transcript: bool = typer.Option(False, help="Print the reasoning transcript."),
) -> None:
    """Run a single agent now."""
    import json
    from datetime import date as _date

    from restaurant_ai.approvals.service import resolve
    from restaurant_ai.kernel.registry import get_agent
    from restaurant_ai.kernel.runner import run_agent

    try:
        spec = get_agent(name)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if path not in {"auto", "model", "deterministic"}:
        typer.echo("--path must be auto, model or deterministic.", err=True)
        raise typer.Exit(code=1)

    trigger_payload: dict = json.loads(payload) if payload else {}
    if path != "auto":
        trigger_payload["_force_path"] = path

    outcome = run_agent(
        spec,
        business_date=_date.fromisoformat(business_date) if business_date else None,
        trigger="cli",
        trigger_payload=trigger_payload or None,
    )

    typer.echo(f"\n{spec.person} — {spec.title}")
    typer.echo(f"  run    {outcome.run_id}")
    if outcome.error:
        # Say so on the status line too. Printing "completed" above an error is
        # how a failed run gets skimmed past.
        typer.echo("  status failed")
        typer.echo(f"  error  {outcome.error}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"  status {'awaiting approval' if outcome.interrupted else 'completed'}")
    _report_reasoning(outcome, transcript=transcript)
    typer.echo(f"  {outcome.summary}")

    if not outcome.interrupted:
        return

    proposal = (outcome.interrupt_payload or {}).get("proposals", [{}])[0]
    typer.echo(f"\n  APPROVAL NEEDED - value {proposal.get('value')}")
    typer.echo(f"  {proposal.get('summary')}\n")
    for line in str(proposal.get("detail", "")).splitlines():
        typer.echo(f"  {line}")

    if approve:
        from restaurant_ai.approvals.service import list_pending

        for pending in list_pending():
            if pending["run_id"] == outcome.run_id:
                resolved = resolve(
                    pending["approval_id"], approved=True, resolved_by="cli", note="--approve"
                )
                typer.echo(f"\n  auto-approved: {resolved['summary']}")


@app.command()
def approvals(
    resolve_id: str = typer.Option(None, "--resolve", help="Approval id to resolve."),
    reject: bool = typer.Option(False, help="Reject rather than approve."),
    who: str = typer.Option("cli", help="Who is deciding."),
) -> None:
    """List pending approvals, or resolve one."""
    from restaurant_ai.approvals.service import list_pending
    from restaurant_ai.approvals.service import resolve as do_resolve

    if resolve_id:
        try:
            outcome = do_resolve(resolve_id, approved=not reject, resolved_by=who)
        except (KeyError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"{'Approved' if not reject else 'Rejected'}: {outcome['summary']}")
        return

    pending = list_pending()
    if not pending:
        typer.echo("Nothing awaiting approval.")
        return

    from restaurant_ai.kernel.registry import display_name

    typer.echo(f"{len(pending)} awaiting approval\n")
    for item in pending:
        typer.echo(f"  {item['approval_id']}")
        typer.echo(f"    from   {display_name(str(item['agent']))}   value {item['value']}")
        typer.echo(f"    {item['title']}")
        typer.echo(f"    requested {item['requested_at']}\n")


@app.command("simulate-day")
def simulate(
    business_date: str = typer.Option(None, help="ISO date; defaults to today."),
    auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Answer approval gates automatically."
    ),
    covers: int = typer.Option(None, help="Override the day's base cover count."),
    quiet: bool = typer.Option(False, help="Only print the end-of-day report."),
) -> None:
    """Replay a full service day through the real ingestion and agent path."""
    from datetime import date as _date

    from restaurant_ai.simulation import journals_balance, simulate_day

    target = _date.fromisoformat(business_date) if business_date else None

    def show(step) -> None:
        if quiet:
            return
        marker = {
            "ok": "  ",
            "failed": "!!",
            "awaiting_approval": "??",
            "approved": "->",
        }.get(step.status, "  ")
        typer.echo(f"{marker} {step.at:%H:%M}  {step.label}")
        if step.detail:
            for line in str(step.detail).splitlines()[:3]:
                typer.echo(f"           {line[:100]}")

    typer.echo("Simulating a service day...\n")
    result = simulate_day(
        business_date=target, auto_approve=auto_approve, covers=covers, on_step=show
    )

    typer.echo("")
    typer.echo("=" * 62)
    typer.echo(
        f"  Trading: {result.orders_ingested} orders ingested"
        + (f", {result.orders_rejected} rejected" if result.orders_rejected else "")
    )
    typer.echo(
        f"  Approvals: {result.approvals_requested} requested, {result.approvals_approved} approved"
    )

    report = result.report
    if report:
        typer.echo("")
        typer.echo(f"  END OF DAY - {report['business_date']}")
        typer.echo(f"    Net revenue       {Decimal(report['net_revenue']):>12,.2f}")
        typer.echo(f"    Covers            {report['covers']:>12}")
        typer.echo(f"    Average check     {Decimal(report['average_check']):>12,.2f}")
        typer.echo(
            f"    COGS              {Decimal(report['cogs']):>12,.2f}"
            f"   ({Decimal(report['food_cost_pct']) * 100:.1f}%)"
        )
        typer.echo(
            f"    Labour            {Decimal(report['labour_cost']):>12,.2f}"
            f"   ({Decimal(report['labour_pct']) * 100:.1f}%)"
        )
        typer.echo(
            f"    Prime cost        {Decimal(report['prime_cost']):>12,.2f}"
            f"   ({Decimal(report['prime_cost_pct']) * 100:.1f}%)"
        )
        typer.echo(f"    Operating margin  {Decimal(report['operating_margin_pct']) * 100:>11.1f}%")
        if report.get("reconciliation"):
            rec = report["reconciliation"]
            typer.echo(
                f"    Reconciliation    {'balanced' if rec['balanced'] else 'VARIANCE ' + rec['variance']}"
                f"   ({rec['matched']} matched, {rec['exceptions']} exception(s))"
            )
        if report.get("commentary"):
            typer.echo("")
            for line in _wrap(report["commentary"], 58):
                typer.echo(f"    {line}")
    else:
        typer.echo("\n  No end-of-day report was produced.")

    balanced, details = journals_balance(result.business_date)
    typer.echo("")
    typer.echo(
        f"  Journals: {len(details)} posted, {'all balanced' if balanced else 'NOT BALANCED'}"
    )
    for number, entry in sorted(details.items()):
        flag = "ok" if entry["balanced"] else "UNBALANCED"
        typer.echo(f"    {number}  Dr {entry['debits']:>12}  Cr {entry['credits']:>12}  {flag}")
    typer.echo("=" * 62)

    if result.failures:
        typer.echo(f"\n{len(result.failures)} step(s) failed:", err=True)
        for failure in result.failures[:10]:
            typer.echo(f"  {failure.label}: {failure.detail}", err=True)
        raise typer.Exit(code=1)
    if not balanced:
        typer.echo("\nJournals do not balance.", err=True)
        raise typer.Exit(code=1)


def _report_reasoning(outcome, transcript: bool = False) -> None:
    """Say which planner ran, what it cost, and optionally what it said.

    The token counts are the point: an estimate of what thirteen agents cost to
    run is guesswork, and this is the measurement.
    """
    from langchain_core.messages import AIMessage, ToolMessage

    from restaurant_ai.kernel.graph import _message_text

    messages = (outcome.state or {}).get("messages") or []
    turns = [m for m in messages if isinstance(m, AIMessage)]
    if not turns:
        typer.echo("  path   deterministic")
        return

    def _tokens(field: str) -> int:
        # usage_metadata is a TypedDict and absent entirely on a stub or a
        # cached reply, so both the message and the field have to be optional.
        counts: list[int] = []
        for message in turns:
            usage: dict = dict(message.usage_metadata or {})
            counts.append(int(usage.get(field) or 0))
        return sum(counts)

    tokens_in, tokens_out = _tokens("input_tokens"), _tokens("output_tokens")
    typer.echo(f"  path   model, {len(turns)} turn(s), {tokens_in} tokens in / {tokens_out} out")

    if not transcript:
        return
    typer.echo("")
    for message in messages:
        if isinstance(message, AIMessage):
            said = _message_text(message)
            if said:
                for line in _wrap(said, 74):
                    typer.echo(f"    | {line}")
            for call in message.tool_calls or []:
                typer.echo(f"    -> {call['name']}({json.dumps(call.get('args') or {})})")
        elif isinstance(message, ToolMessage):
            body = str(message.content)
            typer.echo(f"    <- {message.name}: {body[:200]}{'…' if len(body) > 200 else ''}")
    typer.echo("")


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width)


if __name__ == "__main__":  # pragma: no cover
    app()
