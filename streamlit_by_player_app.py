#!/usr/bin/env python3
"""Local Streamlit UI for the player-tournaments overlap check.

Uses logic from ``tourn_check_web_by_player`` (no Railway or FastAPI).

Install and run::

    pip install streamlit
    streamlit run streamlit_by_player_app.py
"""

from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from stalnuhhin_tours import (
    StalnuhhinError,
    fetch_tours_live_or_snapshot,
    filter_async_online_for_day,
    nearest_sunday,
    seed_meta_from_tour,
)
from tourn_check_web_by_player import (
    DEFAULT_BASE,
    RatingAPIError,
    parse_player_ids_from_text,
    parse_tournament_lines_from_text,
    run_check,
    tournament_line_is_seed_id,
)

_CLEAR_ROW_BG = "background-color: #e8f5e9"


def _style_summary_clear_green(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    def row_style(row: pd.Series) -> list[str]:
        if str(row.get("status", "")) == "clear":
            return [_CLEAR_ROW_BG] * len(row.index)
        return [""] * len(row.index)

    return df.style.apply(row_style, axis=1)


def _parse_optional_float(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    return float(s)


def _seed_meta_override_for_lines(
    tournament_lines: list[str],
    cached: dict[int, dict],
) -> dict[int, dict] | None:
    """Use Load cache only when every digit line is present; otherwise None (full resolve)."""
    if not cached:
        return None
    ids: list[int] = []
    for line in tournament_lines:
        if not tournament_line_is_seed_id(line):
            return None
        ids.append(int(line))
    if not ids:
        return None
    if any(i not in cached for i in ids):
        return None
    return {i: cached[i] for i in ids}


st.set_page_config(page_title="Tournament check (by player)", layout="wide")
st.title("Tournament check")
st.markdown("Проверка заигранности турниров кем-то из игроков")

_DEFAULT_PLAYERS = "91247\n8915\n31980\n35604\n67338\n84385"

if "players_text" not in st.session_state:
    st.session_state.players_text = _DEFAULT_PLAYERS
if "tournaments_text" not in st.session_state:
    st.session_state.tournaments_text = ""
if "check_intersections" not in st.session_state:
    st.session_state.check_intersections = True
if "bulk_seed_meta" not in st.session_state:
    st.session_state.bulk_seed_meta = {}

# Apply bulk Load results before widgets with those keys are created.
_pending = st.session_state.pop("_pending_bulk_load", None)
if isinstance(_pending, dict):
    st.session_state.tournaments_text = _pending.get("tournaments_text", "")
    st.session_state.bulk_seed_meta = _pending.get("bulk_seed_meta") or {}
    st.session_state.check_intersections = bool(_pending.get("check_intersections", False))
    if _pending.get("load_flash"):
        st.session_state.load_flash = _pending["load_flash"]

flash = st.session_state.pop("load_flash", None)
if flash:
    st.success(flash)

players = st.text_area(
    "Players",
    height=120,
    placeholder="One player id per line",
    key="players_text",
)
tournaments = st.text_area(
    "Tournaments",
    height=120,
    placeholder="Substring or id per line",
    key="tournaments_text",
)

st.subheader("Bulk load (stalnuhhin)")
dl1, dl2 = st.columns(2)
with dl1:
    dl_min_raw = st.text_input(
        "Min difficultyForecast",
        value="",
        placeholder="e.g. 2.5",
        help="Empty = no minimum. Missing forecasts excluded when either bound is set.",
    )
with dl2:
    dl_max_raw = st.text_input(
        "Max difficultyForecast",
        value="",
        placeholder="e.g. 5",
        help="Empty = no maximum.",
    )
load_clicked = st.button("Load nearest Sunday асинхрон/онлайн")

if load_clicked:
    try:
        dl_min = _parse_optional_float(dl_min_raw)
        dl_max = _parse_optional_float(dl_max_raw)
    except ValueError:
        st.error("difficultyForecast min/max must be numbers (or empty).")
        st.stop()
    if dl_min is not None and dl_max is not None and dl_min > dl_max:
        st.error(f"Min difficultyForecast ({dl_min}) must be ≤ max ({dl_max}).")
        st.stop()
    sunday = nearest_sunday()
    with st.spinner(f"Loading tours for {sunday.isoformat()}…"):
        try:
            tours, tours_source = fetch_tours_live_or_snapshot()
            filtered = filter_async_online_for_day(
                tours, sunday, dl_min=dl_min, dl_max=dl_max
            )
        except (StalnuhhinError, ValueError) as exc:
            st.error(str(exc))
            st.stop()
    meta: dict[int, dict] = {}
    for tour in filtered:
        entry = seed_meta_from_tour(tour)
        meta[int(entry["id"])] = entry
    ids = [str(tid) for tid in sorted(meta)]
    bounds = []
    if dl_min is not None:
        bounds.append(f"min {dl_min}")
    if dl_max is not None:
        bounds.append(f"max {dl_max}")
    bound_s = f" ({', '.join(bounds)})" if bounds else ""
    source_note = (
        " (from local tours snapshot — live stalnuhhin returned 403)"
        if tours_source == "snapshot"
        else ""
    )
    # Do not write widget keys here — apply on the next run before widgets instantiate.
    st.session_state._pending_bulk_load = {
        "tournaments_text": "\n".join(ids),
        "bulk_seed_meta": meta,
        "check_intersections": False,
        "load_flash": (
            f"Loaded {len(ids)} tournaments for {sunday.isoformat()}{bound_s}{source_note}. "
            "Check intersections turned off."
        ),
    }
    st.rerun()

c1, c2, c3 = st.columns(3)
with c1:
    date_after = st.text_input(
        "dateEnd strictly after (optional)",
        placeholder="YYYY-MM-DD",
        help="Only affects name-based tournament lookup.",
    )
with c2:
    env_base = (os.environ.get("TOURN_CHECK_BASE_URL") or "").strip()
    base_url = st.text_input(
        "API base URL",
        value=env_base or DEFAULT_BASE,
    )
with c3:
    check_intersections = st.checkbox(
        "Check intersections",
        key="check_intersections",
        help="When off, only listed tournament IDs are checked (faster for bulk lists).",
    )

if st.button("Run check", type="primary"):
    try:
        player_ids = parse_player_ids_from_text(players)
        tournament_lines = parse_tournament_lines_from_text(tournaments)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    base = (base_url.strip() or DEFAULT_BASE).rstrip("/")
    date_end = date_after.strip() or None
    override = _seed_meta_override_for_lines(
        tournament_lines, st.session_state.get("bulk_seed_meta") or {}
    )

    with st.spinner("Running check…"):
        try:
            report = run_check(
                player_ids,
                tournament_lines,
                base_url=base,
                date_end_after=date_end,
                verbose=False,
                include_intersections=bool(check_intersections),
                seed_meta_override=override,
            )
        except RatingAPIError as exc:
            st.error(str(exc))
            st.stop()

    st.subheader("Summary")
    summary_df = pd.DataFrame(report["summary"])
    st.dataframe(
        _style_summary_clear_green(summary_df),
        width="stretch",
        hide_index=True,
    )

    warns = report.get("warnings") or []
    if warns:
        with st.expander(f"Warnings ({len(warns)})", expanded=True):
            st.json(warns)

    full_json = json.dumps(report, ensure_ascii=False, indent=2)
    st.download_button(
        "Download full report (JSON)",
        data=full_json,
        file_name="tourn_check_report.json",
        mime="application/json",
    )
    with st.expander("Full report (JSON)"):
        st.code(full_json, language="json")
