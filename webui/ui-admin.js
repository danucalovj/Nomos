/* Views: first-time setup, dashboard, agent management, project settings. */
"use strict";

const SETUP_COLORS = ["#d99a2b", "#85a8c7", "#63a878", "#b08ba8", "#a08cc0", "#82b0bd", "#bd9179", "#a8a8a8"];

/* Fetch the avatar catalog once per page; [] on failure (initials fallback). */
async function fetchAvatarCatalog() {
  try { return (await _req("GET", "/api/avatars")).avatars || []; }
  catch (e) { return []; }
}

/* Shared avatar picker grid: options + change callback → element. */
function avatarGrid(options, selected, onPick) {
  const grid = el("div", { class: "avatar-grid" });
  for (const opt of options) {
    const cell = el("button", {
      class: "av-opt" + (opt.id === selected ? " sel" : ""),
      title: opt.id,
      onclick: () => {
        grid.querySelectorAll(".av-opt").forEach((n) => n.classList.remove("sel"));
        cell.classList.add("sel");
        onPick(opt.id);
      },
    }, el("span", { class: "avatar s36" }, el("img", { src: opt.url, alt: opt.id })));
    grid.append(cell);
  }
  return grid;
}

Views.setup = async function () {
  let color = SETUP_COLORS[0];
  let avatar = "admin"; // the reserved operator's mark, preselected
  const aliasInput = el("input", { type: "text", placeholder: "e.g. john-doe", maxlength: 32 });
  const swatches = el("div", { class: "color-row" },
    SETUP_COLORS.map((c, i) => el("button", {
      class: "color-swatch" + (i === 0 ? " sel" : ""), style: `background:${c}`, title: c,
      onclick: (e) => {
        color = c;
        swatches.querySelectorAll(".color-swatch").forEach((s) => s.classList.remove("sel"));
        e.currentTarget.classList.add("sel");
      },
    })));

  const catalog = [{ id: "admin", url: "/avatars/admin.svg" }, ...await fetchAvatarCatalog()];
  const grid = avatarGrid(catalog, avatar, (id) => { avatar = id; });

  const submit = async () => {
    const alias = aliasInput.value.trim();
    if (!alias) { toast("Setup", "Choose an alias first.", "warn"); return; }
    try {
      const data = await API.completeSetup(alias, color, avatar);
      AC.state.setupComplete = true;
      AC.state.adminAlias = data.admin.alias;
      AC.state.adminColor = data.admin.color;
      AC.state.adminAvatar = data.admin.avatar || "";
      toast("Welcome", `You're set up as ${data.admin.alias}.`);
      location.hash = "#/";
      AC.renderTopbar();
      AC.route();
    } catch (e) { toast("Setup Failed", e.message, "error"); }
  };
  aliasInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });

  AC.setView(el("div", { class: "setup-wrap" },
    el("div", { class: "card" },
      el("h1", {}, "nomos"),
      el("div", { class: "sub" },
        "One-time setup: choose the alias and mark your agents will see when you speak. No login — this machine is the admin console."),
      el("div", { class: "field" }, el("label", {}, "Your Alias"), aliasInput),
      el("div", { class: "field" }, el("label", {}, "Display Color"), swatches),
      el("div", { class: "field" }, el("label", {}, "Your Mark"), grid),
      el("button", { class: "btn primary", style: "width:100%", onclick: submit }, "Create Admin Identity"))));
};

