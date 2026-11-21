from __future__ import annotations

import platform
import sys
import time
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from claudeshift import __version__
from claudeshift.config.paths import resolve_runtime_paths
from claudeshift.core.vault import VaultEngine, VaultError
from claudeshift.licensing.entitlements import ProFeatureRequiredError, get_entitlements
from claudeshift.licensing.gumroad import (
    GUMROAD_PRODUCT_URL,
    LicenseError,
    activate_gumroad_license,
    deactivate_license,
    load_verified_license,
)
from claudeshift.providers.base import inspect_all_providers
from claudeshift.providers.plan_detector import detect_active_plan
from claudeshift.telemetry.analytics import compute_usage_report
from claudeshift.telemetry.dashboard import (
    build_live_monitor_renderable,
    build_usage_summary_renderable,
)
from claudeshift.telemetry.exporter import export_csv, export_json
from claudeshift.telemetry.parser import TelemetryScanner, UsageEvent
from claudeshift.telemetry.pricing import (
    PLAN_QUOTAS,
    PlanQuota,
    load_pricing_overrides,
    sync_live_pricing_catalog,
)

app = typer.Typer(
    name="claudeshift",
    help="Fast local profile switcher, MCP vault, and usage monitor for AI CLIs.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

license_app = typer.Typer(help="Activate and manage your claudeshift Pro license (Gumroad).")
app.add_typer(license_app, name="license")

pricing_app = typer.Typer(help="Manage and sync live AI model pricing definitions.")
app.add_typer(pricing_app, name="pricing")


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, ProFeatureRequiredError):
        console.print(
            Panel(
                f"[bold yellow]{exc.detail}[/bold yellow]\n\n"
                f"[bold]Get a lifetime Pro license ($19):[/bold] [link=https://mgm15.gumroad.com/l/claudeshift-cli][cyan]{GUMROAD_PRODUCT_URL}[/cyan][/link]\n"
                "[dim]Already have a key? Run:[/dim] [cyan]claudeshift license activate <YOUR-KEY>[/cyan]",
                title="[bold magenta]claudeshift PRO Feature[/bold magenta]",
                border_style="magenta",
            )
        )
        raise typer.Exit(code=2)
    console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(code=1)


def _resolve_plan_choice(
    target_home: Path,
    plan_arg: str,
    provider: str,
    events: list[UsageEvent],
    plan_hint: str = "",
) -> tuple[PlanQuota, str, str]:
    """Return (PlanQuota, source_label, dominant_provider)."""
    dominant = provider if provider != "all" else "claude"
    if provider == "all" and events:
        counts = Counter(e.provider for e in events)
        dominant = counts.most_common(1)[0][0]

    choice = plan_arg.strip().lower()
    if choice == "auto":
        detected = detect_active_plan(
            target_home=target_home,
            provider=provider,
            dominant_provider=dominant,
            session_plan_hint=plan_hint,
        )
        return detected.plan, f"auto:{detected.source}", detected.provider

    if choice in PLAN_QUOTAS:
        return PLAN_QUOTAS[choice], "manual", dominant

    valid = ", ".join(["auto", *PLAN_QUOTAS.keys()])
    raise VaultError(f"Unknown plan '{plan_arg}'. Valid options: {valid}")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold cyan]claudeshift[/bold cyan] v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show claudeshift version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """claudeshift CLI root."""


