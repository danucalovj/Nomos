/* Chat experience: Slack-grade message pane (grouping, dividers, NEW line,
   scroll physics, hover actions, reactions, threads, forwarding, files,
   typing, optimistic send, composer with autocompletes + emoji picker), plus
   the Activity / Mentions / Saved / Pins / Decisions sidebar views. */
"use strict";

const GROUP_WINDOW_MS = 5 * 60 * 1000;
const PAGE_SIZE = 60;

/* Emoji categories mirroring server/emoji.py's sections. Anything the server
   adds later lands in "other". */
const EMOJI_CATEGORIES = [
  ["faces", ["smile", "grin", "joy", "sweat_smile", "wink", "blush", "innocent", "thinking",
    "neutral_face", "grimacing", "rolling_eyes", "sob", "scream", "exploding_head", "sunglasses",
    "nerd", "melting_face", "zany", "sleeping", "dizzy_face", "party_face", "heart_eyes",
    "confused", "worried", "cry", "angry"]],
  ["hands", ["thumbsup", "thumbsdown", "wave", "clap", "raised_hands", "pray", "muscle",
    "point_up", "point_right", "ok_hand", "crossed_fingers", "handshake", "salute", "shrug",
    "facepalm", "brain", "eyes", "ear", "speaking_head"]],
  ["status", ["white_check_mark", "x", "warning", "question", "exclamation", "no_entry",
    "heavy_plus_sign", "heavy_minus_sign", "hourglass", "stopwatch", "alarm_clock", "recycle",
    "infinity", "green_circle", "yellow_circle", "red_circle", "checkered_flag", "construction",
    "sos", "arrows_counterclockwise", "fast_forward", "pause"]],
  ["objects", ["rocket", "fire", "sparkles", "tada", "boom", "zap", "star", "bulb", "gear",
    "wrench", "hammer", "hammer_and_wrench", "nut_and_bolt", "test_tube", "microscope",
    "telescope", "satellite", "battery", "plug", "computer", "keyboard", "desktop", "printer",
    "floppy_disk", "cd", "package", "file_folder", "open_file_folder", "page_facing_up",
    "clipboard", "memo", "pencil", "books", "book", "bookmark", "link", "paperclip", "scissors",
    "lock", "unlock", "key", "shield", "magnifying_glass", "chart_up", "chart_down", "bar_chart",
    "calendar", "pushpin", "round_pushpin", "bell", "no_bell", "mega", "loudspeaker", "envelope",
    "inbox", "outbox", "mailbox", "label", "moneybag", "gem", "trophy", "medal", "dart",
    "game_die", "puzzle", "art", "camera", "movie_camera", "film", "robot", "alien", "ghost",
    "skull", "bug", "spider", "snail", "turtle", "rabbit", "owl", "eagle", "octopus", "100"]],
  ["nature", ["seedling", "evergreen_tree", "palm_tree", "sun", "moon", "cloud", "rainbow",
    "snowflake", "droplet", "ocean", "earth", "comet", "milky_way", "mountain", "volcano",
    "heart", "orange_heart", "yellow_heart", "green_heart", "blue_heart", "purple_heart",
    "black_heart", "broken_heart", "heartbeat", "coffee", "tea", "pizza", "cake", "beer",
    "champagne", "popcorn"]],
];

/* ---------- shared per-view chat state ---------- */
let Chat = null; // rebuilt by Views.projectChat; null on other views

function currentConv() {
  if (!Chat) return null;
  return AC.state.channels.find((c) => c.id === Chat.cid)
    || AC.state.dms.find((d) => d.id === Chat.cid) || null;
}

function convLabel(conv) {
  if (!conv) return "conversation";
  if (conv.type === "channel" || conv.name) return "#" + conv.name;
  return (conv.participants || []).join(" ↔ ") || "direct message";
}

function conversationChoices() {
  const chans = AC.state.channels.map((c) => ({ id: c.id, label: "#" + c.name }));
  const dms = AC.state.dms.map((d) => ({ id: d.id, label: "DM: " + convLabel(d) }));
  return chans.concat(dms);
}

/* ---------- sidebar "+" handlers (registered centrally per route) ---------- */
function registerSidebarAdders() {
  AC.on("sidebar_add_channel", sidebarAddChannel, "sb-add-ch");
  AC.on("sidebar_add_dm", sidebarAddDm, "sb-add-dm");
}

async function sidebarAddChannel() {
  const name = el("input", { type: "text", placeholder: "channel-name", maxlength: 50 });
  const topic = el("input", { type: "text", placeholder: "Topic (optional)", maxlength: 200 });
  const okd = await modal({
    title: "CREATE CHANNEL",
    body: el("div", {},
      el("div", { class: "field" }, el("label", {}, "Name"), name),
      el("div", { class: "field" }, el("label", {}, "Topic"), topic)),
    confirmText: "Create Channel",
    onValidate: () => name.value.trim().length >= 2,
  });
  if (!okd) return;
  try {
    const ch = await API.createChannel(AC.state.pid, name.value.trim(), topic.value.trim());
    await AC.refreshSidebarData();
    location.hash = `#/p/${AC.state.pid}/c/${ch.id}`;
  } catch (e) { toast("Error", e.message, "error"); }
}

async function sidebarAddDm() {
  const sel = el("select", {},
    ...AC.state.agents.filter((a) => !a.revoked).map((a) =>
      el("option", { value: a.alias }, a.alias)));
  if (!sel.children.length) { toast("No Agents", "Nobody to message yet."); return; }
  const okd = await modal({
    title: "OPEN DIRECT MESSAGE",
    body: el("div", { class: "field" }, el("label", {}, "With"), sel),
    confirmText: "Open DM",
  });
  if (!okd) return;
  try {
    const dm = await API.openDm(AC.state.pid, sel.value);
    await AC.refreshSidebarData();
    location.hash = `#/p/${AC.state.pid}/c/${dm.id}`;
  } catch (e) { toast("Error", e.message, "error"); }
}

/* =================================================================== chat */

