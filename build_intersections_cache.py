#!/usr/bin/env python3
"""Build seed → intersection ID cache from stalnuhhin async/online tours.

Intended for weekly GitHub Actions (and local runs)::

    python build_intersections_cache.py -o data/intersections_cache.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from stalnuhhin_tours import (
    DEFAULT_TOURS_URL,
    StalnuhhinError,
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
) -> dict:
    tours = fetch_tours(tours_url)
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

    return {
        "generated_at": utc_now_iso(),
        "source": tours_url,
        "base_url": base_url.rstrip("/"),
        "seed_count": len(by_seed),
        "by_seed": dict(sorted(by_seed.items(), key=lambda kv: int(kv[0]))),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "-o",
        "--output",
        default="data/intersections_cache.json",
        help="Output JSON path (default: data/intersections_cache.json)",
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
        payload = build_cache(
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
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {payload['seed_count']} seeds → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
