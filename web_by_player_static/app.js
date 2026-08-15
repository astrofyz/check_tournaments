const DEFAULT_BASE = "https://api.rating.chgk.net";
const ITEMS_PER_PAGE = 512;

function parseTextLines(raw) {
  const lines = [];
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    lines.push(line);
  }
  return lines;
}

function parsePlayerIds(text) {
  const ids = [];
  for (const line of parseTextLines(text)) {
    if (!/^\d+$/.test(line)) {
      throw new Error("Invalid player id (expected integer): " + line);
    }
    ids.push(Number(line));
  }
  if (ids.length === 0) throw new Error("No player ids in input");
  return ids;
}

function parseTournamentLines(text) {
  const lines = parseTextLines(text);
  if (lines.length === 0) throw new Error("No tournament lines in input");
  return lines;
}

/** True if the line is non-empty and every character is a decimal digit (tournament id). */
function tournamentLineIsSeedId(line) {
  return line.length > 0 && /^\d+$/.test(line);
}

/** Drop leading characters until the first Unicode letter or number. */
function stripLeadingUntilLetterOrDigit(s) {
  const idx = s.search(/[\p{L}\p{N}]/u);
  if (idx < 0) return "";
  return s.slice(idx);
}

async function tournamentsMatchingAllWords(base, words, dateEndStrictlyAfter) {
  const sets = [];
  const rowsById = new Map();
  for (const w of words) {
    const chunk = await fetchTournamentsByName(base, w, dateEndStrictlyAfter);
    const ids = new Set();
    for (const r of chunk) {
      if (r.id == null) continue;
      const tid = Number(r.id);
      ids.add(tid);
      if (!rowsById.has(tid)) rowsById.set(tid, r);
    }
    sets.push(ids);
  }
  let common = new Set(sets[0]);
  for (let i = 1; i < sets.length; i++) {
    common = new Set([...common].filter((id) => sets[i].has(id)));
  }
  return [...common]
    .sort((a, b) => a - b)
    .map((id) => rowsById.get(id))
    .filter(Boolean);
}

function editorSurnamesFromTournament(row) {
  const editors = row.editors;
  if (!Array.isArray(editors)) return [];
  const out = [];
  for (const ed of editors) {
    if (ed && typeof ed === "object" && ed.surname != null) {
      const s = String(ed.surname).trim();
      if (s) out.push(s);
    }
  }
  return out;
}

function joinUrl(base, path) {
  const b = base.replace(/\/+$/, "");
  const p = path.startsWith("/") ? path : "/" + path;
  return b + p;
}

async function apiGet(base, path, searchParams) {
  let url = joinUrl(base, path);
  if (searchParams && searchParams.toString()) {
    url += "?" + searchParams.toString();
  }
  let res = await fetch(url, { headers: { Accept: "application/json" } });
  if (res.status === 429) {
    const ra = res.headers.get("Retry-After");
    const waitMs = ra && /^\d+$/.test(ra) ? Number(ra) * 1000 : 2000;
    await new Promise((r) => setTimeout(r, waitMs));
    res = await fetch(url, { headers: { Accept: "application/json" } });
  }
  if (!res.ok) {
    const t = await res.text();
    throw new Error("HTTP " + res.status + " " + url + "\n" + t.slice(0, 500));
  }
  return res.json();
}

async function fetchTournamentItem(base, tournamentId) {
  const url = joinUrl(base, "/tournaments/" + tournamentId);
  let res = await fetch(url, { headers: { Accept: "application/json" } });
  if (res.status === 429) {
    await new Promise((r) => setTimeout(r, 2000));
    res = await fetch(url, { headers: { Accept: "application/json" } });
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    const t = await res.text();
    throw new Error("HTTP " + res.status + " " + url + "\n" + t.slice(0, 500));
  }
  const data = await res.json();
  return data && typeof data === "object" ? data : null;
}

async function fetchTournamentsByName(base, nameSubstring, dateEndStrictlyAfter) {
  const all = [];
  let page = 1;
  while (true) {
    const params = new URLSearchParams();
    params.append("name[]", nameSubstring);
    if (dateEndStrictlyAfter) {
      params.append("dateEnd[strictly_after]", dateEndStrictlyAfter);
    }
    params.set("page", String(page));
    params.set("itemsPerPage", String(ITEMS_PER_PAGE));
    const chunk = await apiGet(base, "/tournaments", params);
    if (!Array.isArray(chunk)) {
      throw new Error("Unexpected /tournaments response");
    }
    all.push(...chunk);
    if (chunk.length < ITEMS_PER_PAGE) break;
    page += 1;
  }
  return all;
}

async function fetchIntersections(base, tournamentId) {
  const data = await apiGet(base, "/tournaments/" + tournamentId + "/intersections");
  if (!Array.isArray(data)) throw new Error("Unexpected intersections response");
  return data;
}

