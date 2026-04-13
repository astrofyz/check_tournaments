"""FastAPI app: UI + POST /check (uses tourn_check_web.run_check)."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from tourn_check_web_by_player import (
    DEFAULT_BASE,
    RatingAPIError,
    parse_player_ids_from_text,
    parse_tournament_lines_from_text,
    run_check,
)

app = FastAPI(title="Tournament check", description="CHGK tournament overlap checker")


class CheckRequest(BaseModel):
    players: str = Field(..., description="One player id per line")
    tournaments: str = Field(..., description="Name substring or numeric id per line")
    date_end_after: str | None = Field(None, description="Optional YYYY-MM-DD for name search only")
    base_url: str | None = Field(None, description="Override API base (default rating.chgk.net)")


class SummaryRow(BaseModel):
    id: int | None = None
    status: str
    name: str
    editor_surnames: list[str] = Field(default_factory=list)
    difficultyForecast: Any = None


class CheckResponse(BaseModel):
    summary_rows: list[SummaryRow]
    warnings: list[dict]


@app.post("/check", response_model=CheckResponse)
def check(body: CheckRequest) -> CheckResponse:
    try:
        player_ids = parse_player_ids_from_text(body.players)
        tournament_lines = parse_tournament_lines_from_text(body.tournaments)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    base = (body.base_url or os.environ.get("TOURN_CHECK_BASE_URL") or DEFAULT_BASE).rstrip("/")
    try:
        report = run_check(
            player_ids,
            tournament_lines,
            base_url=base,
            date_end_after=body.date_end_after,
            verbose=False,
        )
    except RatingAPIError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    rows = [SummaryRow.model_validate(item) for item in report["summary"]]
    return CheckResponse(summary_rows=rows, warnings=report["warnings"])


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tournament check</title>
  <style>
    :root { font-family: system-ui, sans-serif; }
    body { max-width: 52rem; margin: 1rem auto; padding: 0 1rem; }
    label { display: block; margin-top: 0.75rem; font-weight: 600; }
    textarea { width: 100%; min-height: 8rem; font-family: ui-monospace, monospace; font-size: 0.9rem; }
    input[type="text"] { width: 100%; max-width: 12rem; }
    button { margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; }
    #out { margin-top: 1rem; font-size: 0.9rem; min-height: 1rem; }
    #warn { margin-top: 0.75rem; color: #8a5a00; font-size: 0.9rem; }
    .err { color: #a00; padding: 0.75rem; background: #fff0f0; border-radius: 6px; }
    .table-wrap { overflow-x: auto; border-radius: 6px; border: 1px solid #ccc; }
    table.result { width: 100%; border-collapse: collapse; }
    table.result th, table.result td { border-bottom: 1px solid #ddd; padding: 0.5rem 0.65rem; text-align: left; vertical-align: top; }
    table.result th { background: #f0f0f0; font-weight: 600; }
    table.result tr:last-child td { border-bottom: none; }
    table.result tr.row-clear { background: #e8f5e9; }
    table.result tr.row-clear:hover { background: #c8e6c9; }
    table.result tr.row-played { background: #ffebee; }
    table.result tr.row-played:hover { background: #ffcdd2; }
    table.result tr.row-other { background: #f5f5f5; }
    table.result tr.row-other:hover { background: #eeeeee; }
    table.result th.sortable {
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }
    table.result th.sortable:hover { background: #e0e0e0; }
    table.result th.sortable .ind { font-size: 0.75em; opacity: 0.6; margin-left: 0.25rem; }
  </style>
</head>
<body>
  <h1>Tournament check</h1>
  <p>Player IDs (one per line). Tournaments: <strong>name substring</strong> per line, or a <strong>numeric id</strong> per line</p>
  <label for="players">Players</label>
  <textarea id="players" placeholder="12345&#10;67890"></textarea>
  <label for="tournaments">Tournaments</label>
  <textarea id="tournaments" placeholder="Substring or id per line"></textarea>
  <label for="date">dateEnd strictly after (optional)</label>
  <input id="date" type="text" placeholder="YYYY-MM-DD">
  <div><button type="button" id="go">Run check</button></div>
  <div id="warn"></div>
  <div id="out"></div>
  <script>
    const out = document.getElementById("out");
    const warn = document.getElementById("warn");
    let lastRows = [];
    let sortState = { key: null, dir: 1 };

    const COLS = [
      { key: "id", label: "id" },
      { key: "status", label: "status" },
      { key: "name", label: "name" },
      { key: "editors", label: "editors" },
      { key: "difficultyForecast", label: "difficultyForecast" }
    ];

    function escapeHtml(s) {
      if (s == null || s === "") return "";
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }
    function rowClass(status) {
      if (status === "clear") return "row-clear";
      if (status === "played") return "row-played";
      return "row-other";
    }
    function editorsText(row) {
      return (row.editor_surnames || []).join("; ");
    }
    function cmpRows(a, b, col) {
      if (col === "id") {
        const an = a.id != null ? a.id : null;
        const bn = b.id != null ? b.id : null;
        if (an == null && bn == null) return 0;
        if (an == null) return 1;
        if (bn == null) return -1;
        return an - bn;
      }
      if (col === "status") {
        return String(a.status || "").localeCompare(String(b.status || ""), undefined, { sensitivity: "base" });
      }
      if (col === "name") {
        return String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base" });
      }
      if (col === "editors") {
        return editorsText(a).localeCompare(editorsText(b), undefined, { sensitivity: "base" });
      }
      if (col === "difficultyForecast") {
        const va = a.difficultyForecast;
        const vb = b.difficultyForecast;
        if ((va == null || va === "") && (vb == null || vb === "")) return 0;
        if (va == null || va === "") return 1;
        if (vb == null || vb === "") return -1;
        const na = Number(va);
        const nb = Number(vb);
        if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
        return String(va).localeCompare(String(vb), undefined, { sensitivity: "base" });
      }
      return 0;
    }
    function sortedRows(rows, key, dir) {
      const copy = rows.slice();
      copy.sort((a, b) => dir * cmpRows(a, b, key));
      return copy;
    }
    function renderTable(rows) {
      let h = "<div class='table-wrap'><table class='result'><thead><tr>";
      for (const c of COLS) {
        const active = sortState.key === c.key;
        const ind = active ? (sortState.dir === 1 ? "<span class='ind'>▲</span>" : "<span class='ind'>▼</span>") : "";
        h += "<th scope='col' class='sortable' data-sort='" + c.key + "' title='Sort by column'>" + c.label + ind + "</th>";
      }
      h += "</tr></thead><tbody>";
      for (const row of rows) {
        const cls = rowClass(row.status);
        const idCell = row.id != null ? escapeHtml(row.id) : "";
        const eds = escapeHtml(editorsText(row));
        const df = row.difficultyForecast != null && row.difficultyForecast !== "" ? escapeHtml(row.difficultyForecast) : "";
        h += "<tr class='" + cls + "'>";
        h += "<td>" + idCell + "</td>";
        h += "<td>" + escapeHtml(row.status) + "</td>";
        h += "<td>" + escapeHtml(row.name) + "</td>";
        h += "<td>" + eds + "</td>";
        h += "<td>" + df + "</td>";
        h += "</tr>";
      }
      h += "</tbody></table></div>";
      out.innerHTML = h;
    }
    out.addEventListener("click", (ev) => {
      const th = ev.target.closest("th.sortable");
      if (!th || !out.contains(th)) return;
      const key = th.getAttribute("data-sort");
      if (!key) return;
      if (sortState.key === key) {
        sortState.dir = -sortState.dir;
      } else {
        sortState.key = key;
        sortState.dir = 1;
      }
      renderTable(sortedRows(lastRows, sortState.key, sortState.dir));
    });

    document.getElementById("go").onclick = async () => {
      out.innerHTML = "";
      warn.textContent = "";
      lastRows = [];
      sortState = { key: null, dir: 1 };
      const body = {
        players: document.getElementById("players").value,
        tournaments: document.getElementById("tournaments").value,
        date_end_after: document.getElementById("date").value.trim() || null
      };
      try {
        const r = await fetch("/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          const d = data.detail;
          const msg = typeof d === "string" ? d : (Array.isArray(d) ? d.map(x => x.msg || JSON.stringify(x)).join("; ") : JSON.stringify(d));
          out.innerHTML = "<span class='err'>" + (msg || r.statusText || "Error") + "</span>";
          return;
        }
        if (data.warnings && data.warnings.length) {
          warn.textContent = data.warnings.map(w => {
            let s = w.type + ": " + (w.substring || "");
            if (w.normalized && w.normalized !== w.substring) s += " → " + w.normalized;
            if (w.words && w.words.length) s += " [" + w.words.join(", ") + "]";
            return s;
          }).join("\\n");
        }
        const rows = data.summary_rows || [];
        if (!rows.length) {
          out.innerHTML = "<p class='err'>No rows returned.</p>";
          return;
        }
        lastRows = rows;
        renderTable(rows);
      } catch (e) {
        out.innerHTML = "<span class='err'>" + String(e) + "</span>";
      }
    };
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML
