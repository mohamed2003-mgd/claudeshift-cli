from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from claudeshift.telemetry.analytics import UsageReport


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_minutes(mins: float | None) -> str:
    if mins is None:
        return "idle"
    if mins <= 0:
        return "NOW"
    h = int(mins // 60)
    m = int(mins % 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m"


def _render_gauge(pct: float, width: int = 32) -> Text:
    clamped = max(0.0, min(100.0, pct))
    filled = round((clamped / 100.0) * width)
    empty = width - filled
    color = "red" if pct >= 90 else ("yellow" if pct >= 75 else "green")
    bar = Text()
    bar.append("█" * filled, style=f"bold {color}")
    bar.append("░" * empty, style="dim")
    bar.append(f"  {pct:.1f}%", style=f"bold {color}")
    return bar


def build_usage_summary_renderable(report: UsageReport, active_profile: str | None) -> Group:
    alert_style = {"OK": "green", "WARNING": "yellow", "CRITICAL": "bold red"}[report.alert_level]
    profile_label = active_profile or "untracked"

    gauge = _render_gauge(report.window_pct)
    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold", no_wrap=True)
    header.add_column()

    header.add_row(
        "Active Profile:",
        f"[cyan]{profile_label}[/cyan]  |  Plan: [bold]{report.plan.label}[/bold] [dim]({report.plan_source})[/dim]",
    )
    header.add_row("5h Session Gauge:", gauge)
    header.add_row(
        "5h Usage / Budget:",
        f"[bold]${report.window_cost_usd:.2f}[/bold] / ${report.budget_usd:.2f} "
        f"([cyan]{format_tokens(report.window_tokens)}[/cyan] tokens, {report.window_requests} reqs) "
        f"— Status: [{alert_style}]{report.alert_level}[/{alert_style}]",
    )
    header.add_row(
        "Today (UTC):",
        f"[bold]${report.today_cost_usd:.2f}[/bold] ({format_tokens(report.today_tokens)} tokens, {report.today_requests} reqs)",
    )
    header.add_row(
        "All-Time Scanned:",
        f"[bold]${report.total_cost_usd:.2f}[/bold] ({format_tokens(report.total_tokens)} tokens across {report.files_scanned} log files)",
    )

    panel = Panel(
        header,
        title="[bold cyan]claudeshift :: Local Token & Quota Summary[/bold cyan]",
        border_style="red" if report.alert_level == "CRITICAL" else "cyan",
    )

    model_table = Table(title="Breakdown by Model", header_style="bold magenta")
    model_table.add_column("Model", style="cyan")
    model_table.add_column("Reqs", justify="right")
    model_table.add_column("Input", justify="right")
    model_table.add_column("Output", justify="right")
    model_table.add_column("Cache Read", justify="right", style="dim")
    model_table.add_column("Est. API Cost", justify="right", style="bold green")

    for m in report.models[:8]:
        cost_str = f"~${m.cost_usd:.3f}" if m.rate_estimated else f"${m.cost_usd:.3f}"
        model_table.add_row(
            m.model,
            str(m.requests),
            format_tokens(m.input_tokens),
            format_tokens(m.output_tokens),
            format_tokens(m.cache_read_tokens),
            cost_str,
        )

    return Group(panel, model_table)


def build_live_monitor_renderable(
    report: UsageReport,
    active_profile: str | None,
    interval_sec: float,
) -> Group:
    alert_style = {"OK": "bold green", "WARNING": "bold yellow", "CRITICAL": "bold red"}[report.alert_level]
    border = "red" if report.alert_level == "CRITICAL" else ("yellow" if report.alert_level == "WARNING" else "magenta")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()

    grid.add_row(
        "Profile / Plan:",
        f"[cyan]{active_profile or 'untracked'}[/cyan]  •  {report.plan.label} [dim]({report.plan_source})[/dim]",
    )
    grid.add_row("5-Hour Window:", _render_gauge(report.window_pct, width=36))
    grid.add_row(
        "Window Spend:",
        f"[bold]${report.window_cost_usd:.2f}[/bold] / ${report.budget_usd:.2f}  "
        f"({format_tokens(report.window_tokens)} tokens)  •  Alert: [{alert_style}]{report.alert_level}[/{alert_style}]",
    )
    grid.add_row(
        "Live Burn Rate:",
        f"[bold yellow]{format_tokens(int(report.burn_tokens_per_min))}/min[/bold yellow]  •  "
        f"[bold green]${report.burn_usd_per_hour:.2f}/hr[/bold green]  •  "
        f"Est. Time to Limit: [bold cyan]{format_minutes(report.eta_minutes)}[/bold cyan]",
    )
    grid.add_row(
        "Window Rollover:",
        f"Oldest active event expires in [cyan]{format_minutes(report.window_reset_in_minutes)}[/cyan]",
    )
    grid.add_row(
        "Scanner Engine:",
        f"[dim]{report.files_scanned} files ({report.cache_hits} cached, {report.parsed_fresh} fresh) • refresh {interval_sec:.0f}s • Ctrl+C to exit[/dim]",
    )

    top_panel = Panel(
        grid,
        title="[bold magenta]claudeshift PRO :: Live Quota & Burn-Rate Radar[/bold magenta]",
        border_style=border,
    )

    proj_table = Table(title="Top Projects by Cost", header_style="bold cyan")
    proj_table.add_column("Project", style="white")
    proj_table.add_column("Sessions", justify="right")
    proj_table.add_column("Requests", justify="right")
    proj_table.add_column("Tokens", justify="right", style="cyan")
    proj_table.add_column("Est. Cost", justify="right", style="bold green")

    for p in report.projects[:6]:
        proj_table.add_row(
            p.project,
            str(p.sessions),
            str(p.requests),
            format_tokens(p.total_tokens),
            f"${p.cost_usd:.3f}",
        )

    return Group(top_panel, proj_table)