Views.projectChat = async function (cid, mid) {
  const pid = AC.state.pid;
  if (cid == null) {
    const main = AC.state.channels.find((c) => c.is_main) || AC.state.channels[0];
    if (!main) { AC.setView(el("div", { class: "empty" }, el("div", { class: "glyph" }, "⌀"),
      el("div", { class: "e-title" }, "No Channels"), el("p", {}, "This project has no channels yet."))); return; }
    cid = main.id;
  }

  Chat = {
    cid, pid,
    rows: new Map(),      // mid -> {m, node}
    order: [],            // mids in ascending order
    savedSet: new Set(),
    frequent: [],
    oldestId: null,
    hasOlder: false,
    atBottom: true,
    unseenWhileUp: 0,
    prevLastSeen: AC.state.lastSeen[cid] || 0,
    typingTimers: new Map(),
    expandedSysRuns: new Set(),
    thread: null,         // {rootId, node}
    lastTypingSent: 0,
  };

  const conv = currentConv();
  const isDm = conv && !conv.name;

  /* ---- static frame ---- */
  const scroller = el("div", { class: "msg-scroll" });
  const jumpBtn = el("button", { class: "jump-latest hidden", onclick: () => scrollToBottom(true) });
  const scrollWrap = el("div", { style: "position:relative;flex:1;display:flex;flex-direction:column;min-height:0" },
    scroller, jumpBtn);
  const typingStrip = el("div", { class: "typing-strip" });
  const composerWrap = buildComposer({
    placeholder: `Message ${convLabel(conv)} — markdown supported`,
    draftKey: `ac_draft_${pid}_${cid}`,
    onSend: (text, attachmentIds, extra) => sendMessage(text, attachmentIds, extra),
    onTypingPing: () => {
      const now = Date.now();
      if (now - Chat.lastTypingSent > 4000) {
        Chat.lastTypingSent = now;
        API.typing(pid, cid).catch(() => {});
      }
    },
    allowDecision: true,
  });

  const paneHead = el("div", { class: "pane-head" },
    el("span", { class: "p-title" }, convLabel(conv)),
    el("span", { class: "p-sub" }, isDm ? "Direct messages — visible to admin" : (conv && conv.topic) || ""),
    el("span", { class: "spacer" }),
    el("button", { class: "icon-btn", title: "Pinned Messages", onclick: () => showPinsFlyout(cid) }, "⌖"),
    el("button", {
      class: "icon-btn", title: "Members", onclick: () => showMembers(conv),
    }, "⋮"));

  const frame = el("div", { class: "chat-main" }, paneHead, scrollWrap, typingStrip, composerWrap);
  AC.setView(frame);
  installDragDrop(frame, composerWrap);

  // Staleness guard: if the user navigates away while our loads are pending,
  // a newer view will have replaced the global Chat (and possibly bumped the
  // bus generation). We must not register live handlers or touch the DOM.
  const myChat = Chat;
  const myGen = AC._busGen;
  const stale = () => Chat !== myChat || AC._busGen !== myGen;

  /* ---- data load ---- */
  try {
    const [page, savedResp, freq] = await Promise.all([
      API.messages(pid, cid, { limit: PAGE_SIZE }),
      API.saved(pid, { limit: 200 }).catch(() => ({ items: [] })),
      API.frequentEmoji(pid).catch(() => ({ items: [] })),
    ]);
    Chat.savedSet = new Set((savedResp.items || []).map((m) => m.id));
    Chat.frequent = (freq.items || []).map((i) => i.emoji);
    ingest(page.items || []);
    Chat.hasOlder = !!page.has_more;
    renderAll();
    if (mid) {
      await jumpToMessage(mid);
    } else {
      landOnEntry();
    }
    markSeenNewest();
  } catch (e) {
    if (!stale()) {
      AC.setView(el("div", { class: "page narrow" }, el("div", { class: "card" }, "Failed to load conversation: " + e.message)));
    }
    return;
  }
  if (stale()) return; // a newer route won while we were loading

  /* ---- scroll physics ---- */
  scroller.addEventListener("scroll", debounce(async () => {
    const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 80;
    if (nearBottom !== Chat.atBottom) {
      Chat.atBottom = nearBottom;
      if (nearBottom) { Chat.unseenWhileUp = 0; updateJumpPill(); markSeenNewest(); }
    }
    if (scroller.scrollTop <= 60 && Chat.hasOlder && !Chat.loadingOlder) {
      Chat.loadingOlder = true;
      const prevHeight = scroller.scrollHeight;
      try {
        const page = await API.messages(pid, cid, { limit: PAGE_SIZE, before_id: Chat.oldestId });
        ingest(page.items || []);
        Chat.hasOlder = !!page.has_more;
        renderAll();
        scroller.scrollTop = scroller.scrollHeight - prevHeight + scroller.scrollTop;
      } catch (e) { /* transient */ }
      Chat.loadingOlder = false;
    }
  }, 80));

  /* ---- live events ---- */
  AC.on("message", (m) => {
    if (m.conversation_id !== cid) return;
    if (m.parent_id) { onThreadReply(m); bumpThreadBar(m.parent_id); return; }
    if (Chat.rows.has(m.id)) { refreshRow(m.id, m); return; } // optimistic reconcile by id
    // Reconcile by content: if our own POST committed but the response was
    // lost, the message arrives via SSE while a ghost (negative id, same
    // body) is still pending/failed — replace the ghost instead of doubling.
    if (m.role === "admin") {
      const ghostId = Chat.order.find((id) => id < 0 && Chat.rows.get(id)?.m.body === m.body);
      if (ghostId !== undefined) {
        Chat.rows.delete(ghostId);
        Chat.order = Chat.order.filter((x) => x !== ghostId);
        renderAll(); // drop the ghost's node before inserting the real row
      }
    }
    insertMessage(m);
    if (Chat.atBottom) { scrollToBottom(false); markSeenNewest(); }
    else { Chat.unseenWhileUp++; updateJumpPill(); }
  });
  AC.on("message_edited", (m) => { if (m.conversation_id === cid) refreshRow(m.id, m); onThreadReply(m); });
  AC.on("message_deleted", (pl) => {
    const entry = Chat.rows.get(pl.id);
    if (entry) { entry.m.deleted = true; entry.m.body = ""; refreshRow(pl.id, entry.m); }
  });
  AC.on("message_pinned", (m) => { if (m.conversation_id === cid) refreshRow(m.id, m); });
  AC.on("message_unpinned", (pl) => {
    const entry = Chat.rows.get(pl.id);
    if (entry) API.message(pid, pl.id).then((m) => refreshRow(m.id, m)).catch(() => {});
  });
  AC.on("reaction", (pl) => {
    const entry = Chat.rows.get(pl.message_id);
    if (entry) { entry.m.reactions = pl.reactions; renderReactions(entry); }
    if (Chat.thread && Chat.thread.rows.has(pl.message_id)) {
      const te = Chat.thread.rows.get(pl.message_id);
      te.m.reactions = pl.reactions; renderReactions(te);
    }
  });
  AC.on("typing", (pl) => {
    if (pl.conversation_id !== cid) return;
    if (pl.alias === AC.state.adminAlias) return;
    if (Chat.typingTimers.has(pl.alias)) clearTimeout(Chat.typingTimers.get(pl.alias));
    Chat.typingTimers.set(pl.alias, setTimeout(() => {
      Chat.typingTimers.delete(pl.alias); renderTypingStrip();
    }, 6000));
    renderTypingStrip();
  });
  AC.on("document_updated", () => { /* doc cards resolve at read; nothing to do */ });

  /* ---------------- inner helpers (close over Chat/scroller/...) -------- */

  function ingest(items) {
    for (const m of items) {
      if (!Chat.rows.has(m.id)) { Chat.rows.set(m.id, { m, node: null }); Chat.order.push(m.id); }
      else Chat.rows.get(m.id).m = m;
    }
    // Real ids ascending; optimistic ghosts (negative ids) stay at the end in
    // chronological order.
    Chat.order.sort((a, b) => {
      const ga = a < 0 ? 1 : 0, gb = b < 0 ? 1 : 0;
      if (ga !== gb) return ga - gb;
      return ga ? Math.abs(a) - Math.abs(b) : a - b;
    });
    const real = Chat.order.filter((x) => x > 0);
    Chat.oldestId = real.length ? real[0] : null;
  }

  function renderAll() {
    scroller.replaceChildren();
    let prev = null, prevDay = null, sysRun = [];
    const flushSysRun = () => {
      if (!sysRun.length) return;
      const runKey = sysRun[0].id;
      if (sysRun.length >= 3 && !Chat.expandedSysRuns.has(runKey)) {
        scroller.append(el("div", {
          class: "sys-collapse",
          onclick: () => { Chat.expandedSysRuns.add(runKey); renderAll(); },
        }, `${sysRun.length} system events ▸`));
      } else {
        for (const sm of sysRun) scroller.append(mountRow(sm, { compact: false }));
      }
      sysRun = [];
    };
    for (const midKey of Chat.order) {
      const m = Chat.rows.get(midKey).m;
      const day = (m.created_at || "").slice(0, 10);
      if (day !== prevDay) {
        flushSysRun();
        scroller.append(el("div", { class: "date-divider" }, el("span", { class: "chip" }, dayLabel(day))));
        prevDay = day; prev = null;
      }
      if (Chat.prevLastSeen && m.id > Chat.prevLastSeen && !scroller.querySelector(".new-line")
          && m.role !== "admin") {
        flushSysRun();
        scroller.append(el("div", { class: "new-line" }, el("span", { class: "tag" }, "NEW")));
        prev = null;
      }
      if (m.role === "system") { sysRun.push(m); prev = null; continue; }
      flushSysRun();
      const compact = !!prev && prev.author === m.author && prev.role === m.role
        && (new Date(m.created_at) - new Date(prev.created_at)) < GROUP_WINDOW_MS
        && !prev.deleted && m.type === "normal" && prev.type === "normal";
      scroller.append(mountRow(m, { compact }));
      prev = m;
    }
    flushSysRun();
  }

  function insertMessage(m) {
    ingest([m]);
    renderAll(); // simple + correct; list sizes are bounded by pagination
    if (Chat.atBottom) scrollToBottom(false);
  }

  function mountRow(m, opts) {
    // An in-progress inline edit must survive incoming events: re-use the
    // existing node instead of rebuilding it out from under the textarea
    // (issue #29). The edit's own save/cancel path re-renders normally.
    const existing = Chat.rows.get(m.id) && Chat.rows.get(m.id).node;
    if (existing && existing.querySelector(".m-body textarea")) {
      return existing;
    }
    const node = buildMessageRow(m, {
      compact: opts.compact,
      savedSet: Chat.savedSet,
      frequent: Chat.frequent,
      onOpenThread: () => openThread(m.id),
      cid,
    });
    Chat.rows.get(m.id).node = node;
    return node;
  }

  function refreshRow(midKey, fresh) {
    const entry = Chat.rows.get(midKey);
    if (!entry) return;
    entry.m = fresh;
    renderAll();
  }

  function renderReactions(entry) {
    const holder = entry.node && entry.node.querySelector(".reactions");
    if (!holder) { renderAll(); return; }
    holder.replaceWith(buildReactions(entry.m, Chat.frequent));
  }

  function landOnEntry() {
    const newLine = scroller.querySelector(".new-line");
    if (newLine) newLine.scrollIntoView({ block: "center" });
    else scrollToBottom(false);
  }

  function scrollToBottom(smooth) {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    Chat.atBottom = true; Chat.unseenWhileUp = 0; updateJumpPill();
  }

  function updateJumpPill() {
    jumpBtn.classList.toggle("hidden", Chat.unseenWhileUp === 0);
    jumpBtn.textContent = `↓ ${Chat.unseenWhileUp} new message${Chat.unseenWhileUp === 1 ? "" : "s"}`;
  }

  function markSeenNewest() {
    // Only real server ids count — a pending/failed ghost (negative id) at the
    // tail must not freeze the cursor.
    let newest = 0;
    for (let i = Chat.order.length - 1; i >= 0; i--) {
      if (Chat.order[i] > 0) { newest = Chat.order[i]; break; }
    }
    if (newest > (AC.state.lastSeen[cid] || 0)) {
      AC.state.lastSeen[cid] = newest;
      AC.saveLastSeen();
      AC.renderSidebar();
    }
  }

  async function jumpToMessage(target) {
    let guard = 0;
    while (!Chat.rows.has(target) && Chat.hasOlder && guard++ < 10) {
      const page = await API.messages(pid, cid, { limit: PAGE_SIZE, before_id: Chat.oldestId });
      ingest(page.items || []);
      Chat.hasOlder = !!page.has_more;
    }
    renderAll();
    const entry = Chat.rows.get(target);
    if (!entry || !entry.node) { toast("Not Found", "That message is not in this conversation."); return; }
    entry.node.scrollIntoView({ block: "center" });
    entry.node.classList.add("flash");
    setTimeout(() => entry.node.classList.remove("flash"), 2000);
    Chat.atBottom = false;
  }

  function renderTypingStrip() {
    const names = [...Chat.typingTimers.keys()];
    if (!names.length) { typingStrip.replaceChildren(); return; }
    const label = names.length === 1 ? `${names[0]} is typing`
      : names.length === 2 ? `${names[0]} and ${names[1]} are typing`
      : "several agents are typing";
    typingStrip.replaceChildren(
      el("span", {}, label),
      el("span", { class: "typing-dots" }, el("i"), el("i"), el("i")));
  }

  async function sendMessage(text, attachmentIds, extra) {
    const tempId = -Date.now();
    const ghost = {
      id: tempId, conversation_id: cid, author: AC.state.adminAlias, role: "admin",
      type: (extra && extra.type) || "normal", body: text, created_at: new Date().toISOString(),
      reactions: [], attachments: [], reply_count: 0, pinned: false, deleted: false,
      avatar: AC.state.adminAvatar,
    };
    Chat.rows.set(tempId, { m: ghost, node: null });
    Chat.order.push(tempId);
    renderAll();
    const ghostNode = Chat.rows.get(tempId).node;
    if (ghostNode) ghostNode.classList.add("pending");
    scrollToBottom(false);
    try {
      const real = await API.postMessage(pid, cid, {
        body: text, attachment_ids: attachmentIds,
        type: (extra && extra.type) || "normal",
      });
      Chat.rows.delete(tempId);
      Chat.order = Chat.order.filter((x) => x !== tempId);
      if (!Chat.rows.has(real.id)) { ingest([real]); }
      renderAll(); scrollToBottom(false); markSeenNewest();
    } catch (e) {
      const entry = Chat.rows.get(tempId);
      if (entry && entry.node) {
        entry.node.classList.remove("pending");
        entry.node.classList.add("failed");
        entry.node.addEventListener("click", function retry() {
          entry.node.removeEventListener("click", retry);
          Chat.rows.delete(tempId);
          Chat.order = Chat.order.filter((x) => x !== tempId);
          renderAll();
          sendMessage(text, attachmentIds, extra);
        }, { once: true });
      }
      toast("Send Failed", e.message, "error");
    }
  }

  /* ---- thread panel ---- */
  async function openThread(rootId) {
    const panel = document.getElementById("thread-panel");
    let data;
    try { data = await API.thread(pid, rootId); }
    catch (e) { toast("Error", e.message, "error"); return; }
    const rows = new Map();
    const list = el("div", { class: "msg-scroll" });
    const mount = (m) => {
      const node = buildMessageRow(m, {
        compact: false, savedSet: Chat.savedSet, frequent: Chat.frequent,
        onOpenThread: null, cid, inThread: true,
      });
      rows.set(m.id, { m, node });
      return node;
    };
    list.append(mount(data.root));
    list.append(el("div", { class: "date-divider" },
      el("span", { class: "chip" }, `${data.replies.length} repl${data.replies.length === 1 ? "y" : "ies"}`)));
    for (const r of data.replies) list.append(mount(r));

    const alsoSend = el("input", { type: "checkbox" });
    const composer = buildComposer({
      placeholder: "Reply in thread…",
      draftKey: `ac_draft_${pid}_${cid}_t${data.root.id}`,
      compactToolbar: true,
      onSend: async (text, attachmentIds) => {
        const reply = await API.postMessage(pid, cid, {
          body: text, parent_id: data.root.id, attachment_ids: attachmentIds,
        });
        if (alsoSend.checked) {
          API.forwardMessage(pid, reply.id, cid).catch((e) => toast("Error", e.message, "error"));
        }
      },
      onTypingPing: () => {},
    });

    panel.classList.remove("hidden");
    panel.replaceChildren(
      el("div", { class: "pane-head" },
        el("span", { class: "p-title" }, "Thread"),
        el("span", { class: "p-sub" }, convLabel(conv)),
        el("span", { class: "spacer" }),
        el("button", {
          class: "icon-btn", title: "Close",
          onclick: () => { Chat.thread = null; AC.closeThreadPanel(); },
        }, "×")),
      list,
      el("label", { class: "checkline", style: "padding: 4px 16px" }, alsoSend,
        ` Also send to ${convLabel(conv)}`),
      composer);
    list.scrollTop = list.scrollHeight;
    Chat.thread = { rootId: data.root.id, rows, list, mount };
  }

  function onThreadReply(m) {
    const t = Chat.thread;
    if (!t) return;
    if (t.rows.has(m.id)) {
      const entry = t.rows.get(m.id);
      const fresh = t.mount(m);
      entry.node.replaceWith(fresh);
      t.rows.set(m.id, { m, node: fresh });
    } else if (m.parent_id === t.rootId) {
      t.list.append(t.mount(m));
      t.list.scrollTop = t.list.scrollHeight;
    }
  }

  function bumpThreadBar(rootId) {
    const entry = Chat.rows.get(rootId);
    if (!entry) return;
    API.message(pid, rootId).then((m) => refreshRow(rootId, m)).catch(() => {});
  }
};

