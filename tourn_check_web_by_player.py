#!/usr/bin/env python3
"""Alternate overlap checker: uses GET /players/{id}/tournaments instead of per-tournament results.

Compare with tourn_check_web.py (results+teamMembers on every seed ∪ intersection tournament).
Same report shape for summaries; input includes overlap_strategy for benchmarks.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

DEFAULT_BASE = "https://api.rating.chgk.net"
DEFAULT_TIMEOUT = 60
MAX_ITEMS_PER_PAGE = 512


class RatingAPIError(Exception):
    """HTTP or unexpected payload from api.rating.chgk.net."""


def editor_surnames_from_tournament(row: dict[str, Any]) -> list[str]:
    editors = row.get("editors")
    if not isinstance(editors, list):
        return []
    out: list[str] = []
    for ed in editors:
        if isinstance(ed, dict):
            s = ed.get("surname")
            if s is not None and str(s).strip():
                out.append(str(s).strip())
    return out


def parse_text_lines(raw: str) -> list[str]:
    lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def parse_player_ids_from_text(text: str) -> list[int]:
    ids: list[int] = []
    for line in parse_text_lines(text):
        try:
            ids.append(int(line))
        except ValueError as e:
            raise ValueError(f"Invalid player id (expected integer): {line!r}") from e
    if not ids:
        raise ValueError("No player ids in input")
    return ids


def parse_tournament_lines_from_text(text: str) -> list[str]:
    lines = parse_text_lines(text)
    if not lines:
        raise ValueError("No tournament lines in input")
    return lines


class RatingClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        verbose: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def get(self, path: str, params: list[tuple[str, str | int]] | dict[str, Any] | None = None) -> Any:
        url = self._url(path)
        if self.verbose:
            print(f"GET {url} params={params}", file=sys.stderr)
        r = self.session.get(url, params=params, timeout=self.timeout)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0
            time.sleep(wait)
            r = self.session.get(url, params=params, timeout=self.timeout)
        if not r.ok:
            raise RatingAPIError(f"HTTP {r.status_code} for {r.url}\n{r.text[:500]}")
        return r.json()

    def fetch_tournament_item(self, tournament_id: int) -> dict[str, Any] | None:
        """GET /tournaments/{id}; None if 404 or unexpected body."""
        url = self._url(f"/tournaments/{tournament_id}")
        if self.verbose:
            print(f"GET {url}", file=sys.stderr)
        r = self.session.get(url, timeout=self.timeout)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0
            time.sleep(wait)
            r = self.session.get(url, timeout=self.timeout)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise RatingAPIError(f"HTTP {r.status_code} for {r.url}\n{r.text[:500]}")
        data = r.json()
        return data if isinstance(data, dict) else None

    def fetch_tournaments_by_name(
        self,
        name_substring: str,
        date_end_strictly_after: str | None = None,
        items_per_page: int = MAX_ITEMS_PER_PAGE,
    ) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        page = 1
        while True:
            params: list[tuple[str, str | int]] = [
                ("name[]", name_substring),
                ("page", page),
                ("itemsPerPage", items_per_page),
            ]
            if date_end_strictly_after is not None:
                params.insert(1, ("dateEnd[strictly_after]", date_end_strictly_after))
            chunk = self.get("/tournaments", params=params)
            if not isinstance(chunk, list):
                raise RatingAPIError(f"Unexpected /tournaments response type: {type(chunk)}")
            all_rows.extend(chunk)
            if len(chunk) < items_per_page:
                break
            page += 1
        return all_rows

    def fetch_intersections(self, tournament_id: int) -> list[dict[str, Any]]:
        data = self.get(f"/tournaments/{tournament_id}/intersections")
        if not isinstance(data, list):
            raise RatingAPIError(f"Unexpected intersections response type: {type(data)}")
        return data

    def fetch_player_tournaments(self, player_id: int) -> list[dict[str, Any]]:
        """GET /players/{id}/tournaments/ — full list in one response (no pagination)."""
        data = self.get(f"/players/{player_id}/tournaments/")
        if not isinstance(data, list):
            raise RatingAPIError(
                f"Unexpected /players/{{id}}/tournaments/ response type: {type(data)}"
            )
        return data


_thread_local = threading.local()


def _thread_local_client(base_url: str, timeout: float, verbose: bool) -> RatingClient:
    """One Session per thread (requests sessions are not thread-safe)."""
    key = (base_url, timeout, verbose)
    if getattr(_thread_local, "_client_key", None) != key:
        _thread_local._client_key = key
        _thread_local._client = RatingClient(base_url, timeout=timeout, verbose=verbose)
    return _thread_local._client


def intersection_ids_from_response(rows: list[dict[str, Any]]) -> list[int]:
    out: set[int] = set()
    for row in rows:
        tid = row.get("id")
        if tid is not None:
            out.add(int(tid))
    return sorted(out)


def tournament_id_from_player_row(row: dict[str, Any]) -> int | None:
    for key in ("idtournament", "idTournament"):
        v = row.get(key)
        if v is not None:
            return int(v)
    return None


def strip_leading_until_alnum(line: str) -> str:
    """Drop leading characters until the first letter or number (Unicode-aware)."""
    for i, ch in enumerate(line):
        if ch.isalnum():
            return line[i:]
    return ""


def tournament_line_is_seed_id(line: str) -> bool:
    """True if the whole line is decimal digits only (tournament id)."""
    return bool(line) and line.isdigit()


def tournaments_matching_all_words(
    client: RatingClient,
    words: list[str],
    date_end_after: str | None,
) -> list[dict[str, Any]]:
    """Each word is a separate name[] API query; keep tournaments whose id appears in every result set."""
    sets: list[set[int]] = []
    rows_by_id: dict[int, dict[str, Any]] = {}
    for w in words:
        chunk = client.fetch_tournaments_by_name(w, date_end_after)
        ids: set[int] = set()
        for r in chunk:
            tid = r.get("id")
            if tid is None:
                continue
            tid_i = int(tid)
            ids.add(tid_i)
            if tid_i not in rows_by_id:
                rows_by_id[tid_i] = r
        sets.append(ids)
    common = sets[0].copy()
    for s in sets[1:]:
        common &= s
    return [rows_by_id[i] for i in sorted(common) if i in rows_by_id]


def _parallel_workers(n_jobs: int, env_default: int = 8) -> int:
    """Worker count: env override if set; else 8, or 16 when ``n_jobs > 8`` (cap 32)."""
    raw = os.environ.get("TOURN_CHECK_PARALLEL_WORKERS")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, min(32, int(raw)))
        except ValueError:
            pass
    if n_jobs > 8:
        return min(32, max(env_default, 16))
    return env_default


def load_intersections_cache(path: str) -> dict[int, list[int]]:
    """Load seed_id → intersection ids from data/intersections_cache.json. Empty on miss."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    by_seed = data.get("by_seed") if isinstance(data, dict) else None
    if not isinstance(by_seed, dict):
        return {}
    out: dict[int, list[int]] = {}
    for k, v in by_seed.items():
        try:
            tid = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, list):
            continue
        ids: list[int] = []
        for x in v:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        out[tid] = sorted(set(ids))
    return out


