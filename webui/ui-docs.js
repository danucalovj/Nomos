/* Views: document repository (list, rendered view, revisions + diff, edit
   with optimistic concurrency) and full-text search. */
"use strict";

/* ---------- document list ---------- */
Views.docs = async function () {
  const pid = AC.state.pid;
  const data = await API.documents(pid, { limit: 200 });
  const items = data.items || [];
  AC.setView(el("div", { class: "page narrow" },
    el("div", { class: "spread" },
      el("h1", {}, "Documents"),
      el("button", { class: "btn primary", onclick: newDocModal }, "+ New Document")),
    items.length
      ? el("div", { class: "card", style: "padding:0" }, items.map((d) =>
          el("div", { class: "doc-list-item", onclick: () => location.hash = `#/p/${pid}/docs/${encodeURIComponent(d.slug)}` },
            el("span", { class: "t-micro faint" }, "MD"),
            el("span", { class: "t-emphasis", style: "flex:1" }, d.title),
            el("span", { class: "t-chrome faint" }, "rev " + d.current_revision + " · " + timeAgo(d.updated_at)))))
      : el("div", { class: "empty" },
          el("div", { class: "glyph" }, "⌀"),
          el("div", { class: "e-title" }, "No Documents Yet"),
          el("p", {}, "Agents and you share versioned markdown here."))));

  function newDocModal() {
    const title = el("input", { type: "text", placeholder: "Document title" });
    const slug = el("input", { type: "text", placeholder: "slug (optional, auto from title)" });
    modal({
      title: "New Document",
      body: el("div", {},
        el("div", { class: "field" }, el("label", {}, "Title"), title),
        el("div", { class: "field" }, el("label", {}, "Slug"), slug)),
      confirmText: "Create", onValidate: () => title.value.trim().length > 0,
    }).then(async (okd) => {
      if (!okd) return;
      try {
        const d = await API.createDocument(pid, {
          title: title.value.trim(), slug: slug.value.trim() || undefined, body: "# " + title.value.trim() + "\n",
        });
        location.hash = `#/p/${pid}/docs/${encodeURIComponent(d.slug)}/edit`;
      } catch (e) { toast("Create Failed", e.message, "error"); }
    });
  }
  AC.on("document_created", () => Views.docs());
  AC.on("document_updated", () => Views.docs());
};

/* ---------- document view (rendered + revision history/diff) ---------- */
Views.docView = async function (slug) {
  const pid = AC.state.pid;
  let doc;
  try { doc = await API.document(pid, slug); }
  catch (e) { toast("Not Found", e.message, "warn"); location.hash = `#/p/${pid}/docs`; return; }

  const revData = await API.documentRevisions(pid, slug).catch(() => ({ items: [] }));
  const revisions = (revData.items || []).sort((a, b) => b.revision - a.revision);
  const contentBox = el("div", { class: "card" });
  const revList = el("div", { class: "card", style: "padding:0" });
  let shownRev = doc.revision || doc.current_revision;

  const renderContent = (body, revNo) => {
    contentBox.replaceChildren(
      el("div", { class: "spread", style: "margin-bottom:8px" },
        el("span", { class: "t-chrome faint" }, `revision ${revNo}${revNo === doc.current_revision ? " (current)" : ""}`)),
      renderMd(body));
  };

  const showRevision = async (rev) => {
    shownRev = rev.revision;
    try {
      const d = await API.document(pid, slug, rev.revision);
      renderContent(d.body, rev.revision);
      highlightSel();
    } catch (e) { toast("Error", e.message, "error"); }
  };

  const showDiff = async (rev) => {
    // Diff selected revision against the previous one
    const prev = revisions.find((r) => r.revision === rev.revision - 1);
    try {
      const [newer, older] = await Promise.all([
        API.document(pid, slug, rev.revision),
        prev ? API.document(pid, slug, prev.revision) : Promise.resolve({ body: "" }),
      ]);
      contentBox.replaceChildren(
        el("div", { class: "t-chrome faint", style: "margin-bottom:8px" },
          `rev ${prev ? prev.revision : "∅"} → rev ${rev.revision} · ${rev.author} · ${fmtTime(rev.created_at)}`),
        renderDiff(older.body, newer.body));
      shownRev = rev.revision;
      highlightSel();
    } catch (e) { toast("Error", e.message, "error"); }
  };

  const highlightSel = () => {
    revList.querySelectorAll(".rev-item").forEach((n) =>
      n.classList.toggle("sel", Number(n.dataset.rev) === shownRev));
  };

  const shareToChannel = async () => {
    const channels = AC.state.channels || [];
    if (!channels.length) { toast("Share", "No channels to share into.", "warn"); return; }
    const sel = el("select", {}, channels.map((c) =>
      el("option", { value: c.id }, "#" + c.name)));
    const note = el("input", { type: "text", placeholder: "Say something about it (optional)" });
    const okd = await modal({
      title: "Share Document to a Channel",
      body: el("div", {},
        el("div", { class: "field" }, el("label", {}, "Channel"), sel),
        el("div", { class: "field" }, el("label", {}, "Message"), note)),
      confirmText: "Share",
    });
    if (!okd) return;
    try {
      await API.postMessage(pid, Number(sel.value), { body: note.value.trim(), doc_slug: slug });
      toast("Shared", `${doc.title} → ${sel.selectedOptions[0].textContent}`);
    } catch (e) { toast("Share Failed", e.message, "error"); }
  };

  revList.replaceChildren(...revisions.map((r) => el("div", {
    class: "rev-item", "data-rev": r.revision, onclick: () => showRevision(r),
  },
    el("b", { class: "mono" }, "r" + r.revision),
    el("span", { style: "flex:1" }, r.title || ""),
    el("span", { class: "muted small" }, r.author),
    el("span", { class: "faint small" }, fmtTime(r.created_at)),
    el("button", { class: "btn small", onclick: (e) => { e.stopPropagation(); showDiff(r); } }, "diff"))));

  AC.setView(el("div", { class: "page narrow" },
    el("div", { class: "row", style: "gap:12px;margin-bottom:16px" },
      el("a", { href: `#/p/${pid}/docs`, class: "muted" }, "← documents"),
      el("span", { class: "t-display", style: "flex:1" }, doc.title),
      el("button", { class: "btn", onclick: shareToChannel }, "Share…"),
      el("button", { class: "btn primary", onclick: () => location.hash = `#/p/${pid}/docs/${encodeURIComponent(slug)}/edit` }, "Edit")),
    contentBox,
    el("div", { class: "card", style: "padding:0" },
      el("div", { class: "rail-title", style: "padding:12px 16px 0" }, "Revisions"),
      revList)));
  renderContent(doc.body, doc.current_revision);
  highlightSel();
  AC.on("document_updated", (pl) => { if (pl.slug === slug) Views.docView(slug); });
};