/* ---------------- message row builder (used by chat + thread + saved) ----- */

function buildMessageRow(m, opts) {
  const mentionsYou = m.role !== "admin" && AC.state.adminAlias
    && new RegExp(`(?<![\\w\`])@${AC.state.adminAlias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(m.body || "");

  const rowClass = ["msg"];
  if (opts.compact) rowClass.push("compact");
  if (m.role === "admin") rowClass.push("from-admin");
  if (m.type === "decision") rowClass.push("is-decision");
  if (m.role === "system") rowClass.push("system");
  if (m.deleted) rowClass.push("deleted");
  if (mentionsYou) rowClass.push("mentions-you");

  const body = el("div", { class: "m-body" });
  if (m.deleted) {
    body.append(el("div", { class: "md" }, el("em", { class: "faint" }, "This message was deleted.")));
  } else {
    if (m.body) body.append(renderMd(m.body));
    if (m.forwarded_from) body.append(buildForwardedEmbed(m.forwarded_from));
    if (m.doc_card && !m.doc_card.missing) body.append(buildDocCard(m.doc_card));
    const atts = m.attachments || [];
    const images = atts.filter((a) => (a.mime_type || "").startsWith("image/"));
    const files = atts.filter((a) => !(a.mime_type || "").startsWith("image/"));
    if (images.length) body.append(buildImageGrid(images));
    for (const f of files) body.append(buildFileCard(f));
    body.append(buildReactions(m, opts.frequent || []));
    if (!opts.inThread && m.reply_count > 0) body.append(buildThreadBar(m, opts.onOpenThread));
  }

  let node;
  if (m.role === "system") {
    node = el("div", { class: rowClass.join(" "), "data-mid": m.id }, body);
    return node;
  }

  if (opts.compact) {
    node = el("div", { class: rowClass.join(" "), "data-mid": m.id },
      el("span", { class: "m-gutter" }, fmtTime(m.created_at).replace(/\s?[AP]M/i, "")),
      body);
  } else {
    const head = el("div", { class: "m-head" },
      el("span", { class: "m-author", onclick: (e) => showProfilePopover(e, m.author) }, m.author),
      m.role === "admin" ? el("span", { class: "role-tag admin" }, "admin") : null,
      m.type === "decision" ? el("span", { class: "role-tag decision" }, "decision") : null,
      el("span", { class: "m-time", title: new Date(m.created_at).toLocaleString() }, fmtTime(m.created_at)),
      m.edited_at ? el("span", { class: "edited-note", title: "edited " + fmtTime(m.edited_at) }, "(edited)") : null);
    const av = AC.avatarEl({ alias: m.author, avatar: m.avatar }, 36);
    av.classList.add("m-av");
    av.addEventListener("click", (e) => showProfilePopover(e, m.author));
    node = el("div", { class: rowClass.join(" "), "data-mid": m.id }, av, head, body);
  }

  if (!m.deleted && m.id > 0) node.append(buildHoverToolbar(m, opts));
  return node;
}

function buildHoverToolbar(m, opts) {
  const pid = AC.state.pid;
  const isOwn = m.role === "admin";
  const saved = opts.savedSet && opts.savedSet.has(m.id);
  const bar = el("div", { class: "m-actions" });
  for (const emoji of (opts.frequent || []).slice(0, 2)) {
    bar.append(el("button", { title: `:${emoji}:`, onclick: () => API.toggleReaction(pid, m.id, emoji).catch((e) => toast("Error", e.message, "error")) },
      AC.emoji(emoji) || emoji));
  }
  bar.append(el("button", {
    title: "React", onclick: (e) => openEmojiPicker(e.currentTarget, (emoji) =>
      API.toggleReaction(pid, m.id, emoji).catch((err) => toast("Error", err.message, "error"))),
  }, "☺+"));
  if (!opts.inThread) {
    bar.append(el("button", { title: "Reply in Thread", onclick: () => opts.onOpenThread && opts.onOpenThread() }, "⤷"));
  }
  bar.append(el("button", { title: "Forward", onclick: () => openForwardDialog(m) }, "⇱"));
  bar.append(el("button", {
    title: saved ? "Remove from saved" : "Save for later",
    style: saved ? "color: var(--amber)" : "",
    onclick: async (e) => {
      try {
        const res = await API.toggleSave(pid, m.id);
        if (opts.savedSet) res.saved ? opts.savedSet.add(m.id) : opts.savedSet.delete(m.id);
        e.currentTarget.style.color = res.saved ? "var(--amber)" : "";
        e.currentTarget.title = res.saved ? "Remove from saved" : "Save for later";
      } catch (err) { toast("Error", err.message, "error"); }
    },
  }, "⚑"));
  bar.append(el("button", {
    title: m.pinned ? "Unpin" : "Pin to channel",
    style: m.pinned ? "color: var(--amber)" : "",
    onclick: () => (m.pinned ? API.unpinMessage(pid, m.id) : API.pinMessage(pid, m.id))
      .catch((e) => toast("Error", e.message, "error")),
  }, "⌖"));
  if (isOwn) {
    bar.append(el("button", { title: "Edit", onclick: () => inlineEdit(m) }, "✎"));
  }
  bar.append(el("button", {
    class: "danger", title: "Delete",
    onclick: async () => {
      const okd = await modal({
        title: "DELETE MESSAGE", danger: true, confirmText: "Delete",
        body: el("div", {}, el("p", { class: "muted" }, "This cannot be undone."),
          el("div", { class: "card" }, (m.body || "(attachment)").slice(0, 200))),
      });
      if (okd) API.deleteMessage(pid, m.id).catch((e) => toast("Error", e.message, "error"));
    },
  }, "⌫"));
  bar.append(el("button", {
    title: "Copy Link",
    onclick: (e) => {
      const url = `${location.origin}/#/p/${pid}/c/${m.conversation_id}/m/${m.id}`;
      navigator.clipboard.writeText(url).then(() => toast("Copied", "Message link copied."));
    },
  }, "⛓"));
  return bar;
}

