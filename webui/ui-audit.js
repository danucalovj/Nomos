/* Audit tab (issue #17): governance trail — self-reports + file-monitor
   observations + platform mirror in one reverse-chron timeline. Filters,
   expandable rows with unified-diff viewer, per-actor coverage, chain
   verification, JSONL/CSV export, admin watch management.
   Amber = unattributed / divergence only. */
"use strict";

const AUDIT_ACTIONS = [
  "file_edit", "file_create", "file_delete", "command",
  "test_run", "decision", "research", "other",
];

/* Middle-truncate long paths for the target cell (full value in title). */
function auditTruncate(s, head, tail) {
  s = String(s || "");
  if (s.length <= head + tail + 1) return s;
  return s.slice(0, head) + "…" + s.slice(-tail);
}

/* Render unified-diff text into the existing .diff-view language. Pre-split
   text lines go in as text nodes — no HTML interpretation anywhere. */
function renderUnifiedDiff(text) {
  const wrap = el("div", { class: "diff-view" });
  const lines = String(text || "").split("\n");
  const MAX = 400;
  for (const line of lines.slice(0, MAX)) {
    let cls = "same";
    if (line.startsWith("+++") || line.startsWith("---")) cls = "hunk";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    wrap.append(el("div", { class: "diff-line " + cls }, line));
  }
  if (lines.length > MAX) {
    wrap.append(el("div", { class: "diff-line hunk" }, `… ${lines.length - MAX} more lines (see export)`));
  }
  return wrap;
}

function auditActorCell(rec) {
  if (rec.actor_type === "monitor") {
    return el("span", { class: "a-actor" },
      el("span", { class: "a-glyph", title: "File monitor" }, "⌁"),
      el("span", { class: "a-name faint" }, "monitor"));
  }
  if (rec.actor_type === "platform") {
    return el("span", { class: "a-actor" },
      el("span", { class: "a-glyph", title: "Platform" }, "⚙"),
      el("span", { class: "a-name faint" }, "platform"));
  }
  return el("span", { class: "a-actor" },
    AC.avatarEl(rec.actor, 20),
    el("span", { class: "a-name" }, rec.actor));
}

