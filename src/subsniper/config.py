"""Configuration loading and validation.

Credentials come from the environment (.env). Everything else comes from
config.yaml. The two are deliberately kept separate so config.yaml stays safe
to share, screenshot, or commit.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class ConfigError(Exception):
    """Raised when configuration is missing or self-contradictory."""


def _parse_hhmm(value: str, label: str) -> dtime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ConfigError(f"{label} must be 'HH:MM', got {value!r}")
    hh, mm = (int(p) for p in value.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ConfigError(f"{label} is not a valid time: {value!r}")
    return dtime(hh, mm)


def _parse_days(values: Any, label: str) -> set[int]:
    if not isinstance(values, list) or not values:
        raise ConfigError(f"{label} must be a non-empty list of weekday names")
    out: set[int] = set()
    for v in values:
        key = str(v).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ConfigError(f"{label} has unknown weekday {v!r}")
        out.add(WEEKDAYS[key])
    return out


def _compile(patterns: Any, label: str) -> list[re.Pattern[str]]:
    if patterns is None:
        return []
    if not isinstance(patterns, list):
        raise ConfigError(f"{label} must be a list of regex patterns")
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(str(p), re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"{label} contains invalid regex {p!r}: {exc}") from exc
    return compiled


@dataclass(frozen=True)
class PollWindow:
    name: str
    days: set[int]
    start: dtime
    end: dtime
    interval_seconds: float

    def covers(self, weekday: int, now: dtime) -> bool:
        if weekday not in self.days:
            return False
        if self.start <= self.end:
            return self.start <= now < self.end
        # Window wraps past midnight
        return now >= self.start or now < self.end


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    pin: str | None
    pushover_user_key: str
    pushover_api_token: str
    pushover_device: str | None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let credentials land in a log line or traceback.
        return "Credentials(<redacted>)"


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path
    credentials: Credentials

    role_include: list[re.Pattern[str]] = field(default_factory=list)
    role_exclude: list[re.Pattern[str]] = field(default_factory=list)
    poll_windows: list[PollWindow] = field(default_factory=list)

    # -- convenience accessors -------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    @property
    def dry_run(self) -> bool:
        return bool(self.get("autoaccept.dry_run", True))

    @property
    def kill_switch_path(self) -> Path | None:
        name = self.get("autoaccept.kill_switch_file")
        return self.root / str(name) if name else None

    @property
    def arm_file_path(self) -> Path | None:
        name = self.get("autoaccept.arm_file")
        return self.root / str(name) if name else None

    def interval_for(self, weekday: int, now: dtime) -> tuple[float, str]:
        """Return (seconds, window_name) for the current moment."""
        for window in self.poll_windows:
            if window.covers(weekday, now):
                return window.interval_seconds, window.name
        return float(self.get("polling.default_interval_seconds", 120)), "default"


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path = ".env",
    require_credentials: bool = True,
) -> Config:
    """Load and validate config + credentials.

    Fails loudly and specifically. A misconfigured filter here means either
    missed jobs or an unwanted auto-accept, so nothing is silently defaulted
    where a wrong guess would be costly.
    """
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(
            f"No config file at {config_path}. Copy config.example.yaml to config.yaml."
        )

    root = config_path.parent
    load_dotenv(Path(env_path).expanduser(), override=False)

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a YAML mapping at the top level")

    creds = _load_credentials(require_credentials)

    cfg = Config(raw=raw, root=root, credentials=creds)
    cfg.role_include = _compile(cfg.get("filters.role.include"), "filters.role.include")
    cfg.role_exclude = _compile(cfg.get("filters.role.exclude"), "filters.role.exclude")

    if not cfg.role_include:
        raise ConfigError(
            "filters.role.include is empty - that would match every job posted, "
            "including non-teaching roles. Add at least one pattern."
        )

    cfg.poll_windows = _load_windows(cfg.get("polling.windows", []))
    _validate_time_filters(cfg)
    _validate_autoaccept(cfg)

    return cfg


def _load_credentials(require: bool) -> Credentials:
    def need(key: str) -> str:
        val = (os.getenv(key) or "").strip()
        if require and not val:
            raise ConfigError(
                f"{key} is not set. Copy .env.example to .env and fill it in."
            )
        return val

    return Credentials(
        username=need("FRONTLINE_USERNAME"),
        password=need("FRONTLINE_PASSWORD"),
        pin=(os.getenv("FRONTLINE_PIN") or "").strip() or None,
        pushover_user_key=need("PUSHOVER_USER_KEY"),
        pushover_api_token=need("PUSHOVER_API_TOKEN"),
        pushover_device=(os.getenv("PUSHOVER_DEVICE") or "").strip() or None,
    )


def _load_windows(entries: Any) -> list[PollWindow]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ConfigError("polling.windows must be a list")

    windows = []
    for i, entry in enumerate(entries):
        label = f"polling.windows[{i}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a mapping")
        interval = entry.get("interval_seconds")
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ConfigError(f"{label}.interval_seconds must be a positive number")
        if interval < 3:
            raise ConfigError(
                f"{label}.interval_seconds is {interval}s. Anything under 3s is a "
                "denial-of-service pattern against Frontline and will get the "
                "account flagged. Raise it."
            )
        windows.append(
            PollWindow(
                name=str(entry.get("name", f"window-{i}")),
                days=_parse_days(entry.get("days"), f"{label}.days"),
                start=_parse_hhmm(entry.get("start", ""), f"{label}.start"),
                end=_parse_hhmm(entry.get("end", ""), f"{label}.end"),
                interval_seconds=float(interval),
            )
        )
    return windows


def _validate_time_filters(cfg: Config) -> None:
    earliest = cfg.get("filters.time.earliest_start")
    latest = cfg.get("filters.time.latest_end")
    if earliest and latest:
        e = _parse_hhmm(earliest, "filters.time.earliest_start")
        l = _parse_hhmm(latest, "filters.time.latest_end")
        if e >= l:
            raise ConfigError(
                f"filters.time.earliest_start ({earliest}) is not before "
                f"latest_end ({latest}) - no job could ever match."
            )

    lo = cfg.get("filters.time.min_duration_minutes")
    hi = cfg.get("filters.time.max_duration_minutes")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > hi:
        raise ConfigError(
            f"filters.time.min_duration_minutes ({lo}) exceeds "
            f"max_duration_minutes ({hi}) - no job could ever match."
        )

    days = cfg.get("filters.time.allowed_weekdays")
    if days:
        _parse_days(days, "filters.time.allowed_weekdays")


def _validate_autoaccept(cfg: Config) -> None:
    per_day = cfg.get("autoaccept.max_accepts_per_day", 1)
    per_run = cfg.get("autoaccept.max_accepts_per_run", 1)
    for label, val in (("max_accepts_per_day", per_day), ("max_accepts_per_run", per_run)):
        if not isinstance(val, int) or val < 0:
            raise ConfigError(f"autoaccept.{label} must be a non-negative integer")
    if per_run > per_day:
        raise ConfigError(
            f"autoaccept.max_accepts_per_run ({per_run}) exceeds "
            f"max_accepts_per_day ({per_day})"
        )