/* ---------- dashboard ---------- */
Views.dashboard = async function () {
  const data = await API.projects(true);
  AC.state.projects = data.items || [];
  const grid = el("div", { class: "grid cols-2" });
  const page = el("div", { class: "page" },
    el("div", { class: "spread" },
      el("h1", {}, "Projects"),
      el("button", { class: "btn primary", onclick: Views._createProjectModal }, "+ New Project")),
    AC.state.projects.length ? grid : el("div", { class: "empty" },
      el("div", { class: "glyph" }, "⌀"),
      el("div", { class: "e-title" }, "No Projects Yet"),
      el("p", {}, "Create the first project; agents join it through the API.")));
  AC.setView(page);

  for (const p of AC.state.projects) {
    const card = el("div", { class: "proj-card", onclick: () => location.hash = `#/p/${p.id}` },
      el("div", { class: "spread" },
        el("h3", {}, p.name),
        p.archived ? el("span", { class: "stat-pill" }, "archived") : null),
      el("div", { class: "muted small" }, p.description || "—"),
      el("div", { class: "stats" }, el("span", { class: "faint" }, "loading…")));
    grid.append(card);
    // Enrich each card lazily (counts, online agents, awaiting-human)
    (async () => {
      try {
        const [proj, agents, awaiting] = await Promise.all([
          API.project(p.id), API.agents(p.id),
          API.tickets(p.id, { status: "awaiting-human", limit: 200 }).catch(() => ({ items: [] })),
        ]);
        const list = agents.items || [];
        const online = list.filter((a) => !a.revoked && (a.online !== undefined ? a.online : isOnline(a.last_seen))).length;
        const c = proj.counts || {};
        const nAwait = (awaiting.items || []).length;
        // replaceChildren coerces null to the literal text "null" — filter.
        card.querySelector(".stats").replaceChildren(...[
          el("span", { class: "stat" }, el("b", {}, String(c.agents ?? "?")), " agents"),
          el("span", { class: "stat" }, el("b", {}, String(c.tickets ?? "?")), " tickets"),
          el("span", { class: "stat" }, el("b", {}, String(c.documents ?? "?")), " docs"),
          online ? el("span", { class: "stat-pill online" }, `${online} online`) : null,
          nAwait ? el("span", { class: "stat-pill awaiting", title: "Tickets Awaiting Your Input" }, `${nAwait} awaiting you`) : null,
        ].filter(Boolean));
      } catch (e) { /* card stays basic */ }
    })();
  }

  // Recent activity across projects (best-effort)
  const actBody = el("div", { class: "faint" }, "loading…");
  const act = el("div", { class: "card" }, el("h2", {}, "Recent Activity"), actBody);
  if (AC.state.projects.length) page.append(act);
  (async () => {
    const lines = [];
    await Promise.all(AC.state.projects.filter((p) => !p.archived).slice(0, 8).map(async (p) => {
      try {
        const a = await API.activity(p.id, { limit: 8 });
        for (const ev of (a.items || [])) lines.push({ p, ev });
      } catch (e) {}
    }));
    lines.sort((x, y) => (y.ev.created_at || "").localeCompare(x.ev.created_at || ""));
    if (!lines.length) { actBody.textContent = "No activity yet."; return; }
    actBody.classList.remove("faint");
    actBody.replaceChildren(...lines.slice(0, 15).map(({ p, ev }) =>
      el("div", { class: "activity-line" },
        el("span", { class: "t-chrome faint" }, fmtTime(ev.created_at)),
        el("a", { href: `#/p/${p.id}` }, p.name),
        el("span", { class: "muted" }, describeEvent(ev)))));
  })();
};

function describeEvent(ev) {
  const pl = ev.payload || {};
  const t = pl.ticket || {};
  const tno = pl.ticket_number ?? pl.number ?? t.number;
  switch (ev.type) {
    case "message": return `${pl.author || "?"}: ${(pl.body || "").slice(0, 80) || "(attachment)"}`;
    case "mention": return `${pl.by || "?"} mentioned someone`;
    case "reaction": return `${pl.actor || "?"} ${pl.reacted === false ? "removed :" + pl.emoji + ":" : "reacted :" + pl.emoji + ":"} ${AC.emoji(pl.emoji) || ""}`.trim();
    case "agent_updated": return `${(pl.agent && pl.agent.alias) || "an agent"} updated their profile`;
    case "ticket_created": return `ticket #${tno} created: ${pl.title || t.title || ""}`;
    case "ticket_updated": return `ticket #${tno} updated${t.status ? " → " + t.status : ""}`;
    case "ticket_comment": return `comment on #${tno}`;
    case "awaiting_human": return `ticket #${tno} awaits your input`;
    case "document_created": case "document_updated": return `doc "${pl.slug || pl.title || ""}" ${ev.type === "document_created" ? "created" : "updated"}`;
    case "agent_joined": return `${pl.alias || "an agent"} joined`;
    case "agent_revoked": return `${pl.alias || "agent"} key revoked`;
    case "agent_removed": return `${pl.alias || "agent"} removed`;
    case "typing": return ""; // ephemeral — never describes activity
    default: return ev.type.replace(/_/g, " ");
  }
}

/* Working-directory field with a server-backed Browse toggle (issue #27).
   Returns {row, panel}: the input+button row, and the inline browser panel.
   Clicking a directory descends into it and writes its path to the input. */
