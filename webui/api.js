/* Nomos admin API layer. Every endpoint path used by the UI lives here.
   All calls are unauthenticated (implicit admin by design). Responses use the
   {ok, data|error} envelope; req() unwraps it and throws ApiError on failure. */
"use strict";

class ApiError extends Error {
  constructor(status, code, message, extra) {
    super(message);
    this.status = status;
    this.code = code;
    this.extra = extra || {};
  }
}

async function _req(method, path, body, opts) {
  opts = opts || {};
  const init = { method, headers: {} };
  if (body !== undefined && !(body instanceof FormData)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  } else if (body instanceof FormData) {
    init.body = body;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, "network", "Cannot reach the Nomos server.");
  }
  if (opts.blob) {
    if (!res.ok) throw new ApiError(res.status, "export_failed", "Download failed (HTTP " + res.status + ").");
    return await res.blob();
  }
  let payload = null;
  try { payload = await res.json(); } catch (e) { /* non-JSON error body */ }
  if (payload && payload.ok) return payload.data;
  const err = (payload && payload.error) || {};
  throw new ApiError(res.status, err.code || "http_" + res.status,
    err.message || "Request failed (HTTP " + res.status + ").", err);
}

const API = {
  // --- setup ---
  setupStatus: () => _req("GET", "/api/setup/status"),
  completeSetup: (alias, color, avatar) => _req("POST", "/api/setup", { alias, color, avatar }),

  // --- projects ---
  projects: (includeArchived) =>
    _req("GET", "/api/projects" + (includeArchived ? "?include_archived=true" : "")),
  project: (pid) => _req("GET", `/api/projects/${pid}`),
  createProject: (name, description, working_dir) =>
    _req("POST", "/api/projects", { name, description, working_dir: working_dir || "" }),
  agentNotes: (pid, agentId) => _req("GET", `/api/projects/${pid}/agents/${agentId}/notes`),
  browseDirs: (path) => _req("GET", "/api/fs/dirs" + (path ? `?path=${encodeURIComponent(path)}` : "")),
  updateProject: (pid, patch) => _req("PATCH", `/api/projects/${pid}`, patch),
  archiveProject: (pid) => _req("POST", `/api/projects/${pid}/archive`),
  unarchiveProject: (pid) => _req("POST", `/api/projects/${pid}/unarchive`),
  deleteProject: (pid) => _req("DELETE", `/api/projects/${pid}`),
  projectStatuses: (pid) => _req("GET", `/api/projects/${pid}/statuses`),
  exportProject: (pid) => _req("POST", `/api/projects/${pid}/export`, undefined, { blob: true }),

  // --- agents ---
  agents: (pid) => _req("GET", `/api/projects/${pid}/agents`),
  revokeAgent: (pid, aid) => _req("POST", `/api/projects/${pid}/agents/${aid}/revoke`),
  removeAgent: (pid, aid) => _req("DELETE", `/api/projects/${pid}/agents/${aid}`),

  // --- channels & DMs ---
  channels: (pid) => _req("GET", `/api/projects/${pid}/channels`),
  createChannel: (pid, name, topic) => _req("POST", `/api/projects/${pid}/channels`, { name, topic }),
  channel: (pid, cid) => _req("GET", `/api/projects/${pid}/channels/${cid}`),
  dms: (pid) => _req("GET", `/api/projects/${pid}/dms`),
  openDm: (pid, withAlias) => _req("POST", `/api/projects/${pid}/dms`, { with: withAlias }),

  // --- messages ---
  messages: (pid, cid, params) =>
    _req("GET", `/api/projects/${pid}/conversations/${cid}/messages` + _qs(params)),
  postMessage: (pid, cid, body) =>
    _req("POST", `/api/projects/${pid}/conversations/${cid}/messages`, body),
  message: (pid, mid) => _req("GET", `/api/projects/${pid}/messages/${mid}`),
  thread: (pid, mid) => _req("GET", `/api/projects/${pid}/messages/${mid}/thread`),
  editMessage: (pid, mid, body) => _req("PATCH", `/api/projects/${pid}/messages/${mid}`, { body }),
  deleteMessage: (pid, mid) => _req("DELETE", `/api/projects/${pid}/messages/${mid}`),
  pinMessage: (pid, mid) => _req("POST", `/api/projects/${pid}/messages/${mid}/pin`),
  unpinMessage: (pid, mid) => _req("POST", `/api/projects/${pid}/messages/${mid}/unpin`),
  pins: (pid, cid) => _req("GET", `/api/projects/${pid}/conversations/${cid}/pins`),
  decisions: (pid, params) => _req("GET", `/api/projects/${pid}/decisions` + _qs(params)),
  messageEdits: (pid, mid) => _req("GET", `/api/projects/${pid}/messages/${mid}/edits`),

  // --- events / mentions ---
  events: (pid, params) => _req("GET", `/api/projects/${pid}/events` + _qs(params)),
  streamUrl: (pid, sinceId) =>
    // != null, not truthiness: a cursor of 0 is a real position and dropping
    // it would skip the replay of every event in the gap (issue #29).
    `/api/projects/${pid}/stream` + (sinceId != null ? `?since_id=${sinceId}` : ""),
  mentions: (pid, unseenOnly) =>
    _req("GET", `/api/projects/${pid}/mentions` + (unseenOnly ? "?unseen=true" : "")),
  markMentionsSeen: (pid, ids) =>
    _req("POST", `/api/projects/${pid}/mentions/seen`, ids === true ? { all: true } : { mention_ids: ids }),

  // --- attachments ---
  uploadAttachment: (pid, file) => {
    const fd = new FormData();
    fd.append("file", file);
    return _req("POST", `/api/projects/${pid}/attachments`, fd);
  },
  attachmentUrl: (pid, attId) => `/api/projects/${pid}/attachments/${attId}`,

  // --- tickets ---
  tickets: (pid, params) => _req("GET", `/api/projects/${pid}/tickets` + _qs(params)),
  ticket: (pid, n) => _req("GET", `/api/projects/${pid}/tickets/${n}`),
  createTicket: (pid, body) => _req("POST", `/api/projects/${pid}/tickets`, body),
  updateTicket: (pid, n, patch) => _req("PATCH", `/api/projects/${pid}/tickets/${n}`, patch),
  deleteTicket: (pid, n) => _req("DELETE", `/api/projects/${pid}/tickets/${n}`),
  ticketComments: (pid, n) => _req("GET", `/api/projects/${pid}/tickets/${n}/comments`),
  postTicketComment: (pid, n, body) => _req("POST", `/api/projects/${pid}/tickets/${n}/comments`, body),

  // --- board ---
  board: (pid) => _req("GET", `/api/projects/${pid}/board`),
  setBoardColumns: (pid, columns) => _req("PUT", `/api/projects/${pid}/board/columns`, { columns }),
  moveCard: (pid, ticketNumber, columnId) =>
    _req("POST", `/api/projects/${pid}/board/move`, { ticket_number: ticketNumber, column_id: columnId }),

  // --- documents ---
  documents: (pid, params) => _req("GET", `/api/projects/${pid}/documents` + _qs(params)),
  document: (pid, slug, revision) =>
    _req("GET", `/api/projects/${pid}/documents/${encodeURIComponent(slug)}` + (revision ? `?revision=${revision}` : "")),
  createDocument: (pid, body) => _req("POST", `/api/projects/${pid}/documents`, body),
  saveDocument: (pid, slug, body) =>
    _req("PUT", `/api/projects/${pid}/documents/${encodeURIComponent(slug)}`, body),
  documentRevisions: (pid, slug) =>
    _req("GET", `/api/projects/${pid}/documents/${encodeURIComponent(slug)}/revisions`),

  // --- search / activity / metrics ---
  search: (pid, params) => _req("GET", `/api/projects/${pid}/search` + _qs(params)),
  activity: (pid, params) => _req("GET", `/api/projects/${pid}/activity` + _qs(params)),
  metrics: (pid) => _req("GET", `/api/projects/${pid}/metrics`),

  // --- reactions & emoji ---
  toggleReaction: (pid, mid, emoji) =>
    _req("POST", `/api/projects/${pid}/messages/${mid}/reactions`, { emoji }),
  frequentEmoji: (pid) => _req("GET", `/api/projects/${pid}/emoji/frequent`),

  // --- forwarding / saved / typing ---
  forwardMessage: (pid, mid, toConversationId, comment) =>
    _req("POST", `/api/projects/${pid}/messages/${mid}/forward`,
      { to_conversation_id: toConversationId, comment: comment || "" }),
  toggleSave: (pid, mid) => _req("POST", `/api/projects/${pid}/messages/${mid}/save`),
  saved: (pid, params) => _req("GET", `/api/projects/${pid}/saved` + _qs(params)),
  typing: (pid, cid) => _req("POST", `/api/projects/${pid}/conversations/${cid}/typing`),

  // --- identity ---
  updateAdminIdentity: (patch) => _req("PATCH", "/api/admin/identity", patch),
};

function _qs(params) {
  if (!params) return "";
  const pairs = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => pairs.push(encodeURIComponent(k) + "=" + encodeURIComponent(x)));
    else pairs.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
  }
  return pairs.length ? "?" + pairs.join("&") : "";
}
