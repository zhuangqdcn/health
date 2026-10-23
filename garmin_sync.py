from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import dotenv_values
from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator
from garmin_personal_api.client import JsonValue
from garmin_personal_api.errors import GarminPersonalApiError, HttpError

LOG = logging.getLogger("garmin-sync")
ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS = ROOT / ".garmin.sync.settings.json"
SEARCH_PATH = "/activitylist-service/activities/search/activities"


class SyncError(Exception):
    pass


class RateLimitError(SyncError):
    def __init__(self, retry_at: datetime):
        self.retry_at = retry_at
        super().__init__(
            f"Garmin HTTP 429 retry budget exhausted; cooldown saved until {retry_at.isoformat()}"
        )


def parse_clock(value: object, label: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise SyncError(f"{label} must be HH:MM")
    try:
        result = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise SyncError(f"{label} must be HH:MM") from exc
    if result.strftime("%H:%M") != value:
        raise SyncError(f"{label} must be HH:MM")
    return result.hour, result.minute


def parse_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise SyncError(f"{label} must be YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise SyncError(f"{label} must be YYYY-MM-DD") from exc
    if result.isoformat() != value:
        raise SyncError(f"{label} must be YYYY-MM-DD")
    return result


@dataclass(frozen=True)
class Settings:
    download_dir: Path
    zone: ZoneInfo
    window_start: tuple[int, int]
    schedule_time: tuple[int, int]
    history_enabled: bool
    history_start: date | None
    history_days_per_run: int
    include_original_files: bool
    refresh_recent_days: int
    request_delay_seconds: float
    timeout_seconds: float
    retries: int

    @classmethod
    def load(cls, path: Path) -> Settings:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        expected = {
            "download_dir", "timezone", "window_start", "schedule_time", "history",
            "include_original_files", "refresh_recent_days", "request_delay_seconds",
            "timeout_seconds", "retries",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise SyncError("Settings must contain exactly the keys in the example settings")
        history = raw["history"]
        if not isinstance(history, dict) or set(history) != {"enabled", "start_date", "days_per_run"}:
            raise SyncError("history must contain enabled, start_date and days_per_run")
        for label, value in (
            ("history.enabled", history["enabled"]),
            ("include_original_files", raw["include_original_files"]),
        ):
            if not isinstance(value, bool):
                raise SyncError(f"{label} must be a boolean")
        for label, value, minimum in (
            ("history.days_per_run", history["days_per_run"], 1),
            ("refresh_recent_days", raw["refresh_recent_days"], 1),
            ("retries", raw["retries"], 0),
        ):
            if type(value) is not int or value < minimum:
                raise SyncError(f"{label} must be an integer >= {minimum}")
        for label, minimum in (("request_delay_seconds", 0), ("timeout_seconds", 0.001)):
            value = raw[label]
            if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
                raise SyncError(f"{label} must be a finite number >= {minimum}")
        if not isinstance(raw["download_dir"], str) or not raw["download_dir"].strip():
            raise SyncError("download_dir must be a nonempty path")
        if not isinstance(raw["timezone"], str):
            raise SyncError("timezone must be an IANA timezone name")
        try:
            zone = ZoneInfo(raw["timezone"])
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise SyncError("Unknown timezone; install tzdata and use an IANA timezone") from exc
        start = parse_clock(raw["window_start"], "window_start")
        schedule = parse_clock(raw["schedule_time"], "schedule_time")
        if schedule < start:
            raise SyncError("schedule_time must not be earlier than window_start")
        history_start = (
            parse_date(history["start_date"], "history.start_date")
            if history["start_date"] is not None else None
        )
        if history_start and history_start > datetime.now(zone).date():
            raise SyncError("history.start_date must not be in the future")
        output = Path(raw["download_dir"]).expanduser()
        if not output.is_absolute():
            output = path.resolve().parent / output
        return cls(
            output.resolve(), zone, start, schedule, history["enabled"], history_start,
            history["days_per_run"], raw["include_original_files"], raw["refresh_recent_days"],
            raw["request_delay_seconds"], raw["timeout_seconds"], raw["retries"],
        )


def window(settings: Settings, end_date: date) -> tuple[datetime, datetime]:
    hour, minute = settings.window_start
    end = datetime(end_date.year, end_date.month, end_date.day, hour, minute, tzinfo=settings.zone)
    previous = end_date - timedelta(days=1)
    start = datetime(previous.year, previous.month, previous.day, hour, minute, tzinfo=settings.zone)
    return start, end


def latest_window_date(settings: Settings, now: datetime) -> date:
    local = now.astimezone(settings.zone)
    _, end = window(settings, local.date())
    return local.date() if local >= end else local.date() - timedelta(days=1)


def scheduled_date(settings: Settings, now: datetime) -> date:
    local = now.astimezone(settings.zone)
    hour, minute = settings.schedule_time
    return local.date() if (local.hour, local.minute) >= (hour, minute) else local.date() - timedelta(days=1)


def read_api_key(path: Path) -> str:
    if "GARMIN_PERSONAL_API_KEY" in os.environ:
        value = os.environ["GARMIN_PERSONAL_API_KEY"]
    else:
        if not path.is_file():
            raise SyncError(f"Missing credential file: {path}; copy .env.example")
        value = dotenv_values(path).get("GARMIN_PERSONAL_API_KEY")
    if not value or not value.strip():
        raise SyncError("Set GARMIN_PERSONAL_API_KEY in .env or the environment")
    return value.strip()


@dataclass(frozen=True)
class Endpoint:
    name: str
    path: str
    query: tuple[tuple[str, str], ...] = ()

    def request(self, day: date, today: date) -> tuple[str, dict[str, str]]:
        values = {
            "date": day.isoformat(), "calendarDate": day.isoformat(),
            "startDate": day.isoformat(), "endDate": day.isoformat(),
            "fromDate": day.isoformat(), "untilDate": day.isoformat(), "toDate": day.isoformat(),
            "today": today.isoformat(),
        }
        return self.path.format(**values), {k: v.format(**values) for k, v in self.query}


# Every dated GET in the Personal Swagger; one-day ranges respect all range limits.
DAILY = (
    Endpoint("fitness-age", "/fitnessage-service/fitnessage/v2/{calendarDate}"),
    Endpoint("weight-goals", "/goal-service/goal/user/effective/weightgoal/{startDate}/{endDate}"),
    Endpoint("hrv", "/hrv-service/hrv/{calendarDate}"),
    Endpoint("hrv-summary", "/hrv-service/hrv/daily/{fromDate}/{toDate}"),
    Endpoint("nutrition", "/nutrition-service/food/logs/{date}"),
    Endpoint("menstrual-summary", "/periodichealth-service/menstrualcycle/summary/{calendarDate}"),
    Endpoint("menstrual-day", "/periodichealth-service/menstrualcycle/dayview/{calendarDate}",
             (("todayDate", "{today}"), ("includeAnalysis", "true"))),
    Endpoint("sleep", "/sleep-service/sleep/dailySleep", (("date", "{date}"),)),
    Endpoint("sleep-summary", "/sleep-service/sleep/dailySleepSummariesByDate",
             (("startDate", "{date}"), ("endDate", "{date}"))),
    Endpoint("hydration", "/usersummary-service/stats/hydration/daily/{startDate}/{endDate}"),
    Endpoint("steps", "/usersummary-service/stats/steps/daily/{startDate}/{endDate}"),
    Endpoint("stress", "/usersummary-service/stats/stress/daily/{startDate}/{endDate}"),
    Endpoint("daily-summary", "/usersummary-service/usersummary/daily", (("calendarDate", "{date}"),)),
    Endpoint("daily-summaries", "/usersummary-service/v2/dailysummaries",
             (("startDate", "{date}"), ("endDate", "{date}"))),
    Endpoint("weight-entries", "/weight-service/weight/all/{startDate}/{endDate}"),
    Endpoint("weight-range", "/weight-service/weight/range/{startDate}/{endDate}",
             (("includeAll", "true"),)),
    Endpoint("body-battery", "/wellness-service/wellness/bodyBattery/reports/daily",
             (("startDate", "{date}"), ("endDate", "{date}"))),
    Endpoint("spo2", "/wellness-service/wellness/daily/spo2acclimation/{calendarDate}"),
    Endpoint("heart-rate", "/wellness-service/wellness/dailyHeartRate", (("date", "{date}"),)),
    Endpoint("wellness-activities",
             "/wellnessactivity-service/activity/summary/range/{fromDate}/{untilDate}"),
)
SNAPSHOTS = (
    Endpoint("profile-settings", "/userprofile-service/userprofile/settings"),
    Endpoint("heart-rate-zones", "/biometric-service/heartRateZones/v2",
             (("generateDefaults", "false"),)),
)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_run_report(root: Path, report: dict[str, object], before: date | None = None) -> None:
    atomic_write(root / "last-run.json", json_bytes(report))
    if before is not None:
        atomic_write(root / "reports" / f"history-before-{before}.json", json_bytes(report))
        progress = {key: value for key, value in report.items() if key != "daily_files"}
        progress["pid"] = os.getpid()
        atomic_write(root / "reports" / f"history-before-{before}-progress.json", json_bytes(progress))


@contextmanager
def archive_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "sync.lock"
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError("Another sync is using this download directory") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.db = sqlite3.connect(root / ".state" / "sync.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                key TEXT PRIMARY KEY, path TEXT, sha256 TEXT, status TEXT NOT NULL,
                http_status INTEGER, updated TEXT NOT NULL, request TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self) -> None:
        self.db.close()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))

    def row(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM objects WHERE key=?", (key,)).fetchone()

    def ready(self, key: str, refresh_before: datetime | None = None) -> bool:
        row = self.row(key)
        if row is None or row["status"] not in ("ok", "not_found", "forbidden"):
            return False
        if refresh_before and datetime.fromisoformat(row["updated"]) < refresh_before:
            return False
        if row["status"] in ("not_found", "forbidden"):
            return True
        path = self.root / row["path"]
        if not path.is_file():
            LOG.warning("Missing archived file; downloading again: %s", row["path"])
            return False
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != row["sha256"]:
            LOG.warning("Archived file checksum mismatch; downloading again: %s", row["path"])
            return False
        return True

    def record(self, key: str, status: str, request: str, now: datetime,
               path: str | None = None, content: bytes | None = None,
               http_status: int | None = None) -> None:
        digest = None
        if path is not None and content is not None:
            atomic_write(self.root / path, content)
            digest = hashlib.sha256(content).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?,?,?)",
                (key, path, digest, status, http_status, now.isoformat(), request),
            )

    def describe(self, key: str) -> dict[str, object]:
        row = self.row(key)
        if row is None:
            return {"key": key, "status": "not_downloaded"}
        return {k: row[k] for k in ("key", "path", "sha256", "status", "http_status", "updated")}

    def permit(self, endpoint: str) -> None:
        name = f"permission:{endpoint}"
        if self.get_meta(name) == "forbidden":
            with self.db:
                self.db.execute(
                    "DELETE FROM objects WHERE status='forbidden' AND key LIKE ?",
                    (f"day:%:{endpoint}",),
                )
            LOG.info("Access restored for %s; previously forbidden dates will be retried", endpoint)
        self.set_meta(name, "available")


class RetryingSession(requests.Session):
    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        super().__init__()
        self.settings = settings
        self.sleep = sleep
        self.clock = clock
        self.state_file = settings.download_dir / ".state" / "rate-limit.json"
        self.interval = settings.request_delay_seconds
        self.not_before = 0.0
        self.last_request_at: float | None = None
        if self.state_file.exists():
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise SyncError("Invalid saved rate-limit state")
            for key in ("not_before", "interval"):
                value = state.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise SyncError("Invalid saved rate-limit state")
            try:
                datetime.fromtimestamp(state["not_before"], UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise SyncError("Invalid saved rate-limit deadline") from exc
            self.not_before = state["not_before"]
            self.interval = max(self.interval, state["interval"])

    def _wait(self) -> None:
        now = self.clock()
        paced_at = (
            self.last_request_at + self.interval
            if self.last_request_at is not None else now + self.settings.request_delay_seconds
        )
        delay = max(self.not_before, paced_at) - now
        if delay > 0:
            self.sleep(delay)
        self.last_request_at = self.clock()

    def _retry_after(self, response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(value)
                if deadline.tzinfo is None:
                    raise ValueError("Retry-After date lacks timezone")
                reference = datetime.fromtimestamp(self.clock(), UTC)
                server_date = response.headers.get("Date")
                if server_date:
                    try:
                        parsed = parsedate_to_datetime(server_date)
                        if parsed.tzinfo is not None:
                            reference = parsed
                    except (ValueError, TypeError, OverflowError):
                        LOG.warning("Invalid server Date header; using the local clock")
                delay = max(0.0, (deadline - reference).total_seconds())
            except (ValueError, TypeError, OverflowError):
                LOG.warning("Invalid Retry-After header; using exponential backoff")
                return None
        if not math.isfinite(delay) or delay < 0:
            LOG.warning("Invalid Retry-After delay; using exponential backoff")
            return None
        try:
            datetime.fromtimestamp(self.clock() + delay + 1, UTC)
        except (OverflowError, OSError, ValueError):
            LOG.warning("Retry-After deadline is out of range; using exponential backoff")
            return None
        return delay

    def _rate_limited(self, response: requests.Response, attempt: int) -> None:
        server_delay = self._retry_after(response)
        self.interval = max(
            self.settings.request_delay_seconds, min(60.0, max(1.0, self.interval * 2)),
        )
        delay = server_delay if server_delay is not None else 30.0 * (2 ** min(attempt, 5))
        # A small margin avoids retrying inside a rounded server quota window.
        delay = max(delay, self.interval, 1.0) + 1.0
        self.not_before = max(self.not_before, self.clock() + delay)
        atomic_write(self.state_file, json_bytes({
            "not_before": self.not_before, "interval": self.interval,
        }))
        response.close()
        LOG.warning(
            "HTTP 429: cooling down for at least %.1fs; request interval now %.2fs (%s/%s)",
            delay, self.interval, attempt + 1, self.settings.retries + 1,
        )
        if attempt == self.settings.retries:
            raise RateLimitError(datetime.fromtimestamp(self.not_before, UTC))

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(self.settings.retries + 1):
            self._wait()
            try:
                response = super().request(method, url, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.settings.retries:
                    raise SyncError(f"Network request failed after {attempt + 1} attempts") from exc
                LOG.warning("Temporary network failure; retry %s", attempt + 1)
                self.sleep(min(2 ** attempt, 60))
                continue
            if response.status_code == 429:
                self._rate_limited(response, attempt)
                continue
            if response.status_code not in (500, 502, 503, 504) or attempt == self.settings.retries:
                return response
            delay = self._retry_after(response)
            delay = max(delay + 1, 1.0) if delay is not None else float(2 ** attempt)
            LOG.warning("HTTP %s; retry %s", response.status_code, attempt + 1)
            response.close()
            self.sleep(delay)
        raise AssertionError("unreachable")


class Api:
    def __init__(self, settings: Settings, key: str):
        override = os.environ.get("GARMIN_PERSONAL_API_BASE_URL")
        if override and override.rstrip("/") != "https://personal.connectapi.garmin.com":
            raise SyncError("Custom Garmin API hosts are disabled to protect credentials")
        self.key = key
        session = RetryingSession(settings)
        self.auth = PersonalApiAuthenticator(
            session=session, timeout=settings.timeout_seconds,
            bearer_token_cache_file=settings.download_dir / ".state" / "token.json",
        )
        self.client = GarminPersonalApiClient(
            "not-yet-authenticated", session=session, timeout=settings.timeout_seconds,
        )
        self.expires_at = 0.0

    def close(self) -> None:
        self.client.close()
        self.auth.close()

    def fetch(self, path: str, params: dict[str, str | int], binary: bool = False) -> JsonValue | bytes:
        for attempt in range(2):
            if self.expires_at < time.time() + 60 or attempt:
                token = self.auth.obtain_token(self.key, force_refresh=bool(attempt))
                self.client.update_bearer_token(token.access_token)
                self.expires_at = token.expires_at
            try:
                if binary:
                    return self.client.download_activity(path.rsplit("/", 1)[-1])
                return self.client.get(path, params=params)
            except HttpError as exc:
                if exc.status_code != 401 or attempt:
                    raise
                LOG.warning("Access token rejected; obtaining a new token once")
        raise AssertionError("unreachable")


def activity_id(item: JsonValue) -> str:
    value = item.get("activityId") if isinstance(item, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SyncError("Activity search returned a missing or invalid activityId")
    result = str(value)
    if not result.isascii() or not result.isdecimal() or int(result) <= 0:
        raise SyncError("Activity search returned a missing or invalid activityId")
    return result


def activity_start(item: dict) -> datetime | None:
    value = item.get("startTimeGMT")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError("Activity search returned an invalid startTimeGMT") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def activity_duration(item: dict) -> float:
    value = item.get("elapsedDuration", item.get("duration", 0))
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise SyncError("Invalid activity duration in activity search")
    return float(value)


class Synchronizer:
    def __init__(self, settings: Settings, store: Store, api: Api,
                 now: datetime, force: bool = False, reuse_bad_requests: bool = True):
        self.settings, self.store, self.api = settings, store, api
        self.now, self.force = now, force
        self.reuse_bad_requests = reuse_bad_requests
        self.today = now.astimezone(settings.zone).date()
        self.midnight = datetime.combine(self.today, datetime.min.time(), settings.zone)
        self.forbidden: set[str] = set()
        self.errors = 0
        self.downloaded = 0
        self.unavailable = 0
        self.day_keys: dict[date, list[str]] = {}
        self.changed_windows: set[date] = set()
        self.calendar_zone = settings.zone
        self.calendar_zone_source = "settings_fallback"

    def calendar_days(self, end_day: date) -> list[date]:
        start, end = window(self.settings, end_day)
        first = start.astimezone(self.calendar_zone).date()
        final = (end.astimezone(UTC) - timedelta(microseconds=1)).astimezone(self.calendar_zone).date()
        return [first + timedelta(days=offset) for offset in range((final - first).days + 1)]

    def save(self, key: str, relative: str, path: str, params: dict[str, str | int],
             *, refresh: bool = False, binary: bool = False,
             refetch: bool = False) -> JsonValue | bytes:
        threshold = self.midnight if refresh else None
        if not self.force and not refetch and self.store.ready(key, threshold):
            row = self.store.row(key)
            if row["status"] in ("not_found", "forbidden"):
                return None
            content = (self.settings.download_dir / row["path"]).read_bytes()
            return content if binary else json.loads(content)
        request = json.dumps({"path": path, "params": params}, sort_keys=True)
        previous = self.store.row(key)
        if (self.reuse_bad_requests and not self.force and previous
                and previous["status"] == "error" and previous["http_status"] == 400
                and previous["request"] == request):
            self.errors += 1
            LOG.warning("Retaining previously rejected HTTP 400 for %s; use --force to retry", path)
            return None
        try:
            value = self.api.fetch(path, params, binary)
            if binary:
                if not isinstance(value, bytes) or not value:
                    raise SyncError("Empty activity file")
                try:
                    with zipfile.ZipFile(io.BytesIO(value)) as archive:
                        if not archive.namelist() or archive.testzip() is not None:
                            raise SyncError("Activity ZIP file is empty or corrupt")
                except zipfile.BadZipFile as exc:
                    raise SyncError("Activity download is not a valid ZIP file") from exc
                content = value
            else:
                content = json_bytes(value)
            self.store.record(key, "ok", request, self.now, relative, content)
            self.downloaded += 1
            return value
        except RateLimitError:
            self.store.record(key, "rate_limited", request, self.now, http_status=429)
            raise
        except HttpError as exc:
            if exc.status_code in (401, 429):
                raise SyncError(f"HTTP {exc.status_code}; check API key or retry after rate limiting") from exc
            status = {403: "forbidden", 404: "not_found"}.get(exc.status_code, "error")
            self.store.record(key, status, request, self.now, http_status=exc.status_code)
            if status == "error":
                self.errors += 1
                LOG.error("HTTP %s for %s", exc.status_code, path)
            else:
                self.unavailable += 1
                LOG.warning("Unavailable (HTTP %s): %s", exc.status_code, path)
            return None
        except (SyncError, requests.RequestException, ValueError) as exc:
            self.store.record(key, "error", request, self.now)
            self.errors += 1
            # Never log response bodies, request headers, tokens or raw network exceptions.
            LOG.error("Download/validation failed for %s (%s)", path, type(exc).__name__)
            return None

    def day_ready(self, day: date, check_all_endpoints: bool = False) -> bool:
        return not self.force and all(
            (not check_all_endpoints and endpoint.name in self.forbidden)
            or self.store.ready(f"day:{day}:{endpoint.name}")
            for endpoint in DAILY
        )

    def day_needs_request(self, day: date) -> bool:
        if self.force:
            return True
        for endpoint in DAILY:
            key = f"day:{day}:{endpoint.name}"
            if self.store.ready(key):
                continue
            previous = self.store.row(key)
            path, params = endpoint.request(day, self.today)
            request = json.dumps({"path": path, "params": params}, sort_keys=True)
            if (self.reuse_bad_requests and previous and previous["status"] == "error"
                    and previous["http_status"] == 400 and previous["request"] == request):
                continue
            return True
        return False

    def sync_day(self, day: date, refresh: bool = False, check_all_endpoints: bool = False) -> None:
        if day in self.day_keys:
            return
        keys = []
        for endpoint in DAILY:
            key = f"day:{day}:{endpoint.name}"
            keys.append(key)
            if endpoint.name in self.forbidden and not check_all_endpoints:
                self.store.record(key, "forbidden", endpoint.path, self.now, http_status=403)
                continue
            path, params = endpoint.request(day, self.today)
            self.save(key, f"daily/{day}/{endpoint.name}.json", path, params, refresh=refresh)
            row = self.store.row(key)
            if row and row["status"] == "forbidden":
                self.forbidden.add(endpoint.name)
                self.store.set_meta(f"permission:{endpoint.name}", "forbidden")
            elif row and row["status"] == "ok":
                self.store.permit(endpoint.name)
        self.day_keys[day] = keys

    def activities(self, latest: date, before: date | None = None,
                   start: date | None = None,
                   progress: Callable[[int, int], None] | None = None) -> list[dict]:
        result = []
        seen: set[str] = set()
        offset = 0
        scope = f"activity-index-before:{before}" if before else "activity-index"
        folder = f"activity-index/before-{before}" if before else "activity-index"
        if start is not None:
            scope = f"activity-index-range:{start}:{before}"
            folder = f"activity-index/range-{start}-{before}"
        cutoff = datetime.combine(before, datetime.min.time(), self.settings.zone) if before else None
        lower = datetime.combine(start, datetime.min.time(), self.settings.zone) if start else None
        while True:
            params: dict[str, str | int] = {
                "start": offset, "limit": 99, "excludeChildren": "false",
                "sortOrder": "desc", "sortBy": "startLocal",
            }
            # The API filters by each activity's local calendar; additionally
            # enforce the configured timezone's exclusive cutoff below.
            if before:
                params["endDate"] = (before - timedelta(days=1)).isoformat()
            if start:
                params["startDate"] = start.isoformat()
            key = f"{scope}:{offset}"
            page = self.save(
                key, f"{folder}/page-{offset:08d}.json", SEARCH_PATH, params, refetch=True,
            )
            row = self.store.row(key)
            if row is None or row["status"] != "ok":
                raise SyncError("Activity catalog could not be downloaded; history is incomplete")
            if not isinstance(page, list):
                raise SyncError("Activity search must return a JSON array")
            if not page:
                break
            ids = [activity_id(item) for item in page]
            if all(identifier in seen for identifier in ids):
                raise SyncError("Activity pagination made no progress; refusing to truncate history")
            for item, identifier in zip(page, ids):
                if identifier in seen:
                    continue
                seen.add(identifier)
                if not isinstance(item, dict):
                    raise SyncError("Activity search must return objects")
                if cutoff:
                    started = activity_start(item)
                    if started is None:
                        raise SyncError("Cannot enforce --before on an activity without startTimeGMT")
                    if started >= cutoff.astimezone(UTC):
                        continue
                    if lower and started < lower.astimezone(UTC):
                        continue
                result.append(item)
            offset += len(page)
        self.store.set_meta(f"{scope}:count", str(len(result)))
        latest_start, latest_end = window(self.settings, latest)
        recent_cutoff = latest_start - timedelta(days=self.settings.refresh_recent_days)
        for number, item in enumerate(result, 1):
            identifier = activity_id(item)
            started = activity_start(item)
            duration = activity_duration(item)
            selected = before is not None or self.settings.history_enabled or (
                started is None or (
                    started < latest_end.astimezone(UTC)
                    and started + timedelta(seconds=duration) >= recent_cutoff.astimezone(UTC)
                )
            )
            if not selected:
                continue
            recent = started is not None and started >= recent_cutoff
            fingerprint = hashlib.sha256(json_bytes(item)).hexdigest()
            old = self.store.get_meta(f"activity-version:{identifier}")
            old_version = json.loads(old) if old else None
            changed = old_version is None or old_version["fingerprint"] != fingerprint
            before = self.downloaded
            for suffix, endpoint_suffix in (("summary", ""), ("workouts", "/workouts")):
                self.save(
                    f"activity:{identifier}:{suffix}", f"activities/{identifier}/{suffix}.json",
                    f"/activity-service/activity/{identifier}{endpoint_suffix}", {}, refresh=recent,
                    refetch=changed,
                )
            if self.settings.include_original_files:
                self.save(
                    f"activity:{identifier}:original", f"activities/{identifier}/original.zip",
                    f"/download-service/files/activity/{identifier}", {}, binary=True, refresh=recent,
                    refetch=changed,
                )
            version = {"fingerprint": fingerprint, "start": started.isoformat() if started else None}
            if changed or before != self.downloaded:
                for stamp in (version["start"], old_version["start"] if old_version else None):
                    if stamp:
                        local_start = datetime.fromisoformat(stamp).astimezone(self.settings.zone)
                        boundary = latest_window_date(self.settings, local_start) + timedelta(days=1)
                        self.changed_windows.add(boundary)
                        if duration > 0:
                            final = latest_window_date(
                                self.settings, (
                                    local_start.astimezone(UTC) + timedelta(seconds=duration)
                                ).astimezone(self.settings.zone),
                            ) + timedelta(days=1)
                            while boundary < final:
                                boundary += timedelta(days=1)
                                self.changed_windows.add(boundary)
            self.store.set_meta(f"activity-version:{identifier}", json.dumps(version))
            if progress is not None:
                progress(number, len(result))
        return result

    def manifest(self, end_day: date, activities: list[dict]) -> bool:
        start, end = window(self.settings, end_day)
        references = []
        incomplete = []
        for day in self.calendar_days(end_day):
            for endpoint in DAILY:
                description = self.store.describe(f"day:{day}:{endpoint.name}")
                references.append(description)
                if description["status"] not in ("ok", "forbidden", "not_found"):
                    incomplete.append(description["key"])
        selected = []
        unassigned = []
        for item in activities:
            identifier = activity_id(item)
            started = activity_start(item)
            if started is None:
                unassigned.append(identifier)
                continue
            duration = activity_duration(item)
            finishes = started + timedelta(seconds=duration)
            if started < end.astimezone(UTC) and (
                started >= start.astimezone(UTC) or finishes > start.astimezone(UTC)
            ):
                keys = [f"activity:{identifier}:{kind}" for kind in ("summary", "workouts")]
                if self.settings.include_original_files:
                    keys.append(f"activity:{identifier}:original")
                files = [self.store.describe(key) for key in keys]
                incomplete.extend(
                    entry["key"] for entry in files
                    if entry["status"] not in ("ok", "forbidden", "not_found")
                )
                selected.append({
                    "activity_id": identifier, "start_utc": started.isoformat(),
                    "overlaps_from_previous_window": started < start.astimezone(UTC), "files": files,
                })
        complete = not incomplete and not unassigned
        self.store.record(f"window:{end_day}", "ok", "generated window manifest", self.now,
                          f"windows/{end_day}.json", json_bytes({
            "timezone": self.settings.zone.key, "start_inclusive": start.isoformat(),
            "calendar_timezone": self.calendar_zone.key,
            "calendar_timezone_source": self.calendar_zone_source,
            "calendar_dates": [day.isoformat() for day in self.calendar_days(end_day)],
            "end_exclusive": end.isoformat(), "start_utc": start.astimezone(UTC).isoformat(),
            "end_utc": end.astimezone(UTC).isoformat(),
            "duration_hours": (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600,
            "generated_at": self.now.isoformat(),
            "calendar_data_note": (
                "Full Garmin calendar-day responses; not sliced or recomputed into 09:00 windows. "
                "Calendar dates use the Garmin account timezone. Raw activity ZIPs remain intact."
            ),
            "daily_files": references, "activities": selected,
            "activities_without_utc_start": unassigned, "incomplete": incomplete,
            "status": "complete_for_accessible_data" if complete else "incomplete",
        }))
        return complete

    def prepare(self, latest: date, profile_only: bool = False) -> None:
        signature = json.dumps({
            "timezone": self.settings.zone.key, "window_start": self.settings.window_start,
        }, sort_keys=True)
        previous = self.store.get_meta("window_settings")
        if previous and previous != signature:
            raise SyncError("Timezone/window changed; use a separate download directory")
        self.store.set_meta("window_settings", signature)
        for endpoint in SNAPSHOTS[:1] if profile_only else SNAPSHOTS:
            path, params = endpoint.request(latest, self.today)
            value = self.save(
                f"snapshot:{self.today}:{endpoint.name}",
                f"snapshots/{self.today}/{endpoint.name}.json", path, params,
            )
            if endpoint.name == "profile-settings" and isinstance(value, dict):
                profile_zone = value.get("timeZone")
                if profile_zone is not None:
                    if not isinstance(profile_zone, str):
                        raise SyncError("Garmin profile returned an invalid timezone")
                    try:
                        self.calendar_zone = ZoneInfo("UTC" if profile_zone.lower() == "gmt" else profile_zone)
                    except (ValueError, ZoneInfoNotFoundError) as exc:
                        raise SyncError("Unrecognized Garmin account timezone; cannot safely select calendar dates") from exc
                    self.calendar_zone_source = "garmin_profile"
                if profile_zone and self.calendar_zone != self.settings.zone:
                    LOG.warning(
                        "Garmin account timezone is %s; fetching all account dates overlapping the Seattle window",
                        profile_zone,
                    )
        if self.calendar_zone_source == "settings_fallback":
            LOG.warning("Garmin profile timezone is unavailable; assuming configured timezone for calendar queries")
        old_calendar_zone = self.store.get_meta("calendar_timezone")
        if old_calendar_zone and old_calendar_zone != self.calendar_zone.key:
            raise SyncError("Garmin account calendar timezone changed; use a separate download directory")
        self.store.set_meta("calendar_timezone", self.calendar_zone.key)

    def run_history(self, before: date, activity_start_date: date | None = None) -> dict[str, object]:
        start = self.settings.history_start
        if not self.settings.history_enabled or start is None:
            raise SyncError("--before requires history.enabled=true and history.start_date")
        if before <= start or before > self.today:
            raise SyncError("--before must be after history.start_date and not in the future")
        def progress(cursor: date, phase: str, activities_done: int = 0, activities_total: int = 0) -> None:
            atomic_write(
                self.settings.download_dir / "reports" / f"history-before-{before}-progress.json",
                json_bytes({
                    "status": "running", "pid": os.getpid(), "phase": phase,
                    "start_date_inclusive": start.isoformat(), "end_date_exclusive": before.isoformat(),
                    "current_date": cursor.isoformat(), "dates_scanned": (cursor - start).days,
                    "total_dates": (before - start).days, "downloaded": self.downloaded,
                    "errors": self.errors, "activities_processed": activities_done,
                    "activities_total": activities_total, "updated_at": datetime.now(UTC).isoformat(),
                }),
            )
        progress(start, "settings")
        self.prepare(before - timedelta(days=1))
        cursor = start
        processed = 0
        while cursor < before:
            progress(cursor, "daily")
            if not self.day_ready(cursor, check_all_endpoints=True):
                needs_request = self.day_needs_request(cursor)
                if needs_request and processed >= self.settings.history_days_per_run:
                    break
                LOG.info("Downloading historical calendar date %s", cursor)
                self.sync_day(cursor, check_all_endpoints=True)
                if needs_request:
                    processed += 1
            cursor += timedelta(days=1)
        progress(cursor, "activity_catalog")
        activities = self.activities(
            before - timedelta(days=1), before=before, start=activity_start_date,
            progress=lambda done, total: progress(cursor, "activity_files", done, total),
        )
        pending = cursor < before
        report = {
            "run_at": self.now.isoformat(), "mode": "history_before",
            "start_date_inclusive": start.isoformat(), "end_date_exclusive": before.isoformat(),
            "activity_start_date_inclusive": (
                activity_start_date.isoformat() if activity_start_date else None
            ),
            "calendar_timezone": self.calendar_zone.key,
            "activity_cutoff_timezone": self.settings.zone.key,
            "calendar_dates_requested": [day.isoformat() for day in sorted(self.day_keys)],
            "next_calendar_date": cursor.isoformat() if pending else None,
            "downloaded": self.downloaded, "errors": self.errors,
            "unavailable_this_run": self.unavailable,
            "activity_catalog_count": len(activities), "incomplete_windows": 0,
            "health_history_needs_start_date": False, "health_history_pending": pending,
            "note": (
                "Historical calendar-day export, not a full daily-window sync. "
                "Current profile and heart-rate-zone settings are snapshots, not historical versions. "
                "Run normal sync later to generate 09:00-to-09:00 window manifests."
            ),
            "daily_files": [
                self.store.describe(key)
                for day in sorted(self.day_keys) for key in self.day_keys[day]
            ],
            "status": "errors" if self.errors else (
                "history_pending" if pending else "complete_for_accessible_data"
            ),
        }
        write_run_report(self.settings.download_dir, report, before)
        return report

    def run(self, target: date | None = None) -> dict[str, object]:
        latest = target or latest_window_date(self.settings, self.now)
        self.prepare(latest)
        ends = {
            latest - timedelta(days=offset) for offset in range(self.settings.refresh_recent_days)
        }
        for end_day in sorted(ends, reverse=True):
            for day in reversed(self.calendar_days(end_day)):
                self.sync_day(day, refresh=True)
        activities = self.activities(latest)
        ends.update(
            day for day in self.changed_windows
            if day <= latest and (self.settings.download_dir / "windows" / f"{day}.json").is_file()
        )
        history_pending = False
        anchor = self.store.get_meta("first_window_start")
        if anchor is None:
            anchor = (latest - timedelta(days=1)).isoformat()
            self.store.set_meta("first_window_start", anchor)
        history_start = date.fromisoformat(anchor)
        if self.settings.history_enabled and self.settings.history_start:
            history_start = min(history_start, self.settings.history_start)
        if history_start < latest:
            cursor = history_start + timedelta(days=1)
            processed = 0
            while cursor <= latest:
                days = self.calendar_days(cursor)
                ready = all(self.day_ready(day) for day in days)
                needs_request = not ready and any(self.day_needs_request(day) for day in days)
                if needs_request and processed >= self.settings.history_days_per_run:
                    history_pending = True
                    break
                if not ready:
                    for day in days:
                        self.sync_day(day)
                    if needs_request:
                        processed += 1
                if not ready or not self.store.ready(f"window:{cursor}"):
                    ends.add(cursor)
                cursor += timedelta(days=1)
        missing_boundary = self.settings.history_enabled and self.settings.history_start is None
        if missing_boundary:
            LOG.warning(
                "Activity history was enumerated, but health history requires history.start_date. "
                "Swagger exposes no earliest-health-date endpoint; no boundary was guessed."
            )
        incomplete_windows = sum(not self.manifest(day, activities) for day in sorted(ends))
        report = {
            "run_at": self.now.isoformat(), "latest_window": latest.isoformat(),
            "calendar_timezone": self.calendar_zone.key,
            "downloaded": self.downloaded, "errors": self.errors,
            "unavailable_this_run": self.unavailable,
            "activity_catalog_count": len(activities),
            "incomplete_windows": incomplete_windows,
            "activities_without_utc_start": sum(activity_start(item) is None for item in activities),
            "health_history_needs_start_date": missing_boundary,
            "health_history_pending": history_pending,
            "status": "errors" if self.errors or incomplete_windows else (
                "needs_history_start_date" if missing_boundary else (
                    "history_pending" if history_pending else "complete_for_accessible_data"
                )
            ),
        }
        atomic_write(self.settings.download_dir / "last-run.json", json_bytes(report))
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive Garmin Personal API data (Seattle 09:00 windows)")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--env", type=Path, help="Credential dotenv file (default: beside settings)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--scheduled", action="store_true", help="Run only when the local daily sync is due")
    modes.add_argument("--before", help="Export historical calendar dates and activities before YYYY-MM-DD")
    modes.add_argument("--all-history", action="store_true",
                       help="Export the configured history through yesterday in one resumable run")
    parser.add_argument("--start-date", help="Override health and activity start date for historical export")
    parser.add_argument("--days-per-run", type=int, help="Override historical batch size for --before only")
    parser.add_argument("--check-config", action="store_true", help="Validate settings without API calls")
    parser.add_argument("--force", action="store_true", help="Re-fetch selected data, including original files")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)
    try:
        settings = Settings.load(args.settings.resolve())
        before = parse_date(args.before, "--before") if args.before else None
        if args.all_history:
            before = datetime.now(settings.zone).date()
        start = parse_date(args.start_date, "--start-date") if args.start_date else None
        if start is not None:
            if before is None:
                raise SyncError("--start-date requires --before or --all-history")
            settings = replace(settings, history_start=start)
        if args.days_per_run is not None:
            if before is None or args.all_history or args.days_per_run < 1:
                raise SyncError("--days-per-run requires --before and must be >= 1")
            settings = replace(settings, history_days_per_run=args.days_per_run)
        if before is not None and (
            not settings.history_enabled or settings.history_start is None
            or before <= settings.history_start or before > datetime.now(settings.zone).date()
        ):
            raise SyncError("--before requires an enabled history range ending after its start and not in the future")
        if args.all_history:
            history_start = settings.history_start
            if before is None or history_start is None:
                raise SyncError("--all-history requires a historical start date")
            settings = replace(
                settings, history_days_per_run=(before - history_start).days,
                request_delay_seconds=max(1.0, settings.request_delay_seconds),
            )
        if args.check_config:
            print(json_bytes({
                "download_dir": str(settings.download_dir), "timezone": settings.zone.key,
                "history_start_date": (
                    settings.history_start.isoformat() if settings.history_start else None
                ),
                "health_history_needs_start_date": settings.history_enabled and settings.history_start is None,
                "settings_valid": True,
            }).decode(), end="")
            return 0
        now = datetime.now(UTC)
        state_dir = settings.download_dir / ".state"
        with archive_lock(state_dir):
            store = Store(settings.download_dir)
            api = None
            handler = None
            try:
                due = scheduled_date(settings, now).isoformat()
                config_signature = hashlib.sha256(args.settings.read_bytes()).hexdigest()
                completed = store.get_meta("scheduled_completed")
                if args.scheduled and completed == f"{due}:{config_signature}":
                    return 0
                last_attempt = store.get_meta("scheduled_attempt")
                if args.scheduled and last_attempt:
                    attempt = json.loads(last_attempt)
                    if (attempt["due"] == due and attempt["signature"] == config_signature
                            and (now - datetime.fromisoformat(attempt["at"])).total_seconds() < 1800):
                        return 0
                handler = RotatingFileHandler(
                    state_dir / "sync.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                LOG.addHandler(handler)
                key = read_api_key(args.env or args.settings.resolve().parent / ".env")
                api = Api(settings, key)
                if args.scheduled:
                    store.set_meta("scheduled_attempt", json.dumps({
                        "at": now.isoformat(), "due": due, "signature": config_signature,
                    }))
                target = date.fromisoformat(due) if args.scheduled else None
                synchronizer = Synchronizer(settings, store, api, now, args.force)
                report = (
                    synchronizer.run_history(before, activity_start_date=start)
                    if before else synchronizer.run(target)
                )
                if args.scheduled and report["errors"] == 0 and report["incomplete_windows"] == 0:
                    store.set_meta("scheduled_completed", f"{due}:{config_signature}")
                print(json_bytes(report).decode(), end="")
                if report["errors"] or report["incomplete_windows"]:
                    return 1
                return 2 if report["health_history_needs_start_date"] else 0
            except RateLimitError as exc:
                LOG.warning("%s", exc)
                report = {
                    "run_at": now.isoformat(), "status": "rate_limited",
                    "retry_at": exc.retry_at.isoformat(), "http_status": 429,
                    "start_date_inclusive": (
                        settings.history_start.isoformat() if settings.history_start else None
                    ),
                    "end_date_exclusive": before.isoformat() if before else None,
                    "health_history_pending": True,
                    "message": "Cooldown persisted; rerun the same command to resume safely.",
                }
                write_run_report(settings.download_dir, report, before)
                print(json_bytes(report).decode(), end="")
                return 3
            except (SyncError, OSError, ValueError, sqlite3.Error,
                    GarminPersonalApiError, requests.RequestException) as exc:
                log_failure(exc)
                write_run_report(settings.download_dir, {
                    "run_at": now.isoformat(), "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": "Sync failed; see .state/sync.log or run manually for details.",
                }, before)
                return 1
            finally:
                if api is not None:
                    api.close()
                store.close()
                if handler is not None:
                    LOG.removeHandler(handler)
                    handler.close()
    except (SyncError, OSError, ValueError, sqlite3.Error,
            GarminPersonalApiError, requests.RequestException) as exc:
        log_failure(exc)
        return 1


def log_failure(exc: Exception) -> None:
    if isinstance(exc, (HttpError, requests.RequestException)):
        LOG.error("Garmin authentication/request failed; check credentials and connectivity")
    elif isinstance(exc, GarminPersonalApiError):
        LOG.error("Garmin client failed (%s); check credentials and downloaded data", type(exc).__name__)
    else:
        LOG.error("%s", exc)


if __name__ == "__main__":
    sys.exit(main())