@app.command()
def doctor() -> None:
    """Inspect runtime paths, sandbox isolation, tier, subscriptions, and detected AI CLI providers."""
    paths = resolve_runtime_paths()
    ent = get_entitlements(paths.config_dir)
    statuses = inspect_all_providers(paths.target_home, is_sandbox=paths.is_sandbox)

    mode_badge = (
        "[bold yellow]SANDBOX MODE (Isolated)[/bold yellow]"
        if paths.is_sandbox
        else "[bold green]LIVE HOME[/bold green]"
    )
    tier_badge = "[bold magenta]PRO[/bold magenta]" if ent.is_pro else "[bold cyan]FREE[/bold cyan]"

    info_lines = [
        f"[bold]Mode:[/bold]        {mode_badge}",
        f"[bold]Edition:[/bold]     {tier_badge} [dim]({ent.license_source})[/dim]",
        f"[bold]Target Home:[/bold] {paths.target_home}",
        f"[bold]Vault Dir:[/bold]   {paths.vault_dir}",
        f"[bold]Config Dir:[/bold]  {paths.config_dir}",
        f"[bold]Runtime:[/bold]     Python {sys.version.split()[0]} on {platform.system()} {platform.release()}",
    ]
    console.print(
        Panel(
            "\n".join(info_lines),
            title=f"[bold cyan]claudeshift v{__version__} :: System Doctor[/bold cyan]",
            border_style="cyan",
        )
    )

    table = Table(title="Detected AI CLI & Desktop Providers", header_style="bold magenta")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Provider", style="white")
    table.add_column("Status", no_wrap=True)
    table.add_column("Detected Plan", style="green")
    table.add_column("Detected Paths", style="dim")

    for st in statuses:
        if paths.is_sandbox:
            status_str = "[bold green]DETECTED[/bold green]" if st.has_data else "[dim]not found[/dim]"
        elif st.installed and st.has_data:
            status_str = "[bold green]DETECTED (installed)[/bold green]"
        elif st.has_data and not st.installed:
            status_str = "[bold yellow]DATA ONLY (uninstalled)[/bold yellow]"
        elif st.installed and not st.has_data:
            status_str = "[cyan]INSTALLED (no data)[/cyan]"
        else:
            status_str = "[dim]not found[/dim]"

        found_str = ", ".join(p.name for p in st.found_paths) if st.found_paths else "-"
        plan_str = "-"
        if st.has_data and st.spec.id in {"claude", "codex", "gemini"}:
            det = detect_active_plan(paths.target_home, provider=st.spec.id)
            plan_str = f"{det.plan.label} ({det.source})"
        table.add_row(st.spec.id, st.spec.display_name, status_str, plan_str, found_str)

    console.print(table)