function dirField(input) {
  const panel = el("div", { class: "dir-browser hidden" });
  let open = false;
  const load = async (path) => {
    panel.replaceChildren(el("div", { class: "faint", style: "padding:8px" }, "loading…"));
    try {
      const d = await API.browseDirs(path || "");
      input.value = d.path;
      const rows = [];
      if (d.parent) {
        rows.push(el("button", { class: "dir-row", onclick: () => load(d.parent) }, ".."));
      }
      for (const c of d.dirs) {
        rows.push(el("button", {
          class: "dir-row" + (c.selectable ? "" : " blocked"),
          title: c.selectable ? c.path : "System path, not selectable",
          onclick: () => { if (c.selectable) load(c.path); },
        }, c.name));
      }
      panel.replaceChildren(
        el("div", { class: "dir-current" }, d.path),
        el("div", { class: "dir-list" },
          ...(rows.length ? rows : [el("div", { class: "faint", style: "padding:8px" }, "No subdirectories. This directory is selected.")])));
    } catch (e) {
      panel.replaceChildren(el("div", { class: "danger-text", style: "padding:8px" }, e.message));
    }
  };
  const btn = el("button", { class: "btn small", type: "button", onclick: () => {
    open = !open;
    panel.classList.toggle("hidden", !open);
    btn.textContent = open ? "Hide" : "Browse…";
    if (open) load(/^[/~]/.test(input.value.trim()) ? input.value.trim() : "");
  } }, "Browse…");
  return { row: el("div", { class: "row" }, input, btn), panel };
}

Views._createProjectModal = async function () {
  const name = el("input", { type: "text", placeholder: "Project name", maxlength: 100 });
  const desc = el("textarea", { rows: 3, placeholder: "Description (optional)" });
  const wdir = el("input", { type: "text", placeholder: "/absolute/path/to/working/directory (optional)", maxlength: 1024 });
  const wd = dirField(wdir);
  const okd = await modal({
    title: "New Project",
    body: el("div", {}, el("div", { class: "field" }, el("label", {}, "Name"), name),
      el("div", { class: "field" }, el("label", {}, "Description"), desc),
      el("div", { class: "field" }, el("label", {}, "Working Directory"), wd.row, wd.panel,
        el("div", { class: "hint" }, "AGENTS.md is copied there and agents discover it from the project."))),
    confirmText: "Create",
    onValidate: () => name.value.trim().length > 0,
  });
  if (!okd) return;
  try {
    const p = await API.createProject(name.value.trim(), desc.value.trim(), wdir.value.trim());
    toast("Project Created", p.name);
    location.hash = `#/p/${p.id}`;
  } catch (e) { toast("Create Failed", e.message, "error"); }
};

/* ---------- agent management ---------- */

/* Read-only view of an agent's scratchpad + todo list (issue #26). */
async function notesModal(pid, a) {
  const body = el("div", { class: "notes-view" }, el("div", { class: "faint" }, "loading…"));
  (async () => {
    try {
      const n = await API.agentNotes(pid, a.id);
      const todoRows = (n.todos || []).map((t) => el("tr", {},
        el("td", {}, t.text),
        el("td", {}, el("span", { class: statusClass(t.status) }, t.status)),
        el("td", { class: "t-chrome " + (t.priority === "high" ? "" : "faint") }, t.priority)));
      body.replaceChildren(...[
        el("div", { class: "t-micro" }, "Todo List"),
        (n.todos || []).length
          ? el("table", { class: "notes-todos" },
              el("thead", {}, el("tr", {}, ...["Item", "Status", "Priority"].map((h) => el("th", {}, h)))),
              el("tbody", {}, todoRows))
          : el("p", { class: "faint" }, "No todos."),
        el("div", { class: "t-micro", style: "margin-top:16px" }, "Scratchpad"),
        n.scratchpad && n.scratchpad.body
          ? renderMd(n.scratchpad.body)
          : el("p", { class: "faint" }, "Empty."),
        n.scratchpad && n.scratchpad.updated_at
          ? el("div", { class: "t-chrome faint", style: "margin-top:8px" },
              "updated " + fmtTime(n.scratchpad.updated_at))
          : null,
      ].filter(Boolean));
    } catch (e) {
      body.replaceChildren(el("p", { class: "danger-text" }, e.message));
    }
  })();
  await modal({ title: `Notes — ${a.alias}`, body, confirmText: "Close" });
}

