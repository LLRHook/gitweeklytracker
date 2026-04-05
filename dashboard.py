#!/usr/bin/env python3
"""Developer Analytics Dashboard - Git activity + Claude Code token usage."""

import os
from datetime import date

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich.rule import Rule
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from claude_analytics import (
    get_claude_analytics, get_usage_quota,
    ClaudeAnalytics, UsageQuota,
)
from git_tracker import get_git_analytics, GitAnalytics, load_env_config, DAY_NAMES


console = Console()
BUDGET_USD = 100.0  # Updated in main() after .env is loaded
REPORT_DAYS = 7     # Updated in main() after .env is loaded


def fmt_tokens(n: int) -> str:
    """Format token count: 1234567 -> '1.23M', 12345 -> '12.3K'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(usd: float) -> str:
    """Format cost in USD."""
    if usd >= 1.0:
        return f"${usd:.2f}"
    return f"${usd:.3f}"


def intensity_color(value: float, max_value: float) -> str:
    """Return green/yellow/red based on value relative to max."""
    if max_value <= 0:
        return "white"
    ratio = value / max_value
    if ratio < 0.33:
        return "green"
    if ratio < 0.66:
        return "yellow"
    return "red"


def bar_string(value: int, max_value: int, width: int = 30) -> str:
    """Create a horizontal bar using block characters."""
    if max_value == 0:
        return ""
    filled = int((value / max_value) * width)
    return "\u2588" * filled


def _gauge(pct: float, width: int = 20) -> str:
    """Create a colored gauge bar for utilization percentage (0-100)."""
    color = intensity_color(pct, 100)
    filled = int(pct / 100 * width)
    empty = width - filled
    return f"[{color}]{'\u2588' * filled}[/][dim]{'\u2591' * empty}[/]"


def render_header(claude_data: ClaudeAnalytics, quota: UsageQuota) -> Panel:
    """Render the header panel with date range, billing, and quota gauges."""
    bp = claude_data.billing_period
    today = date.today()
    w = console.width

    lines = []
    lines.append("[bold white]Developer Analytics Dashboard[/]")
    lines.append(f"[dim]Last {REPORT_DAYS} days  |  {today.strftime('%B %d, %Y')}[/]")
    lines.append("")

    # Quota gauges (real data from Anthropic API)
    if quota.available and quota.five_hour:
        pct5 = quota.five_hour.utilization
        pct7 = quota.seven_day.utilization if quota.seven_day else 0
        reset5 = quota.five_hour.reset_description
        reset7 = quota.seven_day.reset_description if quota.seven_day else ""

        gauge_width = min(25, max(10, (w - 40) // 4))

        line5 = f"[bold]5-Hour:[/]  {_gauge(pct5, gauge_width)} [{intensity_color(pct5, 100)}]{pct5:.0f}%[/]"
        if reset5:
            line5 += f"  [dim]resets {reset5}[/]"

        line7 = f"[bold]7-Day:[/]   {_gauge(pct7, gauge_width)} [{intensity_color(pct7, 100)}]{pct7:.0f}%[/]"
        if reset7:
            line7 += f"  [dim]resets {reset7}[/]"

        lines.append(line5)
        lines.append(line7)

        # Per-model breakdown if available
        model_parts = []
        if quota.seven_day_opus:
            model_parts.append(f"Opus [bold]{quota.seven_day_opus.utilization:.0f}%[/]")
        if quota.seven_day_sonnet:
            model_parts.append(f"Sonnet [bold]{quota.seven_day_sonnet.utilization:.0f}%[/]")
        if model_parts:
            lines.append(f"[dim]  Models: {' | '.join(model_parts)}[/]")
    else:
        # Fallback to billing period display
        period_bar_filled = bp.days_elapsed
        period_bar_empty = bp.days_remaining
        period_visual = "[green]\u2588[/]" * period_bar_filled + "[dim]\u2591[/]" * period_bar_empty
        lines.append(
            f"[bold]Billing Week:[/] Day [cyan]{bp.days_elapsed}[/]/7  "
            f"{period_visual}  "
            f"[dim]{bp.days_remaining} day{'s' if bp.days_remaining != 1 else ''} remaining[/]"
        )
        lines.append(
            f"[dim]Period: {bp.period_start.strftime('%b %d')} - {bp.period_end.strftime('%b %d')}[/]"
        )

    return Panel(
        "\n".join(lines),
        border_style="bright_blue",
        padding=(1, 2),
    )


def render_claude_project_table(claude_data: ClaudeAnalytics) -> Table:
    """Render per-project token usage table, adapting to terminal width."""
    w = console.width
    compact = w < 100

    table = Table(
        title="[bold]Per-Project Token Usage[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
        pad_edge=True,
        expand=True,
    )
    table.add_column("Project", style="bold white", max_width=24, no_wrap=True)
    table.add_column("In+Out", justify="right", style="bold")
    if not compact:
        table.add_column("Cache", justify="right", style="dim cyan")
    table.add_column("API Cost", justify="right")
    table.add_column("Sess", justify="right", style="dim")
    if not compact:
        table.add_column("Msgs", justify="right", style="dim")

    for p in claude_data.per_project[:15]:
        color = intensity_color(p.estimated_cost_usd, BUDGET_USD)
        row = [
            p.display_name,
            fmt_tokens(p.usage.active),
        ]
        if not compact:
            row.append(fmt_tokens(p.usage.cache))
        row.extend([
            f"[{color}]{fmt_cost(p.estimated_cost_usd)}[/]",
            str(p.session_count),
        ])
        if not compact:
            row.append(str(p.message_count))
        table.add_row(*row)

    table.add_section()
    tc = intensity_color(claude_data.total_cost_usd, BUDGET_USD)
    total_row = [
        "[bold]TOTAL[/]",
        f"[bold]{fmt_tokens(claude_data.total_tokens.active)}[/]",
    ]
    if not compact:
        total_row.append(fmt_tokens(claude_data.total_tokens.cache))
    total_row.extend([
        f"[bold {tc}]{fmt_cost(claude_data.total_cost_usd)}[/]",
        f"[bold]{claude_data.total_sessions}[/]",
    ])
    if not compact:
        total_row.append(f"[bold]{claude_data.total_messages}[/]")
    table.add_row(*total_row)

    return table


def render_daily_burn(claude_data: ClaudeAnalytics) -> Table:
    """Render daily token burn rate as horizontal bar chart."""
    w = console.width
    chart_width = max(10, min(30, w - 65))

    table = Table(
        title="[bold]Daily Token Burn (Active Tokens)[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
        expand=True,
    )
    table.add_column("Date", style="white")
    table.add_column("Day", style="dim")
    table.add_column("In+Out", justify="right", style="bold")
    if w >= 90:
        table.add_column("Cache", justify="right", style="dim")
    table.add_column("Cost", justify="right")
    table.add_column("Chart")
    if w >= 80:
        table.add_column("Msgs", justify="right", style="dim")

    max_active = max((d.usage.active for d in claude_data.daily_usage), default=0)
    today = date.today()

    for d in claude_data.daily_usage:
        dow = DAY_NAMES[d.date.weekday()]
        act = d.usage.active
        cache = d.usage.cache
        bar = bar_string(act, max_active, width=chart_width)
        color = intensity_color(d.estimated_cost_usd, BUDGET_USD)
        is_today = d.date == today
        date_str = d.date.strftime("%b %d")
        if is_today:
            date_str = f"[bold cyan]{date_str}[/]"

        bar_color = intensity_color(act, max_active)
        row = [
            date_str,
            f"[bold]{dow}[/]" if is_today else dow,
            fmt_tokens(act),
        ]
        if w >= 90:
            row.append(fmt_tokens(cache))
        row.extend([
            f"[{color}]{fmt_cost(d.estimated_cost_usd)}[/]",
            f"[{bar_color}]{bar}[/]",
        ])
        if w >= 80:
            row.append(str(d.message_count))
        table.add_row(*row)

    return table


def render_model_breakdown(claude_data: ClaudeAnalytics) -> Table:
    """Render model distribution table."""
    table = Table(
        title="[bold]Model Distribution[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
        expand=True,
    )
    table.add_column("Model", style="bold white")
    table.add_column("In+Out", justify="right", style="bold")
    table.add_column("Cost", justify="right")
    table.add_column("Msgs", justify="right", style="dim")
    table.add_column("Share", justify="right", no_wrap=True)

    total_active = sum(m.usage.active for m in claude_data.model_breakdown) or 1

    for m in claude_data.model_breakdown:
        active = m.usage.active
        share = active / total_active * 100
        color = intensity_color(m.estimated_cost_usd, BUDGET_USD)
        share_bar = "\u2588" * max(1, int(share / 5))
        table.add_row(
            m.display_name,
            fmt_tokens(active),
            f"[{color}]{fmt_cost(m.estimated_cost_usd)}[/]",
            str(m.message_count),
            f"[cyan]{share_bar}[/] {share:.0f}%",
        )

    return table


def render_hourly_distribution(claude_data: ClaudeAnalytics) -> Table:
    """Render hourly activity heatmap."""
    bar_width = max(10, min(25, (console.width // 2) - 20))

    table = Table(
        title="[bold]Hourly Activity[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
        expand=True,
    )
    table.add_column("Hour", style="dim")
    table.add_column("Activity")
    table.add_column("Count", justify="right", style="dim")

    if not claude_data.hourly_distribution:
        table.add_row("--", "No data", "0")
        return table

    max_count = max(claude_data.hourly_distribution.values())

    for hour in range(24):
        count = claude_data.hourly_distribution.get(hour, 0)
        if count == 0:
            continue
        bar = bar_string(count, max_count, width=bar_width)
        time_label = f"{hour:02d}:00"
        is_busiest = hour == claude_data.busiest_hour
        if is_busiest:
            time_label = f"[bold cyan]{time_label}[/]"
            bar = f"[bold yellow]{bar}[/]"
        else:
            bar = f"[{intensity_color(count, max_count)}]{bar}[/]"

        table.add_row(time_label, bar, str(count))

    return table


def render_git_table(git_data: GitAnalytics) -> Table:
    """Render per-repo git commit table."""
    table = Table(
        title="[bold]Git Activity by Repository[/]",
        title_style="bold magenta",
        border_style="magenta",
        show_lines=False,
        expand=True,
    )
    table.add_column("Repo", style="bold white", max_width=24, no_wrap=True)
    table.add_column("Author", style="dim", max_width=20, no_wrap=True)
    table.add_column("Commits", justify="right", style="cyan")
    table.add_column("Ins", justify="right", style="green")
    table.add_column("Del", justify="right", style="red")
    table.add_column("Net", justify="right")

    for s in git_data.per_repo:
        net = s.insertions - s.deletions
        net_color = "green" if net >= 0 else "red"
        table.add_row(
            s.repo_name,
            s.email.split("@")[0],
            str(s.commits),
            f"+{s.insertions:,}",
            f"-{s.deletions:,}",
            f"[{net_color}]{net:+,d}[/]",
        )

    table.add_section()
    net_color = "green" if git_data.net_change >= 0 else "red"
    table.add_row(
        f"[bold]TOTAL ({git_data.active_repo_count} repos)[/]",
        "",
        f"[bold]{git_data.total_commits}[/]",
        f"[bold green]+{git_data.total_insertions:,}[/]",
        f"[bold red]-{git_data.total_deletions:,}[/]",
        f"[bold {net_color}]{git_data.net_change:+,d}[/]",
    )

    return table


def render_git_daily(git_data: GitAnalytics) -> Table:
    """Render git daily activity bar chart."""
    table = Table(
        title="[bold]Git Daily Activity[/]",
        title_style="bold magenta",
        border_style="magenta",
        show_lines=False,
        expand=True,
    )
    table.add_column("Day", style="white")
    table.add_column("Commits", justify="right")
    table.add_column("Chart")

    max_commits = max(git_data.daily_activity.values()) if git_data.daily_activity else 0
    chart_width = max(10, min(25, (console.width // 2) - 20))

    for day in DAY_NAMES:
        count = git_data.daily_activity.get(day, 0)
        bar = bar_string(count, max_commits, width=chart_width)
        color = "magenta" if count > 0 else "dim"
        table.add_row(day, str(count), f"[{color}]{bar}[/]")

    return table


def render_git_top_files(git_data: GitAnalytics) -> Table:
    """Render top changed files."""
    if not git_data.top_files:
        return Table()

    table = Table(
        title="[bold]Top Changed Files[/]",
        title_style="bold magenta",
        border_style="magenta",
        show_lines=False,
        expand=True,
    )
    table.add_column("Changes", justify="right", style="cyan")
    table.add_column("File", style="white", no_wrap=True)

    for count, filepath in git_data.top_files:
        table.add_row(str(count), filepath)

    return table


def render_summary(claude_data: ClaudeAnalytics, git_data: GitAnalytics | None) -> Panel:
    """Render summary stats panel."""
    stats = []
    pw = max(14, min(20, console.width // 6))

    active_tokens = claude_data.total_tokens.active
    stats.append(Panel(
        f"[bold cyan]{fmt_tokens(active_tokens)}[/]\n[dim]Active Tokens[/]",
        border_style="cyan", width=pw,
    ))
    tc = intensity_color(claude_data.total_cost_usd, BUDGET_USD)
    stats.append(Panel(
        f"[bold {tc}]{fmt_cost(claude_data.total_cost_usd)}[/]\n[dim]API Cost[/]",
        border_style=tc, width=pw,
    ))
    stats.append(Panel(
        f"[bold white]{claude_data.total_sessions}[/]\n[dim]Sessions[/]",
        border_style="blue", width=pw,
    ))
    stats.append(Panel(
        f"[bold white]{claude_data.most_active_project}[/]\n[dim]Most Active[/]",
        border_style="blue", width=pw,
    ))
    stats.append(Panel(
        f"[bold white]{claude_data.busiest_hour:02d}:00[/]\n[dim]Busiest Hour[/]",
        border_style="blue", width=pw,
    ))

    if git_data and git_data.total_commits > 0:
        stats.append(Panel(
            f"[bold magenta]{git_data.total_commits}[/]\n[dim]Git Commits[/]",
            border_style="magenta", width=pw,
        ))
        stats.append(Panel(
            f"[bold magenta]{git_data.longest_streak}d[/]\n[dim]Streak[/]",
            border_style="magenta", width=pw,
        ))

    return Panel(
        Columns(stats, equal=True, expand=True),
        title="[bold]Summary[/]",
        border_style="bright_blue",
    )


def main():
    """Main entry point."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")
    emails, repo_paths = load_env_config(env_path if os.path.exists(env_path) else None)

    global BUDGET_USD, REPORT_DAYS
    BUDGET_USD = float(os.environ.get("CLAUDE_BUDGET_USD", "100"))
    REPORT_DAYS = int(os.environ.get("REPORT_DAYS", "7"))
    w = console.width

    console.print()

    # Fetch usage quota (fast - single HTTP call or file read)
    quota = get_usage_quota()

    # Collect Claude analytics with progress
    claude_data = None
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console, transient=True,
    ) as progress:
        task = progress.add_task("Scanning Claude Code data...", total=100)

        def claude_progress(current, total, desc):
            if total > 0:
                progress.update(task, completed=int(current / total * 100), description=desc)

        claude_data = get_claude_analytics(days=REPORT_DAYS, progress_callback=claude_progress)
        progress.update(task, completed=100, description="Claude data loaded.")

    # Collect git analytics with progress
    git_data = None
    if emails:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console, transient=True,
        ) as progress:
            task = progress.add_task("Discovering git repos...", total=None)

            def git_progress(current, total, desc):
                progress.update(task, description=desc)

            git_data = get_git_analytics(
                emails, repo_paths, days=REPORT_DAYS,
                progress_callback=git_progress,
            )

    # Render dashboard
    console.print()
    console.print(render_header(claude_data, quota))
    console.print()

    # Claude section
    console.print(Rule("[bold cyan]Claude Code Analytics[/]", style="cyan"))
    console.print()

    if claude_data.total_messages > 0:
        console.print(render_claude_project_table(claude_data))
        console.print()
        console.print(render_daily_burn(claude_data))
        console.print()

        if w >= 100:
            cols = Columns([
                render_model_breakdown(claude_data),
                render_hourly_distribution(claude_data),
            ], equal=True, expand=True)
            console.print(cols)
        else:
            console.print(render_model_breakdown(claude_data))
            console.print()
            console.print(render_hourly_distribution(claude_data))
    else:
        console.print(f"[dim]No Claude Code activity found in the last {REPORT_DAYS} days.[/]")

    console.print()

    # Git section
    if git_data and git_data.total_commits > 0:
        console.print(Rule("[bold magenta]Git Activity[/]", style="magenta"))
        console.print()
        console.print(render_git_table(git_data))
        console.print()

        if w >= 100:
            cols = Columns([
                render_git_daily(git_data),
                render_git_top_files(git_data),
            ], equal=True, expand=True)
            console.print(cols)
        else:
            console.print(render_git_daily(git_data))
            console.print()
            if git_data.top_files:
                console.print(render_git_top_files(git_data))
    elif emails:
        console.print(Rule("[bold magenta]Git Activity[/]", style="magenta"))
        console.print()
        console.print(f"[dim]No git commits found in the last {REPORT_DAYS} days.[/]")

    console.print()
    console.print(render_summary(claude_data, git_data))
    console.print()


if __name__ == "__main__":
    main()
