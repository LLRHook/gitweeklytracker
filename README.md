# Git Weekly Report

A single Bash script that generates a weekly git activity report across all your repositories, grouped by author email.

## How it works

1. Set your git author email(s)
2. The script auto-discovers every git repo on your machine that has commits from those emails
3. Generates a report with per-email, per-repo insertions/deletions breakdown

Output goes to both the terminal and a `weekly-report-YYYY-MM-DD.md` markdown file.

## Quick start

```bash
cp .env.example .env
# Edit .env with your email(s)
bash git-weekly-report.sh
```

Or pass the email inline:

```bash
GIT_REPORT_EMAILS="you@example.com" bash git-weekly-report.sh
```

## Configuration

All config is via environment variables, optionally loaded from a `.env` file.

### `GIT_REPORT_EMAILS` (required)

Comma-separated git author emails to include in the report.

```
GIT_REPORT_EMAILS="user1@example.com,user2@example.com"
```

### `GIT_REPORT_REPOS` (optional)

Comma-separated paths to repos or parent directories. If unset, all repos under `$HOME` are auto-discovered and filtered to those with commits from the configured emails.

```
GIT_REPORT_REPOS="~/projects/my-app,~/work"
```

Each path can be a git repo directly, or a parent directory whose immediate children are scanned for repos.

## Report contents

- **Per-email breakdown** with a table per author showing commits, insertions, and deletions for each repo (with subtotals)
- **Summary** of total commits, insertions, deletions, net change, and longest streak
- **Top 10 most-changed files** across all authors and repos
- **Daily activity** bar chart (terminal) / table (markdown)

## Requirements

- Bash 3.2+ (ships with macOS)
- Git
- Works on macOS and Linux (handles both `date` flavors)
