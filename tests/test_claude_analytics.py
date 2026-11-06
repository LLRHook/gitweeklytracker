import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

import claude_analytics


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 22, 12, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class ClaudeReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project = self.root / "projects" / "fixture"
        project.mkdir(parents=True)
        self.log = project / "session.jsonl"
        (self.root / "history.jsonl").write_text(json.dumps({
            "sessionId": "session", "project": "/fixture/project",
        }) + "\n")

    def report(self, timestamps):
        records = [{
            "type": "assistant", "timestamp": timestamp,
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        } for timestamp in timestamps]
        self.log.write_text("\n".join(json.dumps(r) for r in records))
        with patch.object(claude_analytics, "datetime", FrozenDatetime):
            return claude_analytics.get_claude_analytics(7, str(self.root))

    def test_daily_rows_include_partial_first_day_of_rolling_window(self):
        result = self.report(["2026-10-15T13:00:00Z", "2026-10-22T11:00:00Z"])
        self.assertEqual(result.total_messages, 2)
        self.assertEqual(sum(d.message_count for d in result.daily_usage), 2)
        self.assertEqual(sum(d.usage.total for d in result.daily_usage),
                         result.total_tokens.total)
        self.assertEqual(str(result.daily_usage[0].date), "2026-10-15")

    def test_future_records_are_excluded_from_report(self):
        result = self.report(["2026-10-22T11:00:00Z", "2026-10-22T13:00:00Z"])
        self.assertEqual(result.total_messages, 1)

    def test_daily_rows_normalize_timestamp_offsets_to_utc(self):
        result = self.report(["2026-10-21T23:30:00-02:00"])
        by_date = {str(d.date): d.message_count for d in result.daily_usage}
        self.assertEqual(by_date["2026-10-22"], 1)

    def test_exact_bounds_and_legacy_timestamps(self):
        result = self.report([
            "2026-10-15T11:59:59Z", "2026-10-15T12:00:00Z",
            "2026-10-22T12:00:00Z", "2026-10-22T12:00:01Z",
            "2026-10-22T11:00:00", "invalid", 123,
        ])
        self.assertEqual(result.total_messages, 3)
        self.assertEqual(sum(d.message_count for d in result.daily_usage), 3)

    def test_empty_report_covers_the_same_dates_as_a_populated_report(self):
        populated = self.report(["2026-10-22T11:00:00Z"])
        self.log.unlink()
        self.log.parent.rmdir()
        self.log.parent.parent.rmdir()
        with patch.object(claude_analytics, "datetime", FrozenDatetime):
            empty = claude_analytics.get_claude_analytics(7, str(self.root))
        self.assertEqual([d.date for d in populated.daily_usage],
                         [d.date for d in empty.daily_usage])


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.creds = {"accessToken": "fixture-access"}
        self.load_credentials = claude_analytics._load_oauth_credentials
        for patcher in [
            patch.object(claude_analytics, "USAGE_BAR_CONFIG_DIR", str(self.root)),
            patch.object(claude_analytics, "_load_oauth_credentials", return_value=self.creds),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / "history.json").write_text(json.dumps([
            {"pct5h": 0.25, "pct7d": 0.5},
        ]))

    def test_http_failure_uses_history_and_has_timeout(self):
        with patch("claude_analytics.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("offline")) as open_url:
            result = claude_analytics.get_usage_quota()
        self.assertTrue(result.available)
        self.assertEqual(result.five_hour.utilization, 25)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)

    def test_timeout_uses_history(self):
        with patch("claude_analytics.urllib.request.urlopen",
                   side_effect=TimeoutError("timed out")):
            result = claude_analytics.get_usage_quota()
        self.assertEqual(result.seven_day.utilization, 50)

    def test_truncated_body_uses_history(self):
        class IncompleteResponse(io.BytesIO):
            def read(self, *args):
                raise IncompleteRead(b"{")

        with patch("claude_analytics.urllib.request.urlopen",
                   return_value=IncompleteResponse()):
            result = claude_analytics.get_usage_quota()
        self.assertEqual(result.seven_day.utilization, 50)

    def test_malformed_live_json_shapes_fall_back_to_history(self):
        for payload in [[], None, {}, {"five_hour": "invalid"},
                        {"five_hour": {"utilization": "invalid"}},
                        {"five_hour": {"utilization": True}},
                        {"five_hour": {"utilization": float("nan")}},
                        {"five_hour": {"utilization": 101}}]:
            with self.subTest(payload=payload):
                with patch("claude_analytics.urllib.request.urlopen",
                           return_value=io.BytesIO(json.dumps(payload).encode())):
                    result = claude_analytics.get_usage_quota()
                self.assertTrue(result.available)
                self.assertEqual(result.five_hour.utilization, 25)

    def test_successful_live_response_preserves_percentage_units(self):
        payload = {
            "five_hour": {"utilization": 12.5, "resets_at": "2026-10-23T00:00:00Z"},
            "seven_day": {"utilization": 0, "resets_at": None},
        }
        with patch("claude_analytics.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(payload).encode())) as open_url:
            result = claude_analytics.get_usage_quota()
        self.assertTrue(result.available)
        self.assertEqual(result.five_hour.utilization, 12.5)
        self.assertEqual(result.seven_day.utilization, 0)
        self.assertEqual(result.seven_day.reset_description, "")
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-access")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)

    def test_history_is_available_without_credentials(self):
        with patch.object(claude_analytics, "_load_oauth_credentials", return_value=None), \
             patch("claude_analytics.urllib.request.urlopen") as open_url:
            result = claude_analytics.get_usage_quota()
        self.assertTrue(result.available)
        self.assertEqual(result.five_hour.utilization, 25)
        open_url.assert_not_called()

    def test_invalid_history_is_unavailable_instead_of_crashing(self):
        for history in [{"bad": "shape"}, [None], [{"pct5h": "invalid"}], []]:
            with self.subTest(history=history):
                (self.root / "history.json").write_text(json.dumps(history))
                with patch("claude_analytics.urllib.request.urlopen",
                           side_effect=urllib.error.URLError("offline")):
                    result = claude_analytics.get_usage_quota()
                self.assertFalse(result.available)

    def test_history_skips_corrupted_latest_entry(self):
        (self.root / "history.json").write_text(json.dumps([
            {"pct5h": 0.25, "pct7d": 0.5}, {"pct5h": "invalid"},
        ]))
        with patch("claude_analytics.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("offline")):
            result = claude_analytics.get_usage_quota()
        self.assertEqual(result.five_hour.utilization, 25)

    def test_malformed_credentials_file_is_ignored(self):
        for payload in [[], None, "invalid"]:
            with self.subTest(payload=payload):
                (self.root / "credentials.json").write_text(json.dumps(payload))
                self.assertIsNone(self.load_credentials())

    def test_refresh_bad_payload_preserves_existing_credentials(self):
        creds = {
            "accessToken": "fixture-old", "refreshToken": "fixture-refresh",
            "expiresAt": "2000-01-01T00:00:00Z",
        }
        payload = {"access_token": "fixture-new", "expires_in": "invalid"}
        with patch("claude_analytics.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(payload).encode())), \
             patch.object(claude_analytics, "_save_oauth_credentials") as save:
            result = claude_analytics._refresh_token_if_needed(creds)
        self.assertEqual(result["accessToken"], "fixture-old")
        save.assert_not_called()

    def test_refresh_success_saves_new_credentials_without_mutating_input(self):
        creds = {"accessToken": "fixture-old", "refreshToken": "fixture-refresh"}
        payload = {"access_token": "fixture-new", "refresh_token": "fixture-rotated",
                   "expires_in": 3600}
        with patch("claude_analytics.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(payload).encode())) as open_url, \
             patch.object(claude_analytics, "datetime", FrozenDatetime), \
             patch.object(claude_analytics, "_save_oauth_credentials") as save:
            result = claude_analytics._refresh_token_if_needed(creds)
        self.assertEqual(result["accessToken"], "fixture-new")
        self.assertEqual(result["refreshToken"], "fixture-rotated")
        self.assertEqual(result["expiresAt"], "2026-10-22T13:00:00+00:00")
        self.assertEqual(creds["accessToken"], "fixture-old")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)
        save.assert_called_once_with(result)

    def test_reset_description_accepts_legacy_timestamp_without_offset(self):
        with patch.object(claude_analytics, "datetime", FrozenDatetime):
            bucket = claude_analytics.UsageBucket(25, "2026-10-22T13:00:00")
            self.assertEqual(bucket.reset_description, "1h 0m")


if __name__ == "__main__":
    unittest.main()
