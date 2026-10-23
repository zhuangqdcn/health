import io
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from garmin_personal_api.errors import HttpError

import garmin_sync as sync


NOW = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
EXAMPLE = sync.ROOT / "garmin.sync.settings.example.json"


def http_error(code):
    return HttpError(status_code=code, method="GET", url="https://example.invalid",
                     response_text="SECRET_MUST_NOT_APPEAR")


def zip_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("activity.fit", b"test-fit")
    return stream.getvalue()


class FakeClock:
    def __init__(self):
        self.now = NOW.timestamp()
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("Negative sleep")
        self.sleeps.append(seconds)
        self.now += seconds


def response(status=200, headers=None):
    value = requests.Response()
    value.status_code = status
    value.headers.update(headers or {})
    value._content = b"{}"
    value._content_consumed = True
    return value


class FakeApi:
    def __init__(self, pages=None):
        self.calls = []
        self.pages = pages or {}
        self.failures = {}

    def fetch(self, path, params, binary=False):
        self.calls.append((path, dict(params), binary))
        failure = self.failures.get(path)
        if failure is not None:
            raise failure
        if path == sync.SEARCH_PATH:
            return self.pages.get(params["start"], [])
        if path == "/userprofile-service/userprofile/settings":
            return {"timeZone": "America/Los_Angeles"}
        return zip_bytes() if binary else {"data": [], "path": path}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.settings = replace(
            sync.Settings.load(self.settings_path), download_dir=self.root / "archive",
            refresh_recent_days=1,
        )
        (self.settings.download_dir / ".state").mkdir(parents=True)
        self.store = sync.Store(self.settings.download_dir)
        self.addCleanup(self.store.close)

    def engine(self, api=None, settings=None, now=NOW):
        return sync.Synchronizer(settings or self.settings, self.store, api or FakeApi(), now)

    def test_defaults_and_relative_paths(self):
        settings = sync.Settings.load(self.settings_path)
        self.assertEqual(settings.download_dir, self.root / "garmin-data")
        self.assertEqual(settings.zone.key, "America/Los_Angeles")
        self.assertEqual(settings.schedule_time, (9, 30))

    def test_invalid_settings(self):
        original = json.loads(self.settings_path.read_text(encoding="utf-8"))
        changes = [
            {"timezone": "PST"}, {"window_start": "9:00"}, {"schedule_time": "08:30"},
            {"retries": True}, {"timeout_seconds": 0}, {"request_delay_seconds": -1},
            {"refresh_recent_days": 0}, {"download_dir": ""}, {"unexpected": 1},
            {"history": {"enabled": True, "start_date": "2020-1-01", "days_per_run": 30}},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.settings_path.write_text(json.dumps(original | change), encoding="utf-8")
                with self.assertRaises(sync.SyncError):
                    sync.Settings.load(self.settings_path)

    def test_dst_windows_are_23_and_25_hours(self):
        for end_day, hours in [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)]:
            start, end = sync.window(self.settings, end_day)
            self.assertEqual(start.hour, 9)
            self.assertEqual(end.hour, 9)
            self.assertEqual((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds(), hours * 3600)

    def test_latest_window_boundary(self):
        before = datetime(2026, 10, 22, 15, 59, 59, tzinfo=UTC)
        at = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)
        self.assertEqual(sync.latest_window_date(self.settings, before), date(2026, 10, 21))
        self.assertEqual(sync.latest_window_date(self.settings, at), date(2026, 10, 22))

    def test_scheduled_gate_in_both_dst_seasons(self):
        for before, at, expected in [
            ("2026-10-22T16:29:00+00:00", "2026-10-22T16:30:00+00:00", date(2026, 10, 22)),
            ("2026-12-22T17:29:00+00:00", "2026-12-22T17:30:00+00:00", date(2026, 12, 22)),
        ]:
            self.assertEqual(sync.scheduled_date(self.settings, datetime.fromisoformat(before)),
                             expected - timedelta(days=1))
            self.assertEqual(sync.scheduled_date(self.settings, datetime.fromisoformat(at)), expected)

    def test_settings_key_is_never_returned_in_config_check(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(sync.main(["--settings", str(self.settings_path), "--check-config"]), 0)
        self.assertTrue(json.loads(output.getvalue())["settings_valid"])
        self.assertNotIn("GARMIN_PERSONAL_API_KEY", output.getvalue())

    def test_env_precedence_and_empty_key_is_error(self):
        path = self.root / ".env"
        path.write_text('GARMIN_PERSONAL_API_KEY="file-key"\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sync.read_api_key(path), "file-key")
        with patch.dict(os.environ, {"GARMIN_PERSONAL_API_KEY": "env-key"}):
            self.assertEqual(sync.read_api_key(path), "env-key")
        with patch.dict(os.environ, {"GARMIN_PERSONAL_API_KEY": ""}):
            with self.assertRaises(sync.SyncError):
                sync.read_api_key(path)

    def test_file_integrity_and_atomic_cleanup(self):
        self.store.record("test", "ok", "/", NOW, "raw/test.json", b"{}")
        path = self.settings.download_dir / "raw" / "test.json"
        self.assertTrue(self.store.ready("test"))
        path.write_bytes(b"corrupt")
        self.assertFalse(self.store.ready("test"))
        path.unlink()
        self.assertFalse(self.store.ready("test"))
        self.assertFalse(path.with_name("test.json.part").exists())

    def test_repeated_run_reuses_files_but_refreshes_catalog(self):
        api = FakeApi()
        first = self.engine(api).run()
        initial_calls = len(api.calls)
        second = self.engine(api).run()
        self.assertGreater(first["downloaded"], 1)
        self.assertEqual(len(api.calls) - initial_calls, 1)
        self.assertEqual(second["downloaded"], 1)
        self.assertTrue(second["health_history_needs_start_date"])

    def test_daily_data_is_raw_and_two_dates_are_referenced(self):
        self.engine().run()
        path = self.settings.download_dir / "windows" / "2026-10-22.json"
        manifest = json.loads(path.read_bytes())
        self.assertEqual(manifest["start_inclusive"], "2026-10-21T09:00:00-07:00")
        self.assertEqual(manifest["end_exclusive"], "2026-10-22T09:00:00-07:00")
        self.assertEqual(len(manifest["daily_files"]), len(sync.DAILY) * 2)
        raw = json.loads((self.settings.download_dir / "daily" / "2026-10-21" / "sleep.json").read_bytes())
        self.assertEqual(raw, {"data": [], "path": "/sleep-service/sleep/dailySleep"})

    def test_pagination_continues_after_short_pages_and_includes_children(self):
        api = FakeApi({
            0: [{"activityId": 1, "startTimeGMT": "2026-10-22 15:00:00"}],
            1: [{"activityId": 2, "startTimeGMT": "2010-01-01 00:00:00"}],
        })
        report = self.engine(api).run()
        pages = [params for path, params, _ in api.calls if path == sync.SEARCH_PATH]
        self.assertEqual([page["start"] for page in pages], [0, 1, 2])
        self.assertTrue(all(page["excludeChildren"] == "false" for page in pages))
        self.assertEqual(report["activity_catalog_count"], 2)
        self.assertTrue((self.settings.download_dir / "activities" / "2" / "original.zip").is_file())
        self.assertTrue(report["health_history_needs_start_date"])

    def test_stalled_pagination_fails(self):
        item = {"activityId": 1, "startTimeGMT": "2026-10-22 15:00:00"}
        with self.assertRaisesRegex(sync.SyncError, "pagination"):
            self.engine(FakeApi({0: [item], 1: [item]})).run()

    def test_activity_catalog_failure_is_not_history_success(self):
        api = FakeApi()
        api.failures[sync.SEARCH_PATH] = http_error(403)
        with self.assertRaisesRegex(sync.SyncError, "catalog"):
            self.engine(api).run()

    def test_empty_json_and_no_data_are_valid(self):
        api = Mock()
        api.fetch.return_value = None
        self.engine(api).save("empty", "raw/empty.json", "/hrv", {})
        self.assertTrue(self.store.ready("empty"))
        self.assertEqual((self.settings.download_dir / "raw" / "empty.json").read_bytes(), b"null\n")

    def test_failed_download_is_retried_not_marked_complete(self):
        api = FakeApi()
        api.failures["/test"] = http_error(500)
        engine = self.engine(api)
        with self.assertLogs(sync.LOG, level="ERROR") as logs:
            engine.save("test", "test.json", "/test", {})
        self.assertNotIn("SECRET_MUST_NOT_APPEAR", "".join(logs.output))
        self.assertEqual(self.store.row("test")["status"], "error")
        self.assertFalse(self.store.ready("test"))
        del api.failures["/test"]
        engine.save("test", "test.json", "/test", {})
        self.assertTrue(self.store.ready("test"))

    def test_forbidden_is_reported_and_restored_permissions_are_backfilled(self):
        api = FakeApi()
        endpoint = next(endpoint for endpoint in sync.DAILY if endpoint.name == "sleep")
        api.failures[endpoint.path] = http_error(403)
        report = self.engine(api).run()
        self.assertGreater(report["unavailable_this_run"], 0)
        self.assertEqual(self.store.row("day:2026-10-21:sleep")["status"], "forbidden")
        del api.failures[endpoint.path]
        self.engine(api, now=NOW + timedelta(days=1)).run()
        # Access restoration invalidates historical forbidden entries rather than
        # silently retaining them as successfully downloaded health history.
        row = self.store.row("day:2026-10-21:sleep")
        self.assertTrue(row is None or row["status"] == "ok")

    def test_not_found_is_distinct_from_success(self):
        api = FakeApi()
        api.failures["/missing"] = http_error(404)
        self.engine(api).save("missing", "missing.json", "/missing", {})
        self.assertEqual(self.store.describe("missing")["status"], "not_found")
        self.assertIsNone(self.store.describe("missing")["path"])

    def test_health_backfill_is_bounded_and_resumes(self):
        settings = replace(self.settings, history_start=date(2026, 10, 15), history_days_per_run=2)
        api = FakeApi()
        first = self.engine(api, settings).run()
        self.assertTrue(first["health_history_pending"])
        self.assertTrue((settings.download_dir / "windows" / "2026-10-16.json").is_file())
        self.assertFalse((settings.download_dir / "windows" / "2026-10-19.json").is_file())
        for _ in range(5):
            report = self.engine(api, settings).run()
            if not report["health_history_pending"]:
                break
        self.assertFalse(report["health_history_pending"])
        self.assertEqual(report["status"], "complete_for_accessible_data")
        for day in range(16, 23):
            self.assertTrue((settings.download_dir / "windows" / f"2026-10-{day}.json").is_file())

    def test_missed_days_since_first_run_are_recovered_without_guessing_account_boundary(self):
        self.engine().run()
        report = self.engine(now=NOW + timedelta(days=5)).run()
        self.assertTrue(report["health_history_needs_start_date"])
        for day in range(22, 28):
            self.assertTrue((self.settings.download_dir / "windows" / f"2026-10-{day}.json").is_file())

    def test_original_zip_is_validated_before_state_is_committed(self):
        api = Mock()
        api.fetch.return_value = b"<html>not a file</html>"
        engine = self.engine(api)
        engine.save("file", "original.zip", "/download/1", {}, binary=True)
        self.assertEqual(self.store.row("file")["status"], "error")
        self.assertFalse((self.settings.download_dir / "original.zip").exists())

    def test_deleted_activity_file_is_repaired(self):
        item = {"activityId": 1, "startTimeGMT": "2026-10-22 15:00:00"}
        api = FakeApi({0: [item]})
        self.engine(api).run()
        path = self.settings.download_dir / "activities" / "1" / "original.zip"
        path.unlink()
        self.engine(api).run()
        self.assertTrue(zipfile.is_zipfile(path))

    def test_changed_historical_activity_refreshes_data_and_manifest(self):
        item = {"activityId": 1, "startTimeGMT": "2026-10-20 15:00:00", "activityName": "old"}
        settings = replace(self.settings, history_start=date(2026, 10, 19))
        api = FakeApi({0: [item]})
        self.engine(api, settings).run()
        api.calls.clear()
        item["activityName"] = "edited"
        report = self.engine(api, settings).run()
        self.assertEqual(report["errors"], 0)
        self.assertTrue(any(path == "/activity-service/activity/1" for path, _, _ in api.calls))
        manifest = json.loads((settings.download_dir / "windows" / "2026-10-20.json").read_bytes())
        self.assertEqual(manifest["activities"][0]["activity_id"], "1")

    def test_half_open_boundary_and_cross_window_activity(self):
        items = [
            {"activityId": 1, "startTimeGMT": "2026-10-21 16:00:00"},
            {"activityId": 2, "startTimeGMT": "2026-10-22 16:00:00"},
            {"activityId": 3, "startTimeGMT": "2026-10-21 15:30:00", "duration": 3600},
        ]
        self.engine(FakeApi({0: items})).run()
        manifest = json.loads((self.settings.download_dir / "windows" / "2026-10-22.json").read_bytes())
        self.assertEqual([item["activity_id"] for item in manifest["activities"]], ["1", "3"])
        self.assertTrue(manifest["activities"][1]["overlaps_from_previous_window"])

    def test_unknown_activity_timestamp_does_not_claim_complete_window(self):
        report = self.engine(FakeApi({0: [{"activityId": 1}]})).run()
        self.assertGreater(report["incomplete_windows"], 0)
        self.assertEqual(report["status"], "errors")
        self.assertTrue((self.settings.download_dir / "activities" / "1" / "original.zip").exists())

    def test_existing_manifest_is_repaired(self):
        settings = replace(self.settings, history_start=date(2026, 10, 19))
        self.engine(settings=settings).run()
        path = settings.download_dir / "windows" / "2026-10-20.json"
        path.write_text("corrupt", encoding="utf-8")
        self.engine(settings=settings).run()
        self.assertEqual(json.loads(path.read_bytes())["status"], "complete_for_accessible_data")

    def test_scheduled_before_nine_thirty_catches_up_only_previous_due_day(self):
        before = datetime(2026, 10, 22, 16, 10, tzinfo=UTC)
        report = self.engine(now=before).run(sync.scheduled_date(self.settings, before))
        self.assertEqual(report["latest_window"], "2026-10-21")

    def test_archive_lock_blocks_overlapping_processes(self):
        with sync.archive_lock(self.root / "lock"):
            with self.assertRaises(sync.SyncError):
                with sync.archive_lock(self.root / "lock"):
                    pass

    def test_catalog_matches_all_documented_get_surfaces(self):
        self.assertEqual(len(sync.DAILY), 20)
        self.assertEqual(len(sync.SNAPSHOTS), 2)
        self.assertEqual(len({endpoint.path for endpoint in sync.DAILY}), 20)
        day = date(2026, 10, 22)
        for endpoint in sync.DAILY + sync.SNAPSHOTS:
            path, query = endpoint.request(day, day)
            self.assertNotIn("{", path + "".join(query.values()))

    def test_rate_limit_backoff_and_network_retry(self):
        limited = requests.Response()
        limited.status_code = 429
        limited.headers["Retry-After"] = "2"
        limited._content = b"limited"
        limited._content_consumed = True
        success = requests.Response()
        success.status_code = 200
        success._content = b"{}"
        success._content_consumed = True
        sleep = Mock()
        session = sync.RetryingSession(self.settings, sleep=sleep)
        self.addCleanup(session.close)
        with patch.object(requests.Session, "request", side_effect=[limited, requests.Timeout(), success]) as call:
            self.assertIs(session.get("https://example.invalid"), success)
        self.assertEqual(call.call_count, 3)
        self.assertIn(unittest.mock.call(2), sleep.call_args_list)

    def test_network_retry_is_bounded(self):
        session = sync.RetryingSession(replace(self.settings, retries=1), sleep=Mock())
        self.addCleanup(session.close)
        with patch.object(requests.Session, "request", side_effect=requests.Timeout()) as call:
            with self.assertRaises(sync.SyncError):
                session.get("https://example.invalid")
        self.assertEqual(call.call_count, 2)

    def test_expired_or_revoked_token_is_refreshed_only_once(self):
        api = sync.Api.__new__(sync.Api)
        api.key = "test"
        api.expires_at = NOW.timestamp()
        api.auth = Mock()
        api.auth.obtain_token.return_value = Mock(access_token="token", expires_at=10**12)
        api.client = Mock()
        api.client.get.side_effect = [http_error(401), {"data": []}]
        self.assertEqual(api.fetch("/test", {}), {"data": []})
        self.assertTrue(api.auth.obtain_token.call_args.kwargs["force_refresh"])
        self.assertEqual(api.client.get.call_count, 2)

    def test_custom_api_host_is_rejected_before_credentials_are_sent(self):
        with patch.dict(os.environ, {"GARMIN_PERSONAL_API_BASE_URL": "https://example.invalid"}):
            with self.assertRaises(sync.SyncError):
                sync.Api(self.settings, "never-send-this-key")

    def test_account_timezone_fetches_all_overlapping_calendar_dates(self):
        engine = self.engine()
        from zoneinfo import ZoneInfo
        engine.calendar_zone = ZoneInfo("Pacific/Kiritimati")
        self.assertEqual(engine.calendar_days(date(2026, 10, 22)),
                         [date(2026, 10, 22), date(2026, 10, 23)])

    def test_timezone_change_does_not_mix_calendar_archives(self):
        self.store.set_meta("calendar_timezone", "UTC")
        with self.assertRaisesRegex(sync.SyncError, "calendar timezone changed"):
            self.engine().run()

    def test_scheduled_deduplication_and_new_due_day_bypasses_failure_throttle(self):
        raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        raw["history"]["enabled"] = False
        self.settings_path.write_text(json.dumps(raw), encoding="utf-8")
        (self.root / ".env").write_text("GARMIN_PERSONAL_API_KEY=test\n", encoding="utf-8")
        api = FakeApi()
        api.close = Mock()
        clock = datetime(2026, 10, 22, 16, 29, tzinfo=UTC)
        with patch("garmin_sync.Api", return_value=api) as constructor, \
                patch("garmin_sync.datetime", wraps=datetime) as dates, \
                patch("sys.stdout", new_callable=io.StringIO):
            dates.now.side_effect = lambda zone: clock.astimezone(zone)
            args = ["--settings", str(self.settings_path), "--scheduled"]
            self.assertEqual(sync.main(args), 0)
            self.assertEqual(sync.main(args), 0)
            self.assertEqual(constructor.call_count, 1)
            clock = datetime(2026, 10, 22, 16, 30, tzinfo=UTC)
            self.assertEqual(sync.main(args), 0)
            self.assertEqual(constructor.call_count, 2)
            self.assertEqual(sync.main(args), 0)
            self.assertEqual(constructor.call_count, 2)

    def test_main_missing_key_writes_explicit_failure_report(self):
        (self.root / ".env").write_text("GARMIN_PERSONAL_API_KEY=\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sync.main(["--settings", str(self.settings_path)]), 1)
        report = json.loads((self.root / "garmin-data" / "last-run.json").read_bytes())
        self.assertEqual(report["status"], "failed")
        log = (self.root / "garmin-data" / ".state" / "sync.log").read_text(encoding="utf-8")
        self.assertIn("Set GARMIN_PERSONAL_API_KEY", log)

    def test_history_before_is_bounded_and_resumable_without_recent_downloads(self):
        settings = replace(self.settings, history_start=date(2016, 12, 27), history_days_per_run=2)
        api = FakeApi({0: [
            {"activityId": 1, "startTimeGMT": "2016-12-31 20:00:00"},
            {"activityId": 2, "startTimeGMT": "2017-01-01 08:00:00"},
        ]})
        first = self.engine(api, settings).run_history(date(2017, 1, 1))
        self.assertEqual(first["calendar_dates_requested"], ["2016-12-27", "2016-12-28"])
        self.assertEqual(first["next_calendar_date"], "2016-12-29")
        self.assertEqual(first["activity_catalog_count"], 1)
        self.assertTrue((settings.download_dir / "activities" / "1" / "original.zip").is_file())
        self.assertFalse((settings.download_dir / "activities" / "2").exists())
        self.assertFalse((settings.download_dir / "daily" / "2017-01-01").exists())
        self.assertFalse((settings.download_dir / "daily" / "2026-10-22").exists())
        search = [params for path, params, _ in api.calls if path == sync.SEARCH_PATH]
        self.assertTrue(all(params["endDate"] == "2016-12-31" for params in search))
        self.assertFalse((settings.download_dir / "windows").exists())
        self.assertIsNone(self.store.get_meta("first_window_start"))
        second = self.engine(api, settings).run_history(date(2017, 1, 1))
        self.assertEqual(second["calendar_dates_requested"], ["2016-12-29", "2016-12-30"])
        final = self.engine(api, settings).run_history(date(2017, 1, 1))
        self.assertEqual(final["calendar_dates_requested"], ["2016-12-31"])
        self.assertFalse(final["health_history_pending"])
        self.assertEqual(final["status"], "complete_for_accessible_data")

    def test_history_before_rejects_unknown_activity_dates(self):
        settings = replace(self.settings, history_start=date(2016, 12, 31))
        with self.assertRaisesRegex(sync.SyncError, "without startTimeGMT"):
            self.engine(FakeApi({0: [{"activityId": 1}]}), settings).run_history(date(2017, 1, 1))

    def test_history_before_requires_explicit_valid_start(self):
        with self.assertRaises(sync.SyncError):
            self.engine().run_history(date(2017, 1, 1))
        settings = replace(self.settings, history_start=date(2017, 1, 1))
        with self.assertRaises(sync.SyncError):
            self.engine(settings=settings).run_history(date(2017, 1, 1))

    def test_history_batch_override_is_validated_without_network(self):
        with patch("garmin_sync.Api") as api:
            self.assertEqual(sync.main(["--settings", str(self.settings_path), "--days-per-run", "3"]), 1)
            self.assertEqual(sync.main([
                "--settings", str(self.settings_path), "--before", "2017-01-01", "--days-per-run", "0",
            ]), 1)
            api.assert_not_called()

    def test_history_explicit_start_filters_old_and_new_activities(self):
        settings = replace(self.settings, history_start=date(2017, 1, 1), history_days_per_run=1)
        api = FakeApi({0: [
            {"activityId": 1, "startTimeGMT": "2016-12-31 20:00:00"},
            {"activityId": 2, "startTimeGMT": "2017-01-15 20:00:00"},
            {"activityId": 3, "startTimeGMT": "2017-03-01 08:00:00"},
        ]})
        report = self.engine(api, settings).run_history(date(2017, 3, 1), date(2017, 1, 1))
        self.assertEqual(report["activity_catalog_count"], 1)
        self.assertEqual(report["activity_start_date_inclusive"], "2017-01-01")
        self.assertTrue((settings.download_dir / "activities" / "2" / "original.zip").exists())
        self.assertFalse((settings.download_dir / "activities" / "1").exists())
        self.assertFalse((settings.download_dir / "activities" / "3").exists())
        search = [params for path, params, _ in api.calls if path == sync.SEARCH_PATH]
        self.assertTrue(all(params["startDate"] == "2017-01-01" for params in search))
        self.assertTrue(all(params["endDate"] == "2017-02-28" for params in search))

    def test_history_requests_all_26_surfaces_when_activity_exists(self):
        settings = replace(self.settings, history_start=date(2017, 2, 28), history_days_per_run=1)
        api = FakeApi({0: [{"activityId": 1, "startTimeGMT": "2017-02-28 20:00:00"}]})
        self.engine(api, settings).run_history(date(2017, 3, 1), date(2017, 2, 28))
        paths = {path for path, _, _ in api.calls}
        self.assertEqual(len(paths), 26)
        self.assertIn("/biometric-service/heartRateZones/v2", paths)
        for endpoint in sync.DAILY:
            path, _ = endpoint.request(date(2017, 2, 28), NOW.astimezone(settings.zone).date())
            self.assertIn(path, paths)

    def test_history_rechecks_each_days_forbidden_endpoint(self):
        settings = replace(self.settings, history_start=date(2017, 1, 1), history_days_per_run=2)
        api = FakeApi()
        api.failures["/sleep-service/sleep/dailySleep"] = http_error(403)
        self.engine(api, settings).run_history(date(2017, 1, 3))
        sleeps = [params for path, params, _ in api.calls if path == "/sleep-service/sleep/dailySleep"]
        self.assertEqual([params["date"] for params in sleeps], ["2017-01-01", "2017-01-02"])

    def test_cli_explicit_start_overrides_only_this_invocation(self):
        original = self.settings_path.read_bytes()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = sync.main([
                "--settings", str(self.settings_path), "--start-date", "2017-01-01",
                "--before", "2017-03-01", "--days-per-run", "59", "--check-config",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["history_start_date"], "2017-01-01")
        self.assertEqual(self.settings_path.read_bytes(), original)
        with patch("garmin_sync.Api") as api:
            self.assertEqual(sync.main([
                "--settings", str(self.settings_path), "--start-date", "2017-01-01",
            ]), 1)
            api.assert_not_called()

    def test_429_long_retry_after_is_not_truncated_to_120_seconds(self):
        clock = FakeClock()
        sent = []
        results = iter([response(429, {"Retry-After": "300"}), response()])
        def send(*args, **kwargs):
            sent.append(clock.time())
            return next(results)
        with sync.RetryingSession(self.settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=send):
                self.assertEqual(session.get("https://example.invalid").status_code, 200)
        self.assertGreaterEqual(sent[1] - sent[0], 301)

    def test_429_http_date_uses_server_clock_to_avoid_clock_skew(self):
        clock = FakeClock()
        server = datetime(2030, 1, 1, tzinfo=UTC)
        headers = {
            "Date": format_datetime(server, usegmt=True),
            "Retry-After": format_datetime(server + timedelta(seconds=300), usegmt=True),
        }
        with sync.RetryingSession(self.settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=[response(429, headers), response()]):
                session.get("https://example.invalid")
        self.assertGreaterEqual(max(clock.sleeps), 301)
        self.assertLess(sum(clock.sleeps), 302)

    def test_429_http_date_without_server_date_honors_deadline(self):
        clock = FakeClock()
        headers = {"Retry-After": format_datetime(NOW + timedelta(seconds=300), usegmt=True)}
        with sync.RetryingSession(self.settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=[response(429, headers), response()]):
                session.get("https://example.invalid")
        self.assertGreaterEqual(clock.time(), (NOW + timedelta(seconds=301)).timestamp())

    def test_429_without_retry_after_uses_conservative_exponential_backoff(self):
        clock = FakeClock()
        with sync.RetryingSession(self.settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=[response(429), response(429), response()]):
                session.get("https://example.invalid")
        self.assertEqual(clock.sleeps[1:], [31.0, 61.0])

    def test_429_invalid_retry_after_never_causes_a_fast_retry(self):
        for index, value in enumerate(("bad", "NaN", "inf", "-1", "1e30", "January")):
            with self.subTest(value=value):
                clock = FakeClock()
                settings = replace(self.settings, download_dir=self.root / str(index))
                with sync.RetryingSession(settings, clock=clock.time, sleep=clock.sleep) as session:
                    with patch.object(requests.Session, "request", side_effect=[
                        response(429, {"Retry-After": value}), response(),
                    ]):
                        session.get("https://example.invalid")
                self.assertGreaterEqual(max(clock.sleeps), 31)

    def test_429_zero_and_past_retry_after_have_a_safety_margin(self):
        for index, value in enumerate(("0", format_datetime(NOW - timedelta(days=1), usegmt=True))):
            with self.subTest(value=value):
                clock = FakeClock()
                settings = replace(self.settings, download_dir=self.root / str(index))
                with sync.RetryingSession(settings, clock=clock.time, sleep=clock.sleep) as session:
                    with patch.object(requests.Session, "request", side_effect=[
                        response(429, {"Retry-After": value}), response(),
                    ]):
                        session.get("https://example.invalid")
                self.assertGreaterEqual(max(clock.sleeps), 2)

    def test_429_exhaustion_preserves_cooldown_and_bounds_attempt_count(self):
        clock = FakeClock()
        settings = replace(self.settings, retries=2)
        with sync.RetryingSession(settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=lambda *a, **k: response(
                429, {"Retry-After": "0"},
            )) as send:
                with self.assertRaises(sync.RateLimitError) as error:
                    session.get("https://example.invalid")
        self.assertEqual(send.call_count, 3)
        self.assertGreater(error.exception.retry_at.timestamp(), clock.time())
        state = json.loads((settings.download_dir / ".state" / "rate-limit.json").read_bytes())
        self.assertEqual(state["not_before"], error.exception.retry_at.timestamp())
        self.assertGreaterEqual(state["interval"], 4)

    def test_429_cooldown_is_respected_by_a_new_session(self):
        clock = FakeClock()
        settings = replace(self.settings, retries=0)
        with sync.RetryingSession(settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", return_value=response(429, {"Retry-After": "180"})):
                with self.assertRaises(sync.RateLimitError) as error:
                    session.get("https://example.invalid")
        sent_at = []
        def send(*args, **kwargs):
            sent_at.append(clock.time())
            return response()
        with sync.RetryingSession(settings, clock=clock.time, sleep=clock.sleep) as resumed:
            with patch.object(requests.Session, "request", side_effect=send):
                resumed.get("https://example.invalid")
        self.assertGreaterEqual(sent_at[0], error.exception.retry_at.timestamp())

    def test_successful_requests_remain_paced_after_429(self):
        clock = FakeClock()
        sent_at = []
        results = iter([response(429, {"Retry-After": "1"}), response(), response()])
        def send(*args, **kwargs):
            sent_at.append(clock.time())
            return next(results)
        with sync.RetryingSession(self.settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=send):
                session.get("https://example.invalid/one")
                session.get("https://example.invalid/two")
        self.assertGreaterEqual(sent_at[2] - sent_at[1], 1)

    def test_token_and_data_requests_share_the_same_limiter(self):
        api = sync.Api(self.settings, "test-key")
        self.addCleanup(api.close)
        self.assertIs(api.auth._session, api.client._session)

    def test_rate_limit_is_not_swallowed_or_marked_as_success(self):
        api = FakeApi()
        api.failures["/test"] = sync.RateLimitError(NOW + timedelta(seconds=60))
        engine = self.engine(api)
        with self.assertRaises(sync.RateLimitError):
            engine.save("limited", "limited.json", "/test", {})
        self.assertEqual(self.store.row("limited")["status"], "rate_limited")
        self.assertEqual(self.store.row("limited")["http_status"], 429)
        self.assertFalse(self.store.ready("limited"))
        self.assertFalse((self.settings.download_dir / "limited.json").exists())

    def test_main_reports_cooldown_with_dedicated_exit_code(self):
        (self.root / ".env").write_text("GARMIN_PERSONAL_API_KEY=test\n", encoding="utf-8")
        api = FakeApi()
        api.close = Mock()
        api.failures["/userprofile-service/userprofile/settings"] = sync.RateLimitError(
            NOW + timedelta(seconds=300),
        )
        with patch("garmin_sync.Api", return_value=api), \
                patch.dict(os.environ, {"GARMIN_PERSONAL_API_KEY": "test"}), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = sync.main(["--settings", str(self.settings_path)])
        self.assertEqual(result, 3)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "rate_limited")
        self.assertEqual(report["http_status"], 429)
        saved = json.loads((self.root / "garmin-data" / "last-run.json").read_bytes())
        self.assertEqual(saved["retry_at"], report["retry_at"])

    def test_invalid_saved_cooldown_is_not_silently_ignored(self):
        path = self.settings.download_dir / ".state" / "rate-limit.json"
        path.write_text('{"not_before":"bad","interval":1}', encoding="utf-8")
        with self.assertRaises(sync.SyncError):
            sync.RetryingSession(self.settings)

    def test_server_errors_also_honor_long_retry_after(self):
        clock = FakeClock()
        with sync.RetryingSession(self.settings, clock=clock.time, sleep=clock.sleep) as session:
            with patch.object(requests.Session, "request", side_effect=[
                response(503, {"Retry-After": "300"}), response(),
            ]):
                session.get("https://example.invalid")
        self.assertGreaterEqual(max(clock.sleeps), 301)

    def test_all_history_ignores_batch_cap_and_preserves_configuration(self):
        raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        raw["history"] = {"enabled": True, "start_date": "2017-01-01", "days_per_run": 1}
        self.settings_path.write_text(json.dumps(raw), encoding="utf-8")
        original = self.settings_path.read_bytes()
        api = FakeApi()
        api.close = Mock()
        clock = datetime(2017, 1, 4, 17, 0, tzinfo=UTC)
        with patch("garmin_sync.Api", return_value=api), \
                patch("garmin_sync.datetime", wraps=datetime) as dates, \
                patch.dict(os.environ, {"GARMIN_PERSONAL_API_KEY": "test"}), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            dates.now.side_effect = lambda zone: clock.astimezone(zone)
            result = sync.main(["--settings", str(self.settings_path), "--all-history"])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["calendar_dates_requested"], ["2017-01-01", "2017-01-02", "2017-01-03"])
        self.assertFalse(report["health_history_pending"])
        self.assertEqual(self.settings_path.read_bytes(), original)
        self.assertFalse((self.root / "garmin-data" / "daily" / "2017-01-04").exists())
        progress = json.loads((
            self.root / "garmin-data" / "reports" / "history-before-2017-01-04-progress.json"
        ).read_bytes())
        self.assertEqual(progress["status"], "complete_for_accessible_data")
        self.assertIsInstance(progress["pid"], int)

    def test_resuming_all_history_keeps_400_errors_visible_without_reissuing_them(self):
        api = FakeApi()
        api.failures["/bad"] = http_error(400)
        first = self.engine(api)
        first.save("bad", "bad.json", "/bad", {})
        calls = len(api.calls)
        resumed = sync.Synchronizer(
            self.settings, self.store, api, NOW, reuse_bad_requests=True,
        )
        resumed.save("bad", "bad.json", "/bad", {})
        self.assertEqual(len(api.calls), calls)
        self.assertEqual(resumed.errors, 1)
        self.assertEqual(self.store.describe("bad")["status"], "error")
        self.assertFalse(self.store.ready("bad"))
        del api.failures["/bad"]
        resumed.save("bad", "bad.json", "/bad", {"changed": "true"})
        self.assertEqual(len(api.calls), calls + 1)
        self.assertTrue(self.store.ready("bad"))

    def test_force_retries_persisted_bad_requests(self):
        api = FakeApi()
        api.failures["/bad"] = http_error(400)
        self.engine(api).save("bad", "bad.json", "/bad", {})
        del api.failures["/bad"]
        forced = sync.Synchronizer(
            self.settings, self.store, api, NOW, force=True, reuse_bad_requests=True,
        )
        forced.save("bad", "bad.json", "/bad", {})
        self.assertTrue(self.store.ready("bad"))

    def test_running_progress_is_saved_before_each_historical_day(self):
        settings = replace(self.settings, history_start=date(2017, 1, 1))
        api = FakeApi()
        original = api.fetch
        observed = []
        def fetch(path, params, binary=False):
            if path == "/wellness-service/wellness/dailyHeartRate":
                progress = json.loads((
                    settings.download_dir / "reports" / "history-before-2017-01-03-progress.json"
                ).read_bytes())
                observed.append((params["date"], progress["current_date"], progress["phase"]))
            return original(path, params, binary)
        api.fetch = fetch
        self.engine(api, settings).run_history(date(2017, 1, 3))
        self.assertEqual(observed, [
            ("2017-01-01", "2017-01-01", "daily"),
            ("2017-01-02", "2017-01-02", "daily"),
        ])

    def test_permanent_request_errors_do_not_stall_next_history_batch(self):
        settings = replace(self.settings, history_start=date(2017, 1, 1), history_days_per_run=1)
        api = FakeApi()
        for day in ("2017-01-01", "2017-01-02", "2017-01-03"):
            api.failures[f"/weight-service/weight/all/{day}/{day}"] = http_error(400)
        first = self.engine(api, settings).run_history(date(2017, 1, 4))
        self.assertEqual(first["next_calendar_date"], "2017-01-02")
        second = self.engine(api, settings).run_history(date(2017, 1, 4))
        self.assertIn("2017-01-02", second["calendar_dates_requested"])
        self.assertEqual(second["next_calendar_date"], "2017-01-03")
        final = self.engine(api, settings).run_history(date(2017, 1, 4))
        self.assertFalse(final["health_history_pending"])
        self.assertEqual(final["errors"], 3)
        self.assertEqual(final["status"], "errors")
        original = "/weight-service/weight/all/2017-01-01/2017-01-01"
        self.assertEqual(sum(path == original for path, _, _ in api.calls), 1)


if __name__ == "__main__":
    unittest.main()
