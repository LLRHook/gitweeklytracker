"""Exercise the real Rust process with synthetic Git repositories and peers."""

import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import git_report
import git_tracker


EMAIL = "report@example.test"
OTHER_EMAIL = "guest@example.test"
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
SINCE = int(datetime(2026, 10, 15, 12, tzinfo=timezone.utc).timestamp())
UNTIL = int(NOW.timestamp())


class RustProcessorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("GIT_TRACKER_BIN", str(
            Path(git_tracker.__file__).parent / "target/release/gitweeklytracker-core"))).resolve()
        if not cls.binary.is_file():
            raise RuntimeError("Build the Rust processor before testing: cargo build --release --locked")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gitweeklytracker-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"HOME": str(self.root), "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_TRACKER_BIN": str(self.binary)})
        self.git("init", "-q", "-b", "main")

    def git(self, *args, repo=None, env=None):
        return subprocess.run(
            ["git", "-C", str(repo or self.repo), "-c", "core.hooksPath=/dev/null",
             "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True, text=True, env=env or self.env,
        ).stdout

    def commit(self, timestamp="2026-10-20T12:00:00Z", filename="activity.txt",
               email=EMAIL, repo=None, content=None):
        repo = repo or self.repo
        path = repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            with path.open("a") as stream:
                stream.write("fixture\n")
        else:
            path.write_bytes(content)
        self.git("add", "--", filename, repo=repo)
        self.git("commit", "-qm", "Fixture activity", repo=repo,
                 env=self.commit_env(timestamp, email))

    def commit_env(self, timestamp, email=EMAIL):
        return {**self.env, "GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": "Fixture Author", "GIT_COMMITTER_EMAIL": email,
                "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}

    def report(self, emails=None, paths=None, now=NOW):
        with patch.dict(os.environ, self.env, clear=True):
            return git_tracker._collect_report(
                emails or [EMAIL], [str(p) for p in (paths if paths is not None else [self.repo])],
                now=now,
            )[0]

    def run_core(self, *extra, env=None):
        return subprocess.run(
            [str(self.binary), "--email", EMAIL, "--repo", str(self.repo),
             "--since", str(SINCE), "--until", str(UNTIL), *extra],
            capture_output=True, text=True, timeout=5, env=env or self.env,
        )

    def fake_git(self, body):
        bindir = self.root / "fake bin"
        bindir.mkdir()
        command = bindir / "git"
        command.write_text(f"#!{sys.executable}\n" + body)
        command.chmod(0o755)
        return {**self.env, "PATH": str(bindir)}

    def test_empty_and_missing_repositories_keep_full_zero_contract(self):
        empty = self.report()
        self.assertEqual(dataclasses.asdict(empty), {
            "per_repo": [], "daily_activity": dict.fromkeys(git_tracker.DAY_NAMES, 0),
            "top_files": [], "longest_streak": 0, "total_commits": 0,
            "total_insertions": 0, "total_deletions": 0, "net_change": 0,
            "active_repo_count": 0, "repos_scanned": 1,
            "start_date": "2026-10-15 12:00", "end_date": "2026-10-22 12:00",
            "emails": [EMAIL],
        })
        missing = self.report(paths=[self.root / "missing"])
        self.assertEqual(missing.repos_scanned, 0)
        self.assertEqual(missing.total_commits, 0)

    def test_multiple_authors_keep_separate_stats_and_ignore_unselected_author(self):
        self.commit("2026-10-18T12:00:00Z", "ours.txt")
        self.commit("2026-10-19T12:00:00Z", "guest.txt", OTHER_EMAIL)
        self.commit("2026-10-20T12:00:00Z", "excluded.txt", "excluded@example.test")
        result = self.report(emails=[EMAIL, OTHER_EMAIL])
        self.assertEqual([(r.email, r.commits, r.insertions, r.files_changed)
                          for r in result.per_repo], [(EMAIL, 1, 1, 1), (OTHER_EMAIL, 1, 1, 1)])
        self.assertEqual((result.total_commits, result.total_insertions,
                          result.total_deletions, result.net_change), (2, 2, 0, 2))
        self.assertEqual(result.active_repo_count, 1)
        self.assertEqual(result.longest_streak, 2)
        self.assertEqual(result.daily_activity["Sun"], 1)
        self.assertEqual(result.daily_activity["Mon"], 1)
        self.assertEqual(result.emails, [EMAIL, OTHER_EMAIL])
        self.assertEqual(result.per_repo[0].repo_path, str(self.repo))
        self.assertIsInstance(result.top_files[0], tuple)

    def test_git_author_pattern_semantics_are_preserved(self):
        self.commit()
        self.commit("2026-10-21T12:00:00Z", email=OTHER_EMAIL)
        self.assertEqual(self.report(emails=[".*@example[.]test"]).total_commits, 2)

    def test_overlapping_and_repeated_authors_keep_existing_counting_contract(self):
        self.commit()
        result = self.report(emails=[EMAIL, EMAIL, ".*@example[.]test"])
        self.assertEqual(result.total_commits, 3)
        self.assertEqual(result.daily_activity["Tue"], 3)
        self.assertEqual(result.longest_streak, 1)
        self.assertEqual(result.active_repo_count, 1)
        self.assertEqual([len(git_report.author_rows(result, i)) for i in range(3)], [1, 1, 1])
        markdown = git_report.render_markdown(result, [str(self.repo)], NOW)
        self.assertEqual(markdown.count("| **Subtotal** | **1** | **+1** | **-0** |"), 3)

    def test_adapter_invokes_one_rust_process_and_reports_progress(self):
        self.commit()
        progress = []
        with patch.dict(os.environ, self.env, clear=True):
            with patch("git_tracker.subprocess.run", wraps=subprocess.run) as run:
                data, _ = git_tracker._collect_report([EMAIL, OTHER_EMAIL], [str(self.repo)],
                    now=NOW, progress_callback=lambda *args: progress.append(args))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], str(self.binary))
        self.assertEqual(data.total_commits, 1)
        self.assertEqual(progress[0][:2], (0, 0))
        self.assertEqual(progress[-1][:2], (1, 1))

    def test_shell_exports_configuration_from_synthetic_env_file(self):
        self.commit(datetime.now(timezone.utc).isoformat())
        source = Path(git_tracker.__file__).parent
        for name in ("git-weekly-report.sh", "git_tracker.py", "git_report.py"):
            shutil.copyfile(source / name, self.root / name)
        (self.root / ".env").write_text("\n".join(
            f"{key}={shlex.quote(value)}" for key, value in {
                "GIT_REPORT_EMAILS": EMAIL, "GIT_REPORT_REPOS": str(self.repo),
                "GIT_TRACKER_BIN": str(self.binary),
            }.items()))
        env = {k: v for k, v in self.env.items() if k not in (
            "GIT_REPORT_EMAILS", "GIT_REPORT_REPOS", "GIT_TRACKER_BIN")}
        result = subprocess.run(["bash", str(self.root / "git-weekly-report.sh")],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Report saved to:", result.stdout)
        markdown = next(self.root.glob("weekly-report-*.md")).read_text()
        self.assertIn("| Total Commits | 1 |", markdown)
        self.assertIn(f"## {EMAIL}", markdown)

    def test_demo_fixture_runs_with_copied_helpers_and_binary_without_capture(self):
        # A synthetic VHS peer verifies packaging and runs the fixture report.
        # It never opens a browser, records video, or writes to demo assets.
        bindir = self.root / "capture peers"
        bindir.mkdir()
        peer = bindir / "vhs"
        peer.write_text(f"#!{sys.executable}\n" +
            "import os, pathlib, re, subprocess, sys\n"
            "tape = pathlib.Path(sys.argv[1]).read_text()\n"
            "directory = re.search(r'^Type \\\"cd \\\'(.*)\\\'\\\"$', tape, re.M).group(1)\n"
            "root = pathlib.Path(directory)\n"
            "for name in ['git-weekly-report.sh', 'git_tracker.py', 'git_report.py', 'target/release/gitweeklytracker-core']:\n"
            "    assert (root / name).is_file(), name\n"
            "env = {k:v for k,v in os.environ.items() if k != 'GIT_TRACKER_BIN'}\n"
            "env.update(GIT_REPORT_EMAILS='demo@example.com', GIT_REPORT_REPOS='./demo-repos')\n"
            "subprocess.run(['bash', 'git-weekly-report.sh'], cwd=root, env=env, check=True, capture_output=True)\n"
            "report = next(root.glob('weekly-report-*.md')).read_text()\n"
            "assert '## demo@example.com' in report\n"
            "assert '| Total Commits | 0 |' not in report\n"
            "print('Verified synthetic capture fixture')\n")
        peer.chmod(0o755)
        # The existing capture script uses BSD date flags. A bounded clock peer
        # keeps this offline packaging check portable to the Linux CI runner.
        clock = bindir / "date"
        clock.write_text(f"#!{sys.executable}\n" +
            "import sys\nfrom datetime import datetime, timedelta, timezone\n"
            "now = datetime.now(timezone.utc)\n"
            "for arg in sys.argv[1:]:\n"
            "    if arg.startswith('-v-') and arg.endswith('d'):\n"
            "        now -= timedelta(days=int(arg[3:-1]))\n"
            "print(now.strftime(sys.argv[-1].lstrip('+')))\n")
        clock.chmod(0o755)
        script = Path(git_tracker.__file__).parent / "demo/pipeline/record-clips.sh"
        result = subprocess.run(["bash", str(script)], cwd=self.root, capture_output=True,
            text=True, timeout=20, env={**self.env, "PATH": str(bindir) + os.pathsep + self.env["PATH"]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("Verified synthetic capture fixture"), 4)

    def test_report_cli_no_repositories_and_invalid_config_do_not_write_reports(self):
        script = Path(git_report.__file__)
        for emails, now in [(EMAIL, NOW.isoformat()), (" , ", NOW.isoformat()), (EMAIL, "invalid")]:
            with self.subTest(emails=emails, now=now):
                result = subprocess.run([sys.executable, str(script), "--now", now],
                    cwd=self.root, capture_output=True, text=True, timeout=10,
                    env={**self.env, "GIT_REPORT_EMAILS": emails,
                         "GIT_REPORT_REPOS": str(self.root / "missing")})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(list(self.root.glob("weekly-report-*.md")))

    def test_merge_count_includes_merge_without_double_counting_file_diffs(self):
        self.commit("2026-10-16T12:00:00Z", "base.txt")
        self.git("checkout", "-qb", "side")
        self.commit("2026-10-17T12:00:00Z", "side.txt")
        self.git("checkout", "-q", "main")
        self.commit("2026-10-18T12:00:00Z", "main.txt")
        self.git("merge", "--no-ff", "-qm", "Fixture merge", "side",
                 env=self.commit_env("2026-10-19T12:00:00Z"))
        result = self.report()
        self.assertEqual((result.total_commits, result.total_insertions,
                          result.per_repo[0].files_changed), (4, 3, 3))
        self.assertEqual(result.longest_streak, 4)
        self.assertEqual(sum(result.daily_activity.values()), 4)
        self.assertEqual(set(result.top_files), {(1, f"{self.repo.name}/{name}.txt")
                                                for name in ("base", "side", "main")})

    def test_rename_and_unusual_filenames_are_not_split_or_git_quoted(self):
        old = "old name.txt"
        new = 'nested/new | `name` é\tline\n.txt'
        self.commit("2026-10-16T12:00:00Z", old)
        (self.repo / "nested").mkdir()
        self.git("mv", "--", old, new)
        self.git("commit", "-qm", "Rename", env=self.commit_env("2026-10-17T12:00:00Z"))
        self.commit("2026-10-18T12:00:00Z", new)
        result = self.report()
        self.assertEqual((result.total_commits, result.total_insertions,
                          result.total_deletions, result.per_repo[0].files_changed), (3, 2, 0, 3))
        self.assertEqual(result.top_files, [(2, f"{self.repo.name}/{new}"),
                                          (1, f"{self.repo.name}/{old}")])
        markdown = git_report.render_markdown(result, [str(self.repo)], NOW)
        self.assertIn("new \\| `name` é\\tline\\n.txt", markdown)

    def test_binary_files_count_as_changed_without_line_insertions(self):
        self.commit(content=b"\0binary\xfffixture")
        result = self.report()
        self.assertEqual((result.total_commits, result.total_insertions,
                          result.total_deletions, result.per_repo[0].files_changed), (1, 0, 0, 1))

    def test_deletions_and_negative_net_change_match_git_shortstat(self):
        self.commit("2026-10-14T12:00:00Z", content=b"one\ntwo\nthree\n")
        self.commit(content=b"one\n")
        result = self.report()
        self.assertEqual((result.total_commits, result.total_insertions,
                          result.total_deletions, result.net_change), (1, 0, 2, -2))
        self.assertIn("| Net Change | -2 |", git_report.render_markdown(result, [], NOW))

    def test_empty_commit_has_activity_and_no_changed_files(self):
        self.git("commit", "--allow-empty", "-qm", "Empty fixture",
                 env=self.commit_env("2026-10-20T12:00:00Z"))
        result = self.report()
        self.assertEqual((result.total_commits, result.total_insertions,
                          result.longest_streak, result.top_files), (1, 0, 1, []))

    def test_committer_window_and_author_day_semantics_are_preserved(self):
        (self.repo / "x").write_text("x\n")
        self.git("add", "x")
        env = self.commit_env("2026-10-20T12:00:00Z")
        env["GIT_AUTHOR_DATE"] = "2026-10-14T23:30:00-02:00"
        self.git("commit", "-qm", "Different author time", env=env)
        result = self.report()
        self.assertEqual(result.total_commits, 1)
        self.assertEqual(result.daily_activity["Thu"], 1)

    def test_top_files_are_limited_to_ten_and_ties_keep_first_seen_order(self):
        for i in range(12):
            self.commit(filename=f"file-{i:02d}")
        result = self.report()
        self.assertEqual(result.total_commits, 12)
        self.assertEqual(result.top_files, [(1, f"{self.repo.name}/file-{i:02d}")
                                          for i in range(11, 1, -1)])

    def test_parent_paths_and_symlink_aliases_are_deduplicated(self):
        self.commit()
        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        result = self.report(paths=[alias, self.repo, self.root])
        self.assertEqual(result.repos_scanned, 1)
        self.assertEqual(result.total_commits, 1)
        self.assertEqual(result.per_repo[0].repo_path, str(alias))

    def test_git_file_repositories_and_tilde_paths_are_discovered(self):
        repo = self.root / "separate repo"
        self.git("init", "-q", "-b", "main", "--separate-git-dir", str(self.root / "metadata"), str(repo))
        self.commit(repo=repo)
        result = self.report(paths=["~/separate repo"])
        self.assertEqual(result.total_commits, 1)
        self.assertEqual(result.per_repo[0].repo_path, str(repo))

    def test_explicit_subdirectory_retains_shell_report_support(self):
        self.commit(filename="nested/first.txt")
        result = self.report(paths=[self.repo / "nested"])
        self.assertEqual((result.total_commits, result.repos_scanned), (1, 1))
        self.assertEqual(result.per_repo[0].repo_name, "nested")

    def test_explicit_dot_segments_use_normalized_absolute_paths(self):
        self.commit()
        (self.repo / "nested").mkdir()
        result = self.report(paths=[str(self.repo / "nested") + "/../."])
        self.assertEqual(result.per_repo[0].repo_path, str(self.repo))

    def test_home_discovery_is_filtered_pruned_and_depth_limited(self):
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        self.commit(stamp)
        for parent in ("node_modules", ".cache", "Library", ".Trash", ".cargo", ".rustup",
                       "one/two/three/four"):
            repo = self.root / parent / "ignored repo"
            repo.mkdir(parents=True)
            self.git("init", "-q", "-b", "main", repo=repo)
            self.commit(stamp, repo=repo)
        unselected = self.root / "unselected"
        unselected.mkdir()
        self.git("init", "-q", "-b", "main", repo=unselected)
        self.commit(stamp, email=OTHER_EMAIL, repo=unselected)
        result = self.report(paths=[], now=now)
        self.assertEqual(result.repos_scanned, 1)
        self.assertEqual(result.per_repo[0].repo_path, str(self.repo))

    def test_missing_git_is_a_zero_activity_report(self):
        empty_path = self.root / "no programs"
        empty_path.mkdir()
        result = self.run_core(env={**self.env, "PATH": str(empty_path)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_commits"], 0)

    def test_git_failure_ignores_partial_output(self):
        env = self.fake_git(f"import sys\nprint('COMMIT:{UNTIL}')\nsys.exit(128)\n")
        result = self.run_core(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_commits"], 0)

    def test_timeout_kills_helpers_holding_stdout_open(self):
        env = self.fake_git(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n")
        start = time.monotonic()
        result = self.run_core("--timeout-ms", "50", env=env)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_commits"], 0)

    def test_large_git_output_does_not_deadlock_the_timeout_monitor(self):
        env = self.fake_git(
            "import sys\n"
            f"if '--shortstat' in sys.argv:\n    print(('COMMIT:{UNTIL}\\n 1 file changed, 1 insertion(+)\\n') * 5000)\n"
            "else:\n    sys.stdout.write('same file.txt\\0' * 5000)\n")
        result = self.run_core(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["total_commits"], 5000)
        self.assertEqual(data["total_insertions"], 5000)
        self.assertEqual(data["top_files"], [[5000, f"{self.repo.name}/same file.txt"]])

    def test_detached_stdout_helper_cannot_extend_git_deadline(self):
        marker = self.root / "detached-helper-started"
        helper = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(2)"
        env = self.fake_git(
            "import subprocess, sys, time\nfrom pathlib import Path\n"
            f"subprocess.Popen([sys.executable, '-c', {helper!r}], start_new_session=True)\n"
            f"deadline=time.monotonic()+1\nwhile not Path({str(marker)!r}).exists() and time.monotonic()<deadline: time.sleep(0.005)\n")
        start = time.monotonic()
        result = self.run_core("--timeout-ms", "500", env=env)
        self.assertLess(time.monotonic() - start, 1.25)
        self.assertTrue(marker.exists(), "The detached peer must actually retain stdout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_commits"], 0)

    def test_report_escapes_controls_without_changing_analytics_paths(self):
        filename = "file-\x1b[2J\x1b[H-\x85-\u2028.txt"
        self.commit(filename=filename)
        data = self.report()
        self.assertEqual(data.top_files[0][1], f"{self.repo.name}/{filename}")
        for output in (git_report.render_terminal(data, Path("report.md")),
                       git_report.render_markdown(data, [str(self.repo)], NOW)):
            for control in ("\x1b", "\x85", "\u2028"):
                self.assertNotIn(control, output)
            self.assertIn(r"\x1b", output)

    def test_analytics_utf8_protocol_is_independent_of_ascii_locale(self):
        self.commit(filename="café.txt")
        code = (
            "from datetime import datetime, timezone; import git_tracker; "
            f"data=git_tracker._collect_report([{EMAIL!r}], [{str(self.repo)!r}], "
            "now=datetime(2026,10,22,12,tzinfo=timezone.utc))[0]; "
            "assert data.total_commits == 1; "
            "assert data.top_files[0][1].endswith('/caf\\u00e9.txt')"
        )
        env = {**self.env, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
               "PYTHONPATH": str(Path(git_tracker.__file__).parent)}
        result = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=env,
                                capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_cli_arguments_fail_without_git_access(self):
        marker = self.root / "git-called"
        env = self.fake_git(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        for args in [("--since", "bad"), ("--since", str(UNTIL + 1)),
                     ("--timeout-ms", "0"), ("--until", "253402300800"),
                     ("--unknown", "value"), ("--email", "")]:
            with self.subTest(args=args):
                result = self.run_core(*args, env=env)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertFalse(marker.exists())

    def test_ambient_git_directory_cannot_redirect_an_explicit_repo(self):
        self.commit()
        env = {**self.env, "GIT_DIR": str(self.root / "missing.git"),
               "GIT_WORK_TREE": str(self.root / "elsewhere")}
        result = self.run_core(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_commits"], 1)


class AdapterTests(unittest.TestCase):
    def test_unconfigured_analytics_does_not_start_a_process(self):
        with patch("git_tracker.subprocess.run") as run:
            self.assertIsNone(git_tracker.get_git_analytics([]))
        run.assert_not_called()

    def test_missing_binary_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"GIT_TRACKER_BIN": str(Path(root) / "missing")}):
                with self.assertRaisesRegex(RuntimeError, "cargo build --release --locked"):
                    git_tracker.get_git_analytics([EMAIL], [root])

    def test_corrupt_backend_response_is_reported(self):
        for payload in ["{", "null", "[]", "{}"]:
            with self.subTest(payload=payload), patch("git_tracker.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 0, payload, "")):
                with self.assertRaisesRegex(RuntimeError, "Invalid response"):
                    git_tracker.get_git_analytics([EMAIL], ["/synthetic"])

    def test_markdown_code_preserves_backticks_pipes_and_line_breaks(self):
        self.assertEqual(git_report.markdown_code("a`b"), "``a`b``")
        self.assertEqual(git_report.markdown_code("`a`"), "`` `a` ``")
        self.assertEqual(git_report.markdown_code("a|b\nc"), "`a\\|b\\nc`")