/* ---------- document edit (optimistic concurrency) ---------- */
Views.docEdit = async function (slug) {
  const pid = AC.state.pid;
  let doc;
  try { doc = await API.document(pid, slug); }
  catch (e) { toast("Not Found", e.message, "warn"); location.hash = `#/p/${pid}/docs`; return; }

  const title = el("input", { type: "text", value: doc.title });
  const ta = el("textarea", { rows: 24, class: "input mono" }, doc.body || "");
  let baseRevision = doc.current_revision;
  const baseNote = el("span", { class: "t-chrome faint" }, `editing from revision ${baseRevision}`);
  const conflictBox = el("div", {});

  const save = async () => {
    try {
      const d = await API.saveDocument(pid, slug, {
        title: title.value.trim() || doc.title, body: ta.value, base_revision: baseRevision,
      });
      toast("Saved", `${d.title} → revision ${d.current_revision}`);
      location.hash = `#/p/${pid}/docs/${encodeURIComponent(slug)}`;
    } catch (e) {
      if (e.status === 409) {
        const cur = e.extra || {};
        const serverBody = cur.current_body !== undefined ? cur.current_body : "(fetch failed)";
        const serverRev = cur.current_revision;
        conflictBox.replaceChildren(el("div", { class: "card", style: "border-color:var(--amber)" },
          el("div", { class: "rail-title", style: "color:var(--amber-bright)" },
            "Conflict — revision " + serverRev + " landed while you edited"),
          el("p", { class: "muted small" }, "Left: current server version. Right: your edit. Merge manually into your editor, then rebase and save."),
          el("div", { class: "conflict-cols" },
            el("div", {}, el("div", { class: "t-chrome faint" }, "server (rev " + serverRev + ")"),
              el("pre", { class: "mono small" }, serverBody)),
            el("div", {}, el("div", { class: "t-chrome faint" }, "yours (unsaved)"),
              el("pre", { class: "mono small" }, ta.value))),
          el("div", { class: "row", style: "margin-top:12px" },
            el("button", {
              class: "btn primary small", onclick: () => {
                baseRevision = serverRev;
                baseNote.textContent = `rebased onto revision ${baseRevision} — resolve differences in the editor, then save`;
                conflictBox.replaceChildren();
              },
            }, "Rebase onto rev " + serverRev + " (keep my text)"),
            el("button", {
              class: "btn small", onclick: () => {
                ta.value = serverBody;
                baseRevision = serverRev;
                baseNote.textContent = `editing from revision ${baseRevision}`;
                conflictBox.replaceChildren();
              },
            }, "Discard mine, take server version"))));
        conflictBox.scrollIntoView({ behavior: "smooth" });
      } else toast("Save Failed", e.message, "error");
    }
  };

  AC.setView(el("div", { class: "page narrow" },
    el("div", { class: "row", style: "gap:12px;margin-bottom:16px" },
      el("a", { href: `#/p/${pid}/docs/${encodeURIComponent(slug)}`, class: "muted" }, "← back"),
      el("span", { class: "t-display" }, "Edit document")),
    el("div", { class: "card" },
      el("div", { class: "field" }, el("label", {}, "Title"), title),
      el("div", { class: "field" }, el("label", {}, "Body (markdown)"), ta),
      el("div", { class: "spread" }, baseNote,
        el("div", { class: "row" },
          el("button", { class: "btn", onclick: () => location.hash = `#/p/${pid}/docs/${encodeURIComponent(slug)}` }, "Cancel"),
          el("button", { class: "btn primary", onclick: save }, "Save New Revision")))),
    conflictBox));
};

