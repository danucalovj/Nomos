/* DOM + formatting helpers shared by all views. */
"use strict";

/** HTML-escape a string for safe interpolation into markup. */
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** DOM builder: el('div', {class: 'x', onclick: fn}, child, 'text', ...) */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(c));
  }
  return node;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? hm : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hm;
}

function timeAgo(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function isOnline(lastSeen) {
  return !!lastSeen && Date.now() - new Date(lastSeen).getTime() < 5 * 60 * 1000;
}

function avatarColor(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360} 45% 42%)`;
}

function initials(name) {
  return String(name || "?").slice(0, 2).toUpperCase();
}

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

/* ---------- markdown rendering + sanitization ----------
   marked does not sanitize its output. We parse the generated HTML with
   DOMParser (inert document: scripts never execute), then walk the tree
   removing dangerous elements (script/style/iframe/object/embed/link/meta/
   form/base) and attributes (on* handlers, javascript:/data: URLs except
   data:image for <img>), before importing the nodes into the live DOM. */
const BLOCKED_TAGS = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "LINK", "META", "FORM", "BASE", "SVG", "MATH"]);

/** Allowlist check for href/src values. Browsers strip ASCII control chars
    and whitespace during URL parsing, so remove them BEFORE reading the
    scheme — `java\nscript:` must not sneak past a startsWith check. */
function safeUrl(raw, isImg) {
  const cleaned = String(raw).replace(/[\u0000-\u0020]/g, "").toLowerCase();
  if (isImg && cleaned.startsWith("data:image/")) return true;
  if (/^(https?|mailto):/.test(cleaned)) return true;
  if (cleaned.includes(":")) return false; // any other explicit scheme
  return true; // relative URL or fragment
}

function sanitizeInto(target, html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.body.querySelectorAll("*").forEach((node) => {
    if (BLOCKED_TAGS.has(node.tagName)) { node.remove(); return; }
    for (const attr of [...node.attributes]) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) node.removeAttribute(attr.name);
      else if ((name === "href" || name === "src" || name === "xlink:href" || name === "srcset") &&
               !safeUrl(attr.value, node.tagName === "IMG")) {
        node.removeAttribute(attr.name);
      }
    }
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });
  target.replaceChildren(...doc.body.childNodes);
}

/** Wrap @mentions and #N ticket refs found in text nodes (outside code blocks). */
function decorateTextRefs(container, ctx) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      let p = n.parentElement;
      while (p && p !== container) {
        if (p.tagName === "CODE" || p.tagName === "PRE" || p.tagName === "A") return NodeFilter.FILTER_REJECT;
        p = p.parentElement;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  const re = /(@[A-Za-z0-9][A-Za-z0-9_.-]*)|(#\d+)\b/g;
  for (const textNode of nodes) {
    const text = textNode.nodeValue;
    if (!re.test(text)) { re.lastIndex = 0; continue; }
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      frag.append(text.slice(last, m.index));
      if (m[1]) {
        const alias = m[1].slice(1);
        const isAdmin = ctx.adminAlias && alias.toLowerCase() === ctx.adminAlias.toLowerCase();
        const isHere = alias.toLowerCase() === "here";
        frag.append(el("span", {
          class: "mention" + (isAdmin ? " admin-mention" : "") + (isHere ? " here-mention" : ""),
        }, m[1]));
      } else {
        frag.append(el("a", { href: `#/p/${ctx.pid}/tickets/${m[2].slice(1)}` }, m[2]));
      }
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    textNode.replaceWith(frag);
  }
}

/** Replace :shortcode: tokens with unicode emoji in text nodes (outside
    code/pre/a), using the live emoji map. */
const SHORTCODE_RE = /:([a-z0-9_+-]+):/g;

function emojify(container, map) {
  if (!map || !Object.keys(map).length) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      let p = n.parentElement;
      while (p && p !== container) {
        if (p.tagName === "CODE" || p.tagName === "PRE") return NodeFilter.FILTER_REJECT;
        p = p.parentElement;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const textNode of nodes) {
    const text = textNode.nodeValue;
    SHORTCODE_RE.lastIndex = 0;
    if (!SHORTCODE_RE.test(text)) continue;
    textNode.nodeValue = text.replace(SHORTCODE_RE, (m, code) => map[code] || m);
  }
}

/** True when the raw message source is just 1-3 emoji (shortcodes or unicode
    emoji), which renders jumbo per the Slack feel. */
function isJumboSource(text, map) {
  const t = String(text || "").trim();
  if (!t) return false;
  const parts = t.split(/\s+/);
  if (parts.length > 3) return false;
  return parts.every((p) => {
    const sc = p.match(/^:([a-z0-9_+-]+):$/);
    if (sc) return !map || !!map[sc[1]];
    return /^(\p{Extended_Pictographic}|\p{Emoji_Presentation})(️|‍(\p{Extended_Pictographic}|\p{Emoji_Presentation}))*$/u.test(p);
  });
}

/** Add a hover copy button to every fenced code block in a rendered .md. */
function addCopyButtons(container) {
  container.querySelectorAll("pre").forEach((pre) => {
    const btn = el("button", {
      class: "copy-code", type: "button", title: "Copy Code",
      onclick: (e) => {
        e.preventDefault();
        const code = pre.querySelector("code");
        navigator.clipboard.writeText(code ? code.textContent : pre.textContent).then(() => {
          btn.classList.add("copied");
          btn.textContent = "copied ✓";
          setTimeout(() => { btn.classList.remove("copied"); btn.textContent = "copy"; }, 1400);
        }).catch(() => {});
      },
    }, "copy");
    pre.append(btn);
  });
}

/** Render markdown into a new .md div: marked -> sanitize -> hljs -> refs ->
    emoji -> copy buttons. */
function renderMd(text, ctx) {
  const emojiMap = (typeof AC !== "undefined" && AC.state.emojiMap) || null;
  const div = el("div", { class: "md" + (isJumboSource(text, emojiMap) ? " jumbo" : "") });
  sanitizeInto(div, marked.parse(String(text || ""), { gfm: true, breaks: true }));
  div.querySelectorAll("pre code").forEach((block) => { try { hljs.highlightElement(block); } catch (e) {} });
  decorateTextRefs(div, ctx || { pid: AC.state.pid, adminAlias: AC.state.adminAlias });
  emojify(div, emojiMap);
  addCopyButtons(div);
  return div;
}

/* ---------- line diff (LCS) for document revisions ---------- */
function lineDiff(aText, bText) {
  const a = String(aText || "").split("\n");
  const b = String(bText || "").split("\n");
  const n = a.length, m = b.length;
  // DP table of LCS lengths (fine for document-sized inputs)
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ type: "same", line: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: "del", line: a[i] }); i++; }
    else { out.push({ type: "add", line: b[j] }); j++; }
  }
  while (i < n) out.push({ type: "del", line: a[i++] });
  while (j < m) out.push({ type: "add", line: b[j++] });
  return out;
}

function renderDiff(aText, bText) {
  const wrap = el("div", { class: "diff-view" });
  for (const d of lineDiff(aText, bText)) {
    const prefix = d.type === "add" ? "+ " : d.type === "del" ? "- " : "  ";
    wrap.append(el("div", { class: "diff-line " + d.type }, prefix + d.line));
  }
  return wrap;
}

function statusClass(status) {
  const known = ["open", "in-progress", "awaiting-human", "blocked", "done", "wontfix"];
  return "status-pill st-" + (known.includes(status) ? status : "other");
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
