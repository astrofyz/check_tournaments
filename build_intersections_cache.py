#!/usr/bin/env python3
"""Build seed → intersection ID cache and a tours snapshot from stalnuhhin.

Intended for weekly GitHub Actions (and local runs)::

    python build_intersections_cache.py \\
      -o data/intersections_cache.json \\
      --tours-snapshot data/tours_snapshot.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from stalnuhhin_tours import (
    DEFAULT_TOURS_SNAPSHOT_PATH,
    DEFAULT_TOURS_URL,
    StalnuhhinError,
    build_tours_snapshot_payload,
    fetch_tours,
    filter_async_online_all,
    utc_now_iso,
)
from tourn_check_web_by_player import (
    DEFAULT_BASE,
    RatingAPIError,
    RatingClient,
    _parallel_workers,
    _thread_local_client,
    intersection_ids_from_response,
)


def build_cache(
    *,
    tours_url: str = DEFAULT_TOURS_URL,
    base_url: str = DEFAULT_BASE,
    workers: int | None = None,
    verbose: bool = False,
) -> tuple[dict, dict]:
    """Return ``(intersections_payload, tours_snapshot_payload)``."""
    tours = fetch_tours(tours_url)
    snapshot = build_tours_snapshot_payload(tours, source=tours_url)
    async_tours = filter_async_online_all(tours)
    seed_ids = [int(t["id"]) for t in async_tours if t.get("id") is not None]
    n_workers = workers if workers is not None else _parallel_workers(len(seed_ids))
    client = RatingClient(base_url, verbose=verbose)
    timeout = client.timeout
    by_seed: dict[str, list[int]] = {}

    def _job(tid: int) -> tuple[int, list[int]]:
        c = _thread_local_client(base_url, timeout, verbose)
        rows = c.fetch_intersections(tid)
        return tid, intersection_ids_from_response(rows)

    if n_workers > 1 and len(seed_ids) > 1:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = [pool.submit(_job, tid) for tid in seed_ids]
            done = 0
            for fut in as_completed(futures):
                tid, iids = fut.result()
                by_seed[str(tid)] = iids
                done += 1
                if verbose or done % 25 == 0 or done == len(seed_ids):
                    print(f"intersections {done}/{len(seed_ids)}", file=sys.stderr)
    else:
        for i, tid in enumerate(seed_ids, 1):
            rows = client.fetch_intersections(tid)
            by_seed[str(tid)] = intersection_ids_from_response(rows)
            if verbose or i % 25 == 0 or i == len(seed_ids):
                print(f"intersections {i}/{len(seed_ids)}", file=sys.stderr)

    intersections = {
        "generated_at": utc_now_iso(),
        "source": tours_url,
        "base_url": base_url.rstrip("/"),
        "seed_count": len(by_seed),
        "by_seed": dict(sorted(by_seed.items(), key=lambda kv: int(kv[0]))),
    }
    return intersections, snapshot


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "-o",
        "--output",
        default="data/intersections_cache.json",
        help="Intersections JSON path (default: data/intersections_cache.json)",
    )
    p.add_argument(
        "--tours-snapshot",
        default=DEFAULT_TOURS_SNAPSHOT_PATH,
        help=f"Tours snapshot JSON path (default: {DEFAULT_TOURS_SNAPSHOT_PATH})",
    )
    p.add_argument("--tours-url", default=DEFAULT_TOURS_URL)
    p.add_argument(
        "--base-url",
        default=(os.environ.get("TOURN_CHECK_BASE_URL") or DEFAULT_BASE).rstrip("/"),
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel intersection workers (default: auto / TOURN_CHECK_PARALLEL_WORKERS)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    try:
        intersections, snapshot = build_cache(
            tours_url=args.tours_url,
            base_url=args.base_url,
            workers=args.workers,
            verbose=args.verbose,
        )
    except (StalnuhhinError, RatingAPIError) as e:
        print(str(e), file=sys.stderr)
        return 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(intersections, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {intersections['seed_count']} seeds → {out}", file=sys.stderr)

    snap_path = Path(args.tours_snapshot)
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {snapshot['tour_count']} tours snapshot → {snap_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
