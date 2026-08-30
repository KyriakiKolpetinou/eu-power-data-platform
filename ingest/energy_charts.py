"""Client for the Fraunhofer ISE energy-charts API v2.

Confirmed against live responses:
  /v2/public_power           -> actuals, series id 'load'
  /v2/public_power_forecast  -> baseline, series id 'load'

Both 15-minute resolution for DE, timestamps local with UTC offset,
history available back to at least 2023. No API key.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

BASE_URL = "https://api.energy-charts.info"
TIMEOUT_S = 30
MAX_RETRIES = 3
EXPECTED_SCHEMA_VERSION = "2.0"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Payload:
    """One API response plus enough metadata to trace where it came from."""

    url: str
    fetched_at: dt.datetime
    body: dict[str, Any]

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def series_units(self) -> dict[str, str]:
        """Map series id -> unit.

        Two series are '%' while the response-level unit is 'MW'. Loading a
        percentage into a megawatt column is the kind of error that produces a
        confidently wrong model, so units are resolved per series here.
        """
        default_unit = self.body.get("unit")
        return {
            s["id"]: s.get("unit", default_unit)
            for s in self.body.get("series", [])
        }

    @property
    def series_names(self) -> dict[str, str]:
        return {s["id"]: s.get("name") for s in self.body.get("series", [])}


def _get(path: str, params: dict[str, Any]) -> Payload:
    url = f"{BASE_URL}{path}"
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT_S)
            resp.raise_for_status()
            payload = Payload(
                url=resp.url,
                fetched_at=dt.datetime.now(dt.timezone.utc),
                body=resp.json(),
            )
            _check_schema(payload)
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            wait = 2 ** attempt
            log.warning("attempt %s/%s failed (%s); retrying in %ss",
                        attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts") from last_error


def _check_schema(payload: Payload) -> None:
    """Warn loudly if the API changes underneath us.

    This is cheap and it is the difference between noticing a schema change
    on the day it happens and noticing three weeks of corrupt data later.
    """
    version = payload.body.get("schema_version")
    if version != EXPECTED_SCHEMA_VERSION:
        log.warning("schema_version is %r, expected %r — check the response shape",
                    version, EXPECTED_SCHEMA_VERSION)
    if payload.body.get("deprecated"):
        log.warning("endpoint reports deprecated=true: %s", payload.url)


def fetch_actuals(country: str, start: str, end: str) -> Payload:
    """Actual public grid load and generation.

    Note: /v2/public_power, not /v2/total_power. total_power reports
    'load_incl_self_consumption', which is ~3 GW larger than the quantity the
    day-ahead forecast targets and is not comparable to it.

    `end` is INCLUSIVE. start=end returns one full day.
    """
    return _get("/v2/public_power", {"country": country, "start": start, "end": end})


def fetch_load_forecast(country: str, start: str, end: str) -> Payload:
    """The operator's published day-ahead load forecast — the external baseline.

    'load' supports only forecast_type='day-ahead'.
    """
    return _get("/v2/public_power_forecast", {
        "country": country,
        "production_type": "load",
        "forecast_type": "day-ahead",
        "start": start,
        "end": end,
    })


def to_records(payload: Payload, country: str, source: str) -> list[dict[str, Any]]:
    """Flatten a v2 response into long-format rows: one row per (timestamp, series).

    All series are kept. Selecting the ones that matter is a modelling
    decision and belongs in dbt, not in the loader.
    """
    units = payload.series_units
    names = payload.series_names
    body = payload.body

    fetched_at = payload.fetched_at.isoformat()
    digest = payload.sha256
    rows: list[dict[str, Any]] = []

    for record in body.get("data", []):
        ts_local = record.get("timestamp")
        if ts_local is None:
            log.error("record without timestamp in %s", payload.url)
            continue

        # Parse the offset-aware local timestamp, then derive UTC from it.
        # Both are stored: UTC is the join key, local is what human electricity
        # consumption actually follows.
        parsed = dt.datetime.fromisoformat(ts_local)
        ts_utc = parsed.astimezone(dt.timezone.utc)

        for series_id, value in (record.get("values") or {}).items():
            rows.append({
                "country": country,
                "bidding_zone": body.get("bidding_zone"),
                "ts_utc": ts_utc.isoformat(),
                "ts_local": ts_local,
                "series_id": series_id,
                "series_name": names.get(series_id),
                "value": None if value is None else float(value),
                "unit": units.get(series_id, body.get("unit")),
                "interval_minutes": body.get("interval_minutes"),
                "source": source,
                "source_url": payload.url,
                "schema_version": body.get("schema_version"),
                "available_until": body.get("available_until"),
                "generated_at": body.get("generated_at"),
                "payload_sha256": digest,
                "ingested_at": fetched_at,
            })

    return rows