function inlineEdit(m) {
  const pid = AC.state.pid;
  const node = document.querySelector(`.msg[data-mid="${m.id}"] .m-body`);
  if (!node) return;
  const ta = el("textarea", { class: "input", style: "min-height: 60px" });
  ta.value = m.body;
  const save = async () => {
    try { await API.editMessage(pid, m.id, ta.value); }
    catch (e) { toast("Error", e.message, "error"); }
  };
  node.replaceChildren(ta,
    el("div", { style: "display:flex;gap:8px;margin-top:8px" },
      el("button", { class: "btn small primary", onclick: save }, "Save"),
      el("button", { class: "btn small", onclick: () => AC.route() }, "Cancel")));
  ta.focus();
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
    if (e.key === "Escape") AC.route();
  });
}

function buildReactions(m, frequent) {
  const pid = AC.state.pid;
  const holder = el("div", { class: "reactions" });
  for (const r of m.reactions || []) {
    const mine = (r.by || []).includes(AC.state.adminAlias);
    holder.append(el("button", {
      class: "reaction" + (mine ? " mine" : ""),
      title: `${(r.by || []).join(", ")} reacted with :${r.emoji}:`,
      onclick: () => API.toggleReaction(pid, m.id, r.emoji).catch((e) => toast("Error", e.message, "error")),
    },
      el("span", { class: "emo" }, AC.emoji(r.emoji) || `:${r.emoji}:`),
      el("span", { class: "cnt" }, String(r.count))));
  }
  holder.append(el("button", {
    class: "reaction add", title: "Add Reaction",
    onclick: (e) => openEmojiPicker(e.currentTarget, (emoji) =>
      API.toggleReaction(pid, m.id, emoji).catch((err) => toast("Error", err.message, "error"))),
  }, "☺+"));
  return holder;
}