/* List-style popup (issue #18): rows navigate on click and close the modal.
   `load` returns [{label parts..., hash}] entries. */
async function drilldownModal(title, load) {
  const body = el("div", { class: "drill-list" }, el("div", { class: "faint" }, "loading…"));
  const closeAll = () => document.getElementById("modal-root").replaceChildren();
  (async () => {
    try {
      const entries = await load();
      body.replaceChildren(...(entries.length ? entries.map((e) =>
        el("button", {
          class: "drill-row",
          onclick: () => { closeAll(); location.hash = e.hash; },
        }, ...e.cells))
        : [el("p", { class: "faint" }, "Nothing yet.")]));
    } catch (e) { body.replaceChildren(el("p", { class: "faint" }, e.message)); }
  })();
  await modal({ title, body, confirmText: "Close" });
}

Views.agentsAdmin = async function () {
  const pid = AC.state.pid;
  const data = await API.agents(pid);
  const agents = data.items || [];
  const rows = agents.map((a) => {
    const online = a.online !== undefined ? a.online : isOnline(a.last_seen);
    return el("tr", {},
      el("td", {}, el("span", { class: "row" },
        AC.avatarEl(a, 36),
        el("span", {},
          el("div", { class: "t-emphasis" }, a.alias),
          el("div", { class: "t-chrome faint" }, a.role || "—")))),
      el("td", {}, a.status_text || a.status_emoji
        ? el("span", { class: "muted small" },
            a.status_emoji ? AC.emoji(a.status_emoji) + " " : "", a.status_text || "")
        : el("span", { class: "faint" }, "—")),
      el("td", {}, a.revoked ? el("span", { class: "stat-pill" }, "revoked")
        : online ? el("span", { class: "stat-pill online" }, "online")
        : el("span", { class: "stat-pill" }, a.status || "idle")),
      el("td", { class: "muted small" }, timeAgo(a.last_seen)),
      el("td", { class: "muted small" }, fmtTime(a.created_at)),
      el("td", {},
        el("div", { class: "agent-actions" },
          el("button", {
            class: "btn small", title: `Audit trail for ${a.alias}`,
            onclick: () => { location.hash = `#/p/${pid}/audit/${encodeURIComponent(a.alias)}`; },
          }, "Audit"),
          el("button", {
            class: "btn small", title: `Tickets ${a.alias} opened or holds`,
            onclick: () => drilldownModal(`Tickets — ${a.alias}`, async () => {
              const [opened, held] = await Promise.all([
                API.tickets(pid, { reporter: a.alias, limit: 100 }),
                API.tickets(pid, { assignee: a.alias, limit: 100 }),
              ]);
              const seen = new Set();
              const entries = [];
              for (const [tag, list] of [["opened", opened.items || []], ["assigned", held.items || []]]) {
                for (const t of list) {
                  if (seen.has(t.number)) continue;
                  seen.add(t.number);
                  entries.push({
                    hash: `#/p/${pid}/tickets/${t.number}`,
                    cells: [
                      el("span", { class: "t-chrome faint" }, `#${t.number}`),
                      el("span", { class: "drill-title" }, t.title),
                      el("span", { class: statusClass(t.status) }, t.status),
                      el("span", { class: "t-chrome faint" }, tag),
                    ],
                  });
                }
              }
              return entries;
            }),
          }, "Tickets"),
          el("button", {
            class: "btn small", title: `Documents ${a.alias} created`,
            onclick: () => drilldownModal(`Documents — ${a.alias}`, async () => {
              const docs = await API.documents(pid, { author: a.alias, limit: 100 });
              return (docs.items || []).map((d) => ({
                hash: `#/p/${pid}/docs/${encodeURIComponent(d.slug)}`,
                cells: [
                  el("span", { class: "t-micro", style: "flex:none" }, "MD"),
                  el("span", { class: "drill-title" }, d.title),
                  el("span", { class: "t-chrome faint" }, `rev ${d.current_revision}`),
                ],
              }));
            }),
          }, "Docs"),
          el("button", {
            class: "btn small", title: `${a.alias}'s scratchpad and todo list`,
            onclick: () => notesModal(pid, a),
          }, "Notes"),
          el("span", { class: "act-sep" }),
          !a.revoked ? el("button", {
            class: "btn small danger",
            onclick: async () => {
              if (!await modal({ title: `Revoke ${a.alias}'s API Key?`, body: el("p", { class: "muted" }, "The agent immediately loses access. Its history remains."), confirmText: "Revoke Key", danger: true })) return;
              try { await API.revokeAgent(pid, a.id); toast("Key Revoked", a.alias); Views.agentsAdmin(); }
              catch (e) { toast("Error", e.message, "error"); }
            },
          }, "Revoke Key") : null,
          el("button", {
            class: "btn small",
            onclick: async () => {
              if (!await modal({ title: `Remove ${a.alias} From the Project?`, body: el("p", { class: "muted" }, "Removes membership and mentions; messages remain under the alias."), confirmText: "Remove", danger: true })) return;
              try { await API.removeAgent(pid, a.id); toast("Agent Removed", a.alias); Views.agentsAdmin(); }
              catch (e) { toast("Error", e.message, "error"); }
            },
          }, "Remove"))));
  });
  AC.setView(el("div", { class: "page wide" },
    el("h1", {}, "Agents"),
    el("div", { class: "card scroll-x", style: "padding:0" },
      agents.length ? el("table", { class: "agents-table" },
        el("thead", {}, el("tr", {}, ...["Agent", "Status", "Presence", "Last seen", "Joined", ""].map((h) => el("th", {}, h)))),
        el("tbody", {}, rows))
        : el("div", { class: "empty" },
            el("div", { class: "glyph" }, "⌀"),
            el("div", { class: "e-title" }, "No Agents Yet"),
            el("p", {}, `Agents join via POST /api/projects/${pid}/agents/join — see /api/docs.`))),
    agents.length ? el("p", { class: "hint" },
      "Agents join via POST /api/projects/" + pid + "/agents/join — see /api/docs.") : null));
  AC.on("agent_joined", () => Views.agentsAdmin(), "agents");
  AC.on("agent_updated", () => Views.agentsAdmin(), "agents-upd");
};