DEFAULT_INTERSECTIONS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "intersections_cache.json",
)

# Cap how many API hits one name line can expand into (short names like "B-52" match 100+).
DEFAULT_MAX_NAME_MATCHES = 5


def _tour_date_end_key(row: dict[str, Any]) -> str:
    """Sort key for preferring newer tournaments (ISO dateEnd strings sort lexicographically)."""
    v = row.get("dateEnd") or row.get("dateStart") or ""
    return str(v)


def select_name_matches(
    rows: list[dict[str, Any]],
    *,
    max_matches: int = DEFAULT_MAX_NAME_MATCHES,
) -> tuple[list[dict[str, Any]], int]:
    """Keep up to ``max_matches`` rows with the latest dateEnd; return (kept, total_before_cap)."""
    usable = [r for r in rows if isinstance(r, dict) and r.get("id") is not None]
    total = len(usable)
    if max_matches <= 0 or total <= max_matches:
        return usable, total
    ranked = sorted(usable, key=_tour_date_end_key, reverse=True)
    return ranked[:max_matches], total


def resolve_seeds_mixed(
    client: RatingClient,
    lines: list[str],
    date_end_after: str | None,
    *,
    seed_meta_override: dict[int, dict[str, Any]] | None = None,
    parallel: bool = True,
    parallel_workers: int | None = None,
    max_name_matches: int = DEFAULT_MAX_NAME_MATCHES,
) -> tuple[
    dict[int, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[int],
    list[dict[str, Any]],
]:
    """Digit-only lines = tournament id; others = name search after trimming leading punctuation.

    Name search: strip leading non-alphanumeric, then GET /tournaments?name[]=full string.
    If that returns nothing and there are at least two whitespace-separated tokens, run one
    search per word and intersect by tournament id (AND of substring matches).

    Ambiguous name hits are capped to ``max_name_matches`` newest by ``dateEnd`` (warning added).

    When ``seed_meta_override`` contains an id, skip the live ``GET /tournaments/{id}``.
    Digit-id lookups that still need the API are fetched in parallel when ``parallel`` is set.
    """
    seed_meta: dict[int, dict[str, Any]] = {}
    matches_by_line: dict[str, list[dict[str, Any]]] = {}
    order: list[int] = []
    resolution_warnings: list[dict[str, Any]] = []
    override = seed_meta_override or {}

    ids_to_fetch: list[int] = []
    seen_fetch: set[int] = set()
    name_lines: list[str] = []
    for line in lines:
        if tournament_line_is_seed_id(line):
            tid_raw = int(line)
            if tid_raw in override:
                continue
            if tid_raw not in seen_fetch:
                seen_fetch.add(tid_raw)
                ids_to_fetch.append(tid_raw)
        else:
            name_lines.append(line)

    fetched_rows: dict[int, dict[str, Any] | None] = {}
    if ids_to_fetch:
        workers = parallel_workers if parallel_workers is not None else _parallel_workers(len(ids_to_fetch))
        base_url = client.base_url
        timeout = client.timeout
        verbose = client.verbose
        if parallel and len(ids_to_fetch) > 1:

            def _id_job(tid: int) -> tuple[int, dict[str, Any] | None]:
                c = _thread_local_client(base_url, timeout, verbose)
                return tid, c.fetch_tournament_item(tid)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_id_job, tid) for tid in ids_to_fetch]
                for fut in as_completed(futures):
                    tid, row = fut.result()
                    fetched_rows[tid] = row
        else:
            for tid in ids_to_fetch:
                fetched_rows[tid] = client.fetch_tournament_item(tid)

    # Name searches: parallel when several distinct lines (each line may still paginate).
    name_search_rows: dict[str, list[dict[str, Any]]] = {}
    if name_lines:
        workers = parallel_workers if parallel_workers is not None else _parallel_workers(len(name_lines))
        base_url = client.base_url
        timeout = client.timeout
        verbose = client.verbose

        def _name_job(line: str) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
            c = _thread_local_client(base_url, timeout, verbose)
            normalized = strip_leading_until_alnum(line)
            if not normalized:
                return line, [], None
            rows = c.fetch_tournaments_by_name(normalized, date_end_after)
            warn: dict[str, Any] | None = None
            words = [w for w in normalized.split() if w]
            if not rows and len(words) >= 2:
                rows = tournaments_matching_all_words(c, words, date_end_after)
                if rows:
                    warn = {
                        "type": "name_search_word_intersection",
                        "substring": line,
                        "normalized": normalized,
                        "words": words,
                    }
            return line, rows, warn

        unique_name_lines = list(dict.fromkeys(name_lines))
        if parallel and len(unique_name_lines) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_name_job, ln) for ln in unique_name_lines]
                for fut in as_completed(futures):
                    line, rows, warn = fut.result()
                    name_search_rows[line] = rows
                    if warn:
                        resolution_warnings.append(warn)
        else:
            for line in unique_name_lines:
                line_out, rows, warn = _name_job(line)
                name_search_rows[line_out] = rows
                if warn:
                    resolution_warnings.append(warn)

    def _add_seed_from_meta(tid: int, meta: dict[str, Any], source_line: str) -> None:
        if tid not in seed_meta:
            seed_meta[tid] = {
                "id": tid,
                "name": meta.get("name"),
                "source_substrings": [],
                "editor_surnames": list(meta.get("editor_surnames") or []),
                "difficultyForecast": meta.get("difficultyForecast"),
            }
            order.append(tid)
        if source_line not in seed_meta[tid]["source_substrings"]:
            seed_meta[tid]["source_substrings"].append(source_line)

    def _add_seed_from_row(row: dict[str, Any], source_line: str) -> int | None:
        if row.get("id") is None:
            return None
        tid = int(row["id"])
        if tid not in seed_meta:
            seed_meta[tid] = {
                "id": tid,
                "name": row.get("name"),
                "source_substrings": [],
                "editor_surnames": editor_surnames_from_tournament(row),
                "difficultyForecast": row.get("difficultyForecast"),
            }
            order.append(tid)
        if source_line not in seed_meta[tid]["source_substrings"]:
            seed_meta[tid]["source_substrings"].append(source_line)
        return tid

    for line in lines:
        if tournament_line_is_seed_id(line):
            tid_raw = int(line)
            if tid_raw in override:
                meta = override[tid_raw]
                tid = int(meta.get("id", tid_raw))
                matches_by_line[line] = [{"id": tid, "name": meta.get("name")}]
                _add_seed_from_meta(tid, meta, line)
                continue
            row = fetched_rows.get(tid_raw)
            if row is None or row.get("id") is None:
                matches_by_line[line] = []
                continue
            tid = _add_seed_from_row(row, line)
            matches_by_line[line] = [{"id": tid, "name": row.get("name")}]
        else:
            normalized = strip_leading_until_alnum(line)
            if not normalized:
                matches_by_line[line] = []
                continue
            rows = name_search_rows.get(line, [])
            kept, total = select_name_matches(rows, max_matches=max_name_matches)
            if total > len(kept):
                resolution_warnings.append(
                    {
                        "type": "name_match_truncated",
                        "substring": line,
                        "normalized": normalized,
                        "total_matches": total,
                        "kept": len(kept),
                        "kept_ids": [r.get("id") for r in kept],
                        "hint": "Use tournament id, a longer name, or dateEnd filter to narrow results.",
                    }
                )
            matches_by_line[line] = [{"id": r.get("id"), "name": r.get("name")} for r in kept]
            for trow in kept:
                _add_seed_from_row(trow, line)

    return seed_meta, matches_by_line, order, resolution_warnings