/* ---------- search ---------- */
Views.search = async function () {
  const pid = AC.state.pid;
  const q = el("input", { type: "text", placeholder: "Search — supports from:alias and in:#channel" });
  const typeSel = el("select", {},
    el("option", { value: "" }, "everything"),
    ["messages", "tickets", "comments", "documents"].map((t) => el("option", { value: t }, t)));
  const chanSel = el("select", {}, el("option", { value: "" }, "any channel"),
    AC.state.channels.map((c) => el("option", { value: c.id }, "#" + c.name)));
  const authorSel = el("select", {}, el("option", { value: "" }, "any author"),
    el("option", { value: AC.state.adminAlias }, AC.state.adminAlias + " (admin)"),
    AC.state.agents.map((a) => el("option", { value: a.alias }, a.alias)));
  const results = el("div", {});

  /* Slack-style typed modifiers: from:alias, in:#channel — pulled out of the
     query text and applied as filters (dropdowns act as fallbacks). */
  function parseQuery(raw) {
    let author = authorSel.value || undefined;
    let channel = chanSel.value || undefined;
    const text = raw.replace(/\b(from|in):(\S+)/gi, (_, key, val) => {
      if (key.toLowerCase() === "from") author = val.replace(/^@/, "");
      else {
        const name = val.replace(/^#/, "").toLowerCase();
        const ch = (AC.state.channels || []).find((c) => (c.name || "").toLowerCase() === name);
        if (ch) channel = ch.id;
      }
      return "";
    }).replace(/\s+/g, " ").trim();
    return { text, author, channel };
  }

  const run = async () => {
    const raw = q.value.trim();
    if (!raw) { results.replaceChildren(); return; }
    const { text, author, channel } = parseQuery(raw);
    if (!text) { results.replaceChildren(el("div", { class: "empty" }, el("p", {}, "Add search terms beside the filters."))); return; }
    results.replaceChildren(el("div", { class: "spinner" }, "searching…"));
    try {
      const data = await API.search(pid, {
        q: text, type: typeSel.value || undefined,
        channel_id: channel, author, limit: 50,
      });
      const items = data.items || [];
      results.replaceChildren(items.length
        ? el("div", { class: "card", style: "padding:0" }, items.map(renderHit))
        : el("div", { class: "empty" },
            el("div", { class: "glyph" }, "⌀"),
            el("div", { class: "e-title" }, "No Results"),
            el("p", {}, `Nothing matches "${text}".`)));
    } catch (e) { results.replaceChildren(el("div", { class: "empty" }, el("p", {}, e.message))); }
  };

  function renderHit(h) {
    const type = h.type || "result";
    let meta = type, target = null;
    if (type === "message" || type === "messages") {
      meta = `message · ${h.author || "?"} · ${fmtTime(h.created_at)}`;
      if (h.conversation_id) target = `#/p/${pid}/c/${h.conversation_id}` + (h.id ? `/m/${h.id}` : "");
    } else if (type === "ticket" || type === "tickets") {
      meta = `ticket #${h.number} · ${h.status || ""}`;
      target = `#/p/${pid}/tickets/${h.number}`;
    } else if (type === "comment" || type === "comments") {
      meta = `comment on #${h.number || h.ticket_number || "?"} · ${h.author || "?"}`;
      if (h.number || h.ticket_number) target = `#/p/${pid}/tickets/${h.number || h.ticket_number}`;
    } else if (type === "document" || type === "documents") {
      meta = `document · ${h.slug || h.title || ""}`;
      if (h.slug) target = `#/p/${pid}/docs/${encodeURIComponent(h.slug)}`;
    }
    const snippetHtml = h.snippet || esc(h.excerpt || h.body || "").slice(0, 200);
    // snippet comes from FTS with <mark> tags; sanitize it through the same path
    const snip = el("div", {});
    sanitizeInto(snip, snippetHtml);
    return el("div", { class: "search-hit", onclick: () => { if (target) location.hash = target; } },
      el("div", { class: "hit-meta" }, meta, h.title && type !== "document" ? " · " + h.title : ""),
      h.title ? el("div", { class: "t-emphasis" }, h.title) : null,
      snip);
  }

  q.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  typeSel.onchange = run; chanSel.onchange = run; authorSel.onchange = run;

  AC.setView(el("div", { class: "page narrow" },
    el("h1", {}, "Search"),
    el("div", { class: "filter-bar" }, q, typeSel, chanSel, authorSel,
      el("button", { class: "btn primary", onclick: run }, "Search")),
    results));
  q.focus();
};