async function fetchPlayerTournaments(base, playerId) {
  const data = await apiGet(base, "/players/" + playerId + "/tournaments/");
  if (!Array.isArray(data)) throw new Error("Unexpected player tournaments response");
  return data;
}

function intersectionIdsFromResponse(rows) {
  const out = new Set();
  for (const row of rows) {
    if (row && row.id != null) out.add(Number(row.id));
  }
  return Array.from(out).sort((a, b) => a - b);
}

function tournamentIdFromPlayerRow(row) {
  if (row.idtournament != null) return Number(row.idtournament);
  if (row.idTournament != null) return Number(row.idTournament);
  return null;
}

function shortLabelFromStatus(status) {
  if (status === "played_listed" || status === "played_via_intersection") return "played";
  if (status === "clear") return "clear";
  return status;
}

async function resolveSeedsMixed(base, lines, dateEndAfter) {
  const seedMeta = {};
  const matchesByLine = {};
  const seedOrder = [];
  const resolutionWarnings = [];

  for (const line of lines) {
    if (tournamentLineIsSeedId(line)) {
      const tidRaw = Number(line);
      const row = await fetchTournamentItem(base, tidRaw);
      if (!row || row.id == null) {
        matchesByLine[line] = [];
        continue;
      }
      const tid = Number(row.id);
      matchesByLine[line] = [{ id: tid, name: row.name }];
      if (seedMeta[tid] === undefined) {
        seedMeta[tid] = {
          id: tid,
          name: row.name,
          source_substrings: [],
          editor_surnames: editorSurnamesFromTournament(row),
          difficultyForecast: row.difficultyForecast,
        };
        seedOrder.push(tid);
      }
      if (!seedMeta[tid].source_substrings.includes(line)) {
        seedMeta[tid].source_substrings.push(line);
      }
    } else {
      const normalized = stripLeadingUntilLetterOrDigit(line);
      if (!normalized) {
        matchesByLine[line] = [];
        continue;
      }
      let rows = await fetchTournamentsByName(base, normalized, dateEndAfter);
      const words = normalized.split(/\s+/).filter(Boolean);
      if (rows.length === 0 && words.length >= 2) {
        rows = await tournamentsMatchingAllWords(base, words, dateEndAfter);
        if (rows.length > 0) {
          resolutionWarnings.push({
            type: "name_search_word_intersection",
            substring: line,
            normalized,
            words: words.slice(),
          });
        }
      }
      matchesByLine[line] = rows.map((r) => ({ id: r.id, name: r.name }));
      for (const trow of rows) {
        if (trow.id == null) continue;
        const tid = Number(trow.id);
        if (seedMeta[tid] === undefined) {
          seedMeta[tid] = {
            id: tid,
            name: trow.name,
            source_substrings: [],
            editor_surnames: editorSurnamesFromTournament(trow),
            difficultyForecast: trow.difficultyForecast,
          };
          seedOrder.push(tid);
        }
        if (!seedMeta[tid].source_substrings.includes(line)) {
          seedMeta[tid].source_substrings.push(line);
        }
      }
    }
  }

  return { seedMeta, matchesByLine, seedOrder, resolutionWarnings };
}

function buildSummary(lineKeys, matchesByLine, tournamentsOut) {
  const statusById = {};
  const extraById = {};
  for (const row of tournamentsOut) {
    if (row.id == null) continue;
    const tid = Number(row.id);
    statusById[tid] = shortLabelFromStatus(String(row.status || ""));
    extraById[tid] = {
      editor_surnames: Array.isArray(row.editor_surnames) ? row.editor_surnames.slice() : [],
      difficultyForecast: row.difficultyForecast,
    };
  }

  const summary = [];
  for (const key of lineKeys) {
    const matches = matchesByLine[key] || [];
    if (matches.length === 0) {
      summary.push({
        id: null,
        name: key,
        status: "not found",
        editor_surnames: [],
        difficultyForecast: null,
      });
      continue;
    }
    for (const m of matches) {
      if (m.id == null) continue;
      const tid = Number(m.id);
      const name = m.name;
      const display = name != null && String(name).trim() ? String(name).trim() : "(tournament id " + tid + ")";
      const ex = extraById[tid] || { editor_surnames: [], difficultyForecast: null };
      summary.push({
        id: tid,
        name: display,
        status: statusById[tid] !== undefined ? statusById[tid] : "clear",
        editor_surnames: ex.editor_surnames,
        difficultyForecast: ex.difficultyForecast,
      });
    }
  }
  return summary;
}

function buildWarnings(matchesByLine) {
  const warnings = [];
  for (const [sub, matches] of Object.entries(matchesByLine)) {
    if (matches.length > 1) {
      warnings.push({
        type: "ambiguous_name_match",
        substring: sub,
        count: matches.length,
        tournaments: matches,
      });
    }
    if (matches.length === 0) {
      warnings.push({ type: "no_match", substring: sub, tournaments: [] });
    }
  }
  return warnings;
}

