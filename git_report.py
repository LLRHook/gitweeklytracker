"""Terminal and Markdown presentation for the shared Rust Git report."""

import argparse
import html
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from git_tracker import DAY_NAMES, _collect_report


def markdown_text(value: str) -> str:
    value = html.escape(value).replace("\\", "\\\\")
    for char in "|`*_[]":
        value = value.replace(char, "\\" + char)
    return value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def markdown_code(value: str) -> str:
    # Code-span contents need literal backslashes; table separators still escape.
    value = value.replace("|", "\\|").replace("\n", "\\n").replace("\r", "\\r")
    fence = "`" * (max((len(run) for run in re.findall(r"`+", value)), default=0) + 1)
    padding = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def author_rows(data, index):
    email = data.emails[index]
    rows = [row for row in data.per_repo if row.email == email]
    # The data contract retains repeated configured emails. Each occurrence
    # represents its own scan, just as each author index did in the Bash report.
    occurrence = data.emails[:index].count(email)
    return rows[occurrence::data.emails.count(email)]


def render_markdown(data, repositories, generated):
    lines = ["# Git Weekly Report", "",
             f"**Period:** {data.start_date} UTC → {data.end_date} UTC", "",
             f"**Repositories scanned:** {data.repos_scanned} ({data.active_repo_count} with activity)", "",
             "<details>", "<summary>Repository list</summary>", ""]
    lines.extend(f"- {markdown_code(repo)}" for repo in repositories)
    lines.extend(["", "</details>", ""])
    for index, email in enumerate(data.emails):
        rows = author_rows(data, index)
        lines.extend([f"## {markdown_text(email)}", ""])
        if rows:
            lines.extend(["| Repo | Commits | Insertions | Deletions |",
                          "|------|---------|------------|-----------|"])
            lines.extend(f"| {markdown_text(row.repo_name)} | {row.commits} | +{row.insertions} | -{row.deletions} |"
                         for row in rows)
            lines.append(f"| **Subtotal** | **{sum(r.commits for r in rows)}** | "
                         f"**+{sum(r.insertions for r in rows)}** | **-{sum(r.deletions for r in rows)}** |")
        else:
            lines.append("*No activity this week.*")
        lines.append("")
    lines.extend(["## Summary", "", "| Metric | Value |", "|--------|-------|",
                  f"| Total Commits | {data.total_commits} |",
                  f"| Total Insertions | +{data.total_insertions} |",
                  f"| Total Deletions | -{data.total_deletions} |",
                  f"| Net Change | {data.net_change:+d} |",
                  f"| Longest Streak | {data.longest_streak} day(s) |", ""])
    if data.top_files:
        lines.extend(["## Top Changed Files", "", "| Changes | File |", "|---------|------|"])
        lines.extend(f"| {count} | {markdown_code(path)} |" for count, path in data.top_files)
        lines.append("")
    lines.extend(["## Daily Activity", "", "| Day | Commits |", "|-----|---------|"])
    lines.extend(f"| {day} | {data.daily_activity[day]} |" for day in DAY_NAMES)
    lines.extend(["", "---", f"*Generated on {generated:%Y-%m-%d %H:%M UTC}*", ""])
    return "\n".join(lines)


def render_terminal(data, report_file):
    lines = ["═══════════════════════════════════════════", " Git Weekly Report",
             f" {data.start_date} UTC → {data.end_date} UTC",
             f" {data.repos_scanned} repo(s) scanned, {data.active_repo_count} with activity",
             "═══════════════════════════════════════════", ""]
    for index, email in enumerate(data.emails):
        rows = author_rows(data, index)
        lines.extend([f"Author: {email}", "─────────────────────────────",
                      f"  {'Commits:':18} {sum(r.commits for r in rows)}",
                      f"  {'Insertions:':18} +{sum(r.insertions for r in rows)}",
                      f"  {'Deletions:':18} -{sum(r.deletions for r in rows)}", ""])
        if rows:
            lines.append(f"  {'Repo':25} {'Commits':>8} {'Ins':>10} {'Del':>10}")
            lines.append(f"  {'─' * 25} {'─' * 8} {'─' * 10} {'─' * 10}")
            lines.extend(f"  {row.repo_name:25} {row.commits:8} {row.insertions:+10d} {('-' + str(row.deletions)):>10}"
                         for row in rows)
            lines.append("")
    lines.extend(["── Summary ──────────────────",
                  f"  {'Total Commits:':18} {data.total_commits}",
                  f"  {'Total Insertions:':18} +{data.total_insertions}",
                  f"  {'Total Deletions:':18} -{data.total_deletions}",
                  f"  {'Net Change:':18} {data.net_change:+d}",
                  f"  {'Longest Streak:':18} {data.longest_streak} day(s)", ""])
    if data.top_files:
        lines.append("── Top Changed Files ────────")
        lines.extend(f"  {count:4}  {path}" for count, path in data.top_files)
        lines.append("")
    lines.append("── Daily Activity ───────────")
    maximum = max(data.daily_activity.values())
    for day in DAY_NAMES:
        count = data.daily_activity[day]
        bar = "█" * (count * 20 // maximum) if maximum else ""
        lines.append(f"  {day}: {bar:20} {count}")
    lines.extend(["", f"Report saved to: {report_file}"])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help="End of the seven-day report interval (ISO 8601)")
    args = parser.parse_args()
    emails = [e.strip() for e in os.environ.get("GIT_REPORT_EMAILS", "").split(",") if e.strip()]
    if not emails:
        parser.error("No valid emails found in GIT_REPORT_EMAILS")
    paths = [p.strip() for p in os.environ.get("GIT_REPORT_REPOS", "").split(",") if p.strip()]
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        data, repositories = _collect_report(emails, paths or None, now=now)
        if not repositories:
            print("Error: No git repositories found with commits from the configured emails.\n"
                  "Set GIT_REPORT_REPOS to specify paths manually.", file=sys.stderr)
            return 1
        report = Path(f"weekly-report-{now:%Y-%m-%d}.md")
        report.write_text(render_markdown(data, repositories, now), encoding="utf-8")
        print(render_terminal(data, report))
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