Views.audit = async function () {
  const pid = AC.state.pid;
  // Deep-linkable actor filter: #/p/{pid}/audit/{alias} (Agents-tab
  // drill-down, issue #18).
  const hashParts = location.hash.replace(/^#\//, "").split("/");
  const presetActor = hashParts[2] === "audit" && hashParts[3]
    ? decodeURIComponent(hashParts[3]) : "";
  const filters = { actor: presetActor, action: "", source: "", target: "" };
  let oldestId = null;
  let hasMore = false;
  let loading = false;
  const rows = new Map(); // audit id -> row node

  /* ---------- header ---------- */
  const verifyResult = el("span", { class: "t-chrome faint" });
  const verifyBtn = el("button", {
    class: "btn", onclick: async () => {
      verifyBtn.disabled = true;
      verifyResult.textContent = "verifying…";
      verifyResult.className = "t-chrome faint";
      try {
        const v = await _req("GET", `/api/projects/${pid}/audit/verify`);
        if (v.ok) {
          verifyResult.textContent = `chain verified · ${v.checked} records`;
          verifyResult.className = "t-chrome a-verify-ok";
        } else {
          verifyResult.textContent = `DIVERGENCE at #${v.first_divergence}`;
          verifyResult.className = "t-chrome a-verify-bad";
        }
      } catch (e) {
        verifyResult.textContent = e.message;
        verifyResult.className = "t-chrome faint";
      }
      verifyBtn.disabled = false;
    },
  }, "Verify Chain");

  const exportBtn = (format) => el("button", {
    class: "btn", onclick: async (ev) => {
      const b = ev.currentTarget;
      b.disabled = true;
      try {
        const res = await fetch(`/api/projects/${pid}/audit/export?format=${format}`);
        if (!res.ok) throw new Error(`export failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = el("a", { href: url, download: `audit-project-${pid}.${format}` });
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        toast("Export Ready", `Audit trail downloaded (${format.toUpperCase()}).`);
      } catch (e) { toast("Export Failed", e.message, "error"); }
      b.disabled = false;
    },
  }, format === "jsonl" ? "Export JSONL" : "Export CSV");

  /* ---------- watch panel (admin console — always shown) ---------- */
  const watchBody = el("div", { class: "faint" }, "loading…");
  const watchCard = el("div", { class: "card" },
    el("div", { class: "rail-title" }, "File Monitor"), watchBody);

  async function renderWatch() {
    let w = null;
    try {
      const d = await _req("GET", `/api/projects/${pid}/audit/watch`);
      w = d.watch;
    } catch (e) {
      watchBody.replaceChildren(el("span", { class: "faint" }, e.message));
      return;
    }
    if (w) {
      watchBody.replaceChildren(
        el("div", { class: "a-watch-row" },
          el("span", { class: "a-watch-dot" + (w.active ? " on" : "") }),
          el("span", { class: "mono", title: w.path }, auditTruncate(w.path, 18, 34)),
          el("span", { class: "t-chrome faint" },
            `${w.files ?? "?"} files · last scan ${w.last_scan ? timeAgo(w.last_scan) : "—"}`),
          el("span", { class: "spacer" }),
          el("button", {
            class: "btn small", onclick: async () => {
              if (!await modal({
                title: "Stop Watching This Directory?",
                body: el("p", { class: "muted" }, "Existing observations stay in the trail; new changes will no longer be observed."),
                confirmText: "Remove Watch", danger: true,
              })) return;
              try { await _req("DELETE", `/api/projects/${pid}/audit/watch`); renderWatch(); }
              catch (e) { toast("Watch Removal Failed", e.message, "error"); }
            },
          }, "Remove")));
    } else {
      const input = el("input", { type: "text", placeholder: "/absolute/path/to/working/directory" });
      watchBody.replaceChildren(
        el("div", { class: "a-watch-row" },
          input,
          el("button", {
            class: "btn primary", onclick: async () => {
              const path = input.value.trim();
              if (!path) { toast("Watch", "Enter an absolute directory path.", "warn"); return; }
              try {
                await _req("POST", `/api/projects/${pid}/audit/watch`, { path });
                toast("Watch Registered", "Baseline scan runs silently; changes appear from now on.");
                renderWatch();
              } catch (e) { toast("Watch Registration Failed", e.message, "error"); }
            },
          }, "Watch Directory")),
        el("p", { class: "hint-sans" },
          "The monitor observes file changes out of band. Changes nobody self-reports surface as unattributed anomalies."));
    }
  }

  /* ---------- coverage ---------- */
  const coverageStrip = el("div", { class: "a-coverage" });
  async function renderCoverage() {
    try {
      // Shape: {actors: [{actor, self_reports}], observed, correlated,
      // unattributed} — per-actor cards for reporting, one monitor card for
      // observations (FS events carry no per-agent attribution).
      const d = await _req("GET", `/api/projects/${pid}/audit/coverage`);
      const cards = (d.actors || []).map((c) =>
        el("div", { class: "cov-card" },
          el("div", { class: "cov-actor" },
            AC.avatarEl(c.actor, 20),
            el("span", { class: "t-label" }, c.actor)),
          el("div", { class: "cov-stats t-chrome" },
            el("span", {}, `${c.self_reports ?? 0} reported`))));
      if ((d.observed ?? 0) > 0 || cards.length) {
        cards.push(
          el("div", { class: "cov-card" },
            el("div", { class: "cov-actor" },
              el("span", { class: "a-glyph" }, "⌁"),
              el("span", { class: "t-label" }, "monitor")),
            el("div", { class: "cov-stats t-chrome" },
              el("span", {}, `${d.observed ?? 0} observed`),
              el("span", {}, `${d.correlated ?? 0} correlated`),
              el("span", { class: (d.unattributed || 0) > 0 ? "cov-warn" : "faint" },
                `${d.unattributed ?? 0} unattributed`))));
      }
      coverageStrip.replaceChildren(...cards);
    } catch (e) { coverageStrip.replaceChildren(); }
  }
  const coverageRefresh = debounce(renderCoverage, 600);

  /* ---------- filter bar ---------- */
  const actorSel = el("select", {},
    el("option", { value: "" }, "Any Actor"),
    el("option", { value: AC.state.adminAlias || "admin" }, (AC.state.adminAlias || "admin") + " (admin)"),
    AC.state.agents.map((a) => el("option", { value: a.alias }, a.alias)),
    el("option", { value: "monitor" }, "monitor"),
    el("option", { value: "platform" }, "platform"));
  const actionSel = el("select", {}, el("option", { value: "" }, "Any Action"),
    AUDIT_ACTIONS.map((a) => el("option", { value: a }, a)));
  const sourceSel = el("select", {}, el("option", { value: "" }, "Any Source"),
    ["self_report", "monitor", "platform"].map((s) => el("option", { value: s }, s)));
  const targetInput = el("input", { type: "text", placeholder: "target contains…" });
  if (presetActor) actorSel.value = presetActor;
  actorSel.onchange = () => { filters.actor = actorSel.value; reload(); };
  actionSel.onchange = () => { filters.action = actionSel.value; reload(); };
  sourceSel.onchange = () => { filters.source = sourceSel.value; reload(); };
  targetInput.oninput = debounce(() => { filters.target = targetInput.value.trim(); reload(); }, 350);

  /* ---------- trail ---------- */
  const listEl = el("div", { class: "a-list" });
  const moreBtn = el("button", { class: "btn small hidden", style: "margin: 12px 0 0" }, "Load More");
  moreBtn.onclick = () => loadPage();
  const trailCard = el("div", { class: "card", style: "padding: 0" }, listEl);

  function passesFilters(rec) {
    if (filters.actor && rec.actor !== filters.actor) return false;
    if (filters.action && rec.action !== filters.action) return false;
    if (filters.source && rec.source !== filters.source) return false;
    if (filters.target && !(rec.target || "").toLowerCase().includes(filters.target.toLowerCase())) return false;
    return true;
  }

  function detailPanel(rec) {
    const parts = [];
    parts.push(el("div", { class: "a-d-summary" }, rec.summary || ""));
    let detailObj = null;
    try { detailObj = rec.detail ? JSON.parse(rec.detail) : null; } catch (e) {}
    if (detailObj && Object.keys(detailObj).length) {
      parts.push(el("pre", { class: "a-d-json" }, JSON.stringify(detailObj, null, 2)));
    }
    if (rec.diff) parts.push(renderUnifiedDiff(rec.diff));
    const meta = el("div", { class: "a-d-meta t-chrome faint" });
    if (rec.correlated_id != null) {
      meta.append(el("a", {
        href: "#", onclick: (e) => {
          e.preventDefault();
          const target = rows.get(rec.correlated_id);
          if (target) {
            target.scrollIntoView({ block: "center" });
            target.classList.add("flash");
            setTimeout(() => target.classList.remove("flash"), 2000);
          } else toast("Not Loaded", `Record #${rec.correlated_id} is further down the trail.`, "warn");
        },
      }, `correlates #${rec.correlated_id}`), " · ");
    }
    meta.append(el("span", { title: rec.entry_hash || "" }, `hash ${(rec.entry_hash || "").slice(0, 12)}…`));
    parts.push(meta);
    return el("div", { class: "a-detail" }, parts);
  }

  function buildRow(rec) {
    // `claimed` is server-resolved in BOTH correlation directions (the link
    // lives on whichever row landed second); fall back to correlated_id for
    // live SSE records that predate a later claim.
    const unattributed = rec.source === "monitor"
      && !(rec.claimed ?? (rec.correlated_id != null));
    const row = el("div", {
      class: "a-row" + (unattributed ? " unattributed" : ""),
      "data-aid": String(rec.id),
    },
      el("span", { class: "a-time" }, fmtTime(rec.created_at)),
      auditActorCell(rec),
      el("span", { class: "a-action" }, rec.action),
      el("span", { class: "a-target", title: rec.target || "" }, auditTruncate(rec.target, 14, 28)),
      el("span", { class: "a-summary" }, rec.summary || ""),
      el("span", { class: "a-src" }, rec.source));
    let open = null;
    row.addEventListener("click", () => {
      if (open) { open.remove(); open = null; row.classList.remove("expanded"); return; }
      open = detailPanel(rec);
      row.after(open);
      row.classList.add("expanded");
    });
    rows.set(rec.id, row);
    return row;
  }

  function queryString(beforeId) {
    const p = new URLSearchParams();
    if (filters.actor) p.set("actor", filters.actor);
    if (filters.action) p.set("action", filters.action);
    if (filters.source) p.set("source", filters.source);
    if (filters.target) p.set("target", filters.target);
    if (beforeId) p.set("before_id", String(beforeId));
    p.set("limit", "60");
    return p.toString();
  }

  let loadGen = 0; // bumped on filter change: in-flight responses go stale
  async function loadPage() {
    if (loading) return;
    loading = true;
    moreBtn.disabled = true;
    const gen = loadGen;
    try {
      const d = await _req("GET", `/api/projects/${pid}/audit?${queryString(oldestId)}`);
      if (gen !== loadGen) {
        // Filters changed while this request was in flight: drop the stale
        // rows and immediately load the current filter's page (issue #29).
        loading = false;
        loadPage();
        return;
      }
      const items = d.items || [];
      for (const rec of items) {
        listEl.append(buildRow(rec));
        oldestId = oldestId == null ? rec.id : Math.min(oldestId, rec.id);
      }
      hasMore = !!d.has_more;
      moreBtn.classList.toggle("hidden", !hasMore);
      if (!listEl.children.length) {
        listEl.replaceChildren(el("div", { class: "empty" },
          el("div", { class: "glyph" }, "⌁"),
          el("div", { class: "e-title" }, "No Audit Records"),
          el("p", {}, "Agents self-report their work here, and the file monitor observes the working directory once a watch is registered.")));
      }
    } catch (e) {
      if (gen !== loadGen) {
        // Same stale-recovery as the success path: the filter changed while
        // this request failed, so load the CURRENT filter instead of
        // leaving the pane empty until the next user action.
        loading = false;
        loadPage();
        return;
      }
      listEl.replaceChildren(el("div", { class: "empty" }, el("p", {}, e.message)));
    }
    loading = false;
    moreBtn.disabled = false;
  }

  function reload() {
    loadGen++;
    rows.clear();
    oldestId = null;
    listEl.replaceChildren();
    if (!loading) loadPage();
  }

  /* ---------- assemble ---------- */
  const page = el("div", { class: "page" },
    el("div", { class: "spread" },
      el("h1", { class: "page-title" }, "Audit"),
      el("div", { class: "a-header-actions" }, verifyResult, verifyBtn, exportBtn("jsonl"), exportBtn("csv"))),
    watchCard,
    coverageStrip,
    el("div", { class: "filter-bar" }, actorSel, actionSel, sourceSel, targetInput),
    trailCard,
    moreBtn);
  AC.setView(page);

  // Auto-load next page when the reader nears the bottom of the trail.
  page.addEventListener("scroll", debounce(() => {
    if (hasMore && page.scrollTop + page.clientHeight > page.scrollHeight - 240) loadPage();
  }, 150));

  renderWatch();
  renderCoverage();
  reload();

  /* ---------- live ---------- */
  AC.on("audit", (rec) => {
    if (!rec || rec.id == null || rows.has(rec.id)) return;
    if (!passesFilters(rec)) return;
    const empty = listEl.querySelector(".empty");
    if (empty) empty.remove();
    listEl.prepend(buildRow(rec));
    coverageRefresh();
  }, "audit-live");
  AC.on("audit_anomaly", () => coverageRefresh(), "audit-anom");
};