@app.command()
def save(
    name: str = typer.Argument(..., help="Profile name (e.g., work, personal, client-a)."),
    provider: str = typer.Option(
        "claude", "--provider", "-p", help="Provider ID (claude, codex, gemini, cursor)."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", "-f", help="Update an existing profile."),
    note: str = typer.Option("", "--note", "-n", help="Optional note for this profile."),
) -> None:
    """Capture the current live AI CLI configuration into the vault as a named profile."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        manifest = engine.save_profile(name=name, provider=provider, overwrite=overwrite, note=note)
    except (VaultError, ProFeatureRequiredError) as exc:
        _handle_error(exc)
        return

    mcp_list = ", ".join(manifest.identity.mcp_servers) if manifest.identity.mcp_servers else "none"
    console.print(
        Panel(
            f"[bold green]Saved profile '[cyan]{manifest.name}[/cyan]'[/bold green] ({manifest.provider})\n"
            f"[bold]Account:[/bold]  {manifest.identity.account or '-'}\n"
            f"[bold]Model:[/bold]    {manifest.identity.model or '-'}\n"
            f"[bold]MCPs:[/bold]     {mcp_list}\n"
            f"[bold]Payload:[/bold]  {manifest.file_count} files ({_format_bytes(manifest.total_bytes)}) — SHA-256 verified",
            title="[bold green]Vault Snapshot Created[/bold green]",
            border_style="green",
        )
    )


@app.command()
def use(
    name: str = typer.Argument(..., help="Profile name to activate."),
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
    no_sync: bool = typer.Option(
        False, "--no-sync", help="Skip syncing current active profile before switching."
    ),
) -> None:
    """Atomically switch the active profile with SHA-256 verification and crash rollback."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        res = engine.use_profile(name=name, provider=provider, sync_current=not no_sync)
    except (VaultError, ProFeatureRequiredError) as exc:
        _handle_error(exc)
        return

    sync_note = f" (auto-synced '{res.previous_profile}')" if res.synced_previous else ""
    backup_note = f"\n[bold]Safety Backup:[/bold] [dim]{res.backup_id}[/dim]" if res.backup_id else ""
    console.print(
        Panel(
            f"[bold green]Switched {res.provider} → [cyan]{res.active_profile}[/cyan][/bold green]{sync_note}\n"
            f"[bold]Switch Time:[/bold]   {res.elapsed_ms} ms (atomic swap){backup_note}",
            title="[bold cyan]claudeshift Active[/bold cyan]",
            border_style="cyan",
        )
    )


@app.command("list")
def list_cmd(
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
) -> None:
    """List saved profiles, accounts, models, and MCP servers."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        profiles = engine.list_profiles(provider=provider)
        active = engine.get_active_profile(provider=provider)
    except VaultError as exc:
        _handle_error(exc)
        return

    if not profiles:
        console.print(
            f"[yellow]No profiles saved for '{provider}' yet.[/yellow] "
            f"Run: [cyan]claudeshift save work -p {provider}[/cyan]"
        )
        return

    table = Table(title=f"Saved Profiles ({provider})", header_style="bold magenta")
    table.add_column("Active", justify="center", no_wrap=True)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Account / Auth", style="white")
    table.add_column("Model", style="green")
    table.add_column("MCP Servers", style="dim")
    table.add_column("Size", justify="right", style="dim")

    for m in profiles:
        marker = "[bold green]●[/bold green]" if m.name == active else ""
        mcps = ", ".join(m.identity.mcp_servers) if m.identity.mcp_servers else "-"
        table.add_row(
            marker,
            m.name,
            m.identity.account or "-",
            m.identity.model or "-",
            mcps,
            _format_bytes(m.total_bytes),
        )
    console.print(table)


@app.command()
def current(
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
) -> None:
    """Show the currently active profile name."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        active = engine.get_active_profile(provider=provider)
    except VaultError as exc:
        _handle_error(exc)
        return

    if active:
        console.print(f"[bold cyan]{provider}[/bold cyan] active profile: [bold green]{active}[/bold green]")
    else:
        console.print(f"[yellow]No active tracked profile for '{provider}'.[/yellow]")


@app.command()
def remove(
    name: str = typer.Argument(..., help="Profile name to delete from vault."),
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
) -> None:
    """Delete a saved profile from the vault (does not touch live files)."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        engine.remove_profile(name=name, provider=provider)
    except VaultError as exc:
        _handle_error(exc)
        return
    console.print(f"[green]Removed profile '[cyan]{name}[/cyan]' from {provider} vault.[/green]")


@app.command()
def backups(
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
) -> None:
    """[PRO] List automatic pre-switch safety snapshots."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        items = engine.list_backups(provider=provider)
    except (VaultError, ProFeatureRequiredError) as exc:
        _handle_error(exc)
        return

    if not items:
        console.print(f"[dim]No backups recorded yet for '{provider}'.[/dim]")
        return

    table = Table(title=f"Safety Backups ({provider})", header_style="bold magenta")
    table.add_column("Backup ID", style="cyan", no_wrap=True)
    table.add_column("Created (UTC)", style="white")
    table.add_column("Reason", style="dim")
    table.add_column("Size", justify="right", style="dim")

    for b in items:
        table.add_row(b.backup_id, b.created_at, b.reason, _format_bytes(b.total_bytes))
    console.print(table)


@app.command()
def restore(
    backup_id: str = typer.Argument(..., help="Backup ID from 'claudeshift backups'."),
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
) -> None:
    """[PRO] Restore live configuration from a pre-switch safety backup."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        info = engine.restore_backup(backup_id=backup_id, provider=provider)
    except (VaultError, ProFeatureRequiredError) as exc:
        _handle_error(exc)
        return
    console.print(f"[bold green]Restored {info.provider} from backup '[cyan]{info.backup_id}[/cyan]'.[/bold green]")


@app.command()
def diff(
    profile_a: str = typer.Argument(..., help="First profile name."),
    profile_b: str = typer.Argument(..., help="Second profile name."),
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider ID."),
) -> None:
    """[PRO] Compare MCP servers and config file hashes between two profiles."""
    paths = resolve_runtime_paths()
    engine = VaultEngine(paths)
    try:
        d = engine.diff_profiles(profile_a=profile_a, profile_b=profile_b, provider=provider)
    except (VaultError, ProFeatureRequiredError) as exc:
        _handle_error(exc)
        return

    lines = [
        f"[bold]MCP only in {d.profile_a}:[/bold] {', '.join(d.only_in_a_mcp) or '-'}",
        f"[bold]MCP only in {d.profile_b}:[/bold] {', '.join(d.only_in_b_mcp) or '-'}",
        f"[bold]Shared MCPs:[/bold]          {', '.join(d.shared_mcp) or '-'}",
        "",
        f"[bold]Changed files ({len(d.changed_files)}):[/bold]   {', '.join(d.changed_files) or 'none'}",
        f"[bold]Only in {d.profile_a}:[/bold]      {', '.join(d.only_in_a_files) or 'none'}",
        f"[bold]Only in {d.profile_b}:[/bold]      {', '.join(d.only_in_b_files) or 'none'}",
    ]
    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold magenta]Profile Diff: {d.profile_a} ↔ {d.profile_b} ({d.provider})[/bold magenta]",
            border_style="magenta",
        )
    )


@app.command()
def usage(
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider to scan (claude, codex, all)."),
    plan: str = typer.Option(
        "auto", "--plan", help="Plan: auto | pro | max5 | max20 | codex_go | codex_plus | codex_pro | api."
    ),
    budget: float | None = typer.Option(None, "--budget", help="Custom 5-hour USD budget override."),
) -> None:
    """Show offline token consumption, auto-detected subscription gauge, and real model breakdown."""
    paths = resolve_runtime_paths()
    load_pricing_overrides(paths.config_dir)
    scanner = TelemetryScanner(paths.target_home, paths.vault_dir)
    events, stats = scanner.scan(provider=provider)

    if not events:
        console.print(
            f"[yellow]No local usage logs found for '{provider}' under {paths.target_home}.[/yellow]"
        )
        return

    try:
        resolved_plan, plan_source, dominant = _resolve_plan_choice(
            paths.target_home, plan, provider, events, plan_hint=str(stats.get("plan_hint") or "")
        )
    except VaultError as exc:
        _handle_error(exc)
        return

    engine = VaultEngine(paths)
    active = engine.get_active_profile(dominant)
    report = compute_usage_report(
        events,
        plan_id=resolved_plan.id,
        custom_budget_usd=budget,
        scan_stats=stats,
        resolved_plan=resolved_plan,
        plan_source=plan_source,
    )
    console.print(build_usage_summary_renderable(report, active))


@app.command()
def monitor(
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider to monitor (claude, codex, all)."),
    plan: str = typer.Option(
        "auto", "--plan", help="Plan: auto | pro | max5 | max20 | codex_go | codex_plus | codex_pro | api."
    ),
    budget: float | None = typer.Option(None, "--budget", help="Custom 5-hour USD budget override."),
    interval: float = typer.Option(2.0, "--interval", "-i", help="Refresh interval in seconds."),
    once: bool = typer.Option(False, "--once", help="Render a single frame and exit."),
) -> None:
    """[PRO] Live real-time quota radar, burn-rate speedometer, ETA countdown, and project costs."""
    paths = resolve_runtime_paths()
    load_pricing_overrides(paths.config_dir)
    ent = get_entitlements(paths.config_dir)
    if not ent.live_dashboard:
        _handle_error(
            ProFeatureRequiredError(
                feature="live_dashboard",
                detail="Live Quota Radar & Burn-Rate Dashboard require claudeshift Pro.",
            )
        )
        return

    scanner = TelemetryScanner(paths.target_home, paths.vault_dir)
    engine = VaultEngine(paths)

    def _frame():
        events, stats = scanner.scan(provider=provider)
        resolved_plan, plan_source, dominant = _resolve_plan_choice(
            paths.target_home, plan, provider, events, plan_hint=str(stats.get("plan_hint") or "")
        )
        active = engine.get_active_profile(dominant)
        report = compute_usage_report(
            events,
            plan_id=resolved_plan.id,
            custom_budget_usd=budget,
            scan_stats=stats,
            resolved_plan=resolved_plan,
            plan_source=plan_source,
        )
        return build_live_monitor_renderable(report, active, interval_sec=interval)

    if once:
        console.print(_frame())
        return

    try:
        with Live(_frame(), console=console, refresh_per_second=2, screen=False) as live:
            while True:
                time.sleep(max(0.5, interval))
                live.update(_frame())
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped live monitor.[/dim]")


@app.command("export")
def export_cmd(
    output: Path = typer.Argument(..., help="Destination file path (e.g., report.csv or report.json)."),
    fmt: str = typer.Option("auto", "--format", "-f", help="Export format: auto | csv | json."),
    provider: str = typer.Option("claude", "--provider", "-p", help="Provider to export (claude, codex, all)."),
    plan: str = typer.Option("auto", "--plan", help="Plan profile for JSON summary metrics."),
) -> None:
    """[PRO] Export local token usage & cost telemetry to CSV or JSON for billing and audits."""
    paths = resolve_runtime_paths()
    load_pricing_overrides(paths.config_dir)
    ent = get_entitlements(paths.config_dir)
    if not ent.csv_export:
        _handle_error(
            ProFeatureRequiredError(
                feature="csv_export",
                detail="CSV/JSON billing & usage exports require claudeshift Pro.",
            )
        )
        return

    resolved_fmt = fmt.lower()
    if resolved_fmt == "auto":
        resolved_fmt = "json" if output.suffix.lower() == ".json" else "csv"
    if resolved_fmt not in {"csv", "json"}:
        _handle_error(VaultError("Unsupported format. Use --format csv or --format json."))
        return

    scanner = TelemetryScanner(paths.target_home, paths.vault_dir)
    events, stats = scanner.scan(provider=provider)
    if not events:
        console.print(f"[yellow]No usage events found to export for '{provider}'.[/yellow]")
        return

    if resolved_fmt == "csv":
        dest = export_csv(events, output)
    else:
        resolved_plan, plan_source, _ = _resolve_plan_choice(
            paths.target_home, plan, provider, events, plan_hint=str(stats.get("plan_hint") or "")
        )
        report = compute_usage_report(
            events,
            plan_id=resolved_plan.id,
            scan_stats=stats,
            resolved_plan=resolved_plan,
            plan_source=plan_source,
        )
        dest = export_json(report, events, output)

    console.print(
        f"[bold green]Exported {len(events)} usage events ({resolved_fmt.upper()}) → [cyan]{dest}[/cyan][/bold green]"
    )


@pricing_app.command("sync")
def pricing_sync() -> None:
    """Download the latest live model pricing catalog (GPT-6, Claude 5.x, Gemini 3.x, etc.)."""
    paths = resolve_runtime_paths()
    try:
        count, dest = sync_live_pricing_catalog(paths.config_dir)
    except RuntimeError as exc:
        _handle_error(exc)
        return
    console.print(
        f"[bold green]Synced {count} live AI model pricing entries → [cyan]{dest}[/cyan][/bold green]"
    )


@license_app.command("activate")
def license_activate(
    key: str = typer.Argument(..., help="Your Gumroad license key."),
) -> None:
    """Verify your Gumroad license key online and unlock claudeshift Pro."""
    paths = resolve_runtime_paths()
    try:
        record = activate_gumroad_license(paths.config_dir, license_key=key)
    except LicenseError as exc:
        _handle_error(exc)
        return

    console.print(
        Panel(
            f"[bold green]claudeshift PRO Activated![/bold green]\n"
            f"[bold]Key:[/bold]          [cyan]{record.key_masked}[/cyan]\n"
            f"[bold]Licensed To:[/bold]  {record.email or '-'}\n"
            f"[bold]Activations:[/bold]  {record.uses}\n"
            f"[bold]Offline Grace:[/bold] Valid until [dim]{record.grace_until}[/dim]",
            title="[bold magenta]License Verified (Gumroad)[/bold magenta]",
            border_style="magenta",
        )
    )


@license_app.command("status")
def license_status() -> None:
    """Display current license tier, verification state, and offline grace window."""
    paths = resolve_runtime_paths()
    record, reason = load_verified_license(paths.config_dir)

    if record is None:
        console.print(
            Panel(
                f"[bold cyan]Edition:[/bold cyan] FREE\n"
                f"[bold]State:[/bold]   [dim]{reason}[/dim]\n\n"
                "Upgrade to PRO for unlimited profiles, rolling backups, live quota radar, and CSV/JSON exports.\n"
                "Activate with: [cyan]claudeshift license activate <YOUR-GUMROAD-KEY>[/cyan]",
                title="[bold cyan]claudeshift License Status[/bold cyan]",
                border_style="cyan",
            )
        )
        return

    console.print(
        Panel(
            f"[bold magenta]Edition:[/bold magenta]       PRO (Verified)\n"
            f"[bold]Key:[/bold]           [cyan]{record.key_masked}[/cyan]\n"
            f"[bold]Licensed To:[/bold]   {record.email or '-'}\n"
            f"[bold]Activated At:[/bold]  {record.activated_at}\n"
            f"[bold]Offline Grace:[/bold] {record.grace_until}",
            title="[bold magenta]claudeshift License Status[/bold magenta]",
            border_style="magenta",
        )
    )


@license_app.command("deactivate")
def license_deactivate() -> None:
    """Remove the locally cached Pro license from this machine."""
    paths = resolve_runtime_paths()
    removed = deactivate_license(paths.config_dir)
    if removed:
        console.print("[green]Deactivated local Pro license. Reverted to FREE tier.[/green]")
    else:
        console.print("[dim]No active local license found.[/dim]")