/* ---------- project settings ---------- */
Views.settings = async function () {
  const pid = AC.state.pid;
  const p = await API.project(pid);
  const name = el("input", { type: "text", value: p.name, maxlength: 100 });
  const desc = el("textarea", { rows: 3 }, p.description || "");
  const statuses = el("input", { type: "text", value: (p.settings.ticket_statuses || []).join(", ") });
  const wdir = el("input", { type: "text", value: p.working_dir || "", maxlength: 1024,
    placeholder: "/absolute/path (AGENTS.md is copied there)" });
  const swd = dirField(wdir);
  const sysMsgs = el("input", { type: "checkbox" });
  sysMsgs.checked = p.settings.system_messages_enabled !== false;

  const save = async () => {
    try {
      const patch = {
        name: name.value.trim(),
        description: desc.value,
        ticket_statuses: statuses.value.split(",").map((s) => s.trim()).filter(Boolean),
        system_messages_enabled: sysMsgs.checked,
      };
      if (wdir.value.trim() !== (p.working_dir || "")) {
        patch.working_dir = wdir.value.trim(); // empty clears it
      }
      await API.updateProject(pid, patch);
      toast("Saved", "Project settings updated.");
      AC.state.project = await API.project(pid);
      const st = await API.projectStatuses(pid);
      AC.state.statuses = st.statuses || [];
      AC.renderTopbar();
    } catch (e) { toast("Save Failed", e.message, "error"); }
  };

  // --- admin identity (alias / color / mark) ---
  let idColor = AC.state.adminColor || SETUP_COLORS[0];
  let idAvatar = AC.state.adminAvatar || "admin";
  const idAlias = el("input", { type: "text", value: AC.state.adminAlias || "", maxlength: 32 });
  const idSwatches = el("div", { class: "color-row" },
    SETUP_COLORS.map((c) => el("button", {
      class: "color-swatch" + (c === idColor ? " sel" : ""), style: `background:${c}`, title: c,
      onclick: (e) => {
        idColor = c;
        idSwatches.querySelectorAll(".color-swatch").forEach((s) => s.classList.remove("sel"));
        e.currentTarget.classList.add("sel");
      },
    })));
  const idGridBox = el("div", {}, el("span", { class: "faint small" }, "loading marks…"));
  (async () => {
    const catalog = [{ id: "admin", url: "/avatars/admin.svg" }, ...await fetchAvatarCatalog()];
    idGridBox.replaceChildren(avatarGrid(catalog, idAvatar, (id) => { idAvatar = id; }));
  })();
  const saveIdentity = async () => {
    try {
      const data = await _req("PATCH", "/api/admin/identity", {
        alias: idAlias.value.trim() || undefined, color: idColor, avatar: idAvatar,
      });
      AC.state.adminAlias = data.admin.alias;
      AC.state.adminColor = data.admin.color;
      AC.state.adminAvatar = data.admin.avatar || "";
      toast("Identity Updated", `Speaking as ${data.admin.alias}.`);
      AC.renderTopbar();
      AC.renderSidebar();
    } catch (e) { toast("Identity Update Failed", e.message, "error"); }
  };

  const exportBtn = el("button", {
    class: "btn",
    onclick: async () => {
      exportBtn.disabled = true; exportBtn.textContent = "Exporting…";
      try {
        const blob = await API.exportProject(pid);
        const url = URL.createObjectURL(blob);
        const a = el("a", { href: url, download: `nomos-project-${pid}-export.tar.gz` });
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
        toast("Export Ready", "Tarball downloaded.");
      } catch (e) { toast("Export Failed", e.message, "error"); }
      exportBtn.disabled = false; exportBtn.textContent = "Export Project Data";
    },
  }, "Export Project Data");

  AC.setView(el("div", { class: "page narrow" },
    el("h1", {}, "Project Settings"),
    el("div", { class: "card" },
      el("div", { class: "rail-title" }, "Project"),
      el("div", { class: "field" }, el("label", {}, "Name"), name),
      el("div", { class: "field" }, el("label", {}, "Description"), desc),
      el("div", { class: "field" }, el("label", {}, "Ticket statuses (comma-separated, order = workflow)"), statuses),
      el("div", { class: "field" }, el("label", {}, "Working Directory"), swd.row, swd.panel),
      el("label", { class: "checkline" }, sysMsgs, " Post system messages to #general on ticket/agent events"),
      el("button", { class: "btn primary", onclick: save }, "Save Settings")),
    el("div", { class: "card" },
      el("div", { class: "rail-title" }, "Your Identity"),
      el("div", { class: "field" }, el("label", {}, "Alias"), idAlias),
      el("div", { class: "field" }, el("label", {}, "Display Color"), idSwatches),
      el("div", { class: "field" }, el("label", {}, "Mark"), idGridBox),
      el("button", { class: "btn primary", onclick: saveIdentity }, "Update identity")),
    el("div", { class: "card" },
      el("div", { class: "rail-title" }, "Data"),
      el("div", { class: "row" }, exportBtn)),
    el("div", { class: "card", style: "border-color:var(--danger)" },
      el("div", { class: "rail-title", style: "color:var(--danger)" }, "Danger Zone"),
      el("div", { class: "row", style: "gap:12px" },
        p.archived
          ? el("button", { class: "btn", onclick: async () => {
              try { await API.unarchiveProject(pid); toast("Unarchived", p.name); AC.state.project = await API.project(pid); Views.settings(); }
              catch (e) { toast("Error", e.message, "error"); }
            } }, "Unarchive Project")
          : el("button", { class: "btn", onclick: async () => {
              try { await API.archiveProject(pid); toast("Archived", p.name + " is now read-only."); AC.state.project = await API.project(pid); Views.settings(); }
              catch (e) { toast("Error", e.message, "error"); }
            } }, "Archive Project (Read-Only)"),
        el("button", {
          class: "btn danger",
          onclick: async () => {
            const confirmInput = el("input", { type: "text", placeholder: p.name });
            const okd = await modal({
              title: "Delete Project Permanently?",
              body: el("div", {},
                el("p", { class: "muted" }, "This deletes ALL channels, messages, DMs, tickets, documents, attachments, and agent keys for this project. It cannot be undone."),
                el("div", { class: "field" }, el("label", {}, `Type the project name (${p.name}) to confirm`), confirmInput)),
              confirmText: "Delete Everything", danger: true,
              onValidate: () => confirmInput.value === p.name,
            });
            if (!okd) return;
            try {
              await API.deleteProject(pid);
              toast("Project Deleted", p.name);
              location.hash = "#/";
            } catch (e) { toast("Delete Failed", e.message, "error"); }
          },
        }, "Delete Project…")))));
};
