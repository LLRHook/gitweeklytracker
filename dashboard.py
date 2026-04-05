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

from claude_analytics import get_claude_analytics, ClaudeAnalytics
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


def render_header(claude_data: ClaudeAnalytics) -> Panel:
    """Render the header panel with date range and billing info."""
    bp = claude_data.billing_period
    today = date.today()

    lines = []
    lines.append(f"[bold white]Developer Analytics Dashboard[/]")
    lines.append(f"[dim]Last {REPORT_DAYS} days  |  {today.strftime('%B %d, %Y')}[/]")
    lines.append("")

    # Billing period info
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
    """Render per-project token usage table."""
    table = Table(
        title="[bold]Per-Project Token Usage[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
        pad_edge=True,
    )
    table.add_column("Project", style="bold white", min_width=18, max_width=28)
    table.add_column("Input", justify="right", style="green")
    table.add_column("Output", justify="right", style="yellow")
    table.add_column("Cache", justify="right", style="dim cyan")
    table.add_column("API Cost", justify="right")
    table.add_column("Sessions", justify="right", style="dim")
    table.add_column("Msgs", justify="right", style="dim")

    for p in claude_data.per_project[:15]:
        cache_total = p.usage.cache
        color = intensity_color(p.estimated_cost_usd, BUDGET_USD)
        table.add_row(
            p.display_name,
            fmt_tokens(p.usage.input_tokens),
            fmt_tokens(p.usage.output_tokens),
            fmt_tokens(cache_total),
            f"[{color}]{fmt_cost(p.estimated_cost_usd)}[/]",
            str(p.session_count),
            str(p.message_count),
        )

    # Totals row
    table.add_section()
    tc = intensity_color(claude_data.total_cost_usd, BUDGET_USD)
    total_cache = claude_data.total_tokens.cache
    table.add_row(
        "[bold]TOTAL[/]",
        fmt_tokens(claude_data.total_tokens.input_tokens),
        fmt_tokens(claude_data.total_tokens.output_tokens),
        fmt_tokens(total_cache),
        f"[bold {tc}]{fmt_cost(claude_data.total_cost_usd)}[/]",
        f"[bold]{claude_data.total_sessions}[/]",
        f"[bold]{claude_data.total_messages}[/]",
    )

    return table


def render_daily_burn(claude_data: ClaudeAnalytics) -> Table:
    """Render daily token burn rate as horizontal bar chart."""
    table = Table(
        title="[bold]Daily Token Burn (Active Tokens)[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
    )
    table.add_column("Date", style="white", min_width=12)
    table.add_column("Day", style="dim", min_width=5)
    table.add_column("In+Out", justify="right", style="bold", min_width=8)
    table.add_column("Cache", justify="right", style="dim", min_width=8)
    table.add_column("Cost", justify="right", min_width=8)
    table.add_column("Chart", min_width=30)
    table.add_column("Msgs", justify="right", style="dim", min_width=5)

    max_active = max((d.usage.active for d in claude_data.daily_usage), default=0)
    today = date.today()

    for d in claude_data.daily_usage:
        dow = DAY_NAMES[d.date.weekday()]
        act = d.usage.active
        cache = d.usage.cache
        bar = bar_string(act, max_active)
        color = intensity_color(d.estimated_cost_usd, BUDGET_USD)
        is_today = d.date == today
        date_str = d.date.strftime("%b %d")
        if is_today:
            date_str = f"[bold cyan]{date_str}[/]"

        bar_color = intensity_color(act, max_active)

        table.add_row(
            date_str,
            f"[bold]{dow}[/]" if is_today else dow,
            fmt_tokens(act),
            fmt_tokens(cache),
            f"[{color}]{fmt_cost(d.estimated_cost_usd)}[/]",
            f"[{bar_color}]{bar}[/]",
            str(d.message_count),
        )

    return table


def render_model_breakdown(claude_data: ClaudeAnalytics) -> Table:
    """Render model distribution table."""
    table = Table(
        title="[bold]Model Distribution[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
    )
    table.add_column("Model", style="bold white", min_width=12)
    table.add_column("In+Out", justify="right", style="bold")
    table.add_column("Cache", justify="right", style="dim")
    table.add_column("Cost", justify="right")
    table.add_column("Msgs", justify="right", style="dim")
    table.add_column("Share", justify="right", min_width=12, no_wrap=True)

    total_active = sum(m.usage.active for m in claude_data.model_breakdown) or 1

    for m in claude_data.model_breakdown:
        active = m.usage.active
        cache = m.usage.cache
        share = active / total_active * 100
        color = intensity_color(m.estimated_cost_usd, BUDGET_USD)
        share_bar = "\u2588" * max(1, int(share / 5))
        table.add_row(
            m.display_name,
            fmt_tokens(active),
            fmt_tokens(cache),
            f"[{color}]{fmt_cost(m.estimated_cost_usd)}[/]",
            str(m.message_count),
            f"[cyan]{share_bar}[/] {share:.0f}%",
        )

    return table