async function runCheck(playerIds, tournamentLines, baseUrl, dateEndAfter) {
  const base = baseUrl.replace(/\/+$/, "");
  const teamIds = new Set(playerIds);

  const { seedMeta, matchesByLine, seedOrder, resolutionWarnings } = await resolveSeedsMixed(
    base,
    tournamentLines,
    dateEndAfter || null
  );

  const intersectionsBySeed = {};
  for (const tid of seedOrder) {
    const rows = await fetchIntersections(base, tid);
    intersectionsBySeed[String(tid)] = intersectionIdsFromResponse(rows);
  }

  const uniquePlayerIds = [...new Set(playerIds)];
  const tournamentsPerPlayer = {};
  for (const pid of uniquePlayerIds) {
    const rows = await fetchPlayerTournaments(base, pid);
    const tids = new Set();
    for (const r of rows) {
      const x = tournamentIdFromPlayerRow(r);
      if (x != null) tids.add(x);
    }
    tournamentsPerPlayer[pid] = tids;
  }

  const tournamentsOut = [];
  for (const tid of seedOrder) {
    const meta = seedMeta[tid];
    const iids = intersectionsBySeed[String(tid)];

    const listedHits = uniquePlayerIds.filter((p) => tournamentsPerPlayer[p].has(tid)).sort((a, b) => a - b);
    const playedListed = listedHits.length > 0;

    const matchingIntersectionIds = [];
    const interHits = {};
    for (const iid of iids) {
      const hp = uniquePlayerIds.filter((pl) => tournamentsPerPlayer[pl].has(iid)).sort((a, b) => a - b);
      if (hp.length > 0) {
        matchingIntersectionIds.push(iid);
        interHits[String(iid)] = hp;
      }
    }
    const playedIntersection = matchingIntersectionIds.length > 0;

    let status;
    if (playedListed) status = "played_listed";
    else if (playedIntersection) status = "played_via_intersection";
    else status = "clear";

    tournamentsOut.push({
      id: tid,
      name: meta.name,
      source_substrings: meta.source_substrings || [],
      editor_surnames: meta.editor_surnames || [],
      difficultyForecast: meta.difficultyForecast,
      intersection_ids: iids,
      played_listed: playedListed,
      played_intersection: playedIntersection,
      status,
      matching_players_listed: listedHits,
      matching_players_by_intersection_id: interHits,
      matching_intersection_ids: matchingIntersectionIds,
    });
  }

  return {
    summary: buildSummary(tournamentLines, matchesByLine, tournamentsOut),
    warnings: resolutionWarnings.concat(buildWarnings(matchesByLine)),
  };
}

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

function renderTable(container, rows) {
  let html =
    '<div class="table-wrap"><table class="result"><thead><tr>' +
    "<th>id</th><th>status</th><th>name</th><th>editors</th><th>difficultyForecast</th>" +
    "</tr></thead><tbody>";
  for (const row of rows) {
    const eds = (row.editor_surnames || []).join("; ");
    const df =
      row.difficultyForecast != null && row.difficultyForecast !== ""
        ? escapeHtml(row.difficultyForecast)
        : "";
    html += "<tr class=\"" + rowClass(row.status) + "\">";
    html += "<td>" + (row.id != null ? escapeHtml(row.id) : "") + "</td>";
    html += "<td>" + escapeHtml(row.status) + "</td>";
    html += "<td>" + escapeHtml(row.name) + "</td>";
    html += "<td>" + escapeHtml(eds) + "</td>";
    html += "<td>" + df + "</td>";
    html += "</tr>";
  }
  html += "</tbody></table></div>";
  container.innerHTML = html;
}

document.getElementById("go").addEventListener("click", async () => {
  const out = document.getElementById("out");
  const warn = document.getElementById("warn");
  out.innerHTML = "";
  warn.textContent = "";

  const playersText = document.getElementById("players").value;
  const tournamentsText = document.getElementById("tournaments").value;
  const dateRaw = document.getElementById("date").value.trim();
  const baseRaw = document.getElementById("base").value.trim() || DEFAULT_BASE;

  try {
    const playerIds = parsePlayerIds(playersText);
    const tournamentLines = parseTournamentLines(tournamentsText);
    const report = await runCheck(playerIds, tournamentLines, baseRaw, dateRaw || null);

    if (report.warnings.length > 0) {
      warn.textContent = report.warnings
        .map((w) => {
          let s = w.type + ": " + (w.substring || "");
          if (w.normalized && w.normalized !== w.substring) {
            s += " → " + w.normalized;
          }
          if (w.words && w.words.length) {
            s += " [" + w.words.join(", ") + "]";
          }
          return s;
        })
        .join("\n");
    }
    if (report.summary.length === 0) {
      out.innerHTML = "<p class=\"err\">No rows returned.</p>";
      return;
    }
    renderTable(out, report.summary);
  } catch (e) {
    out.innerHTML = "<p class=\"err\">" + escapeHtml(String(e.message || e)) + "</p>";
  }
});
