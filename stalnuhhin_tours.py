#!/usr/bin/env python3
"""Fetch and filter tournaments from chgk.stalnuhhin.ee planner cache (tours.php)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

DEFAULT_TOURS_URL = "https://chgk.stalnuhhin.ee/api/tours.php"
ASYNC_ONLINE_TYPE_IDS = frozenset({8, 11})
DEFAULT_TIMEOUT = 120


class StalnuhhinError(Exception):
    """Failed to fetch or parse stalnuhhin tours data."""


def nearest_sunday(today: date | None = None) -> date:
    """Upcoming Sunday; if today is Sunday, return today."""
    d = today if today is not None else date.today()
    days_ahead = (6 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def fetch_tours(
    url: str = DEFAULT_TOURS_URL,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """GET tours.php and return the ``tours`` list."""
    try:
        r = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException as e:
        raise StalnuhhinError(f"Failed to fetch {url}: {e}") from e
    if not r.ok:
        raise StalnuhhinError(f"HTTP {r.status_code} for {url}\n{r.text[:500]}")
    try:
        data = r.json()
    except ValueError as e:
        raise StalnuhhinError(f"Invalid JSON from {url}") from e
    if not isinstance(data, dict):
        raise StalnuhhinError(f"Unexpected tours.php root type: {type(data)}")
    tours = data.get("tours")
    if not isinstance(tours, list):
        raise StalnuhhinError("tours.php missing tours list")
    return tours


def _parse_tour_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _difficulty_forecast(tour: dict[str, Any]) -> float | None:
    raw = tour.get("difficultyForecast")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def filter_async_online_for_day(
    tours: list[dict[str, Any]],
    day: date,
    *,
    dl_min: float | None = None,
    dl_max: float | None = None,
) -> list[dict[str, Any]]:
    """Keep Асинхрон/Онлайн (type 8|11) playable on ``day``, optional difficultyForecast bounds.

    When either ``dl_min`` or ``dl_max`` is set, tours with missing difficultyForecast are excluded.
    """
    if dl_min is not None and dl_max is not None and dl_min > dl_max:
        raise ValueError(f"dl_min ({dl_min}) must be <= dl_max ({dl_max})")

    filtering_dl = dl_min is not None or dl_max is not None
    out: list[dict[str, Any]] = []
    for tour in tours:
        if not isinstance(tour, dict):
            continue
        typ = tour.get("type") or {}
        if not isinstance(typ, dict):
            continue
        try:
            type_id = int(typ.get("id"))
        except (TypeError, ValueError):
            continue
        if type_id not in ASYNC_ONLINE_TYPE_IDS:
            continue
        start = _parse_tour_date(tour.get("dateStart"))
        end = _parse_tour_date(tour.get("dateEnd"))
        if start is None or end is None:
            continue
        if not (start <= day <= end):
            continue
        if filtering_dl:
            dl = _difficulty_forecast(tour)
            if dl is None:
                continue
            if dl_min is not None and dl < dl_min:
                continue
            if dl_max is not None and dl > dl_max:
                continue
        out.append(tour)

    out.sort(key=lambda t: int(t.get("id") or 0))
    return out


def filter_async_online_all(tours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All Асинхрон/Онлайн tours in the payload (for intersections cache builder)."""
    out: list[dict[str, Any]] = []
    for tour in tours:
        if not isinstance(tour, dict):
            continue
        typ = tour.get("type") or {}
        if not isinstance(typ, dict):
            continue
        try:
            type_id = int(typ.get("id"))
        except (TypeError, ValueError):
            continue
        if type_id not in ASYNC_ONLINE_TYPE_IDS:
            continue
        if tour.get("id") is None:
            continue
        out.append(tour)
    out.sort(key=lambda t: int(t.get("id") or 0))
    return out


def seed_meta_from_tour(tour: dict[str, Any]) -> dict[str, Any]:
    """Build seed_meta entry compatible with tourn_check_web_by_player."""
    from tourn_check_web_by_player import editor_surnames_from_tournament

    tid = int(tour["id"])
    return {
        "id": tid,
        "name": tour.get("name"),
        "source_substrings": [str(tid)],
        "editor_surnames": editor_surnames_from_tournament(tour),
        "difficultyForecast": tour.get("difficultyForecast"),
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