function buildThreadBar(m, onOpen) {
  // Participant stack: we only know reply authors after fetching the thread;
  // show the root author + reply count cheaply (stack fills on open).
  const stack = el("span", { class: "stack" }, AC.avatarEl({ alias: m.author, avatar: m.avatar }, 16));
  return el("div", { class: "thread-bar", onclick: onOpen },
    stack,
    el("span", { class: "replies" }, `${m.reply_count} repl${m.reply_count === 1 ? "y" : "ies"}`),
    el("span", { class: "last" }, "View Thread"),
    el("span", { class: "go" }, "→"));
}

function buildForwardedEmbed(f) {
  if (f.missing) {
    return el("div", { class: "fwd" }, el("div", { class: "fwd-src" }, "FORWARDED MESSAGE"),
      el("div", { class: "fwd-body" }, el("em", { class: "faint" }, "The original message no longer exists.")));
  }
  const srcLabel = f.conversation_type === "dm" ? "FROM A DIRECT MESSAGE"
    : "FORWARDED FROM " + (f.conversation_label || "").toUpperCase();
  const bodyNode = f.deleted
    ? el("div", { class: "fwd-body" }, el("em", { class: "faint" }, "This message was deleted."))
    : el("div", { class: "fwd-body" }, renderMd(f.body));
  const node = el("div", { class: "fwd" },
    el("div", { class: "fwd-src" }, srcLabel),
    el("div", { class: "fwd-head" },
      AC.avatarEl(f.author, 16),
      el("span", { class: "fwd-author" }, f.author),
      el("span", { class: "fwd-time" }, fmtTime(f.created_at))),
    bodyNode);
  for (const a of f.attachments || []) {
    if ((a.mime_type || "").startsWith("image/")) node.append(buildImageGrid([a]));
    else node.append(buildFileCard(a));
  }
  if (f.conversation_type !== "dm" && !f.deleted) {
    node.style.cursor = "pointer";
    node.title = "Jump to original";
    node.addEventListener("click", (e) => {
      if (e.target.closest("a, .file-card, img")) return;
      location.hash = `#/p/${AC.state.pid}/c/${f.conversation_id}/m/${f.message_id}`;
    });
  }
  return node;
}

function buildFileCard(a) {
  const ext = ((a.filename || "").split(".").pop() || "file").slice(0, 4).toUpperCase();
  return el("a", { class: "file-card", href: a.url || API.attachmentUrl(AC.state.pid, a.id), download: a.filename },
    el("span", { class: "tile" }, ext),
    el("span", { class: "fname" }, a.filename),
    el("span", { class: "fsize" }, fmtBytes(a.size || 0)),
    el("span", { class: "dl" }, "↓"));
}

function buildDocCard(d) {
  return el("a", { class: "file-card doc-card", href: `#/p/${AC.state.pid}/docs/${encodeURIComponent(d.slug)}` },
    el("span", { class: "tile" }, "MD"),
    el("span", { class: "fname" }, d.title),
    el("span", { class: "fmeta" }, `rev ${d.current_revision}${d.author ? " · " + d.author : ""}`),
    el("span", { class: "open" }, "Open →"));
}

function buildImageGrid(images) {
  const grid = el("div", { class: "img-grid" });
  images.forEach((a, idx) => {
    const url = a.url || API.attachmentUrl(AC.state.pid, a.id);
    const img = el("img", { src: url, alt: a.filename, loading: "lazy" });
    img.addEventListener("click", () => openLightbox(images, idx));
    grid.append(img);
  });
  return grid;
}

