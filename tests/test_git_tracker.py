import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import git_tracker


REPORT_EMAIL = "vivanov@paradigmtesting.com"


@contextmanager
def local_timezone(name):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 22, 12, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class GitReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")

    def git(self, *args, env=None):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True, env=env,
        ).stdout

    def commit(self, timestamp, filename="activity.txt"):
        path = self.repo / filename
        with path.open("a") as stream:
            stream.write(timestamp + "\n")
        self.git("add", filename)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Victor Ivanov",
            "GIT_AUTHOR_EMAIL": REPORT_EMAIL,
            "GIT_COMMITTER_NAME": "Victor Ivanov",
            "GIT_COMMITTER_EMAIL": REPORT_EMAIL,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        self.git("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "commit", "-qm", "Fixture activity", env=env)

    def report(self):
        with patch.object(git_tracker, "datetime", FrozenDatetime):
            return git_tracker.get_git_analytics(
                [REPORT_EMAIL], [str(self.repo)], days=7,
            )

    def test_report_window_is_utc_in_non_utc_timezone(self):
        # The first two commits are within the UTC window. The final one is
        # after its end; local-time parsing used to select the wrong commits.
        self.commit("2026-10-15T13:00:00+00:00", "first.txt")
        self.commit("2026-10-22T11:00:00+00:00", "last.txt")
        self.commit("2026-10-22T13:00:00+00:00", "future.txt")
        with local_timezone("America/New_York"):
            result = self.report()
        self.assertEqual(result.total_commits, 2)
        self.assertEqual(result.total_insertions, 2)
        self.assertEqual(result.daily_activity["Thu"], 2)
        self.assertEqual(set(result.top_files), {(1, "repo/first.txt"),
                                                (1, "repo/last.txt")})

    def test_daily_activity_and_streak_use_utc_commit_dates(self):
        # Both commits were authored on Tuesday in their original offsets,
        # but they fall on consecutive Monday/Tuesday dates in UTC.
        self.commit("2026-10-20T00:30:00+02:00")
        self.commit("2026-10-20T12:30:00+02:00")
        result = self.report()
        self.assertEqual(result.daily_activity["Mon"], 1)
        self.assertEqual(result.daily_activity["Tue"], 1)
        self.assertEqual(result.longest_streak, 2)

    def test_window_includes_exact_bounds_and_excludes_adjacent_seconds(self):
        self.commit("2026-10-15T11:59:59+00:00", "before.txt")
        self.commit("2026-10-15T12:00:00+00:00", "start.txt")
        self.commit("2026-10-22T12:00:00+00:00", "end.txt")
        self.commit("2026-10-22T12:00:01+00:00", "after.txt")
        result = self.report()
        self.assertEqual(set(result.top_files), {(1, "repo/start.txt"),
                                                (1, "repo/end.txt")})

    def shell_report(self, now, tz):
        # Copy the script to keep the developer's .env and report files out of
        # the test. Freeze only report bounds; date parsing stays native.
        root = Path(self.temp.name)
        script = root / "report.sh"
        source = Path(git_tracker.__file__).with_name("git-weekly-report.sh")
        script.write_text(source.read_text())
        bin_dir = root / "bin"
        bin_dir.mkdir()
        date_command = bin_dir / "date"
        date_command.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""\
            import os
            import subprocess
            import sys
            from datetime import datetime, timedelta
            args = sys.argv[1:]
            if ('-u' in args and '-j' not in args and '-f' not in args
                    and ('-d' not in args or '7 days ago' in args)):
                now = datetime.fromisoformat(os.environ['FROZEN_NOW'])
                if '-v-7d' in args or '7 days ago' in args:
                    now -= timedelta(days=7)
                print(now.strftime(args[-1].lstrip('+')))
            else:
                sys.exit(subprocess.call([{shutil.which('date')!r}, *args]))
            """))
        date_command.chmod(0o755)
        result = subprocess.run(
            ["bash", str(script)], cwd=root, capture_output=True, text=True,
            timeout=30, env={
                **os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                "FROZEN_NOW": now, "TZ": tz,
                "GIT_REPORT_EMAILS": REPORT_EMAIL,
                "GIT_REPORT_REPOS": str(self.repo),
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return next(root.glob("weekly-report-*.md")).read_text()

    def test_shell_report_uses_utc_window_and_daily_dates(self):
        self.commit("2026-10-15T13:00:00+00:00", "first.txt")
        self.commit("2026-10-20T00:30:00+02:00", "monday.txt")
        self.commit("2026-10-22T13:00:00+00:00", "future.txt")
        report = self.shell_report("2026-10-22T12:00:00+00:00", "America/New_York")
        self.assertIn("`repo/first.txt`", report)
        self.assertNotIn("`repo/future.txt`", report)
        self.assertIn("| Mon | 1 |", report)
        self.assertIn("| Tue | 0 |", report)

    def test_shell_streak_spans_spring_daylight_saving_transition(self):
        for day in (7, 8, 9):
            self.commit(f"2026-03-{day:02d}T12:00:00+00:00")
        report = self.shell_report("2026-03-10T12:00:00+00:00", "America/New_York")
        self.assertIn("| Longest Streak | 3 day(s) |", report)


if __name__ == "__main__":
    unittest.main()
