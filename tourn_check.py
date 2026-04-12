#!/usr/bin/env python3
"""Tournament / player overlap checker for rating.chgk.net API."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests

DEFAULT_BASE = "https://api.rating.chgk.net"
DEFAULT_TIMEOUT = 60
MAX_ITEMS_PER_PAGE = 512


def editor_surnames_from_tournament(row: dict[str, Any]) -> list[str]:
    """Collect editor surnames from a tournament object (editors[i].surname)."""
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


def load_text_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        lines: list[str] = []
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


def load_player_ids(path: str) -> list[int]:
    ids: list[int] = []
    for line in load_text_lines(path):
        try:
            ids.append(int(line))
        except ValueError as e:
            raise SystemExit(f"Invalid player id (expected integer): {line!r} in {path}") from e
    if not ids:
        raise SystemExit(f"No player ids found in {path}")
    return ids


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
            raise SystemExit(f"HTTP {r.status_code} for {r.url}\n{r.text[:500]}")
        return r.json()

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
                raise SystemExit(f"Unexpected /tournaments response type: {type(chunk)}")
            all_rows.extend(chunk)
            if len(chunk) < items_per_page:
                break
            page += 1
        return all_rows

    def fetch_intersections(self, tournament_id: int) -> list[dict[str, Any]]:
        data = self.get(f"/tournaments/{tournament_id}/intersections")
        if not isinstance(data, list):
            raise SystemExit(f"Unexpected intersections response type: {type(data)}")
        return data

    def fetch_results(self, tournament_id: int) -> list[dict[str, Any]]:
        params: list[tuple[str, str | int]] = [("includeTeamMembers", 1)]
        data = self.get(f"/tournaments/{tournament_id}/results", params=params)
        if not isinstance(data, list):
            raise SystemExit(f"Unexpected results response type: {type(data)}")
        return data


def intersection_ids_from_response(rows: list[dict[str, Any]]) -> list[int]:
    out: set[int] = set()
    for row in rows:
        tid = row.get("id")
        if tid is not None:
            out.add(int(tid))
    return sorted(out)


def extract_participant_ids(results: list[dict[str, Any]]) -> set[int]:
    players: set[int] = set()
    for row in results:
        members = row.get("teamMembers")
        if not isinstance(members, list):
            continue
        for m in members:
            if not isinstance(m, dict):
                continue
            pl = m.get("player")
            if isinstance(pl, dict) and pl.get("id") is not None:
                players.add(int(pl["id"]))
    return players


def resolve_seeds_by_substrings(
    client: RatingClient,
    substrings: list[str],
    date_end_after: str | None,
) -> tuple[dict[int, dict[str, Any]], dict[str, list[dict[str, Any]]], list[int]]:
    """Returns (seed_meta by id, matches_by_substring for warnings, ordered seed ids)."""
    seed_meta: dict[int, dict[str, Any]] = {}
    matches_by_substring: dict[str, list[dict[str, Any]]] = {}
    order: list[int] = []

    for sub in substrings:
        rows = client.fetch_tournaments_by_name(sub, date_end_after)
        matches_by_substring[sub] = [{"id": r.get("id"), "name": r.get("name")} for r in rows]
        for row in rows:
            tid = row.get("id")
            if tid is None:
                continue
            tid = int(tid)
            if tid not in seed_meta:
                seed_meta[tid] = {
                    "id": tid,
                    "name": row.get("name"),
                    "source_substrings": [],
                    "editor_surnames": editor_surnames_from_tournament(row),
                    "difficultyForecast": row.get("difficultyForecast"),
                }
                order.append(tid)
            if sub not in seed_meta[tid]["source_substrings"]:
                seed_meta[tid]["source_substrings"].append(sub)

    return seed_meta, matches_by_substring, order


def short_label_from_status(status: str) -> str:
    if status in ("played_listed", "played_via_intersection"):
        return "played"
    if status == "clear":
        return "clear"
    return status


def build_summary(
    substrings: list[str],
    matches_by_substring: dict[str, list[dict[str, Any]]],
    tournaments_out: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per tournaments-file line: id, name, status, editors, difficultyForecast."""
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
    for sub in substrings:
        matches = matches_by_substring.get(sub, [])
        if not matches:
            summary.append(
                {
                    "id": None,
                    "name": sub,
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


def build_warnings(matches_by_substring: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for sub, matches in matches_by_substring.items():
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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Check team players against listed CHGK tournaments and intersections.")
    p.add_argument("--players", required=True, help="Path to UTF-8 text file: one player id per line")
    p.add_argument("--tournaments", required=True, help="Path to UTF-8 text file: one name substring per line")
    p.add_argument("--base-url", default=DEFAULT_BASE, help=f"API base (default: {DEFAULT_BASE})")
    p.add_argument(
        "--date-end-after",
        default=None,
        metavar="YYYY-MM-DD",
        help="Optional: pass as dateEnd[strictly_after] when listing tournaments (omit for no date filter)",
    )
    p.add_argument("--verbose", action="store_true", help="Log each HTTP GET to stderr")
    p.add_argument(
        "--output",
        "-o",
        default="tourn_check_report.json",
        help="Write full JSON report to this path (default: %(default)s)",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print full JSON report to stdout (default is tab-separated short summary only)",
    )
    args = p.parse_args(argv)

    player_ids = load_player_ids(args.players)
    substrings = load_text_lines(args.tournaments)
    if not substrings:
        raise SystemExit(f"No tournament name lines in {args.tournaments}")

    date_cutoff: str | None = args.date_end_after
    team_ids = set(player_ids)

    client = RatingClient(args.base_url, verbose=args.verbose)

    seed_meta, matches_by_substring, seed_order = resolve_seeds_by_substrings(
        client, substrings, date_cutoff
    )

    intersections_by_seed: dict[str, list[int]] = {}
    all_tournament_ids: set[int] = set(seed_meta.keys())

    for tid in seed_order:
        inter_rows = client.fetch_intersections(tid)
        iids = intersection_ids_from_response(inter_rows)
        intersections_by_seed[str(tid)] = iids
        all_tournament_ids.update(iids)

    participants_by_tournament: dict[int, set[int]] = {}
    for tid in sorted(all_tournament_ids):
        results = client.fetch_results(tid)
        participants_by_tournament[tid] = extract_participant_ids(results)

    tournaments_out: list[dict[str, Any]] = []
    for tid in seed_order:
        meta = seed_meta[tid]
        iids = intersections_by_seed[str(tid)]
        seed_players = participants_by_tournament.get(tid, set())
        played_listed = bool(team_ids & seed_players)

        matching_intersection_ids: list[int] = []
        for iid in iids:
            if team_ids & participants_by_tournament.get(iid, set()):
                matching_intersection_ids.append(iid)

        played_intersection = bool(matching_intersection_ids)
        if played_listed:
            status = "played_listed"
        elif played_intersection:
            status = "played_via_intersection"
        else:
            status = "clear"

        listed_hits = sorted(team_ids & seed_players)
        inter_hits: dict[str, list[int]] = {}
        for iid in matching_intersection_ids:
            hp = sorted(team_ids & participants_by_tournament.get(iid, set()))
            inter_hits[str(iid)] = hp

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

    summary = build_summary(substrings, matches_by_substring, tournaments_out)

    report = {
        "input": {
            "player_ids": player_ids,
            "tournament_name_substrings": substrings,
            "date_end_strictly_after": date_cutoff,
            "base_url": args.base_url,
        },
        "intersections_by_seed": intersections_by_seed,
        "tournaments": tournaments_out,
        "warnings": build_warnings(matches_by_substring),
        "summary": summary,
    }

    with open(args.output, "w", encoding="utf-8") as out_f:
        json.dump(report, out_f, ensure_ascii=False, indent=2)
        out_f.write("\n")

    if args.print_json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        for line in format_summary_lines(summary):
            print(line)


if __name__ == "__main__":
    main()