function openLightbox(images, index) {
  const root = document.getElementById("lightbox-root");
  const show = (i) => {
    const a = images[i];
    const url = a.url || API.attachmentUrl(AC.state.pid, a.id);
    root.replaceChildren(
      el("img", { src: url, alt: a.filename }),
      el("div", { class: "lb-meta" },
        el("span", {}, `${a.filename} · ${fmtBytes(a.size || 0)}${a.uploader ? " · " + a.uploader : ""}`),
        images.length > 1 ? el("button", { class: "icon-btn", onclick: (e) => { e.stopPropagation(); show((i + images.length - 1) % images.length); } }, "←") : null,
        images.length > 1 ? el("button", { class: "icon-btn", onclick: (e) => { e.stopPropagation(); show((i + 1) % images.length); } }, "→") : null,
        el("a", { class: "btn small", href: url, download: a.filename, onclick: (e) => e.stopPropagation() }, "Download"),
        el("button", { class: "icon-btn", onclick: close }, "×")));
  };
  const close = () => { root.replaceChildren(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  root.onclick = (e) => { if (e.target === root || e.target.tagName === "IMG") close(); };
  document.addEventListener("keydown", onKey);
  show(index);
}

/* ---------------- forward dialog ---------------- */

async function openForwardDialog(m) {
  const pid = AC.state.pid;
  const sel = el("select", {},
    ...conversationChoices().map((c) => el("option", { value: c.id }, c.label)));
  const comment = el("textarea", { class: "input", placeholder: "Add a message, if you'd like (optional)" });
  const preview = el("div", { class: "fwd", style: "margin-top: 12px" },
    el("div", { class: "fwd-src" }, "PREVIEW"),
    el("div", { class: "fwd-head" },
      AC.avatarEl({ alias: m.author, avatar: m.avatar }, 16),
      el("span", { class: "fwd-author" }, m.author),
      el("span", { class: "fwd-time" }, fmtTime(m.created_at))),
    el("div", { class: "fwd-body" }, renderMd((m.body || "(attachment)").slice(0, 600))));
  const okd = await modal({
    title: "FORWARD MESSAGE",
    confirmText: "Forward",
    body: el("div", {},
      el("div", { class: "field" }, el("label", {}, "To"), sel),
      el("div", { class: "field" }, el("label", {}, "Comment"), comment),
      preview),
  });
  if (!okd) return;
  try {
    await API.forwardMessage(pid, m.id, Number(sel.value), comment.value.trim());
    toast("Forwarded", "Message forwarded.");
  } catch (e) { toast("Error", e.message, "error"); }
}

/* ---------------- pins flyout + members ---------------- */

async function showPinsFlyout(cid) {
  const pid = AC.state.pid;
  let pins;
  try { pins = await API.pins(pid, cid); } catch (e) { toast("Error", e.message, "error"); return; }
  const items = pins.items || [];
  await modal({
    title: `PINNED — ${items.length}`,
    confirmText: "Close",
    body: items.length
      ? el("div", {}, ...items.map((p) => el("div", {
          class: "card", style: "margin-bottom: 8px; cursor: pointer",
          onclick: () => { document.getElementById("modal-root").replaceChildren(); location.hash = `#/p/${pid}/c/${cid}/m/${p.id}`; },
        },
          el("div", { class: "small muted" }, `${p.author} · ${fmtTime(p.created_at)}`),
          el("div", {}, (p.body || "(attachment)").slice(0, 160)))))
      : el("div", { class: "empty" }, el("div", { class: "glyph" }, "⌖"),
          el("div", { class: "e-title" }, "Nothing Pinned"),
          el("p", {}, "Pin a message from its hover toolbar.")),
  });
}

function showMembers(conv) {
  if (!conv) return;
  const members = conv.members || [];
  modal({
    title: "MEMBERS",
    confirmText: "Close",
    body: el("div", {}, ...members.map((mm) => {
      const agent = AC.agentByAlias(mm.alias);
      return el("div", { style: "display:flex;align-items:center;gap:8px;padding:4px 0" },
        AC.avatarEl(mm.alias, 20),
        el("span", {}, mm.alias),
        mm.role === "admin" ? el("span", { class: "role-tag admin" }, "admin") : null,
        agent ? el("span", { class: "presence " + (agent.online ? "online" : "away"), style: "position:static" }) : null);
    })),
  });
}

/* ---------------- profile popover ---------------- */

function showProfilePopover(evt, alias) {
  const root = document.getElementById("popover-root");
  const agent = AC.agentByAlias(alias);
  const isAdmin = AC.state.adminAlias && alias.toLowerCase() === AC.state.adminAlias.toLowerCase();
  const rect = evt.currentTarget.getBoundingClientRect();
  // Outside-close uses pointerdown: it fires BEFORE click handlers can
  // re-render panel internals, so a button that gets replaced mid-click
  // still counts as inside.
  const close = () => { root.replaceChildren(); document.removeEventListener("pointerdown", onDoc); };
  const onDoc = (e) => { if (!root.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener("pointerdown", onDoc), 0);

  const pop = el("div", { class: "popover" },
    el("div", { class: "pp-head" },
      AC.avatarEl(alias, 64),
      el("div", {},
        el("div", { class: "pp-alias" }, alias, isAdmin ? el("span", { class: "role-tag admin", style: "margin-left:6px" }, "admin") : null),
        el("div", { class: "pp-role" }, isAdmin ? "human administrator" : (agent && agent.role) || "agent"))),
    agent && (agent.status_emoji || agent.status_text)
      ? el("div", { class: "pp-status" }, AC.emoji(agent.status_emoji) || "", " ", agent.status_text || "")
      : null,
    agent ? el("div", { class: "pp-line" },
      el("span", { class: "presence " + (agent.online ? "online" : "away"), style: "position:static" }),
      agent.online ? "online" : `last seen ${timeAgo(agent.last_seen)}`) : null,
    !isAdmin ? el("div", { class: "pp-actions" },
      el("button", {
        class: "btn small primary",
        onclick: async () => {
          close();
          try {
            const dm = await API.openDm(AC.state.pid, alias);
            await AC.refreshSidebarData();
            location.hash = `#/p/${AC.state.pid}/c/${dm.id}`;
          } catch (e) { toast("Error", e.message, "error"); }
        },
      }, "Message")) : null);
  pop.style.top = Math.min(window.innerHeight - 220, rect.bottom + 6) + "px";
  pop.style.left = Math.min(window.innerWidth - 280, rect.left) + "px";
  root.replaceChildren(pop);
}

/* ---------------- emoji picker ---------------- */

function openEmojiPicker(anchor, onPick) {
  const root = document.getElementById("popover-root");
  const map = AC.state.emojiMap || {};
  // Outside-close uses pointerdown: it fires BEFORE click handlers can
  // re-render panel internals, so a button that gets replaced mid-click
  // still counts as inside.
  const close = () => { root.replaceChildren(); document.removeEventListener("pointerdown", onDoc); };
  const onDoc = (e) => { if (!root.contains(e.target)) close(); };
  setTimeout(() => document.addEventListener("pointerdown", onDoc), 0);

  let activeTab = "frequent";
  let query = "";
  let frequent = [];
  API.frequentEmoji(AC.state.pid).then((d) => {
    frequent = (d.items || []).map((i) => i.emoji);
    render();
  }).catch(() => {});

  const foot = el("div", { class: "ep-foot" });
  const grid = el("div", { class: "ep-grid" });
  const tabsRow = el("div", { class: "ep-tabs" });
  const search = el("input", { type: "text", placeholder: "Search emoji" });
  search.addEventListener("input", () => { query = search.value.trim().toLowerCase(); render(); });

  function shortcodesFor() {
    if (query) return Object.keys(map).filter((k) => k.includes(query));
    if (activeTab === "frequent") return frequent.length ? frequent : Object.keys(map).slice(0, 24);
    const cat = EMOJI_CATEGORIES.find(([name]) => name === activeTab);
    return cat ? cat[1].filter((k) => map[k]) : Object.keys(map);
  }

  function render() {
    tabsRow.replaceChildren(
      ...[["frequent", "★"], ...EMOJI_CATEGORIES.map(([name]) => [name, name])].map(([key, label]) =>
        el("button", {
          class: "ep-tab" + (activeTab === key && !query ? " active" : ""),
          onclick: () => { activeTab = key; search.value = ""; query = ""; render(); },
        }, label)));
    grid.replaceChildren(...shortcodesFor().map((code) =>
      el("button", {
        class: "ep-cell", title: `:${code}:`,
        onmouseenter: () => { foot.textContent = `:${code}:`; },
        onclick: () => { close(); onPick(code); },
      }, map[code] || code)));
    if (!grid.children.length) grid.append(el("div", { class: "faint", style: "padding: 12px" }, "No matches."));
  }

  const panel = el("div", { class: "emoji-picker" },
    el("div", { class: "ep-search" }, search), tabsRow, grid, foot);
  const rect = anchor.getBoundingClientRect();
  panel.style.position = "fixed";
  panel.style.top = Math.max(8, Math.min(window.innerHeight - 396, rect.bottom + 6)) + "px";
  panel.style.left = Math.max(8, Math.min(window.innerWidth - 336, rect.left - 160)) + "px";
  root.replaceChildren(panel);
  render();
  search.focus();
}

/* ---------------- composer ---------------- */

function buildComposer(opts) {
  const pid = AC.state.pid;
  const staged = []; // {file, chip}
  const tray = el("div", { class: "att-tray" });
  const ta = el("textarea", { placeholder: opts.placeholder || "Message" });
  const decisionToggle = opts.allowDecision
    ? el("input", { type: "checkbox", title: "Post as Decision" }) : null;

  try { ta.value = localStorage.getItem(opts.draftKey) || ""; } catch (e) {}
  const saveDraft = debounce(() => {
    try {
      if (ta.value) localStorage.setItem(opts.draftKey, ta.value);
      else localStorage.removeItem(opts.draftKey);
    } catch (e) {}
  }, 300);

  const autoGrow = () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + "px";
  };

  const wrap = (before, after) => {
    const s = ta.selectionStart, e = ta.selectionEnd;
    const sel = ta.value.slice(s, e) || "text";
    ta.setRangeText(before + sel + (after !== undefined ? after : before), s, e, "select");
    ta.focus();
    saveDraft();
  };

  const fmtBar = el("div", { class: "fmt-bar" },
    el("button", { title: "Bold", onclick: () => wrap("**") }, "B"),
    el("button", { title: "Italic", onclick: () => wrap("_") }, el("i", {}, "I")),
    el("button", { title: "Strike", onclick: () => wrap("~~") }, "S̶"),
    el("button", { title: "Inline Code", onclick: () => wrap("`") }, "⟨⟩"),
    el("button", { title: "Code Block", onclick: () => wrap("\n```\n", "\n```\n") }, "⧉"),
    el("button", { title: "Quote", onclick: () => wrap("\n> ", "") }, "❝"),
    el("button", { title: "List", onclick: () => wrap("\n- ", "") }, "•"));

  function stageFile(file) {
    const chip = el("span", { class: "att-chip" }, file.name,
      el("button", {
        onclick: () => {
          const i = staged.findIndex((s) => s.chip === chip);
          if (i >= 0) staged.splice(i, 1);
          chip.remove();
        },
      }, "×"));
    staged.push({ file, chip });
    tray.append(chip);
  }

  const fileInput = el("input", { type: "file", multiple: true, style: "display:none" });
  fileInput.addEventListener("change", () => {
    for (const f of fileInput.files) stageFile(f);
    fileInput.value = "";
  });

  const sendBtn = el("button", { class: "btn-send" }, "Send");
  const doSend = async () => {
    const text = ta.value.trim();
    if (!text && !staged.length) return;
    sendBtn.disabled = true;
    try {
      // Upload staged files WITHOUT unstaging them — a transient failure must
      // not lose the user's selection. Chips clear only after success.
      const ids = [];
      for (const s of staged) {
        const att = await API.uploadAttachment(pid, s.file);
        ids.push(att.id);
      }
      for (const s of staged.splice(0)) s.chip.remove();
      ta.value = "";
      autoGrow();
      try { localStorage.removeItem(opts.draftKey); } catch (e) {}
      await opts.onSend(text, ids, decisionToggle && decisionToggle.checked ? { type: "decision" } : undefined);
      if (decisionToggle) decisionToggle.checked = false;
    } catch (e) {
      if (!ta.value) { ta.value = text; autoGrow(); } // don't lose the draft
      toast("Error", e.message, "error");
    } finally {
      sendBtn.disabled = false;
      ta.focus();
    }
  };
  sendBtn.addEventListener("click", doSend);

  ta.addEventListener("input", () => { autoGrow(); saveDraft(); opts.onTypingPing && opts.onTypingPing(); maybeAutocomplete(); });
  ta.addEventListener("keydown", (e) => {
    if (acState.open) {
      if (["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) {
        e.preventDefault();
        acKeydown(e.key);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
    else if (e.key === "ArrowUp" && !ta.value && opts.allowDecision) {
      // edit last own message
      const own = Chat && [...Chat.order].reverse()
        .map((id) => Chat.rows.get(id).m)
        .find((m) => m.role === "admin" && !m.deleted && m.id > 0);
      if (own) { e.preventDefault(); inlineEdit(own); }
    }
  });
  ta.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) { stageFile(f); e.preventDefault(); }
      }
    }
  });

  /* -- @-mention and :emoji: autocomplete -- */
  const acPop = el("div", { class: "ac-pop hidden" });
  const acState = { open: false, kind: null, items: [], sel: 0, start: 0 };

  function maybeAutocomplete() {
    const upto = ta.value.slice(0, ta.selectionStart);
    const mMatch = upto.match(/(?:^|\s)@([A-Za-z0-9_.-]*)$/);
    const eMatch = upto.match(/(?:^|\s):([a-z0-9_+-]{2,})$/);
    if (mMatch) {
      const q = mMatch[1].toLowerCase();
      const options = [
        { alias: "here", desc: "Notify every agent in the project" },
        ...(AC.state.adminAlias ? [{ alias: AC.state.adminAlias, desc: "the human admin" }] : []),
        ...AC.state.agents.filter((a) => !a.revoked).map((a) => ({ alias: a.alias, desc: a.role, agent: a })),
      ].filter((o) => o.alias.toLowerCase().startsWith(q));
      // start includes the "@" itself — the inserted text re-adds it.
      showAc("mention", options, upto.length - q.length - 1);
    } else if (eMatch) {
      const q = eMatch[1];
      const options = Object.keys(AC.state.emojiMap || {})
        .filter((k) => k.startsWith(q)).slice(0, 12)
        .map((k) => ({ code: k }));
      showAc("emoji", options, upto.length - q.length - 1);
    } else hideAc();
  }

  function showAc(kind, items, start) {
    if (!items.length) { hideAc(); return; }
    acState.open = true; acState.kind = kind; acState.items = items.slice(0, 12); acState.sel = 0; acState.start = start;
    renderAc();
    acPop.classList.remove("hidden");
  }
  function hideAc() { acState.open = false; acPop.classList.add("hidden"); }
  function renderAc() {
    acPop.replaceChildren(...acState.items.map((item, i) => el("div", {
      class: "ac-item" + (i === acState.sel ? " sel" : ""),
      onclick: () => pickAc(i),
    },
      acState.kind === "mention"
        ? [item.alias === "here" ? el("span", { class: "mention here-mention" }, "@here") : AC.avatarEl(item.alias, 20),
           el("span", {}, item.alias),
           item.agent ? el("span", { class: "presence " + (item.agent.online ? "online" : "away"), style: "position:static" }) : null,
           el("span", { class: "desc" }, item.desc || "")]
        : [el("span", {}, AC.emoji(item.code)), el("span", { class: "mono" }, `:${item.code}:`)])));
  }
  function acKeydown(key) {
    if (key === "Escape") { hideAc(); return; }
    if (key === "ArrowDown") { acState.sel = (acState.sel + 1) % acState.items.length; renderAc(); return; }
    if (key === "ArrowUp") { acState.sel = (acState.sel + acState.items.length - 1) % acState.items.length; renderAc(); return; }
    pickAc(acState.sel);
  }
  function pickAc(i) {
    const item = acState.items[i];
    const insert = acState.kind === "mention" ? `@${item.alias} ` : `:${item.code}: `;
    ta.setRangeText(insert, acState.start, ta.selectionStart, "end");
    hideAc();
    ta.focus();
    saveDraft();
  }

  const composer = el("div", { class: "composer", style: "position:relative" },
    tray,
    opts.compactToolbar ? null : fmtBar,
    ta,
    el("div", { class: "send-row" },
      el("button", { class: "icon-btn", title: "Emoji", onclick: (e) => openEmojiPicker(e.currentTarget, (code) => { ta.setRangeText(`:${code}: `, ta.selectionStart, ta.selectionEnd, "end"); ta.focus(); saveDraft(); }) }, "☺"),
      el("button", { class: "icon-btn", title: "Attach Files", onclick: () => fileInput.click() }, "📎"),
      decisionToggle ? el("label", { class: "checkline", style: "margin: 0 0 0 8px" }, decisionToggle, " decision") : null,
      el("span", { class: "spacer" }),
      sendBtn),
    acPop, fileInput);

  const node = el("div", { class: "composer-wrap" },
    composer,
    el("div", { class: "composer-hint" }, "enter to send · shift+enter newline · markdown"));
  setTimeout(autoGrow, 0);
  return node;
}

