"""Git activity tracking - port of git-weekly-report.sh logic to Python."""

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, timezone


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


def _run_git(repo_path: str, args: list[str], timeout: int = 10) -> str:
    """Run a git command in a given repo and return stdout."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _discover_repos(
    emails: list[str],
    explicit_paths: list[str] | None = None,
    progress_callback=None,
) -> list[str]:
    """Discover git repositories, optionally filtered by author email."""
    repos: list[str] = []

    if explicit_paths:
        for path in explicit_paths:
            path = os.path.expanduser(path.strip())
            if not os.path.isdir(path):
                continue
            # Check if it's a git repo itself
            if os.path.isdir(os.path.join(path, ".git")):
                repos.append(os.path.abspath(path))
            else:
                # Scan children
                try:
                    for child in os.listdir(path):
                        child_path = os.path.join(path, child)
                        if os.path.isdir(os.path.join(child_path, ".git")):
                            repos.append(os.path.abspath(child_path))
                except PermissionError:
                    pass
    else:
        # Auto-discover under $HOME
        home = os.path.expanduser("~")
        if progress_callback:
            progress_callback(0, 0, "Discovering git repos...")

        try:
            result = subprocess.run(
                ["find", home, "-maxdepth", "5", "-name", ".git", "-type", "d",
                 "-not", "-path", "*/node_modules/*",
                 "-not", "-path", "*/.cache/*",
                 "-not", "-path", "*/Library/*",
                 "-not", "-path", "*/.Trash/*",
                 "-not", "-path", "*/.cargo/*",
                 "-not", "-path", "*/.rustup/*"],
                capture_output=True, text=True, timeout=60,
            )
            git_dirs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            git_dirs = []

        all_repos = [os.path.dirname(d) for d in git_dirs]

        if progress_callback:
            progress_callback(0, len(all_repos), f"Filtering {len(all_repos)} repos by author...")

        # Filter to repos with commits from our emails
        for idx, repo_dir in enumerate(all_repos):
            if progress_callback and idx % 20 == 0:
                progress_callback(idx, len(all_repos), f"Filtering repos... ({idx}/{len(all_repos)})")
            for email in emails:
                hit = _run_git(repo_dir, [
                    "log", f"--author={email}", "--since=1 year ago",
                    "--oneline", "-1",
                ])
                if hit.strip():
                    repos.append(repo_dir)
                    break

    # Deduplicate
    seen = set()
    unique = []
    for r in repos:
        rp = os.path.realpath(r)
        if rp not in seen:
            seen.add(rp)
            unique.append(r)
    return unique


@dataclass
class _RepoEmailData:
    """All data collected from a single git log call for one repo+email."""
    stats: RepoStats
    commit_dates: list[date] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


def _collect_repo_email_data(
    repo_path: str, email: str, since_iso: str, until_iso: str
) -> _RepoEmailData:
    """Single-pass data collection for one repo+email.

    Runs two git commands (shortstat + name-only) instead of four separate ones.
    """
    repo_name = os.path.basename(repo_path)
    stats = RepoStats(repo_path=repo_path, repo_name=repo_name, email=email)
    commit_dates: list[date] = []

    # First call: commits with stats and dates
    output = _run_git(repo_path, [
        "log", f"--author={email}",
        f"--since={since_iso}", f"--until={until_iso}",
        "--format=COMMIT:%H|%ai|%s", "--shortstat",
    ])

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT:"):
            stats.commits += 1
            # Extract date for daily activity and streak
            parts = line.split("|")
            if len(parts) >= 2:
                date_part = parts[1].split()[0]
                try:
                    commit_dates.append(
                        datetime.strptime(date_part, "%Y-%m-%d").date()
                    )
                except ValueError:
                    pass
        else:
            # Parse shortstat line
            m = re.search(r"(\d+) file", line)
            if m:
                stats.files_changed += int(m.group(1))
            m = re.search(r"(\d+) insertion", line)
            if m:
                stats.insertions += int(m.group(1))
            m = re.search(r"(\d+) deletion", line)
            if m:
                stats.deletions += int(m.group(1))

    # Second call: changed file names (only if there were commits)
    changed_files: list[str] = []
    if stats.commits > 0:
        file_output = _run_git(repo_path, [
            "log", f"--author={email}",
            f"--since={since_iso}", f"--until={until_iso}",
            "--name-only", "--format=",
        ])
        for line in file_output.splitlines():
            line = line.strip()
            if line:
                changed_files.append(f"{repo_name}/{line}")

    return _RepoEmailData(
        stats=stats,
        commit_dates=commit_dates,
        changed_files=changed_files,
    )


def _compute_streak_from_dates(all_dates: set[date]) -> int:
    """Compute longest consecutive-day commit streak from a set of dates."""
    if not all_dates:
        return 0
    sorted_dates = sorted(all_dates)
    max_streak = 1
    current_streak = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 1
    return max_streak


def get_git_analytics(
    emails: list[str],
    repo_paths: list[str] | None = None,
    days: int = 7,
    progress_callback=None,
) -> GitAnalytics | None:
    """Compute git analytics. Returns None if no emails configured."""
    if not emails:
        return None

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    since_iso = start.strftime("%Y-%m-%dT%H:%M:%S")
    until_iso = now.strftime("%Y-%m-%dT%H:%M:%S")
    start_display = start.strftime("%Y-%m-%d %H:%M")
    end_display = now.strftime("%Y-%m-%d %H:%M")

    repos = _discover_repos(emails, repo_paths, progress_callback)
    if not repos:
        return GitAnalytics(
            per_repo=[], daily_activity={d: 0 for d in DAY_NAMES},
            top_files=[], longest_streak=0, total_commits=0,
            total_insertions=0, total_deletions=0, net_change=0,
            active_repo_count=0, repos_scanned=0,
            start_date=start_display, end_date=end_display, emails=emails,
        )

    if progress_callback:
        progress_callback(0, len(repos), "Scanning git repos...")

    all_stats: list[RepoStats] = []
    day_counts: dict[str, int] = {d: 0 for d in DAY_NAMES}
    file_counts: dict[str, int] = {}
    all_commit_dates: set[date] = set()

    for idx, repo in enumerate(repos):
        if progress_callback:
            progress_callback(idx, len(repos), f"Scanning {os.path.basename(repo)}...")
        for email in emails:
            data = _collect_repo_email_data(repo, email, since_iso, until_iso)
            if data.stats.commits > 0:
                all_stats.append(data.stats)
                # Accumulate daily activity
                for d in data.commit_dates:
                    dow = DAY_NAMES[d.weekday()]
                    day_counts[dow] += 1
                    all_commit_dates.add(d)
                # Accumulate file counts
                for f in data.changed_files:
                    file_counts[f] = file_counts.get(f, 0) + 1

    daily_activity = day_counts
    sorted_files = sorted(file_counts.items(), key=lambda x: x[1], reverse=True)
    top_files = [(count, path) for path, count in sorted_files[:10]]
    longest_streak = _compute_streak_from_dates(all_commit_dates)

    total_commits = sum(s.commits for s in all_stats)
    total_ins = sum(s.insertions for s in all_stats)
    total_del = sum(s.deletions for s in all_stats)
    active_repos = len({s.repo_path for s in all_stats})

    return GitAnalytics(
        per_repo=all_stats,
        daily_activity=daily_activity,
        top_files=top_files,
        longest_streak=longest_streak,
        total_commits=total_commits,
        total_insertions=total_ins,
        total_deletions=total_del,
        net_change=total_ins - total_del,
        active_repo_count=active_repos,
        repos_scanned=len(repos),
        start_date=start_display,
        end_date=end_display,
        emails=emails,
    )


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