def short_label_from_status(status: str) -> str:
    if status in ("played_listed", "played_via_intersection"):
        return "played"
    if status == "clear":
        return "clear"
    return status


def build_summary(
    line_keys: list[str],
    matches_by_line: dict[str, list[dict[str, Any]]],
    tournaments_out: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    status_by_id: dict[int, str] = {}
    extra_by_id: dict[int, dict[str, Any]] = {}
    for row in tournaments_out:
        tid = row.get("id")
        if tid is None:
            continue
        tid = int(tid)
        status_by_id[tid] = short_label_from_status(str(row.get("status", "")))
        extra_by_id[tid] = {
            "editor_surnames": list(row.get("editor_surnames") or []),
            "difficultyForecast": row.get("difficultyForecast"),
        }

    summary: list[dict[str, Any]] = []
    for key in line_keys:
        matches = matches_by_line.get(key, [])
        if not matches:
            summary.append(
                {
                    "id": None,
                    "name": key,
                    "status": "not found",
                    "editor_surnames": [],
                    "difficultyForecast": None,
                }
            )
            continue
        for m in matches:
            tid = m.get("id")
            if tid is None:
                continue
            tid = int(tid)
            name = m.get("name")
            display = str(name).strip() if name else f"(tournament id {tid})"
            ex = extra_by_id.get(
                tid,
                {"editor_surnames": [], "difficultyForecast": None},
            )
            summary.append(
                {
                    "id": tid,
                    "name": display,
                    "status": status_by_id.get(tid, "clear"),
                    "editor_surnames": ex["editor_surnames"],
                    "difficultyForecast": ex["difficultyForecast"],
                }
            )
    return summary


def _tsv_cell(value: Any) -> str:
    s = "" if value is None else str(value)
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def format_summary_lines(summary: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in summary:
        tid = row.get("id")
        id_s = "" if tid is None else str(tid)
        eds = row.get("editor_surnames") or []
        ed_s = "; ".join(_tsv_cell(x) for x in eds)
        df = row.get("difficultyForecast")
        df_s = "" if df is None else _tsv_cell(df)
        lines.append(
            f"{id_s}\t{row['status']}\t{_tsv_cell(row['name'])}\t({ed_s})\t{df_s}"
        )
    return lines


def build_warnings(matches_by_line: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for sub, matches in matches_by_line.items():
        if len(matches) > 1:
            warnings.append(
                {
                    "type": "ambiguous_name_match",
                    "substring": sub,
                    "count": len(matches),
                    "tournaments": matches,
                }
            )
        if len(matches) == 0:
            warnings.append(
                {
                    "type": "no_match",
                    "substring": sub,
                    "tournaments": [],
                }
            )
    return warnings


def run_check(
    player_ids: list[int],
    tournament_lines: list[str],
    *,
    base_url: str = DEFAULT_BASE,
    date_end_after: str | None = None,
    verbose: bool = False,
    include_intersections: bool = True,
    seed_meta_override: dict[int, dict[str, Any]] | None = None,
    intersections_cache_path: str | None = DEFAULT_INTERSECTIONS_CACHE_PATH,
) -> dict[str, Any]:
    """Run overlap check using /players/{id}/tournaments; same report shape as tourn_check_web.run_check.

    When ``include_intersections`` is False, skip intersection fetches (listed-seed overlap only).
    ``seed_meta_override`` supplies prebuilt meta (e.g. from stalnuhhin) to skip id lookups.
    ``intersections_cache_path`` is read when intersections are enabled; misses fall back to live API.
    """
    timing = os.environ.get("TOURN_CHECK_TIMING", "").lower() in ("1", "true", "yes")
    t_mark = time.perf_counter()

    def _timing_note(label: str) -> None:
        nonlocal t_mark
        if not timing:
            return
        now = time.perf_counter()
        print(f"[tourn_check_by_player timing] {label}: {now - t_mark:.3f}s", file=sys.stderr)
        t_mark = now

    team_ids = set(player_ids)
    client = RatingClient(base_url, verbose=verbose)
    timeout = client.timeout

    parallel = os.environ.get("TOURN_CHECK_PARALLEL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    seed_meta, matches_by_line, seed_order, resolution_warnings = resolve_seeds_mixed(
        client,
        tournament_lines,
        date_end_after,
        seed_meta_override=seed_meta_override,
        parallel=parallel,
    )
    _timing_note("resolve seeds (name/id lookups)")

    parallel_workers = _parallel_workers(len(seed_order)) if parallel else 1

    intersections_by_seed: dict[str, list[int]] = {}
    all_tournament_ids: set[int] = set(seed_meta.keys())
    cache_hits = 0
    cache_misses = 0

    if include_intersections:
        inter_cache = (
            load_intersections_cache(intersections_cache_path)
            if intersections_cache_path
            else {}
        )
        need_fetch: list[int] = []
        for tid in seed_order:
            if tid in inter_cache:
                iids = inter_cache[tid]
                intersections_by_seed[str(tid)] = iids
                all_tournament_ids.update(iids)
                cache_hits += 1
            else:
                need_fetch.append(tid)

        if need_fetch:
            cache_misses = len(need_fetch)
            if parallel and len(need_fetch) > 1:

                def _inter_job(tid: int) -> tuple[int, list[int]]:
                    c = _thread_local_client(base_url, timeout, verbose)
                    rows = c.fetch_intersections(tid)
                    return tid, intersection_ids_from_response(rows)

                with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
                    futures = [pool.submit(_inter_job, tid) for tid in need_fetch]
                    for fut in as_completed(futures):
                        tid, iids = fut.result()
                        intersections_by_seed[str(tid)] = iids
                        all_tournament_ids.update(iids)
            else:
                for tid in need_fetch:
                    inter_rows = client.fetch_intersections(tid)
                    iids = intersection_ids_from_response(inter_rows)
                    intersections_by_seed[str(tid)] = iids
                    all_tournament_ids.update(iids)
        _timing_note(
            f"fetch intersections ({len(seed_order)} seeds, cache_hits={cache_hits}, "
            f"live={cache_misses} → {len(all_tournament_ids)} tournament ids total)"
        )
    else:
        for tid in seed_order:
            intersections_by_seed[str(tid)] = []
        _timing_note(f"skip intersections ({len(seed_order)} seeds, listed-only)")

    unique_player_ids = list(dict.fromkeys(player_ids))
    tournaments_per_player: dict[int, set[int]] = {}

    if parallel and len(unique_player_ids) > 1:

        def _pt_job(pid: int) -> tuple[int, set[int]]:
            c = _thread_local_client(base_url, timeout, verbose)
            rows = c.fetch_player_tournaments(pid)
            tids: set[int] = set()
            for r in rows:
                x = tournament_id_from_player_row(r)
                if x is not None:
                    tids.add(x)
            return pid, tids

        with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
            futures = [pool.submit(_pt_job, pid) for pid in unique_player_ids]
            for fut in as_completed(futures):
                pid, tids = fut.result()
                tournaments_per_player[pid] = tids
    else:
        for pid in unique_player_ids:
            rows = client.fetch_player_tournaments(pid)
            tids = set()
            for r in rows:
                x = tournament_id_from_player_row(r)
                if x is not None:
                    tids.add(x)
            tournaments_per_player[pid] = tids

    n_pt_rows = sum(len(tournaments_per_player[p]) for p in unique_player_ids)
    _timing_note(
        f"fetch player tournaments ({len(unique_player_ids)} players, {n_pt_rows} idtournament rows total)"
    )

    tournaments_out: list[dict[str, Any]] = []
    for tid in seed_order:
        meta = seed_meta[tid]
        iids = intersections_by_seed[str(tid)]
        listed_hits = sorted(p for p in team_ids if tid in tournaments_per_player.get(p, set()))
        played_listed = bool(listed_hits)

        matching_intersection_ids: list[int] = []
        inter_hits: dict[str, list[int]] = {}
        for iid in iids:
            hp = sorted(pl for pl in team_ids if iid in tournaments_per_player.get(pl, set()))
            if hp:
                matching_intersection_ids.append(iid)
                inter_hits[str(iid)] = hp

        played_intersection = bool(matching_intersection_ids)
        if played_listed:
            status = "played_listed"
        elif played_intersection:
            status = "played_via_intersection"
        else:
            status = "clear"

        tournaments_out.append(
            {
                "id": tid,
                "name": meta.get("name"),
                "source_substrings": meta.get("source_substrings", []),
                "editor_surnames": meta.get("editor_surnames", []),
                "difficultyForecast": meta.get("difficultyForecast"),
                "intersection_ids": iids,
                "played_listed": played_listed,
                "played_intersection": played_intersection,
                "status": status,
                "matching_players_listed": listed_hits,
                "matching_players_by_intersection_id": inter_hits,
                "matching_intersection_ids": matching_intersection_ids,
            }
        )

    summary = build_summary(tournament_lines, matches_by_line, tournaments_out)
    kinds = ["id" if tournament_line_is_seed_id(ln) else "name" for ln in tournament_lines]
    _timing_note("build summary / report dict")

    return {
        "input": {
            "player_ids": player_ids,
            "tournament_lines": tournament_lines,
            "tournament_line_kinds": kinds,
            "date_end_strictly_after": date_end_after,
            "base_url": base_url,
            "overlap_strategy": "player_tournaments_api",
            "include_intersections": include_intersections,
            "intersections_cache_hits": cache_hits if include_intersections else 0,
            "intersections_cache_misses": cache_misses if include_intersections else 0,
        },
        "intersections_by_seed": intersections_by_seed,
        "tournaments": tournaments_out,
        "warnings": resolution_warnings + build_warnings(matches_by_line),
        "summary": summary,
    }


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"
