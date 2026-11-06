"""Python data contract and single-process adapter for the Rust Git processor."""

import json
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class RepoStats:
    repo_path: str
    repo_name: str
    email: str
    commits: int = 0
    insertions: int = 0
    deletions: int = 0
    files_changed: int = 0


@dataclass
class GitAnalytics:
    per_repo: list[RepoStats]
    daily_activity: dict[str, int]  # day name (Mon-Sun) -> count
    top_files: list[tuple[int, str]]  # (count, filepath)
    longest_streak: int
    total_commits: int
    total_insertions: int
    total_deletions: int
    net_change: int
    active_repo_count: int
    repos_scanned: int
    start_date: str
    end_date: str
    emails: list[str]


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _collect_report(
    emails: list[str],
    repo_paths: list[str] | None = None,
    days: int = 7,
    progress_callback=None,
    *,
    now: datetime | None = None,
) -> tuple[GitAnalytics, list[str]]:
    """Collect a complete report in one Rust invocation, including repo listing."""
    now = now if now is not None else datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    default_binary = Path(__file__).resolve().parent / "target/release/gitweeklytracker-core"
    binary = os.path.expanduser(os.environ.get("GIT_TRACKER_BIN", str(default_binary)))
    command = [binary, "--since", str(math.floor(start.timestamp())),
               "--until", str(math.floor(now.timestamp()))]
    for email in emails:
        command.extend(["--email", email])
    for path in repo_paths or []:
        command.extend(["--repo", path.strip()])
    if progress_callback:
        progress_callback(0, 0, "Scanning git repos with Rust...")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise RuntimeError(
            "Rust Git processor not found. Run cargo build --release --locked in "
            "the project directory, or set GIT_TRACKER_BIN to the compiled binary."
        ) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Rust Git processor failed: {error.stderr.strip()}") from error
    except OSError as error:
        raise RuntimeError(f"Cannot start Rust Git processor: {error}") from error
    try:
        payload = json.loads(result.stdout)
        repositories = payload.pop("repositories")
        payload["per_repo"] = [RepoStats(**row) for row in payload["per_repo"]]
        payload["top_files"] = [tuple(row) for row in payload["top_files"]]
        data = GitAnalytics(**payload)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError("Invalid response from Rust Git processor") from error
    if progress_callback:
        progress_callback(data.repos_scanned, data.repos_scanned, "Git report complete.")
    return data, repositories


def get_git_analytics(
    emails: list[str],
    repo_paths: list[str] | None = None,
    days: int = 7,
    progress_callback=None,
) -> GitAnalytics | None:
    """Compute Git analytics with Rust; retain the dashboard's dataclass API."""
    if not emails:
        return None
    return _collect_report(emails, repo_paths, days, progress_callback)[0]


def load_env_config(env_path: str | None = None) -> tuple[list[str], list[str] | None]:
    """Load GIT_REPORT_EMAILS and GIT_REPORT_REPOS from .env."""
    from dotenv import load_dotenv

    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    emails_str = os.environ.get("GIT_REPORT_EMAILS", "")
    emails = [e.strip() for e in emails_str.split(",") if e.strip()]

    repos_str = os.environ.get("GIT_REPORT_REPOS", "")
    repo_paths = [r.strip() for r in repos_str.split(",") if r.strip()] if repos_str else None

    return emails, repo_paths


if __name__ == "__main__":
    emails, repo_paths = load_env_config()
    if not emails:
        print("No GIT_REPORT_EMAILS set in .env")
    else:
        print(f"Tracking emails: {emails}")
        data = get_git_analytics(emails, repo_paths, days=7)
        if data:
            print(f"Repos scanned: {data.repos_scanned}, active: {data.active_repo_count}")
            print(f"Total commits: {data.total_commits}")
            print(f"Insertions: +{data.total_insertions}, Deletions: -{data.total_deletions}")
            print(f"Net: {data.net_change:+d}")
            print(f"Streak: {data.longest_streak} days")
            print(f"\nPer-repo:")
            for s in data.per_repo:
                print(f"  {s.repo_name} ({s.email}): {s.commits} commits, +{s.insertions}/-{s.deletions}")