def render_hourly_distribution(claude_data: ClaudeAnalytics) -> Table:
    """Render hourly activity heatmap."""
    table = Table(
        title="[bold]Hourly Activity[/]",
        title_style="bold cyan",
        border_style="blue",
        show_lines=False,
    )
    table.add_column("Hour", style="dim", min_width=6)
    table.add_column("Activity", min_width=30)
    table.add_column("Count", justify="right", style="dim", min_width=6)

    if not claude_data.hourly_distribution:
        table.add_row("--", "No data", "0")
        return table

    max_count = max(claude_data.hourly_distribution.values())

    for hour in range(24):
        count = claude_data.hourly_distribution.get(hour, 0)
        if count == 0:
            continue
        bar = bar_string(count, max_count, width=25)
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
    )
    table.add_column("Repo", style="bold white", min_width=20, max_width=30)
    table.add_column("Author", style="dim", max_width=30)
    table.add_column("Commits", justify="right", style="cyan")
    table.add_column("Insertions", justify="right", style="green")
    table.add_column("Deletions", justify="right", style="red")
    table.add_column("Net", justify="right")

    for s in git_data.per_repo:
        net = s.insertions - s.deletions
        net_color = "green" if net >= 0 else "red"
        net_str = f"[{net_color}]{net:+,d}[/]"
        table.add_row(
            s.repo_name,
            s.email.split("@")[0],
            str(s.commits),
            f"+{s.insertions:,}",
            f"-{s.deletions:,}",
            net_str,
        )

    # Totals
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
    )
    table.add_column("Day", style="white", min_width=5)
    table.add_column("Commits", justify="right", min_width=8)
    table.add_column("Chart", min_width=25)

    max_commits = max(git_data.daily_activity.values()) if git_data.daily_activity else 0

    for day in DAY_NAMES:
        count = git_data.daily_activity.get(day, 0)
        bar = bar_string(count, max_commits, width=25)
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
    )
    table.add_column("Changes", justify="right", style="cyan", min_width=8)
    table.add_column("File", style="white")

    for count, filepath in git_data.top_files:
        table.add_row(str(count), filepath)

    return table


def render_summary(claude_data: ClaudeAnalytics, git_data: GitAnalytics | None) -> Panel:
    """Render summary stats panel."""
    stats = []

    # Claude stats
    active_tokens = claude_data.total_tokens.active
    stats.append(Panel(
        f"[bold cyan]{fmt_tokens(active_tokens)}[/]\n[dim]Active Tokens[/]\n[dim](in+out)[/]",
        border_style="cyan",
        width=20,
    ))
    stats.append(Panel(
        f"[bold dim]{fmt_tokens(claude_data.total_tokens.total)}[/]\n[dim]Total w/ Cache[/]",
        border_style="dim",
        width=20,
    ))
    tc = intensity_color(claude_data.total_cost_usd, BUDGET_USD)
    stats.append(Panel(
        f"[bold {tc}]{fmt_cost(claude_data.total_cost_usd)}[/]\n[dim]API Cost[/]",
        border_style=tc,
        width=20,
    ))
    stats.append(Panel(
        f"[bold white]{claude_data.total_sessions}[/]\n[dim]Sessions[/]",
        border_style="blue",
        width=20,
    ))
    stats.append(Panel(
        f"[bold white]{claude_data.most_active_project}[/]\n[dim]Most Active[/]",
        border_style="blue",
        width=20,
    ))
    stats.append(Panel(
        f"[bold white]{claude_data.busiest_hour:02d}:00[/]\n[dim]Busiest Hour[/]",
        border_style="blue",
        width=20,
    ))

    # Git stats
    if git_data and git_data.total_commits > 0:
        stats.append(Panel(
            f"[bold magenta]{git_data.total_commits}[/]\n[dim]Git Commits[/]",
            border_style="magenta",
            width=20,
        ))
        stats.append(Panel(
            f"[bold magenta]{git_data.longest_streak}d[/]\n[dim]Streak[/]",
            border_style="magenta",
            width=20,
        ))

    return Panel(
        Columns(stats, equal=True, expand=True),
        title="[bold]Summary[/]",
        border_style="bright_blue",
    )


def main():
    """Main entry point."""
    # Load config
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")
    emails, repo_paths = load_env_config(env_path if os.path.exists(env_path) else None)

    # Set config from .env (must happen after load_env_config)
    global BUDGET_USD, REPORT_DAYS
    BUDGET_USD = float(os.environ.get("CLAUDE_BUDGET_USD", "100"))
    REPORT_DAYS = int(os.environ.get("REPORT_DAYS", "7"))

    console.print()

    # Collect Claude analytics with progress
    claude_data = None
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
        transient=True,
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
            console=console,
            transient=True,
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
    console.print(render_header(claude_data))
    console.print()

    # Claude section
    console.print(Rule("[bold cyan]Claude Code Analytics[/]", style="cyan"))
    console.print()

    if claude_data.total_messages > 0:
        console.print(render_claude_project_table(claude_data))
        console.print()
        console.print(render_daily_burn(claude_data))
        console.print()

        # Model breakdown and hourly side by side if terminal is wide enough
        if console.width >= 120:
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

        if console.width >= 120:
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

    # Summary
    console.print(render_summary(claude_data, git_data))
    console.print()


if __name__ == "__main__":
    main()
