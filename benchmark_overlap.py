#!/usr/bin/env python3
"""Compare wall time: tourn_check_web (results per tournament) vs tourn_check_web_by_player."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from tourn_check_web import parse_player_ids_from_text, parse_tournament_lines_from_text, run_check as run_via_results
from tourn_check_web_by_player import run_check as run_via_player_tournaments


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--players", required=True, help="UTF-8 file: one player id per line")
    p.add_argument("--tournaments", required=True, help="UTF-8 file: tournament lines")
    args = p.parse_args()

    players_text = Path(args.players).read_text(encoding="utf-8")
    tournaments_text = Path(args.tournaments).read_text(encoding="utf-8")
    pids = parse_player_ids_from_text(players_text)
    tlines = parse_tournament_lines_from_text(tournaments_text)

    for label, fn in (
        ("player_tournaments_api (tourn_check_web_by_player)", run_via_player_tournaments),
        ("results_per_tournament (tourn_check_web)", run_via_results)
    ):
        t0 = time.perf_counter()
        fn(pids, tlines)
        print(f"{label}: {time.perf_counter() - t0:.3f}s")


if __name__ == "__main__":
    main()
