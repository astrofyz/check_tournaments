# `tourn_check.py` reference

CLI tool that queries [rating.chgk.net](https://api.rating.chgk.net): it finds tournaments by name substrings, loads question-intersections and results (with team members), and checks whether given player IDs appear on the listed tournament or on any intersecting tournament.

## How to run

Install dependency (in your environment):

```bash
pip install -r requirements.txt
```

**Default run** — full JSON is written to `tourn_check_report.json` (change with `-o` / `--output`). **Stdout** is the short tab-separated table only (`id`, `name`, `status`, editor surnames, `difficultyForecast`).

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt
```

**Custom JSON path:**

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt -o my_report.json
```

**Print full JSON to stdout** (short lines are not printed in this mode; the file is still written):

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt --print-json
```

**Optional date filter** — only tournaments whose `dateEnd` is strictly after the given calendar day:

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt --date-end-after 2026-04-01
```

**Capture only the short table** (JSON still goes to the output file):

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt > summary.tsv
```

**Debug HTTP** — log each GET URL to stderr:

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt --verbose
```

**Another API base** (rare):

```bash
python3 tourn_check.py --players players.txt --tournaments tournaments.txt --base-url https://api.rating.chgk.net
```

## Web UI (`web_app.py`, `tourn_check_web.py`)

The original CLI remains [`tourn_check.py`](tourn_check.py). The web stack is a **separate copy** in [`tourn_check_web.py`](tourn_check_web.py) (name substrings **or** digit-only lines as tournament ids) plus [**FastAPI**](https://fastapi.tiangolo.com/) in [`web_app.py`](web_app.py), run under [**Uvicorn**](https://www.uvicorn.org/) (HTTP server).

### Run locally

On macOS with **Homebrew Python**, system-wide `pip install` is blocked (PEP 668). Use a **virtual environment** in the project:

```bash
cd /path/to/tourn_check
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn web_app:app --reload --host 127.0.0.1 --port 8000
```

After `activate`, `python` and `pip` point at `.venv`; FastAPI and Uvicorn are found there. To run without activating:  
`.venv/bin/uvicorn web_app:app --reload --host 127.0.0.1 --port 8000`

Open `http://127.0.0.1:8000/`, paste players and tournaments, click **Run check**. The page calls `POST /check` on the same host (no CORS setup needed). Interactive API docs: `http://127.0.0.1:8000/docs`.

### Deploy on Railway

1. Push this repo to GitHub (or connect the repo in Railway).
2. **New Project** → deploy from the repo; ensure the **root directory** is the project root (where `web_app.py` and `requirements.txt` live).
3. Set the **start command** (or use the included [`Procfile`](Procfile)):  
   `uvicorn web_app:app --host 0.0.0.0 --port $PORT`  
   Railway injects `$PORT`; binding to `0.0.0.0` is required so their proxy can reach the process.
4. **Generate domain** under the service settings and open the HTTPS URL.

Optional: set env var `TOURN_CHECK_BASE_URL` if you ever need a non-default rating API base.

### Result table (web UI)

The page loads **`summary_rows`** from `POST /check` as JSON, then **renders HTML in the browser**: a `<table>` with one row per summary entry. **Sorting** is **client-side only**: the full row list is kept in `lastRows`; clicking a `<th class="sortable">` updates `sortState` (column + ascending/descending), runs `Array.sort` with a small comparator (`id` numeric with nulls last, `difficultyForecast` numeric when possible, otherwise string `localeCompare`), and **rebuilds the table** from the sorted copy. No extra server round-trips. Row colours stay tied to `status` (`clear` / `played` / other).

### Why checks are slow & how to profile

Most time is usually **waiting on the rating API**, not Python or the table. For each run the server roughly:

1. Resolves seeds (name search and/or `GET /tournaments/{id}` per line).
2. **`GET /tournaments/{seed}/intersections`** once per seed.
3. **`GET /tournaments/{id}/results?includeTeamMembers=1`** **once per distinct tournament id** (every seed plus every intersection target), **sequentially**.

So total latency grows with the **size of the union of tournaments**; many intersections ⇒ many result fetches.

**Quick phase timings (stderr):** run Uvicorn with:

```bash
TOURN_CHECK_TIMING=1 .venv/bin/uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Each `/check` logs segments from [`tourn_check_web.run_check`](tourn_check_web.py) (resolve seeds, intersections, results loop, build report). The results line tells you how many HTTP calls that step represents.

**Python profiler:** `python -m cProfile -o prof.out -m uvicorn web_app:app --host 127.0.0.1 --port 8000` then inspect `prof.out` with `snakeviz` or `pstats`. That confirms time is in `requests` / HTTP.

**Browser:** DevTools → **Network** → select the `check` request: **Waiting (TTFB)** is almost all server-side work for this app.

**Web vs `tourn_check.py` on your laptop:** the heavy steps are the same (same number of API calls for the same inputs). If the **web app feels slower**, common causes are:

1. **Where Python runs** — e.g. **Railway** (or another cloud region) is farther from `api.rating.chgk.net` than your home network, so each HTTP round trip costs more. Compare **local** Uvicorn vs **local** CLI with the same pasted text to see this.
2. **`uvicorn --reload`** — extra file watching; use `--reload` only while editing.
3. **Different inputs** — the web form and your `.txt` files must list the same tournaments/players to compare fairly.

**Parallel HTTP (web engine only):** [`tourn_check_web.run_check`](tourn_check_web.py) defaults to **parallel** `GET`s for intersections and results (`ThreadPoolExecutor`, **one `requests.Session` per worker thread**). Tune with:

- `TOURN_CHECK_PARALLEL=0` — sequential behavior, closest to the original CLI.
- `TOURN_CHECK_PARALLEL_WORKERS=8` (default 8, max 32) — concurrency cap.

If the API returns **429**, lower workers or set `TOURN_CHECK_PARALLEL=0`.

### Alternate strategy: player tournament list ([`tourn_check_web_by_player.py`](tourn_check_web_by_player.py))

Instead of `GET /tournaments/{id}/results?includeTeamMembers=1` for **every** tournament in seeds ∪ intersections, this module calls **`GET /players/{player_id}/tournaments/`** once per **distinct** input player. The API returns the **full** list in one response (even for very active players). Each row includes `idtournament`. Overlap is computed the same way logically: listed seed id and each intersection id are checked against those per-player tournament sets.

- **Usually faster when** there are **few players** and **many** distinct tournaments in seeds ∪ intersections (typical intersection-heavy checks).
- **Usually slower when** there are **many** distinct players (one large GET per player) or payloads are huge to transfer/parse.

**Compare wall time** (same `players` / `tournaments` files as the CLI):

```bash
python3 benchmark_overlap.py --players .smoke_players.txt --tournaments .smoke_tournaments.txt
```

Use `TOURN_CHECK_TIMING=1` with either module’s `run_check` to see phase breakdown on stderr.

### Input file formats

- **`players.txt`**: one numeric player ID per line; UTF-8; empty lines and `#` comments ignored.
- **`tournaments.txt`**: one name **substring** per line (API search in tournament title); UTF-8; same comment rules.

## Functions (what each does)

| Function / method | Role |
|-------------------|------|
| `editor_surnames_from_tournament(row)` | Reads `row["editors"]` as a list and returns each editor’s `surname` as strings (for summary and full JSON). |
| `load_text_lines(path)` | Reads a UTF-8 file, returns non-empty non-comment lines (stripped). |
| `load_player_ids(path)` | Uses `load_text_lines`, parses each line as an integer player ID; exits if none or invalid. |
| `RatingClient.__init__` | Stores base URL, timeout, verbose flag; creates a `requests.Session` with `Accept: application/json`. |
| `RatingClient._url(path)` | Joins base URL and path into a full URL string. |
| `RatingClient.get(path, params)` | Performs GET with optional query params; on 429, waits (`Retry-After` or 2s) and retries once; raises on non-success HTTP. |
| `RatingClient.fetch_tournaments_by_name(...)` | Paginates `GET /tournaments` with `name[]` and optional `dateEnd[strictly_after]`; returns a flat list of tournament objects. |
| `RatingClient.fetch_intersections(tournament_id)` | `GET /tournaments/{id}/intersections` — tournaments that share questions with the seed. |
| `RatingClient.fetch_results(tournament_id)` | `GET /tournaments/{id}/results?includeTeamMembers=1` — result rows with `teamMembers` / `player.id`. |
| `intersection_ids_from_response(rows)` | From intersection API array, collects unique tournament `id`s, sorted. |
| `extract_participant_ids(results)` | Walks every results row’s `teamMembers` and collects nested `player.id` values. |
| `resolve_seeds_by_substrings(...)` | For each substring, fetches matching tournaments; builds `seed_meta` (per seed: name, sources, `editor_surnames`, `difficultyForecast`), `matches_by_substring` (for warnings/summary), and `seed_order`. |
| `short_label_from_status(status)` | Maps full status to short label: `played_listed` / `played_via_intersection` → `played`, `clear` → `clear`. |
| `build_summary(...)` | Builds the `summary` array: one entry per tournaments-file line (or per match when multiple tournaments hit one line), with `id`, `name`, `status`, `editor_surnames`, `difficultyForecast`. |
| `_tsv_cell(value)` | Escapes tabs/newlines for safe TSV cells in the short stdout table. |
| `format_summary_lines(summary)` | Turns summary dicts into tab-separated lines for stdout (default mode). |
| `build_warnings(matches_by_substring)` | Adds `ambiguous_name_match` and `no_match` warning objects for the JSON report. |
| `main()` | Parses CLI, loads files, runs client + logic; writes full JSON to `--output`, prints short TSV to stdout unless `--print-json`. |

## Short summary columns (default stdout)

1. **id** — tournament id (empty if `not found`).
2. **name** — API title or search substring when not found.
3. **status** — `played`, `clear`, or `not found`.
4. **editor_surnames** — `editors[i].surname` joined with `; ` (from the tournament list payload).
5. **difficultyForecast** — API field of the same name when present.

Editor names and difficulty come from the **`/tournaments` search response** for each seed (not re-fetched per tournament).