function installDragDrop(frame, composerWrap) {
  let overlay = null, depth = 0;
  const clear = () => { depth = 0; if (overlay) { overlay.remove(); overlay = null; } };
  frame.addEventListener("dragenter", (e) => {
    e.preventDefault();
    depth++;
    if (!overlay) {
      overlay = el("div", { class: "drag-overlay" }, "Drop files to attach");
      frame.style.position = "relative";
      frame.append(overlay);
    }
  });
  frame.addEventListener("dragover", (e) => e.preventDefault());
  frame.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) clear();
  });
  frame.addEventListener("drop", (e) => {
    e.preventDefault();
    clear();
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    // Route dropped files into the composer's file input staging
    const input = composerWrap.querySelector('input[type="file"]');
    if (!input) return;
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    input.files = dt.files;
    input.dispatchEvent(new Event("change"));
  });
}

/* =================================== sidebar nav views ==================== */

function dayLabel(day) {
  const today = new Date().toISOString().slice(0, 10);
  const yest = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  if (day === today) return "Today";
  if (day === yest) return "Yesterday";
  const d = new Date(day + "T12:00:00Z");
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

Views.activity = async function () {
  const pid = AC.state.pid;
  const page = el("div", { class: "page" }, el("h1", { class: "page-title" }, "Activity"));
  const list = el("div", { class: "card" });
  page.append(list);
  AC.setView(page);
  let beforeId = null;
  const loadMore = el("button", { class: "btn small", style: "margin-top: 12px" }, "Load more");
  async function load() {
    const data = await API.activity(pid, { limit: 60, before_id: beforeId });
    for (const ev of data.items || []) {
      beforeId = ev.id;
      const pl = ev.payload || {};
      list.append(el("div", {
        class: "activity-line",
        onclick: () => {
          if (ev.type === "message" && pl.conversation_id) location.hash = `#/p/${pid}/c/${pl.conversation_id}/m/${pl.id}`;
        },
        style: ev.type === "message" ? "cursor:pointer" : "",
      },
        el("span", { class: "faint mono" }, fmtTime(ev.created_at)),
        " ", AC.tapeLabel(ev.type, pl)));
    }
    loadMore.classList.toggle("hidden", !data.has_more);
  }
  loadMore.addEventListener("click", () => load().catch((e) => toast("Error", e.message, "error")));
  page.append(loadMore);
  await load();
};

Views.mentions = async function () {
  const pid = AC.state.pid;
  const data = await API.mentions(pid, false);
  const items = data.items || [];
  const page = el("div", { class: "page" },
    el("h1", { class: "page-title" }, "Mentions"),
    items.some((m) => !m.seen) ? el("button", {
      class: "btn small", style: "margin-bottom: 12px",
      onclick: async () => {
        await API.markMentionsSeen(pid, true).catch((e) => toast("Error", e.message, "error"));
        await AC.refreshMentions();
        Views.mentions();
      },
    }, "Mark All Read") : null,
    items.length ? el("div", {}, ...items.map((m) => {
      const msg = m.message;
      const comment = m.comment;
      const target = msg ? `#/p/${pid}/c/${msg.conversation_id}/m/${msg.id}`
        : comment ? `#/p/${pid}/tickets/${comment.ticket_number}` : null;
      return el("div", {
        class: "card" + (m.seen ? "" : " unread"), style: "margin-bottom: 8px" + (target ? ";cursor:pointer" : ""),
        onclick: () => { if (target) location.hash = target; },
      },
        el("div", { class: "small muted mono" },
          `${(msg && msg.author) || (comment && comment.author) || "?"} · ${fmtTime(m.mentioned_at)}`
          + (comment ? ` · ticket #${comment.ticket_number}` : "")
          + (m.seen ? "" : " · UNSEEN")),
        el("div", {}, ((msg && msg.body) || (comment && comment.body) || "").slice(0, 220)));
    })) : el("div", { class: "empty" }, el("div", { class: "glyph" }, "@"),
      el("div", { class: "e-title" }, "No Mentions"),
      el("p", {}, "When someone mentions you, it lands here.")));
  AC.setView(page);
};

Views.saved = async function () {
  const pid = AC.state.pid;
  const data = await API.saved(pid, { limit: 100 });
  const items = data.items || [];
  const savedSet = new Set(items.map((m) => m.id));
  const page = el("div", { class: "page" },
    el("h1", { class: "page-title" }, "Saved"),
    items.length ? el("div", {}, ...items.map((m) => el("div", { class: "card", style: "margin-bottom: 8px" },
      el("div", { class: "small muted mono", style: "margin-bottom: 4px" },
        `${m.author} · ${fmtTime(m.created_at)}`),
      m.deleted ? el("em", { class: "faint" }, "This message was deleted.") : renderMd(m.body),
      el("div", { style: "display:flex;gap:8px;margin-top:8px" },
        el("button", { class: "btn small", onclick: () => { location.hash = `#/p/${pid}/c/${m.conversation_id}/m/${m.id}`; } }, "Jump"),
        el("button", {
          class: "btn small", onclick: async (e) => {
            await API.toggleSave(pid, m.id).catch(() => {});
            e.currentTarget.closest(".card").remove();
          },
        }, "Unsave")))))
      : el("div", { class: "empty" }, el("div", { class: "glyph" }, "⚑"),
          el("div", { class: "e-title" }, "Nothing Saved"),
          el("p", {}, "Flag messages from their hover toolbar to keep them here.")));
  AC.setView(page);
};

Views.pins = async function () {
  const pid = AC.state.pid;
  const page = el("div", { class: "page" }, el("h1", { class: "page-title" }, "Pins"));
  let any = false;
  for (const c of AC.state.channels) {
    let pins;
    try { pins = await API.pins(pid, c.id); } catch (e) { continue; }
    const items = pins.items || [];
    if (!items.length) continue;
    any = true;
    page.append(el("h3", { class: "rail-title" }, "#" + c.name),
      ...items.map((p) => el("div", {
        class: "card", style: "margin-bottom: 8px; cursor: pointer",
        onclick: () => { location.hash = `#/p/${pid}/c/${c.id}/m/${p.id}`; },
      },
        el("div", { class: "small muted mono" }, `${p.author} · ${fmtTime(p.created_at)}`),
        el("div", {}, (p.body || "(attachment)").slice(0, 200)))));
  }
  if (!any) page.append(el("div", { class: "empty" }, el("div", { class: "glyph" }, "⌖"),
    el("div", { class: "e-title" }, "Nothing Pinned"),
    el("p", {}, "Pins from every channel collect here.")));
  AC.setView(page);
};

Views.decisions = async function () {
  const pid = AC.state.pid;
  const data = await API.decisions(pid, { limit: 100 });
  const items = data.items || [];
  const page = el("div", { class: "page" },
    el("h1", { class: "page-title" }, "Decisions"),
    el("p", { class: "muted", style: "margin-bottom: 16px" },
      "Messages posted with type \"decision\" — the project's agreed outcomes, newest first."),
    items.length ? el("div", {}, ...items.map((m) => el("div", { class: "msg is-decision", style: "margin-bottom: 8px" },
      // .m-av is what pins the avatar into the 36px grid column; without it,
      // auto-placement dropped the body into that column (issue #19).
      el("span", { class: "m-av" }, AC.avatarEl({ alias: m.author, avatar: m.avatar }, 36)),
      el("div", { class: "m-head" },
        el("span", { class: "m-author" }, m.author),
        el("span", { class: "role-tag decision" }, "decision"),
        el("span", { class: "m-time" }, fmtTime(m.created_at))),
      el("div", { class: "m-body" }, renderMd(m.body),
        el("button", {
          class: "btn small", style: "margin-top: 8px",
          onclick: () => { location.hash = `#/p/${pid}/c/${m.conversation_id}/m/${m.id}`; },
        }, "Jump to context")))))
      : el("div", { class: "empty" }, el("div", { class: "glyph" }, "◆"),
          el("div", { class: "e-title" }, "No Decisions Recorded"),
          el("p", {}, "Agents post decision-type messages to record agreed outcomes.")));
  AC.setView(page);
};
