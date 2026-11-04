/* The server dashboard: a server picker, then one server's settings on the ofuda shelf, plus the
   Commands guide. Uses the helpers from index.html ($, esc, api, toast, svg, M). Every save is
   validated again by the bot, which also checks the signed-in person has Manage Server there. */
(() => {
const TABS = [
  {id: "welcome", kanji: "迎", label: "Welcome", group: "People"},
  {id: "roles", kanji: "役", label: "Roles", group: "People"},
  {id: "forum", kanji: "授", label: "Forum roles", group: "People"},
  {id: "messages", kanji: "文", label: "Messages", group: "Posts"},
  {id: "tags", kanji: "札", label: "Tags", group: "Posts"},
  {id: "replies", kanji: "響", label: "Auto-replies", group: "Posts"},
  {id: "starboard", kanji: "星", label: "Starboard", group: "Community"},
  {id: "suggestions", kanji: "案", label: "Suggestions", group: "Community"},
  {id: "moderation", kanji: "守", label: "Moderation", group: "Safety"},
  {id: "logging", kanji: "記", label: "Logging", group: "Safety"},
  {id: "vc-generators", kanji: "室", label: "Generators", group: "VC maker"},
  {id: "vc-controls", kanji: "鍵", label: "Controls", group: "VC maker"},
  {id: "vc-extras", kanji: "添", label: "Extras", group: "VC maker"},
  {id: "tk-panels", kanji: "票", label: "Panels", group: "Tickets"},
  {id: "tk-list", kanji: "帳", label: "Tickets", group: "Tickets"},
  {id: "tk-settings", kanji: "規", label: "Settings", group: "Tickets"},
];
const LOGS = [
  ["messages", "Messages", "Deleted, edited and bulk-deleted messages."],
  ["members", "Members", "Joins, leaves, nickname and role changes."],
  ["moderation", "Moderation", "Bans, unbans, kicks and timeouts, also ones done by hand."],
  ["voice", "Voice", "Joining, leaving and moving between voice channels."],
  ["server", "Server", "Channels and roles created, deleted or renamed."],
];
const KIND_WORD = {buttons: "Buttons", dropdown: "Dropdown", reactions: "Reactions"};
const MODE_WORD = {normal: "Click to get, click again to lose", unique: "One role at a time", verify: "Get only, never taken away"};
const MATCH_WORD = {contains: "Anywhere in the message", word: "As a whole word", exact: "The whole message", starts: "At the start"};
const ACTION_PILL = {ban: "bad", unban: "ok", kick: "warn", timeout: "warn", untimeout: "ok", warn: "gold"};
const ACTION_WORD = {ban: "Ban", unban: "Unban", kick: "Kick", timeout: "Timeout", untimeout: "Timeout ended", warn: "Warning"};
const EMPTY_EMBED = {enabled: false, color: "#f2879f", author_name: "", author_icon: "", author_url: "", title: "", url: "",
  description: "", thumbnail: "", image: "", footer: "", footer_icon: "", timestamp: false, fields: []};
const newMessage = (content = "") => ({content, embed: JSON.parse(JSON.stringify(EMPTY_EMBED))});

let G = null;           // the open server
let tab = "welcome";
let editing = null;     // what an editor on the current tab is working on (a copy), or null
let casePage = 0, caseUser = "";

const root = () => $("#dash-root");
const body = () => $("#tab-body");
const field = (id, label, inner, help = "") =>
  `<div class="field"><label for="${id}">${label}</label>${inner}${help ? `<div class="help">${help}</div>` : ""}</div>`;
const sw = (id, label, on) =>
  `<label class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span class="track"></span>${label}</label>`;
const btn = (id, label, cls = "", icon = "") =>
  `<button class="btn ${cls}" type="button" id="${id}">${cls.includes("ghost") ? "" : '<span class="p5-hl" aria-hidden="true"></span>'}${icon ? svg(icon) : ""}<span>${esc(label)}</span></button>`;
const val = id => $("#" + id).value;
const on = id => $("#" + id).checked;
const loading = text => `<div class="loading-fox"><img class="mascot" src="${M("run")}" alt="" width="56" height="56"><span>${esc(text)}<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span></span></div>`;
const emptyState = (fox, title, text) => `<div class="empty"><div style="position:relative"><div class="fox-halo"></div><img class="mascot fox" src="${M(fox)}" alt="" width="132" height="132"></div><h3>${esc(title)}</h3><p>${text}</p></div>`;

function channelSelect(id, value, none = "Not set") {
  const groups = {};
  for (const c of G.channels) (groups[c.category || "No category"] ||= []).push(c);
  const opts = Object.entries(groups).map(([cat, list]) => `<optgroup label="${esc(cat)}">${list.map(c =>
    `<option value="${c.id}" ${c.id === value ? "selected" : ""} ${c.can_send ? "" : "disabled"}>#${esc(c.name)}${c.can_send ? "" : " (the bot can't post here)"}</option>`).join("")}</optgroup>`).join("");
  return `<select id="${id}"><option value="">${esc(none)}</option>${opts}</select>`;
}
function chips(name, items, selected) {
  return `<div class="chiprow" data-chips="${name}">${items.map(it =>
    `<button class="chip" type="button" data-id="${it.id}" aria-pressed="${selected.includes(it.id)}" ${it.problem ? "disabled" : ""}
      title="${esc(it.problem || "")}">${it.dot ? `<span aria-hidden="true" style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${esc(it.dot)};margin-right:6px"></span>` : ""}${esc(it.label)}</button>`).join("")}</div>`;
}
const roleChips = (name, sel) => chips(name, G.roles.map(r => ({id: r.id, label: "@" + r.name, dot: r.color, problem: r.problem})), sel);
const channelChips = (name, sel) => chips(name, G.channels.map(c => ({id: c.id, label: "#" + c.name})), sel);
const picked = name => [...root().querySelectorAll(`[data-chips="${name}"] .chip[aria-pressed="true"]`)].map(b => b.dataset.id);
function wireChips() {
  root().querySelectorAll(".chiprow[data-chips] .chip").forEach(b => b.onclick = () => b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")));
}

async function saveSection(section, data, okText) {
  try {
    const r = await api(`/api/dash/guild/${G.guild.id}/config`, {method: "POST", body: {section, data}});
    G.config[section] = r.data;
    toast("ok", "Saved", okText);
    return true;
  } catch (e) { toast("bad", "Not saved", e.message); return false; }
}

/* =====================================================================================
   The embed builder: content + an embed built field by field, with a live preview
   ===================================================================================== */
const B = new Map();  // builder id -> the message it edits (kept in sync while typing)
const mediaUrl = v => v.startsWith("upload:") ? `/api/dash/guild/${G.guild.id}/media/${encodeURIComponent(v.slice(7))}` : v;

function sample(text, isUrl = false) {
  const me = (window.ME && ME.user) || {};
  const map = {
    "user": "@" + (me.name || "NewMember"), "mention": "@" + (me.name || "NewMember"), "user.mention": "@" + (me.name || "NewMember"),
    "user.name": me.name || "newmember", "user.display": me.name || "New Member", "user.nick": me.name || "New Member",
    "user.id": me.id || "123456789012345678", "user.avatar": me.avatar || "/ds/icon.png", "user.icon": me.avatar || "/ds/icon.png",
    "user.created": "2 years ago", "server": G ? G.guild.name : "Your server", "server.name": G ? G.guild.name : "Your server",
    "server.icon": (G && G.guild.icon) || "", "server.id": G ? G.guild.id : "", "channel": "#welcome",
    "membercount": String(G ? G.guild.members ?? 0 : 0), "server.members": String(G ? G.guild.members ?? 0 : 0),
  };
  return (text || "").replace(/\{(user|mention|server|membercount|channel)(?:[.(]([a-z_]+)\)?)?\}/gi, (m, k, a) => {
    const key = (k + (a ? "." + a : "")).toLowerCase().replace("(", ".");
    return key in map ? map[key] : m;
  });
}
function md(text) {  // just enough markdown for a believable preview
  return esc(sample(text)).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>")
    .replace(/__(.+?)__/g, "<u>$1</u>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>")
    .replace(/(@[\w .-]{1,32}?)(?=$|[\s,.!?<])/g, '<span class="dc-mention">$1</span>');
}
const img = v => { const u = mediaUrl(sample(v, true)); return /^(https?:\/\/|\/)/.test(u) ? esc(u) : ""; };

function preview(msg) {
  const e = msg.embed;
  const a = e.enabled ? `<div class="dc-embed" style="border-left-color:${esc(e.color)}">
      <div class="dc-embed-main">
        ${e.author_name ? `<div class="dc-author">${img(e.author_icon) ? `<img src="${img(e.author_icon)}" alt="">` : ""}<span>${md(e.author_name)}</span></div>` : ""}
        ${e.title ? `<div class="dc-title">${md(e.title)}</div>` : ""}
        ${e.description ? `<div class="dc-desc">${md(e.description)}</div>` : ""}
        ${e.fields.length ? `<div class="dc-fields">${e.fields.map(f => `<div class="dc-field ${f.inline ? "inline" : ""}"><b>${md(f.name)}</b><div>${md(f.value)}</div></div>`).join("")}</div>` : ""}
        ${img(e.image) ? `<img class="dc-image" src="${img(e.image)}" alt="">` : ""}
        ${e.footer || e.timestamp ? `<div class="dc-footer">${img(e.footer_icon) ? `<img src="${img(e.footer_icon)}" alt="">` : ""}<span>${md(e.footer)}${e.footer && e.timestamp ? " · " : ""}${e.timestamp ? "Today at " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : ""}</span></div>` : ""}
      </div>${img(e.thumbnail) ? `<img class="dc-thumb" src="${img(e.thumbnail)}" alt="">` : ""}</div>` : "";
  return `<div class="dc-msg"><img class="dc-avatar" src="/ds/icon.png" alt=""><div style="min-width:0"><div class="dc-name">Yae Music <span class="dc-app">APP</span></div>
    ${msg.content ? `<div class="dc-content">${md(msg.content)}</div>` : ""}${a}</div></div>`;
}

function builder(id, msg, {contentLabel = "Text", contentHelp = ""} = {}) {
  B.set(id, msg);
  const e = msg.embed;
  const up = key => `<button class="btn icon xs ghost" type="button" data-up="${key}" aria-label="Upload a picture or GIF">${svg("upload")}</button>`;
  const urlIn = (key, label, ph) => `<div class="field"><label>${label}</label><div class="b-url"><input class="inp" data-k="${key}" value="${esc(e[key])}" placeholder="${ph}">${up(key)}</div></div>`;
  const help = "Placeholders: " + Object.keys(G.placeholders).map(k => `<code>${esc(k)}</code>`).join(" ");
  return `<div class="builder" id="b-${id}">
    <div class="b-edit">
      ${field(`b-${id}-content`, contentLabel, `<textarea id="b-${id}-content" data-k="content" rows="3" maxlength="2000" style="font-family:var(--body);font-size:15px">${esc(msg.content)}</textarea>`, contentHelp || help)}
      <div class="switches" style="margin-bottom:12px">${sw(`b-${id}-embed`, "Add an embed", e.enabled)}</div>
      <div class="b-embed" ${e.enabled ? "" : "hidden"}>
        <div class="b-row3">${urlIn("author_icon", "Author icon", "Link or {user.avatar}")}
          <div class="field"><label>Author name</label><input class="inp" data-k="author_name" maxlength="256" value="${esc(e.author_name)}"></div>
          <div class="field"><label>Author link</label><input class="inp" data-k="author_url" value="${esc(e.author_url)}" placeholder="https://"></div></div>
        <div class="b-row2"><div class="field"><label>Title</label><input class="inp" data-k="title" maxlength="256" value="${esc(e.title)}"></div>
          <div class="field"><label>Title link</label><input class="inp" data-k="url" value="${esc(e.url)}" placeholder="https://"></div></div>
        <div class="field"><label>Description <span class="b-count" data-for="description"></span></label><textarea class="inp" data-k="description" rows="5" maxlength="4096" style="font-family:var(--body);font-size:15px">${esc(e.description)}</textarea></div>
        <div class="field"><label>Fields <span class="muted">(${e.fields.length}/25)</span></label>
          ${e.fields.map((f, i) => `<div class="b-fieldrow" data-i="${i}"><input class="inp" data-f="name" maxlength="256" value="${esc(f.name)}" placeholder="Name">
            <input class="inp" data-f="value" maxlength="1024" value="${esc(f.value)}" placeholder="Value">
            <label class="switch" title="Side by side with its neighbours"><input type="checkbox" data-f="inline" ${f.inline ? "checked" : ""}><span class="track"></span>Inline</label>
            <button class="btn icon xs ghost" type="button" data-rmf="${i}" aria-label="Remove this field">${svg("x")}</button></div>`).join("")}
          <button class="btn sm" type="button" data-addf ${e.fields.length >= 25 ? "disabled" : ""}><span class="p5-hl" aria-hidden="true"></span>${svg("plus")}<span>Add a field</span></button></div>
        <div class="b-row2">${urlIn("image", "Image, big at the bottom", "Link to a picture or GIF")}${urlIn("thumbnail", "Thumbnail, small at the top right", "Link or {user.avatar}")}</div>
        <div class="b-row3"><div class="field"><label>Footer</label><input class="inp" data-k="footer" maxlength="2048" value="${esc(e.footer)}"></div>
          ${urlIn("footer_icon", "Footer icon", "Link or {server.icon}")}
          <div class="field"><label>Colour</label><input type="color" data-k="color" value="${esc(e.color)}" style="height:44px;padding:4px;width:100%"></div></div>
        <div class="switches">${sw(`b-${id}-ts`, "Show the time it was posted", e.timestamp)}</div>
      </div>
      <input type="file" hidden accept="image/png,image/jpeg,image/gif,image/webp" class="b-file">
    </div>
    <div class="b-preview"><div class="b-preview-label">Preview</div><div class="b-preview-box"></div>
      <p class="help">Placeholders show sample values here. GIFs move in Discord too.</p></div>
  </div>`;
}
function wireBuilder(id) {
  const box = $("#b-" + id), msg = B.get(id), e = msg.embed;
  const paint = () => {
    $(".b-preview-box", box).innerHTML = preview(msg);
    const c = $('[data-for="description"]', box);
    if (c) c.textContent = `${e.description.length}/4096`;
  };
  box.querySelectorAll("[data-k]").forEach(inp => inp.oninput = () => {
    if (inp.dataset.k === "content") msg.content = inp.value; else e[inp.dataset.k] = inp.value;
    paint();
  });
  box.querySelectorAll(".b-fieldrow").forEach(row => row.querySelectorAll("[data-f]").forEach(inp => inp.oninput = inp.onchange = () => {
    const f = e.fields[+row.dataset.i];
    f[inp.dataset.f] = inp.type === "checkbox" ? inp.checked : inp.value;
    paint();
  }));
  $(`#b-${id}-embed`).onchange = ev => { e.enabled = ev.target.checked; $(".b-embed", box).hidden = !e.enabled; paint(); };
  $(`#b-${id}-ts`).onchange = ev => { e.timestamp = ev.target.checked; paint(); };
  const redraw = () => { const host = box.parentElement; host.innerHTML = builder(id, msg); wireBuilder(id); };
  box.querySelector("[data-addf]").onclick = () => { e.fields.push({name: "", value: "", inline: false}); redraw(); };
  box.querySelectorAll("[data-rmf]").forEach(b => b.onclick = () => { e.fields.splice(+b.dataset.rmf, 1); redraw(); });
  const file = $(".b-file", box);
  let target = null;
  box.querySelectorAll("[data-up]").forEach(b => b.onclick = () => { target = b.dataset.up; file.click(); });
  file.onchange = async () => {
    const f = file.files[0];
    file.value = "";
    if (!f || !target) return;
    if (f.size > 8 * 1024 * 1024) return toast("bad", "That file is too big", "Uploads can be up to 8 MB.");
    const data = await new Promise(res => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(f); });
    try {
      const r = await api(`/api/dash/guild/${G.guild.id}/media`, {method: "POST", body: {filename: f.name, data}});
      e[target] = r.token;
      redraw();
      toast("ok", "Uploaded", `${f.name} is in the embed. Save to keep it.`);
    } catch (err) { toast("bad", "Not uploaded", err.message); }
  };
  paint();
}
const readBuilder = id => JSON.parse(JSON.stringify(B.get(id)));
const builderHost = (id, msg, opts) => `<div>${builder(id, msg, opts)}</div>`;

/* =====================================================================================
   Entry points (index.html calls these)
   ===================================================================================== */
window.dashRoute = async parts => {
  const [gid, t] = parts;
  if (!gid) return showPicker();
  if (!G || G.guild.id !== gid) {
    root().innerHTML = loading("Reading the server's channels and roles");
    try { G = await api(`/api/dash/guild/${gid}`); }
    catch (e) { G = null; return showProblem(e.message); }
    casePage = 0; caseUser = "";
  }
  const next = TABS.some(x => x.id === t) ? t : "welcome";
  if (next !== tab) editing = null;
  tab = next;
  showGuild();
};

function showProblem(text) {
  root().innerHTML = emptyState("hurt", "This server can't be opened", esc(text)) +
    `<div class="btnrow" style="justify-content:center"><a class="btn" href="#servers"><span class="p5-hl" aria-hidden="true"></span><span>All servers</span></a></div>`;
}

async function showPicker() {
  G = null;
  $("#crumb-t").textContent = "Servers";
  root().innerHTML = loading("Finding the servers you can manage");
  let data;
  try { data = await api("/api/dash/guilds"); } catch (e) { return showProblem(e.message); }
  const owner = window.ME && ME.kind === "owner";
  const head = `<header class="fleet-head page-intro"><div class="fh-words">
      <p class="eyebrow">Dashboard · <b>${data.guilds.length} server${data.guilds.length === 1 ? "" : "s"}</b></p>
      <h1 class="brush fh-title">Your servers</h1>
      <p class="lede">${owner ? "As the panel's owner you can open every server the bot is in." : "Servers where you have the Manage Server permission and the bot is a member."}</p>
    </div><img class="mascot fh-fox" src="${M("fox-pounce")}" alt="" width="92" height="92"></header>${intentsNote(data.intents)}`;
  if (!data.guilds.length) {
    root().innerHTML = head + emptyState("placeblock", "No servers to manage yet", owner
      ? "The bot isn't in any server. Add it with the invite on the Overview."
      : "You need the Manage Server permission in a server the bot is in.");
    return;
  }
  root().innerHTML = head + `<div class="grid gauto bleed">${data.guilds.map(g => `
    <a class="card hoverable" href="#servers/${g.id}" style="text-decoration:none;color:inherit"><span class="p5-hl" aria-hidden="true"></span>
      <header><h3>${esc(g.name)}</h3><span class="k">${g.members ?? "?"} members</span></header>
      <div style="display:flex;align-items:center;gap:14px">
        ${g.icon ? `<img src="${esc(g.icon)}" alt="" width="56" height="56" style="border-radius:50%;border:2.5px solid var(--slab-bg)">` : `<span class="seal dead" role="img" aria-label="No icon"><b>${esc(g.name.slice(0, 1))}</b></span>`}
        <p class="card-sub" style="margin:0">Welcome, roles, messages, tags, moderation and logs.</p>
      </div></a>`).join("")}</div>`;
}

function intentsNote(i) {
  if (!i || (i.members && i.message_content)) return "";
  return `<div class="note bad" style="margin-bottom:18px"><b>Two Discord switches are off</b>Welcome messages, autorole, member logs and auto-replies need <i>Server Members Intent</i> and <i>Message Content Intent</i>. Turn both on in the Developer Portal → your application → Bot, then restart the bot from the Overview.</div>`;
}

function showGuild() {
  const g = G.guild;
  $("#crumb-t").textContent = g.name;
  const missing = G.missing.length ? `<div class="note warn" style="margin-bottom:18px"><b>The bot is missing ${G.missing.length === 1 ? "a permission" : "some permissions"}</b>${G.missing.map(m => `${esc(m.perm.replace(/_/g, " "))} (for ${esc(m.for)})`).join(" · ")}. Give its role these in Server Settings → Roles → ${esc(G.bot_role)}.</div>` : "";
  const groups = [...new Set(TABS.map(t => t.group))];
  const count = {roles: G.menus.length, messages: G.posts.length, tags: G.tags.length, replies: G.config.autoresponders.items.length,
                 forum: G.config.forumroles.rules.length, "vc-generators": G.config.voicemaster.setups.length,
                 "tk-panels": (G.ticket_panels || []).length};
  root().innerHTML = `
    <header class="fleet-head page-intro"><div class="fh-words">
      <p class="eyebrow"><a href="#servers">All servers</a> · <b>${esc(g.id)}</b></p>
      <h1 class="brush fh-title">${esc(g.name)}</h1>
      <p class="lede">${g.members ?? "?"} members. Each panel saves on its own.</p>
    </div>${g.icon ? `<img class="fh-fox" src="${esc(g.icon)}" alt="" width="92" height="92" style="border-radius:50%;border:2.5px solid var(--slab-bg)">` : ""}</header>
    ${intentsNote(G.intents)}${missing}
    <nav class="shelf" role="tablist" aria-label="Server settings" style="margin-bottom:24px"><span class="shelf-rope" aria-hidden="true"></span>
      ${groups.map(gr => `<div class="shelf-group"><span class="sg-label">${gr}</span>${TABS.filter(t => t.group === gr).map(t =>
        `<button class="slipbtn" type="button" role="tab" aria-selected="${t.id === tab}" data-tab="${t.id}"><span class="sl-cord" aria-hidden="true"></span><span class="sl-kanji" aria-hidden="true">${t.kanji}</span><span class="sl-label">${t.label}</span>${count[t.id] ? `<span class="n">${count[t.id]}</span>` : ""}</button>`).join("")}</div>`).join("")}
    </nav>
    <div id="tab-body"></div>`;
  root().querySelectorAll(".slipbtn").forEach(b => b.onclick = () => { location.hash = `#servers/${g.id}/${b.dataset.tab}`; });
  ({welcome: tabWelcome, roles: tabRoles, forum: tabForum, messages: tabMessages, tags: tabTags, replies: tabReplies,
    starboard: tabStarboard, suggestions: tabSuggestions, moderation: tabModeration, logging: tabLogging,
    "vc-generators": tabVcGenerators, "vc-controls": tabVcControls, "vc-extras": tabVcExtras,
    "tk-panels": tabTkPanels, "tk-list": tabTkList, "tk-settings": tabTkSettings})[tab]();
}

/* ---------- Welcome ---------- */
function tabWelcome() {
  const w = G.config.welcome, f = G.config.farewell, a = G.config.autorole;
  body().innerHTML = `<div class="stack">
    <section class="panel"><header><h3>Welcome message</h3><span class="sp">${w.enabled ? "On" : "Off"}</span></header><div class="pad">
      <div class="b-row2">${field("w-channel", "Channel", channelSelect("w-channel", w.channel_id))}<div class="switches" style="align-content:center">${sw("w-enabled", "Post this when someone joins", w.enabled)}</div></div>
      ${builderHost("welcome", JSON.parse(JSON.stringify(w.message)), {contentLabel: "Text above the embed", contentHelp: "<code>{user}</code> here pings the new member. Placeholders work in every field."})}
      <div class="btnrow">${btn("w-save", "Save", "primary", "save")}${btn("w-test", "Send a test", "")}</div>
      <p class="muted" style="font-size:12.5px;margin-top:10px">In Discord, <code>/greetings testgreet</code> posts it in the channel you're in.</p>
    </div></section>
    <div class="grid g2">
      <section class="panel"><header><h3>Welcome DM</h3><span class="sp">${w.dm_enabled ? "On" : "Off"}</span></header><div class="pad">
        <div class="switches" style="margin-bottom:12px">${sw("w-dm", "Also send new members a direct message", w.dm_enabled)}</div>
        ${builderHost("dm", JSON.parse(JSON.stringify(w.dm)), {contentLabel: "Message"})}
        <p class="help">Only reaches people whose DMs are open. Saved with the welcome message.</p>
      </div></section>
      <section class="panel"><header><h3>Autorole</h3><span class="sp">${a.enabled ? "On" : "Off"}</span></header><div class="pad">
        <div class="switches" style="margin-bottom:16px">${sw("a-enabled", "Give roles to everyone who joins", a.enabled)}${sw("a-restore", "Give people their roles back when they rejoin", a.restore_roles)}</div>
        <div class="field"><label>Roles for new members</label>${roleChips("a-roles", a.role_ids)}</div>
        <div class="field"><label>Roles for new bots</label>${roleChips("a-bots", a.bot_role_ids)}<div class="help">Greyed-out roles are above the bot's own role (${esc(G.bot_role)}) or belong to an integration.</div></div>
        <div class="btnrow">${btn("a-save", "Save", "", "save")}</div>
      </div></section>
    </div>
    <section class="panel"><header><h3>Farewell message</h3><span class="sp">${f.enabled ? "On" : "Off"}</span></header><div class="pad">
      <div class="b-row2">${field("f-channel", "Channel", channelSelect("f-channel", f.channel_id))}<div class="switches" style="align-content:center">${sw("f-enabled", "Post this when someone leaves", f.enabled)}</div></div>
      ${builderHost("farewell", JSON.parse(JSON.stringify(f.message)), {contentLabel: "Text above the embed"})}
      <div class="btnrow">${btn("f-save", "Save", "", "save")}${btn("f-test", "Send a test", "ghost")}</div>
    </div></section></div>`;
  ["welcome", "dm", "farewell"].forEach(wireBuilder);
  wireChips();
  $("#w-save").onclick = () => saveSection("welcome", {enabled: on("w-enabled"), channel_id: val("w-channel") || null,
    message: readBuilder("welcome"), dm_enabled: on("w-dm"), dm: readBuilder("dm")}, "New members are greeted with this from now on.");
  $("#f-save").onclick = () => saveSection("farewell", {enabled: on("f-enabled"), channel_id: val("f-channel") || null,
    message: readBuilder("farewell")}, "It posts when someone leaves.");
  $("#a-save").onclick = () => saveSection("autorole", {enabled: on("a-enabled"), restore_roles: on("a-restore"),
    role_ids: picked("a-roles"), bot_role_ids: picked("a-bots")}, "New members get these roles from now on.");
  $("#w-test").onclick = () => test("welcome");
  $("#f-test").onclick = () => test("farewell");
}
async function test(section) {
  try {
    await api(`/api/dash/guild/${G.guild.id}/test`, {method: "POST", body: {section}});
    toast("ok", "Test sent", `The saved ${section} message was posted, with you as the member. Save first if you changed it.`);
  } catch (e) { toast("bad", "The test didn't go out", e.message); }
}

/* ---------- Roles (reaction-role menus) ---------- */
function tabRoles() {
  if (editing) return menuEditor();
  const ch = id => (G.channels.find(c => c.id === id) || {name: "deleted-channel"}).name;
  body().innerHTML = `<div class="btnrow" style="margin-bottom:18px">${btn("rr-new", "New menu", "primary", "plus")}</div>` + (G.menus.length
    ? `<div class="grid gauto">${G.menus.map(m => `<div class="card"><header><h3>${esc(m.title)}</h3><span class="k">${KIND_WORD[m.kind]}</span></header>
        <p class="card-sub">#${esc(ch(m.channel_id))} · ${m.roles.length} role${m.roles.length === 1 ? "" : "s"} · ${esc(MODE_WORD[m.mode])}</p>
        ${m.message_id ? "" : `<div class="note warn" style="margin:8px 0"><b>Its message was deleted</b>Save it again to post it again.</div>`}
        <div class="btnrow">${btn("rr-edit-" + m.id, "Edit", "sm")}${btn("rr-del-" + m.id, "Delete", "sm ghost")}</div></div>`).join("")}</div>`
    : emptyState("placeblock", "No role menus yet", "A menu is a message with buttons, a dropdown or reactions. Members click to give themselves roles: colours, pings, regions, game roles."));
  $("#rr-new").onclick = () => { editing = {kind: "buttons", mode: "normal", title: "Pick your roles", description: "", color: "#f2879f", channel_id: "", roles: []}; menuEditor(); };
  for (const m of G.menus) {
    $("#rr-edit-" + m.id).onclick = () => { editing = JSON.parse(JSON.stringify(m)); menuEditor(); };
    $("#rr-del-" + m.id).onclick = async () => {
      if (!confirm(`Delete "${m.title}"? Its message is removed from Discord. Roles members already have stay.`)) return;
      try { await api(`/api/dash/guild/${G.guild.id}/menus/${m.id}`, {method: "DELETE"}); G.menus = G.menus.filter(x => x.id !== m.id); toast("ok", "Menu deleted", "Its message is gone from Discord."); showGuild(); }
      catch (e) { toast("bad", "Not deleted", e.message); }
    };
  }
}
function menuEditor() {
  const m = editing, assignable = G.roles.filter(r => !r.problem);
  const seg = (name, value, opts) => `<div class="seg" role="group" data-seg="${name}">${Object.entries(opts).map(([k, v]) =>
    `<button type="button" data-v="${k}" aria-pressed="${k === value}">${esc(v)}</button>`).join("")}</div>`;
  const rows = m.roles.map((r, i) => `<tr data-i="${i}">
      <td><select class="inp rr-role" aria-label="Role">${assignable.map(x => `<option value="${x.id}" ${x.id === r.role_id ? "selected" : ""}>@${esc(x.name)}</option>`).join("")}</select></td>
      <td><input class="inp rr-emoji" value="${esc(r.emoji || "")}" placeholder="${m.kind === "reactions" ? "Needed" : "Optional"}" aria-label="Emoji" style="width:110px"></td>
      <td><input class="inp rr-label" value="${esc(r.label || "")}" placeholder="The role's name" aria-label="Label" ${m.kind === "reactions" ? "disabled" : ""}></td>
      ${m.kind === "dropdown" ? `<td><input class="inp rr-desc" value="${esc(r.description || "")}" placeholder="Optional" aria-label="Description"></td>` : ""}
      <td><button class="btn icon xs ghost rr-rm" type="button" aria-label="Remove this role">${svg("x")}</button></td></tr>`).join("");
  body().innerHTML = `<section class="panel"><header><h3>${m.id ? "Edit the menu" : "New menu"}</h3><span class="sp">${m.id ? "Menu " + m.id : "Not posted yet"}</span></header><div class="pad">
    <div class="grid g2">
      <div>${field("rr-channel", "Channel", channelSelect("rr-channel", m.channel_id, "Pick a channel"), "Changing it moves the menu: the old message is deleted.")}
        <div class="field"><label>Kind</label>${seg("kind", m.kind, KIND_WORD)}<div class="help">Buttons and the dropdown work best. Reactions use emoji under the message.</div></div>
        <div class="field"><label>How roles behave</label>${seg("mode", m.mode, MODE_WORD)}</div></div>
      <div>${field("rr-title", "Title", `<input id="rr-title" value="${esc(m.title)}" maxlength="256">`)}
        ${field("rr-desc", "Text above the menu", `<textarea id="rr-desc" rows="4" style="font-family:var(--body);font-size:15px">${esc(m.description)}</textarea>`, "Reaction menus list each emoji and role under this by themselves.")}
        ${field("rr-color", "Colour", `<input type="color" id="rr-color" value="${esc(m.color)}" style="height:44px;padding:4px">`)}</div>
    </div>
    <div class="field"><label>Roles in the menu</label>
      ${m.roles.length ? `<div class="tablewrap"><table class="tbl"><thead><tr><th>Role</th><th>Emoji</th><th>Label</th>${m.kind === "dropdown" ? "<th>Description</th>" : ""}<th><span class="sr-only">Remove</span></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="muted">No roles yet.</p>`}
      <div class="help">Emoji: paste one, or a custom one from this server: ${G.emojis.length ? G.emojis.slice(0, 12).map(e => `<button class="chip rr-emo" type="button" data-e="${esc(e.text)}" title=":${esc(e.name)}:"><img src="${esc(e.url)}" alt=":${esc(e.name)}:" width="16" height="16" style="vertical-align:-3px"></button>`).join(" ") : "this server has none"}.</div>
    </div>
    <div class="btnrow">${btn("rr-add", "Add a role", "sm", "plus")}</div>
    <hr>
    <div class="btnrow">${btn("rr-save", m.id ? "Save and update the message" : "Post the menu", "primary", "save")}${btn("rr-cancel", "Cancel", "ghost")}</div>
  </div></section>`;
  const grab = () => {
    m.channel_id = val("rr-channel"); m.title = val("rr-title"); m.description = val("rr-desc"); m.color = val("rr-color");
    body().querySelectorAll("tbody tr").forEach(tr => {
      const r = m.roles[+tr.dataset.i];
      r.role_id = $(".rr-role", tr).value; r.emoji = $(".rr-emoji", tr).value.trim(); r.label = $(".rr-label", tr).value.trim();
      if ($(".rr-desc", tr)) r.description = $(".rr-desc", tr).value.trim();
    });
  };
  body().querySelectorAll("[data-seg] button").forEach(b => b.onclick = () => { grab(); m[b.parentElement.dataset.seg] = b.dataset.v; menuEditor(); });
  body().querySelectorAll(".rr-rm").forEach(b => b.onclick = () => { grab(); m.roles.splice(+b.closest("tr").dataset.i, 1); menuEditor(); });
  let lastEmoji = null;
  body().querySelectorAll(".rr-emoji").forEach(inp => inp.onfocus = () => lastEmoji = inp);
  body().querySelectorAll(".rr-emo").forEach(b => b.onclick = () => { (lastEmoji || body().querySelector(".rr-emoji") || {}).value = b.dataset.e; });
  $("#rr-add").onclick = () => {
    grab();
    const free = assignable.find(r => !m.roles.some(x => x.role_id === r.id));
    if (!free) return toast("warn", "No more roles to add", assignable.length ? "Every role the bot can hand out is already in the menu." : `The bot can't hand out any role yet. Drag its role (${G.bot_role}) above the roles it should give.`);
    m.roles.push({role_id: free.id, emoji: "", label: "", description: ""});
    menuEditor();
  };
  $("#rr-cancel").onclick = () => { editing = null; tabRoles(); };
  $("#rr-save").onclick = async () => {
    grab();
    const b = $("#rr-save"); b.classList.add("busy"); b.disabled = true;
    try {
      const r = await api(`/api/dash/guild/${G.guild.id}/menus` + (m.id ? `/${m.id}` : ""), {method: m.id ? "PUT" : "POST", body: m});
      G.menus = G.menus.filter(x => x.id !== r.menu.id).concat([r.menu]).sort((a, b) => a.id - b.id);
      toast("ok", m.id ? "Menu updated" : "Menu posted", "It's live in Discord now.");
      editing = null;
      showGuild();
    } catch (e) { toast("bad", "Not posted", e.message); b.classList.remove("busy"); b.disabled = false; }
  };
}

/* ---------- Forum roles: a tag on a forum post gives its author a role ---------- */
function tabForum() {
  const cfg = G.config.forumroles;
  if (!G.forums.length) {
    body().innerHTML = emptyState("idle", "This server has no forum channels",
      "Forum roles watch a forum channel: when a post gets a tag you pick, like Accepted, its author gets a role. Make a forum channel in Discord first, then come back.");
    return;
  }
  if (!editing) editing = {rules: JSON.parse(JSON.stringify(cfg.rules))};
  const rules = editing.rules, assignable = G.roles.filter(r => !r.problem);
  const forumOf = r => G.forums.find(f => f.id === r.forum_id) || G.forums[0];
  const tagName = (f, id) => { const t = f.tags.find(t => t.id === id); return t ? (t.emoji ? t.emoji + " " : "") + t.name : "?"; };
  body().innerHTML = `<p class="lede" style="margin-bottom:16px">When a post in a forum gets the tag you pick, the person who made the post gets a role. It doesn't matter who adds the tag, so make the tag moderator-only in the forum's settings.</p>
    <section class="panel"><header><h3>Forum roles</h3><span class="sp">${rules.length}/25</span></header><div class="pad">
      <div class="b-row2">${field("fr-log", "Log channel", channelSelect("fr-log", cfg.log_channel_id, "No log"), "A line for every role given or taken, with who added the tag.")}
        <div class="switches" style="align-content:center">${sw("fr-enabled", "Give roles for tagged posts", cfg.enabled)}</div></div>
      ${rules.map((r, i) => {
        const f = forumOf(r);
        if (!r.forum_id) r.forum_id = f.id;
        if (!f.tags.some(t => t.id === r.tag_id)) r.tag_id = f.tags[0] ? f.tags[0].id : "";
        if (!r.role_id && assignable[0]) r.role_id = assignable[0].id;
        const tag = f.tags.find(t => t.id === r.tag_id);
        const role = G.roles.find(x => x.id === r.role_id);
        return `<details class="ar-item" ${r._open ? "open" : ""} data-i="${i}"><summary><b>#${esc(f.name)} · ${esc(tag ? tagName(f, tag.id) : "no tag")} → @${esc(role ? role.name : "?")}</b></summary>
        <div class="b-row3" style="margin-top:12px">
          ${field(`fr-f-${i}`, "Forum", `<select id="fr-f-${i}" class="fr-forum">${G.forums.map(x => `<option value="${x.id}" ${x.id === f.id ? "selected" : ""}>#${esc(x.name)}</option>`).join("")}</select>`)}
          ${field(`fr-t-${i}`, "When a post gets this tag", f.tags.length ? `<select id="fr-t-${i}">${f.tags.map(t => `<option value="${t.id}" ${t.id === r.tag_id ? "selected" : ""}>${esc(tagName(f, t.id))}${t.moderated ? "" : " (anyone can add it)"}</option>`).join("")}</select>` : `<select id="fr-t-${i}" disabled><option>#${esc(f.name)} has no tags</option></select>`)}
          ${field(`fr-r-${i}`, "Its author gets", `<select id="fr-r-${i}">${assignable.map(x => `<option value="${x.id}" ${x.id === r.role_id ? "selected" : ""}>@${esc(x.name)}</option>`).join("")}</select>`)}
        </div>
        ${tag && !tag.moderated ? `<div class="note warn" style="margin-bottom:12px"><b>Anyone can add this tag</b>The post's author could tag their own post and get the role. In Discord, edit #${esc(f.name)} → Tags → ${esc(tag.name)} → turn on "Only allow moderators to apply this tag".</div>` : ""}
        ${!f.tags.length ? `<div class="note warn" style="margin-bottom:12px"><b>No tags yet</b>Add tags to #${esc(f.name)} in Discord (edit the forum → Tags), then reload this page.</div>` : ""}
        <div class="switches" style="margin-bottom:12px">
          ${sw(`fr-rm-${i}`, "Take the role away if the tag is removed again", r.remove_on_untag)}
          ${sw(`fr-me-${i}`, "Post a message in the post", r.message_enabled)}
          ${sw(`fr-dm-${i}`, "Send the author a DM", r.dm_enabled)}</div>
        <div id="fr-mbox-${i}" ${r.message_enabled ? "" : "hidden"}>${builderHost("frm" + i, r.message || newMessage("Congratulations {user}, your post was accepted!"), {contentLabel: "Message in the post"})}</div>
        <div id="fr-dbox-${i}" ${r.dm_enabled ? "" : "hidden"}>${builderHost("frd" + i, r.dm || newMessage("Your post in {server} was accepted, and you've been given a new role."), {contentLabel: "DM"})}</div>
        ${!f.can_post && r.message_enabled ? `<div class="note warn" style="margin-bottom:12px"><b>The bot can't post in #${esc(f.name)}</b>Give it View Channel and Send Messages in Threads there.</div>` : ""}
        <div class="btnrow">${btn(`fr-del-${i}`, "Remove this rule", "sm ghost")}</div></details>`;
      }).join("")}
      ${rules.length ? "" : `<p class="muted">No rules yet. Add one: a forum, a tag and a role.</p>`}
      <div class="btnrow" style="margin-top:14px">${btn("fr-add", "Add a rule", "", "plus")}${btn("fr-save", "Save", "primary", "save")}</div>
      <p class="help" style="margin-top:10px">If the bot was offline when a post was tagged, it gives the role when it comes back, quietly (no message or DM), for the newest posts.</p>
    </div></section>`;
  rules.forEach((r, i) => { wireBuilder("frm" + i); wireBuilder("frd" + i); });
  const grab = () => rules.forEach((r, i) => {
    r.forum_id = val(`fr-f-${i}`); r.tag_id = $(`#fr-t-${i}`).disabled ? "" : val(`fr-t-${i}`); r.role_id = val(`fr-r-${i}`);
    r.remove_on_untag = on(`fr-rm-${i}`); r.message_enabled = on(`fr-me-${i}`); r.dm_enabled = on(`fr-dm-${i}`);
    r.message = readBuilder("frm" + i); r.dm = readBuilder("frd" + i); r._open = $(`details[data-i="${i}"]`).open;
  });
  rules.forEach((r, i) => {
    $(`#fr-f-${i}`).onchange = () => { grab(); r.tag_id = ""; tabForum(); };
    $(`#fr-t-${i}`).onchange = () => { grab(); tabForum(); };
    $(`#fr-me-${i}`).onchange = e => { $(`#fr-mbox-${i}`).hidden = !e.target.checked; };
    $(`#fr-dm-${i}`).onchange = e => { $(`#fr-dbox-${i}`).hidden = !e.target.checked; };
    $(`#fr-del-${i}`).onclick = () => { grab(); rules.splice(i, 1); tabForum(); };
  });
  $("#fr-add").onclick = () => {
    grab();
    if (!assignable.length) return toast("warn", "No role to give yet", `The bot can't hand out any role. Drag its role (${G.bot_role}) above the roles it should give.`);
    rules.push({forum_id: G.forums[0].id, tag_id: "", role_id: assignable[0].id, remove_on_untag: false,
                message_enabled: false, dm_enabled: false, _open: true});
    tabForum();
  };
  $("#fr-save").onclick = async () => {
    grab();
    const ok = await saveSection("forumroles", {enabled: on("fr-enabled"), log_channel_id: val("fr-log") || null,
      rules: rules.map(({_open, ...x}) => x)}, "Tagged posts give their authors these roles from now on.");
    if (ok) { editing = null; showGuild(); }
  };
}

/* ---------- Messages (embeds posted from here, editable later) ---------- */
function tabMessages() {
  if (editing) return postEditor();
  const ch = id => (G.channels.find(c => c.id === id) || {name: "deleted-channel"}).name;
  body().innerHTML = `<p class="lede" style="margin-bottom:16px">Write a message or an embed here and the bot posts it: rules, announcements, info panels. You can edit it later and the posted message changes too.</p>
    <div class="btnrow" style="margin-bottom:18px">${btn("p-new", "New message", "primary", "plus")}</div>` + (G.posts.length
    ? `<div class="grid gauto">${G.posts.map(p => `<div class="card"><header><h3>${esc(p.name)}</h3><span class="k">#${esc(ch(p.channel_id))}</span></header>
        <p class="card-sub">${esc((p.message.embed.enabled ? p.message.embed.title || p.message.embed.description : p.message.content).slice(0, 90) || "Embed")}</p>
        <div class="btnrow">${btn("p-edit-" + p.id, "Edit", "sm")}${btn("p-del-" + p.id, "Delete", "sm ghost")}</div></div>`).join("")}</div>`
    : emptyState("placeblock", "Nothing posted from here yet", "Make a rules embed, an announcement or an info panel, with images or GIFs."));
  $("#p-new").onclick = () => { editing = {name: "", channel_id: "", message: newMessage()}; editing.message.embed.enabled = true; postEditor(); };
  for (const p of G.posts) {
    $("#p-edit-" + p.id).onclick = () => { editing = JSON.parse(JSON.stringify(p)); postEditor(); };
    $("#p-del-" + p.id).onclick = async () => {
      if (!confirm(`Delete "${p.name}"? The message is removed from Discord too.`)) return;
      try { await api(`/api/dash/guild/${G.guild.id}/posts/${p.id}`, {method: "DELETE"}); G.posts = G.posts.filter(x => x.id !== p.id); toast("ok", "Deleted", "The message is gone from Discord."); showGuild(); }
      catch (e) { toast("bad", "Not deleted", e.message); }
    };
  }
}
function postEditor() {
  const p = editing;
  body().innerHTML = `<section class="panel"><header><h3>${p.id ? "Edit the message" : "New message"}</h3></header><div class="pad">
    <div class="b-row2">${field("p-name", "Name (only shown here)", `<input id="p-name" value="${esc(p.name)}" maxlength="80" placeholder="Rules">`)}
      ${field("p-channel", "Channel", channelSelect("p-channel", p.channel_id, "Pick a channel"), "Changing it moves the message: the old one is deleted.")}</div>
    ${builderHost("post", p.message)}
    <div class="btnrow">${btn("p-save", p.id ? "Save and update the message" : "Post it", "primary", "save")}${btn("p-cancel", "Cancel", "ghost")}</div>
  </div></section>`;
  wireBuilder("post");
  $("#p-cancel").onclick = () => { editing = null; tabMessages(); };
  $("#p-save").onclick = async () => {
    const b = $("#p-save"); b.classList.add("busy"); b.disabled = true;
    try {
      const r = await api(`/api/dash/guild/${G.guild.id}/posts` + (p.id ? `/${p.id}` : ""), {method: p.id ? "PUT" : "POST",
        body: {name: val("p-name"), channel_id: val("p-channel"), message: readBuilder("post")}});
      G.posts = G.posts.filter(x => x.id !== r.post.id).concat([r.post]).sort((a, b) => a.id - b.id);
      toast("ok", p.id ? "Updated" : "Posted", "It's in Discord now.");
      editing = null;
      showGuild();
    } catch (e) { toast("bad", "Not posted", e.message); b.classList.remove("busy"); b.disabled = false; }
  };
}

/* ---------- Tags ---------- */
function tabTags() {
  if (editing) return tagEditor();
  body().innerHTML = `<p class="lede" style="margin-bottom:16px">A tag is a saved message anyone can post with <code>/tag show name</code>: rules, links, answers to questions people keep asking. People with Manage Messages can also make text tags in Discord with <code>/tag create</code>.</p>
    <div class="btnrow" style="margin-bottom:18px">${btn("t-new", "New tag", "primary", "plus")}</div>` + (G.tags.length
    ? `<div class="tablewrap"><table class="tbl"><thead><tr><th>Name</th><th>Posts</th><th class="num">Used</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${G.tags.map((t, i) => `
        <tr><td class="mono">${esc(t.name)}</td><td>${esc((t.message.content || t.message.embed.title || t.message.embed.description || "Embed").slice(0, 70))}</td><td class="num">${t.uses}</td>
        <td style="white-space:nowrap">${btn("t-edit-" + i, "Edit", "xs")} ${btn("t-del-" + i, "Delete", "xs ghost")}</td></tr>`).join("")}</tbody></table></div>`
    : emptyState("placeblock", "No tags yet", "Make one for your rules, your invite link or a FAQ answer."));
  $("#t-new").onclick = () => { editing = {name: "", message: newMessage()}; tagEditor(); };
  G.tags.forEach((t, i) => {
    $("#t-edit-" + i).onclick = () => { editing = {...JSON.parse(JSON.stringify(t)), old: t.name}; tagEditor(); };
    $("#t-del-" + i).onclick = async () => {
      if (!confirm(`Delete the tag "${t.name}"?`)) return;
      try { await api(`/api/dash/guild/${G.guild.id}/tags/${encodeURIComponent(t.name)}`, {method: "DELETE"}); G.tags = G.tags.filter(x => x.name !== t.name); toast("ok", "Tag deleted", ""); showGuild(); }
      catch (e) { toast("bad", "Not deleted", e.message); }
    };
  });
}
function tagEditor() {
  const t = editing;
  body().innerHTML = `<section class="panel"><header><h3>${t.old ? "Edit the tag" : "New tag"}</h3></header><div class="pad">
    ${field("t-name", "Name", `<input id="t-name" value="${esc(t.name)}" maxlength="32" placeholder="rules">`, "1-32 letters, numbers, spaces, - or _. People type it after /tag show.")}
    ${builderHost("tag", t.message)}
    <div class="btnrow">${btn("t-save", "Save", "primary", "save")}${btn("t-cancel", "Cancel", "ghost")}</div>
  </div></section>`;
  wireBuilder("tag");
  $("#t-cancel").onclick = () => { editing = null; tabTags(); };
  $("#t-save").onclick = async () => {
    try {
      const r = await api(`/api/dash/guild/${G.guild.id}/tags` + (t.old ? `/${encodeURIComponent(t.old)}` : ""), {method: t.old ? "PUT" : "POST",
        body: {name: val("t-name"), message: readBuilder("tag")}});
      G.tags = G.tags.filter(x => x.name !== t.old && x.name !== r.tag.name).concat([r.tag]).sort((a, b) => a.name.localeCompare(b.name));
      toast("ok", "Tag saved", `Post it with /tag show ${r.tag.name}.`);
      editing = null;
      showGuild();
    } catch (e) { toast("bad", "Not saved", e.message); }
  };
}

/* ---------- Auto-replies ---------- */
function tabReplies() {
  const cfg = G.config.autoresponders;
  if (!editing) editing = {items: JSON.parse(JSON.stringify(cfg.items))};
  const items = editing.items;
  body().innerHTML = `<p class="lede" style="margin-bottom:16px">When a message contains a trigger, the bot answers with the reply you set. One reply per message, and at most one every 10 seconds per channel.</p>
    <section class="panel"><header><h3>Auto-replies</h3><span class="sp">${items.length}/50</span></header><div class="pad">
      <div class="switches" style="margin-bottom:16px">${sw("ar-enabled", "Answer triggers", cfg.enabled)}</div>
      ${items.map((it, i) => `<details class="ar-item" ${it._open ? "open" : ""} data-i="${i}"><summary><b>${esc(it.trigger || "New auto-reply")}</b> <span class="muted">${esc(MATCH_WORD[it.match] || "")}</span></summary>
        <div class="b-row2" style="margin-top:12px">${field(`ar-t-${i}`, "Trigger", `<input class="ar-trigger" id="ar-t-${i}" value="${esc(it.trigger)}" maxlength="200" placeholder="hello">`)}
          ${field(`ar-m-${i}`, "Matches", `<select class="ar-match" id="ar-m-${i}">${Object.entries(MATCH_WORD).map(([k, v]) => `<option value="${k}" ${k === it.match ? "selected" : ""}>${v}</option>`).join("")}</select>`)}</div>
        <div class="switches" style="margin-bottom:12px">${sw(`ar-r-${i}`, "Reply to the message (instead of just posting)", it.reply !== false)}</div>
        ${builderHost("ar" + i, it.message, {contentLabel: "Reply"})}
        <div class="btnrow">${btn(`ar-del-${i}`, "Remove this auto-reply", "sm ghost")}</div></details>`).join("")}
      <div class="btnrow" style="margin-top:14px">${btn("ar-add", "Add an auto-reply", "", "plus")}${btn("ar-save", "Save", "primary", "save")}</div>
    </div></section>`;
  items.forEach((it, i) => wireBuilder("ar" + i));
  const grab = () => items.forEach((it, i) => {
    it.trigger = val(`ar-t-${i}`); it.match = val(`ar-m-${i}`); it.reply = on(`ar-r-${i}`); it.message = readBuilder("ar" + i);
    it._open = $(`details[data-i="${i}"]`).open;
  });
  $("#ar-add").onclick = () => { grab(); items.push({trigger: "", match: "contains", reply: true, message: newMessage(), _open: true}); tabReplies(); };
  items.forEach((it, i) => $(`#ar-del-${i}`).onclick = () => { grab(); items.splice(i, 1); tabReplies(); });
  $("#ar-save").onclick = async () => {
    grab();
    const ok = await saveSection("autoresponders", {enabled: on("ar-enabled"), items: items.map(({_open, ...x}) => x)},
      "The bot answers these triggers from now on.");
    if (ok) { editing = null; showGuild(); }
  };
}

/* ---------- Starboard ---------- */
function tabStarboard() {
  const s = G.config.starboard;
  body().innerHTML = `<div class="grid g2">
    <section class="panel"><header><h3>Starboard</h3><span class="sp">${s.enabled ? "On" : "Off"}</span></header><div class="pad">
      <p>When a message gets enough star reactions, the bot reposts it in the starboard channel: a hall of fame for the best posts.</p>
      <div class="switches" style="margin-bottom:16px">${sw("s-enabled", "Use a starboard", s.enabled)}${sw("s-self", "Count people starring their own message", s.self_star)}</div>
      ${field("s-channel", "Starboard channel", channelSelect("s-channel", s.channel_id, "Pick a channel"))}
      <div class="b-row2">${field("s-emoji", "Emoji", `<input id="s-emoji" value="${esc(s.emoji)}" maxlength="64">`, "A star by default; any emoji works.")}
        ${field("s-threshold", "Stars needed", `<input type="number" id="s-threshold" min="1" max="100" value="${s.threshold}">`)}</div>
      <div class="btnrow">${btn("s-save", "Save", "primary", "save")}</div>
    </div></section>
    <section class="panel"><header><h3>Channels left out</h3></header><div class="pad">
      <div class="field"><label>Stars in these channels don't count</label>${channelChips("s-ignored", s.ignored_channel_ids)}</div>
      <p class="help">Posts from age-restricted channels only go to an age-restricted starboard.</p>
    </div></section></div>`;
  wireChips();
  $("#s-save").onclick = () => saveSection("starboard", {enabled: on("s-enabled"), self_star: on("s-self"), channel_id: val("s-channel") || null,
    emoji: val("s-emoji"), threshold: +val("s-threshold"), ignored_channel_ids: picked("s-ignored")}, "Starred messages go to the starboard from now on.");
}

/* ---------- Suggestions ---------- */
function tabSuggestions() {
  const s = G.config.suggestions;
  body().innerHTML = `<div class="grid g2">
    <section class="panel"><header><h3>Suggestions</h3><span class="sp">${s.enabled ? "On" : "Off"}</span></header><div class="pad">
      <p>Members post ideas with <code>/suggest</code>. The bot numbers them, posts them in the suggestions channel and adds vote reactions.</p>
      <div class="switches" style="margin-bottom:16px">${sw("g-enabled", "Take suggestions", s.enabled)}${sw("g-anon", "Hide who made each suggestion", s.anonymous)}${sw("g-dm", "Tell people by DM when their suggestion is decided", s.dm_on_decision)}</div>
      ${field("g-channel", "Suggestions channel", channelSelect("g-channel", s.channel_id, "Pick a channel"))}
      <div class="btnrow">${btn("g-save", "Save", "primary", "save")}</div>
    </div></section>
    <section class="panel"><header><h3>Deciding</h3></header><div class="pad">
      <div class="tablewrap"><table class="tbl"><tbody>
        <tr><td class="mono">/suggestion approve</td><td>Approve it, with an optional reason</td></tr>
        <tr><td class="mono">/suggestion deny</td><td>Turn it down</td></tr>
        <tr><td class="mono">/suggestion consider</td><td>Mark it as being considered</td></tr>
        <tr><td class="mono">/suggestion implemented</td><td>Mark it as done</td></tr></tbody></table></div>
      <p class="help" style="margin-top:10px">These need the Manage Server permission. The suggestion's message changes colour and shows the decision.</p>
    </div></section></div>`;
  $("#g-save").onclick = () => saveSection("suggestions", {enabled: on("g-enabled"), anonymous: on("g-anon"), dm_on_decision: on("g-dm"),
    channel_id: val("g-channel") || null}, "/suggest posts there from now on.");
}

/* ---------- Moderation ---------- */
function tabModeration() {
  const md = G.config.moderation;
  body().innerHTML = `<div class="split">
    <div class="stack"><section class="panel"><header><h3>Cases</h3><span class="sp" id="case-count"></span></header><div class="pad">
      <div class="logbar"><input class="inp" type="search" id="case-user" placeholder="Filter by user ID" aria-label="Filter by user ID" value="${esc(caseUser)}"></div>
      <div id="case-list"><p class="muted">Reading the cases.</p></div>
      <div class="btnrow" id="case-pages" style="margin-top:12px"></div>
    </div></section></div>
    <aside class="stack">
      <section class="panel"><header><h3>Settings</h3></header><div class="pad">
        ${field("m-log", "Mod-log channel", channelSelect("m-log", md.modlog_channel_id, "No mod log"), "Every ban, kick, timeout and warning gets a numbered entry there.")}
        <div class="switches" style="margin-bottom:16px">${sw("m-dm", "Tell people by DM why they were punished", md.dm_users)}</div>
        <div class="btnrow">${btn("m-save", "Save", "primary", "save")}</div>
      </div></section>
      <section class="panel"><header><h3>Commands</h3></header><div class="pad"><p>Every moderation command, with what it needs, is on the <a href="#commands">Commands</a> page.</p></div></section>
    </aside></div>`;
  $("#m-save").onclick = () => saveSection("moderation", {modlog_channel_id: val("m-log") || null, dm_users: on("m-dm")}, "Moderation uses these from the next case on.");
  let t;
  $("#case-user").oninput = e => { clearTimeout(t); t = setTimeout(() => { caseUser = e.target.value.replace(/\D/g, ""); casePage = 0; loadCases(); }, 350); };
  loadCases();
}
async function loadCases() {
  let r;
  try { r = await api(`/api/dash/guild/${G.guild.id}/cases?offset=${casePage * 25}${caseUser ? "&user=" + caseUser : ""}`); }
  catch (e) { $("#case-list").innerHTML = `<div class="note bad"><b>The cases didn't load</b>${esc(e.message)}</div>`; return; }
  $("#case-count").textContent = r.total + " total";
  $("#case-list").innerHTML = r.cases.length ? `<div class="tablewrap"><table class="tbl"><thead><tr><th class="num">#</th><th>Action</th><th>Member</th><th>Reason</th><th>When</th></tr></thead><tbody>${r.cases.map(c => `
      <tr><td class="num">${c.case_no}</td><td><span class="pill ${ACTION_PILL[c.action] || "ghost"}">${esc(ACTION_WORD[c.action] || c.action)}</span></td>
      <td>${esc(c.user_name)}<br><span class="mono muted" style="font-size:11px">${esc(c.user_id)}</span></td>
      <td>${esc(c.reason || "No reason given")}<br><span class="muted" style="font-size:12px">by ${esc(c.mod_name)}${c.expires_at ? " · until " + new Date(c.expires_at * 1000).toLocaleString() : ""}</span></td>
      <td class="mono" style="white-space:nowrap;font-size:12px">${new Date(c.created_at * 1000).toLocaleDateString()}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty" style="padding:20px"><img class="mascot" src="${M("nod")}" alt="" width="64" height="64"><h3>${caseUser ? "No cases for that user" : "No cases yet"}</h3><p>Cases appear here when someone uses /ban, /kick, /timeout or /warn.</p></div>`;
  const pages = Math.ceil(r.total / 25);
  $("#case-pages").innerHTML = pages > 1 ? `${casePage > 0 ? btn("case-prev", "Newer", "sm ghost") : ""}<span class="muted" style="align-self:center">Page ${casePage + 1} of ${pages}</span>${casePage + 1 < pages ? btn("case-next", "Older", "sm ghost") : ""}` : "";
  if ($("#case-prev")) $("#case-prev").onclick = () => { casePage--; loadCases(); };
  if ($("#case-next")) $("#case-next").onclick = () => { casePage++; loadCases(); };
}

/* ---------- Logging ---------- */
function tabLogging() {
  const l = G.config.logging;
  body().innerHTML = `<div class="grid g2">
    <section class="panel"><header><h3>Where logs go</h3><span class="sp">${l.enabled ? "On" : "Off"}</span></header><div class="pad">
      <div class="switches" style="margin-bottom:16px">${sw("l-enabled", "Log server events", l.enabled)}</div>
      ${LOGS.map(([k, name, what]) => field("l-" + k, name, channelSelect("l-" + k, l.channels[k], "Don't log these"), what)).join("")}
      <div class="btnrow">${btn("l-save", "Save", "primary", "save")}</div>
    </div></section>
    <section class="panel"><header><h3>What to leave out</h3></header><div class="pad">
      <div class="switches" style="margin-bottom:16px">${sw("l-bots", "Leave out messages and voice moves by bots", l.ignore_bots)}</div>
      <div class="field"><label>Channels whose messages aren't logged</label>${channelChips("l-ignored", l.ignored_channel_ids)}
        <div class="help">The log channels themselves are always left out. Deleted messages can be quoted only if they were sent while the bot was running.</div></div>
    </div></section></div>`;
  wireChips();
  $("#l-save").onclick = () => saveSection("logging", {enabled: on("l-enabled"), ignore_bots: on("l-bots"), ignored_channel_ids: picked("l-ignored"),
    channels: Object.fromEntries(LOGS.map(([k]) => [k, val("l-" + k) || null]))}, "Events are logged where you picked.");
}

/* ---------- shared pickers for the VC maker and tickets ---------- */
const optList = (items, value, none) => (none !== undefined ? `<option value="">${esc(none)}</option>` : "") +
  items.map(x => `<option value="${x.id}" ${x.id === value ? "selected" : ""}>${esc(x.label)}</option>`).join("");
const categorySelect = (id, value, none = "Pick a category") =>
  `<select id="${id}">${optList(G.categories.map(c => ({id: c.id, label: c.name})), value, none)}</select>`;
const voiceSelect = (id, value, none = "Pick a voice channel") =>
  `<select id="${id}">${optList(G.voice_channels.map(c => ({id: c.id, label: (c.category ? c.category + " / " : "") + c.name})), value, none)}</select>`;
const roleSelect = (id, value, none = "No role", assignableOnly = true) =>
  `<select id="${id}">${optList(G.roles.filter(r => !assignableOnly || !r.problem).map(r => ({id: r.id, label: "@" + r.name})), value, none)}</select>`;
const textSelect = (id, value, none = "Pick a channel") =>
  `<select id="${id}">${optList(G.channels.map(c => ({id: c.id, label: "#" + c.name})), value, none)}</select>`;
const segHTML = (name, value, opts) => `<div class="seg" role="group" data-seg="${name}">${Object.entries(opts).map(([k, v]) =>
  `<button type="button" data-v="${k}" aria-pressed="${k === value}">${esc(v)}</button>`).join("")}</div>`;
const wireSegs = (scope, onPick) => scope.querySelectorAll("[data-seg] button").forEach(b => b.onclick = () => {
  b.parentElement.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  onPick && onPick(b.parentElement.dataset.seg, b.dataset.v);
});
const segVal = (scope, name) => (scope.querySelector(`[data-seg="${name}"] button[aria-pressed="true"]`) || {}).dataset?.v;
const saveVM = (changes, okText) => saveSection("voicemaster", {...JSON.parse(JSON.stringify(G.config.voicemaster)), ...changes}, okText);

/* ---------- VC maker: generators ---------- */
const VM_MODES = {
  default: ["Their own", "The member picks the name and limit; saved settings are used"],
  sequence: ["Numbered", "Gaming 1, Gaming 2... with a fixed limit"],
  predefined: ["Template", "A name pattern: {username}, {seq}, {animal}, {color}, {tree}"],
  clone: ["Copy the generator", "Same name, limit and bitrate as the generator"],
  dynamic: ["Always one ready", "No generator: one empty room is always waiting in the category"],
};
function tabVcGenerators() {
  const cfg = G.config.voicemaster;
  if (!editing) editing = {setups: JSON.parse(JSON.stringify(cfg.setups))};
  const setups = editing.setups;
  const live = G.temp_channels || [];
  body().innerHTML = `<p class="lede" style="margin-bottom:16px">A generator is a voice channel people join to get their own temporary channel, moved in automatically and deleted when everyone leaves. The owner controls it with the panel in its chat or with <code>/voice</code>.</p>
    <section class="panel"><header><h3>Generators</h3><span class="sp">${setups.length}/10</span></header><div class="pad">
      <div class="switches" style="margin-bottom:16px">${sw("vm-enabled", "Make temporary voice channels", cfg.enabled)}</div>
      ${setups.map((s, i) => `<details class="ar-item" ${s._open ? "open" : ""} data-i="${i}"><summary><b>${esc(VM_MODES[s.mode][0])}</b> <span class="muted">${s.mode === "dynamic" ? "" : esc((G.voice_channels.find(v => v.id === s.channel_id) || {name: "pick a channel"}).name)} · ${esc(s.name || "")}</span></summary>
        <div class="field" style="margin-top:12px"><label>Kind</label>${segHTML("mode" + i, s.mode, Object.fromEntries(Object.entries(VM_MODES).map(([k, v]) => [k, v[0]])))}<div class="help">${esc(VM_MODES[s.mode][1])}</div></div>
        <div class="b-row2">${s.mode === "dynamic" ? "" : field(`vg-ch-${i}`, "Generator (people join this)", voiceSelect(`vg-ch-${i}`, s.channel_id))}
          ${field(`vg-cat-${i}`, "New channels go in", categorySelect(`vg-cat-${i}`, s.category_id, s.mode === "dynamic" ? "Pick a category" : "The generator's category"))}</div>
        <div class="b-row3">${field(`vg-name-${i}`, "Name", `<input id="vg-name-${i}" value="${esc(s.name || "")}" maxlength="90" placeholder="{username}'s channel">`, s.mode === "default" ? "Used until the member saves their own." : s.mode === "clone" ? "Not used: the generator's name is copied." : "")}
          ${field(`vg-limit-${i}`, "User limit (0 = none)", `<input type="number" id="vg-limit-${i}" min="0" max="99" value="${s.limit ?? 0}">`)}
          ${field(`vg-br-${i}`, `Bitrate, kbps (max ${G.bitrate_limit})`, `<input type="number" id="vg-br-${i}" min="8" max="${G.bitrate_limit}" value="${s.bitrate ?? 64}">`)}</div>
        <div class="field"><label>Permissions come from</label>${segHTML("perm" + i, s.permissions_from || "category", {category: "The category", generator: "The generator channel"})}</div>
        <div class="switches" style="margin-bottom:12px">${sw(`vg-ed-${i}`, "Owners can change the name and limit", s.editable !== false)}</div>
        <div class="btnrow">${btn(`vg-del-${i}`, "Remove this generator", "sm ghost")}</div></details>`).join("")}
      ${setups.length ? "" : `<p class="muted">No generators yet.</p>`}
      <div class="btnrow" style="margin-top:14px">${btn("vg-add", "Add a generator", "", "plus")}${btn("vg-save", "Save", "primary", "save")}</div>
    </div></section>
    <section class="panel" style="margin-top:24px"><header><h3>Live channels</h3><span class="sp">${live.length}</span></header><div class="pad">
      ${live.length ? `<div class="tablewrap"><table class="tbl"><thead><tr><th>Channel</th><th>Owner</th><th class="num">People</th></tr></thead><tbody>${live.map(c =>
        `<tr><td>${esc(c.name)}</td><td class="mono">${c.owner_id ? esc(c.owner_id) : "<span class=muted>nobody (ready room)</span>"}</td><td class="num">${c.members}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">No temporary channels right now.</p>`}</div></section>`;
  const grab = () => setups.forEach((s, i) => {
    s.mode = segVal(body(), "mode" + i) || s.mode;
    if ($(`#vg-ch-${i}`)) s.channel_id = val(`vg-ch-${i}`);
    s.category_id = val(`vg-cat-${i}`); s.name = val(`vg-name-${i}`); s.limit = +val(`vg-limit-${i}`) || 0;
    s.bitrate = +val(`vg-br-${i}`) || 64; s.editable = on(`vg-ed-${i}`);
    s.permissions_from = segVal(body(), "perm" + i) || "category"; s._open = $(`details[data-i="${i}"]`).open;
  });
  wireSegs(body(), seg => { if (seg.startsWith("mode")) { grab(); tabVcGenerators(); } });
  setups.forEach((s, i) => $(`#vg-del-${i}`).onclick = () => { grab(); setups.splice(i, 1); tabVcGenerators(); });
  $("#vg-add").onclick = () => {
    grab();
    setups.push({id: "s" + Math.random().toString(36).slice(2, 8), mode: "default", channel_id: "", category_id: "", name: "{username}'s channel",
                 limit: 0, bitrate: 64, editable: true, permissions_from: "category", _open: true});
    tabVcGenerators();
  };
  $("#vg-save").onclick = async () => {
    grab();
    if (await saveVM({enabled: on("vm-enabled"), setups: setups.map(({_open, ...x}) => x)}, "Joining a generator makes a channel from now on.")) {
      editing = null; showGuild();
    }
  };
}

/* ---------- VC maker: controls ---------- */
const VM_FEATURES = [["name", "Rename"], ["limit", "User limit"], ["status", "Channel status"], ["lock", "Lock and unlock"],
  ["claim", "Claim"], ["reject", "Reject people"], ["permit", "Permit people"], ["ghost", "Hide and show"], ["lfm", "Looking for members"],
  ["text", "Text channel"], ["bitrate", "Bitrate"], ["invite", "Invite by DM"], ["transfer", "Transfer"], ["nsfw", "Age-restricted"], ["region", "Region"]];
function tabVcControls() {
  const c = G.config.voicemaster;
  body().innerHTML = `<div class="grid g2">
    <section class="panel"><header><h3>What owners can do</h3></header><div class="pad">
      <p>Switch off anything members shouldn't change. People with Manage Channels can always use everything.</p>
      <div class="switches" style="grid-template-columns:repeat(2,minmax(0,1fr));display:grid">${VM_FEATURES.map(([k, l]) => sw("vf-" + k, l, c.features[k] !== false)).join("")}</div>
      <hr><div class="switches">${sw("vf-manage", "Also give owners Discord's Manage Channel on their channel", c.owner_manage_channel)}${sw("vf-move", "Also let owners move and disconnect people in it", c.owner_move_members)}</div>
    </div></section>
    <section class="panel"><header><h3>The control panel</h3></header><div class="pad">
      <p>Every new channel gets a message in its chat with two menus, Channel settings and Channel permissions, plus Load my settings. Members can also use <code>/voice</code>.</p>
      <div class="switches" style="margin-bottom:16px">${sw("vf-iface", "Post the control panel in each new channel", c.interface)}${sw("vf-ping", "Ping the owner with it", c.interface_ping)}</div>
      <p class="help"><code>/voice panel</code> (Manage Channels) posts the same panel in any channel, where it works on whatever temporary channel the person is in.</p>
    </div></section></div>
    <div class="form-foot btnrow">${btn("vf-save", "Save", "primary", "save")}</div>`;
  $("#vf-save").onclick = () => saveVM({features: Object.fromEntries(VM_FEATURES.map(([k]) => [k, on("vf-" + k)])),
    owner_manage_channel: on("vf-manage"), owner_move_members: on("vf-move"), interface: on("vf-iface"), interface_ping: on("vf-ping")},
    "Owners get these controls from now on.");
}

/* ---------- VC maker: extras ---------- */
function tabVcExtras() {
  const c = G.config.voicemaster;
  body().innerHTML = `<div class="grid g2">
    <section class="panel"><header><h3>Channels and roles</h3></header><div class="pad">
      ${field("vx-log", "Log channel", channelSelect("vx-log", c.log_channel_id, "No log"), "Channels made, claimed and handed over.")}
      ${field("vx-lfm", "Looking for members channel", channelSelect("vx-lfm", c.lfm_channel_id, "Off"), "Where Looking for members posts go, with a join link.")}
      ${field("vx-role", "Owner role", roleSelect("vx-role", c.temp_role_id), "Given to people while they own a temporary channel, and taken back when they leave it.")}
    </div></section>
    <section class="panel"><header><h3>Text channels</h3></header><div class="pad">
      <div class="switches" style="margin-bottom:12px">${sw("vx-auto", "Give every new voice channel its own text channel", c.auto_text)}</div>
      ${field("vx-access", "Role that can also see those text channels", roleSelect("vx-access", c.text_access_role_id, "Nobody else", false), "Moderators, for example. The people in the voice channel always can.")}
    </div></section></div>
    <section class="panel" style="margin-top:24px"><header><h3>First message in each text channel</h3></header><div class="pad">
      ${builderHost("vmtext", JSON.parse(JSON.stringify(c.text_message)), {contentLabel: "Message"})}
    </div></section>
    <div class="form-foot btnrow">${btn("vx-save", "Save", "primary", "save")}</div>`;
  wireBuilder("vmtext");
  $("#vx-save").onclick = () => saveVM({log_channel_id: val("vx-log") || null, lfm_channel_id: val("vx-lfm") || null,
    temp_role_id: val("vx-role") || null, auto_text: on("vx-auto"), text_access_role_id: val("vx-access") || null,
    text_message: readBuilder("vmtext")}, "Saved.");
}

/* ---------- Tickets: panels ---------- */
const BTN_COLORS = {primary: "Blurple", secondary: "Grey", success: "Green", danger: "Red"};
const newType = () => ({id: "", label: "Support", emoji: "", description: "", color: "primary", mode: "channel", category_id: "",
  thread_channel_id: "", closed_category_id: "", support_role_ids: [], name_template: "{type}-{number}", limit_per_user: 1, ping_support: true,
  questions: [], ticket_message: {content: "", embed: {...JSON.parse(JSON.stringify(EMPTY_EMBED)), enabled: true, title: "Thanks for reaching out, {user.display}",
    description: "Tell us what you need and someone from the team will be with you soon.\nPress **Close** when you're done."}}, _open: true});
function tabTkPanels() {
  if (editing) return panelEditor();
  const ch = id => (G.channels.find(c => c.id === id) || {name: "deleted-channel"}).name;
  const panels = G.ticket_panels || [];
  body().innerHTML = `<p class="lede" style="margin-bottom:16px">A panel is a message with a button (or dropdown option) for each kind of ticket: Support, Apply to be mod, Report someone... Edit it any time, add or remove kinds, and the posted message updates itself.</p>
    <div class="btnrow" style="margin-bottom:18px">${btn("tp-new", "New panel", "primary", "plus")}</div>` + (panels.length
    ? `<div class="grid gauto">${panels.map(p => `<div class="card"><header><h3>${esc(p.data.name)}</h3><span class="k">#${esc(ch(p.channel_id))}</span></header>
        <p class="card-sub">${p.data.types.map(t => esc(t.label)).join(" · ")}</p><p class="muted mono" style="font-size:11px">Panel ${p.id}</p>
        <div class="btnrow">${btn("tp-edit-" + p.id, "Edit", "sm")}${btn("tp-del-" + p.id, "Delete", "sm ghost")}</div></div>`).join("")}</div>`
    : emptyState("placeblock", "No ticket panels yet", "Make one, add the kinds of tickets people can open, and post it."));
  $("#tp-new").onclick = () => {
    editing = {name: "Tickets", channel_id: "", style: "buttons", placeholder: "Pick what it's about",
      message: {content: "", embed: {...JSON.parse(JSON.stringify(EMPTY_EMBED)), enabled: true, title: "Need help?", description: "Pick what it's about below to open a private ticket with the team."}},
      types: [newType()]};
    panelEditor();
  };
  panels.forEach(p => {
    $("#tp-edit-" + p.id).onclick = () => { editing = {...JSON.parse(JSON.stringify(p.data)), id: p.id, channel_id: p.channel_id}; panelEditor(); };
    $("#tp-del-" + p.id).onclick = async () => {
      if (!confirm(`Delete the "${p.data.name}" panel? Its message is removed. Open tickets stay.`)) return;
      try { await api(`/api/dash/guild/${G.guild.id}/ticketpanels/${p.id}`, {method: "DELETE"}); G.ticket_panels = panels.filter(x => x.id !== p.id); toast("ok", "Panel deleted", ""); showGuild(); }
      catch (e) { toast("bad", "Not deleted", e.message); }
    };
  });
}
function panelEditor() {
  const p = editing;
  const qRow = (ti, q, qi) => `<div class="b-fieldrow" style="grid-template-columns:minmax(0,1.2fr) minmax(0,1.2fr) 80px auto auto auto" data-q="${ti}-${qi}">
      <input class="inp" data-qf="label" maxlength="45" value="${esc(q.label)}" placeholder="Question">
      <input class="inp" data-qf="placeholder" maxlength="100" value="${esc(q.placeholder || "")}" placeholder="Hint (optional)">
      <input class="inp" type="number" data-qf="max" min="1" max="4000" value="${q.max || 1000}" title="Longest answer">
      <label class="switch"><input type="checkbox" data-qf="required" ${q.required !== false ? "checked" : ""}><span class="track"></span>Needed</label>
      <label class="switch"><input type="checkbox" data-qf="long" ${q.long ? "checked" : ""}><span class="track"></span>Long</label>
      <button class="btn icon xs ghost" type="button" data-rmq="${ti}-${qi}" aria-label="Remove this question">${svg("x")}</button></div>`;
  body().innerHTML = `<section class="panel"><header><h3>${p.id ? "Edit the panel" : "New panel"}</h3><span class="sp">${p.id ? "Panel " + p.id : "Not posted yet"}</span></header><div class="pad">
    <div class="b-row3">${field("tp-name", "Name (only shown here)", `<input id="tp-name" value="${esc(p.name)}" maxlength="80">`)}
      ${field("tp-channel", "Channel", channelSelect("tp-channel", p.channel_id, "Pick a channel"), "Changing it moves the panel.")}
      <div class="field"><label>Show the kinds as</label>${segHTML("style", p.style, {buttons: "Buttons", dropdown: "A dropdown"})}</div></div>
    ${p.style === "dropdown" ? field("tp-ph", "Dropdown hint", `<input id="tp-ph" value="${esc(p.placeholder)}" maxlength="150">`) : ""}
    <div class="field"><label>The panel's message</label></div>
    ${builderHost("tpmsg", p.message)}
    <h3 class="section">Kinds of tickets <span class="muted">(${p.types.length}/25)</span></h3>
    ${p.types.map((t, i) => `<details class="ar-item" ${t._open ? "open" : ""} data-ti="${i}"><summary><b>${esc((t.emoji ? t.emoji + " " : "") + (t.label || "New kind"))}</b> <span class="muted">${t.mode === "thread" ? "private thread" : "channel"} · ${t.questions.length} question${t.questions.length === 1 ? "" : "s"}</span></summary>
      <div class="b-row3" style="margin-top:12px">${field(`tt-label-${i}`, "Name", `<input id="tt-label-${i}" value="${esc(t.label)}" maxlength="80" placeholder="Support">`)}
        ${field(`tt-emoji-${i}`, "Emoji", `<input id="tt-emoji-${i}" value="${esc(t.emoji)}" maxlength="64" placeholder="Optional">`)}
        ${p.style === "dropdown" ? field(`tt-desc-${i}`, "Description", `<input id="tt-desc-${i}" value="${esc(t.description)}" maxlength="100">`)
          : field(`tt-color-${i}`, "Button colour", `<select id="tt-color-${i}">${Object.entries(BTN_COLORS).map(([k, v]) => `<option value="${k}" ${k === t.color ? "selected" : ""}>${v}</option>`).join("")}</select>`)}</div>
      <div class="field"><label>Each ticket is</label>${segHTML("mode" + i, t.mode, {channel: "A private channel", thread: "A private thread"})}</div>
      <div class="b-row2">${t.mode === "thread" ? field(`tt-th-${i}`, "Threads are made in", textSelect(`tt-th-${i}`, t.thread_channel_id))
          : field(`tt-cat-${i}`, "New tickets go in", categorySelect(`tt-cat-${i}`, t.category_id))}
        ${t.mode === "thread" ? "" : field(`tt-ccat-${i}`, "Closed tickets move to", categorySelect(`tt-ccat-${i}`, t.closed_category_id, "Stay where they are"))}</div>
      <div class="field"><label>The team for these tickets</label>${roleChips("tt-roles-" + i, t.support_role_ids)}<div class="help">They see these tickets, can claim, close, reopen and delete them. People with Manage Channels always can.</div></div>
      <div class="b-row3">${field(`tt-name-${i}`, "Ticket name", `<input id="tt-name-${i}" value="${esc(t.name_template)}" maxlength="80">`, "{type}, {number}, {user}")}
        ${field(`tt-limit-${i}`, "Open at once, per person", `<input type="number" id="tt-limit-${i}" min="0" max="10" value="${t.limit_per_user}">`, "0 = no limit")}
        <div class="switches" style="align-content:center">${sw(`tt-ping-${i}`, "Ping the team in new tickets", t.ping_support)}</div></div>
      <div class="field"><label>Questions asked first (up to 5)</label>${t.questions.map((q, qi) => qRow(i, q, qi)).join("")}
        <button class="btn sm" type="button" data-addq="${i}" ${t.questions.length >= 5 ? "disabled" : ""}><span class="p5-hl" aria-hidden="true"></span>${svg("plus")}<span>Add a question</span></button>
        <div class="help">Asked in a pop-up before the ticket opens; the answers are posted in it.</div></div>
      <div class="field"><label>First message in the ticket</label></div>
      ${builderHost("tt" + i, t.ticket_message)}
      <div class="btnrow">${btn(`tt-del-${i}`, "Remove this kind", "sm ghost")}</div></details>`).join("")}
    <div class="btnrow">${btn("tt-add", "Add a kind of ticket", "", "plus")}</div>
    <hr>
    <div class="btnrow">${btn("tp-save", p.id ? "Save and update the panel" : "Post the panel", "primary", "save")}${btn("tp-cancel", "Cancel", "ghost")}</div>
  </div></section>`;
  wireBuilder("tpmsg");
  p.types.forEach((t, i) => wireBuilder("tt" + i));
  wireChips();
  const grab = () => {
    p.name = val("tp-name"); p.channel_id = val("tp-channel"); p.style = segVal(body(), "style") || p.style;
    if ($("#tp-ph")) p.placeholder = val("tp-ph");
    p.message = readBuilder("tpmsg");
    p.types.forEach((t, i) => {
      t.label = val(`tt-label-${i}`); t.emoji = val(`tt-emoji-${i}`);
      if ($(`#tt-desc-${i}`)) t.description = val(`tt-desc-${i}`);
      if ($(`#tt-color-${i}`)) t.color = val(`tt-color-${i}`);
      t.mode = segVal(body(), "mode" + i) || t.mode;
      if ($(`#tt-th-${i}`)) t.thread_channel_id = val(`tt-th-${i}`);
      if ($(`#tt-cat-${i}`)) t.category_id = val(`tt-cat-${i}`);
      if ($(`#tt-ccat-${i}`)) t.closed_category_id = val(`tt-ccat-${i}`);
      t.support_role_ids = picked("tt-roles-" + i); t.name_template = val(`tt-name-${i}`); t.limit_per_user = +val(`tt-limit-${i}`) || 0;
      t.ping_support = on(`tt-ping-${i}`); t.ticket_message = readBuilder("tt" + i); t._open = $(`details[data-ti="${i}"]`).open;
      t.questions = [...body().querySelectorAll(`[data-q^="${i}-"]`)].map(row => ({
        label: $('[data-qf="label"]', row).value, placeholder: $('[data-qf="placeholder"]', row).value, max: +$('[data-qf="max"]', row).value || 1000,
        required: $('[data-qf="required"]', row).checked, long: $('[data-qf="long"]', row).checked, min: 0}));
    });
  };
  wireSegs(body(), () => { grab(); panelEditor(); });
  body().querySelectorAll("[data-addq]").forEach(b => b.onclick = () => { grab(); p.types[+b.dataset.addq].questions.push({label: "", placeholder: "", max: 1000, required: true, long: false}); panelEditor(); });
  body().querySelectorAll("[data-rmq]").forEach(b => b.onclick = () => { grab(); const [ti, qi] = b.dataset.rmq.split("-").map(Number); p.types[ti].questions.splice(qi, 1); panelEditor(); });
  p.types.forEach((t, i) => $(`#tt-del-${i}`).onclick = () => { grab(); p.types.splice(i, 1); panelEditor(); });
  $("#tt-add").onclick = () => { grab(); const t = newType(); t.label = ""; p.types.push(t); panelEditor(); };
  $("#tp-cancel").onclick = () => { editing = null; tabTkPanels(); };
  $("#tp-save").onclick = async () => {
    grab();
    const b = $("#tp-save"); b.classList.add("busy"); b.disabled = true;
    try {
      const payload = {...p, types: p.types.map(({_open, ...x}) => x)};
      const r = await api(`/api/dash/guild/${G.guild.id}/ticketpanels` + (p.id ? `/${p.id}` : ""), {method: p.id ? "PUT" : "POST", body: payload});
      G.ticket_panels = (G.ticket_panels || []).filter(x => x.id !== r.panel.id).concat([r.panel]).sort((a, b) => a.id - b.id);
      toast("ok", p.id ? "Panel updated" : "Panel posted", "It's live in Discord now.");
      editing = null;
      showGuild();
    } catch (e) { toast("bad", "Not posted", e.message); b.classList.remove("busy"); b.disabled = false; }
  };
}

/* ---------- Tickets: the list ---------- */
let tkStatus = "open";
async function tabTkList() {
  body().innerHTML = `<div class="logbar">${segHTML("tks", tkStatus, {open: "Open", closed: "Closed", deleted: "Deleted"})}</div><div id="tk-rows">${loading("Reading the tickets")}</div>`;
  wireSegs(body(), (_, v) => { tkStatus = v; tabTkList(); });
  let r;
  try { r = await api(`/api/dash/guild/${G.guild.id}/tickets?status=${tkStatus}`); }
  catch (e) { $("#tk-rows").innerHTML = `<div class="note bad"><b>The tickets didn't load</b>${esc(e.message)}</div>`; return; }
  const when = s => s ? new Date(s * 1000).toLocaleString() : "—";
  $("#tk-rows").innerHTML = r.tickets.length ? `<div class="tablewrap"><table class="tbl"><thead><tr><th class="num">#</th><th>Kind</th><th>Opened by</th><th>Claimed by</th><th>Opened</th><th>${tkStatus === "open" ? "Last message" : "Closed"}</th><th></th></tr></thead><tbody>${r.tickets.map(t => `
    <tr><td class="num">${t.number}</td><td>${esc(t.type_label || "")}</td><td>${esc(t.owner_name)}</td><td class="mono">${t.claimed_by ? esc(t.claimed_by) : "—"}</td>
    <td style="white-space:nowrap">${when(t.created_at)}</td><td style="white-space:nowrap">${when(tkStatus === "open" ? t.last_activity : t.closed_at)}${t.close_reason ? `<br><span class="muted">${esc(t.close_reason)}</span>` : ""}</td>
    <td>${tkStatus !== "deleted" ? `<a href="https://discord.com/channels/${G.guild.id}/${t.channel_id}" target="_blank" rel="noopener">Open in Discord</a>` : ""}</td></tr>`).join("")}</tbody></table></div>`
    : emptyState("nod", `No ${tkStatus} tickets`, tkStatus === "open" ? "Tickets show up here when someone opens one from a panel." : "Nothing here yet.");
}

/* ---------- Tickets: settings ---------- */
function tabTkSettings() {
  const c = G.config.tickets;
  body().innerHTML = `<div class="grid g2">
    <section class="panel"><header><h3>Closing</h3></header><div class="pad"><div class="switches">
      ${sw("ts-two", "Ask \"are you sure?\" before closing", c.two_step_close)}
      ${sw("ts-keep", "Keep closed tickets, with Reopen, Transcript and Delete buttons (off: delete them on close)", c.keep_closed)}
      ${sw("ts-claim", "Show a Claim button, so someone on the team takes a ticket", c.claiming)}
      ${sw("ts-leave", "Close someone's tickets when they leave the server", c.close_when_owner_leaves)}</div>
      <div class="b-row2" style="margin-top:12px">${field("ts-auto", "Close after this many quiet hours", `<input type="number" id="ts-auto" min="0" max="720" value="${c.auto_close_hours}">`, "0 = never")}
        ${field("ts-glimit", "Open tickets per person, all kinds", `<input type="number" id="ts-glimit" min="0" max="50" value="${c.global_limit}">`, "0 = no limit")}</div>
    </div></section>
    <section class="panel"><header><h3>Transcripts and log</h3></header><div class="pad">
      ${field("ts-tr", "Transcript channel", channelSelect("ts-tr", c.transcript_channel_id, "None"), "Transcripts are web pages (HTML) of up to 1000 messages, with pictures.")}
      <div class="switches" style="margin-bottom:12px">${sw("ts-save", "Save a transcript there when a ticket closes or is deleted", c.save_transcript)}${sw("ts-dm", "Send the person who opened it a copy when it closes", c.dm_transcript)}</div>
      ${field("ts-log", "Log channel", channelSelect("ts-log", c.log_channel_id, "No log"), "Opened, closed, reopened, claimed and deleted, with who did it.")}
    </div></section></div>
    <div class="form-foot btnrow">${btn("ts-save-btn", "Save", "primary", "save")}</div>`;
  $("#ts-save-btn").onclick = () => saveSection("tickets", {two_step_close: on("ts-two"), keep_closed: on("ts-keep"), claiming: on("ts-claim"),
    close_when_owner_leaves: on("ts-leave"), auto_close_hours: +val("ts-auto") || 0, global_limit: +val("ts-glimit") || 0,
    transcript_channel_id: val("ts-tr") || null, save_transcript: on("ts-save"), dm_transcript: on("ts-dm"), log_channel_id: val("ts-log") || null},
    "Tickets work this way from now on.");
}

/* =====================================================================================
   The bot's own profile (owner only, its own screen in the rail)
   ===================================================================================== */
let PROF = null;
window.showProfile = async () => {
  $("#crumb-t").textContent = "Bot profile";
  const root = $("#profile-root");
  root.innerHTML = loading("Asking Discord for the bot's profile");
  try { PROF = await api("/api/profile"); }
  catch (e) {
    const hint = /isn't running|didn't answer/.test(e.message) ? " Start the bot from the top bar first." : "";
    root.innerHTML = emptyState("idle", "The profile can't be read", esc(e.message) + hint); return;
  }
  const pr = PROF.presence;
  const pending = {avatar: null, banner: null, remove_avatar: false, remove_banner: false};
  const STATUS = {online: "Online", idle: "Idle", dnd: "Do not disturb", invisible: "Invisible"};
  const ACT = {none: "Nothing", playing: "Playing", listening: "Listening to", watching: "Watching", competing: "Competing in", streaming: "Streaming", custom: "Custom status"};
  root.innerHTML = `<header class="fleet-head page-intro"><div class="fh-words"><p class="eyebrow">Bot · <b>${esc(PROF.id)}</b></p>
      <h1 class="brush fh-title">Bot profile</h1><p class="lede">How the bot looks everywhere on Discord. Changes show up for everyone within a minute.</p></div>
      <img class="mascot fh-fox" src="${M("piglindance")}" alt="" width="92" height="92"></header>
    <div class="split">
      <div class="stack">
        <section class="panel"><header><h3>Pictures</h3></header><div class="pad">
          <div class="b-row2">
            <div class="field"><label>Avatar</label><div class="btnrow">${btn("pf-av", "Upload", "sm", "upload")}${btn("pf-av-rm", "Remove", "sm ghost")}</div><div class="help">PNG, JPG or GIF, up to 8 MB.</div></div>
            <div class="field"><label>Banner</label><div class="btnrow">${btn("pf-bn", "Upload", "sm", "upload")}${btn("pf-bn-rm", "Remove", "sm ghost")}</div><div class="help">Wide, about 600 × 240.</div></div>
          </div><input type="file" id="pf-file" hidden accept="image/png,image/jpeg,image/gif,image/webp">
        </div></section>
        <section class="panel"><header><h3>Name and About me</h3></header><div class="pad">
          ${field("pf-name", "Username", `<input id="pf-name" value="${esc(PROF.username)}" maxlength="32">`, "Discord allows two username changes an hour. Per-server nicknames are set in each server.")}
          ${field("pf-about", "About me", `<textarea id="pf-about" rows="5" maxlength="400" style="font-family:var(--body);font-size:15px">${esc(PROF.about)}</textarea>`, "Shown on the bot's profile. Markdown works.")}
        </div></section>
        <section class="panel"><header><h3>Status</h3></header><div class="pad">
          <div class="field"><label>Shown as</label>${segHTML("pfst", pr.status, STATUS)}</div>
          <div class="b-row2">${field("pf-act", "Activity", `<select id="pf-act">${Object.entries(ACT).map(([k, v]) => `<option value="${k}" ${k === pr.activity ? "selected" : ""}>${v}</option>`).join("")}</select>`)}
            ${field("pf-text", "Text", `<input id="pf-text" value="${esc(pr.text)}" maxlength="128" placeholder="/play">`)}</div>
          <div id="pf-url-row" ${pr.activity === "streaming" ? "" : "hidden"}>${field("pf-url", "Stream link", `<input id="pf-url" value="${esc(pr.url || "")}" placeholder="https://twitch.tv/...">`)}</div>
        </div></section>
        <div class="btnrow">${btn("pf-save", "Save", "primary", "save")}</div>
      </div>
      <aside class="stack"><section class="panel"><header><h3>Preview</h3></header><div class="pad" style="padding:0">
        <div class="pf-card"><div class="pf-banner" id="pfv-banner"></div><img class="pf-avatar" id="pfv-avatar" alt="">
          <div class="pf-body"><b id="pfv-name"></b> <span class="dc-app">APP</span><div class="pf-status" id="pfv-status"></div>
          <div class="pf-about" id="pfv-about"></div></div></div></div></section></aside>
    </div>`;
  const paint = () => {
    const banner = pending.remove_banner ? null : pending.banner ? "data:image;base64," + pending.banner : PROF.banner;
    $("#pfv-banner").style.backgroundImage = banner ? `url("${banner}")` : "";
    $("#pfv-avatar").src = pending.remove_avatar ? "/ds/icon.png" : pending.avatar ? "data:image;base64," + pending.avatar : PROF.avatar;
    $("#pfv-name").textContent = val("pf-name");
    const act = val("pf-act"), text = val("pf-text");
    $("#pfv-status").textContent = (STATUS[segVal(root, "pfst")] || "Online") + (act !== "none" && text ? " · " + (act === "custom" ? text : ACT[act] + " " + text) : "");
    $("#pfv-about").innerHTML = md(val("pf-about"));
    $("#pf-url-row").hidden = act !== "streaming";
  };
  let target = null;
  const file = $("#pf-file");
  $("#pf-av").onclick = () => { target = "avatar"; file.click(); };
  $("#pf-bn").onclick = () => { target = "banner"; file.click(); };
  $("#pf-av-rm").onclick = () => { pending.avatar = null; pending.remove_avatar = true; paint(); };
  $("#pf-bn-rm").onclick = () => { pending.banner = null; pending.remove_banner = true; paint(); };
  file.onchange = async () => {
    const f = file.files[0]; file.value = "";
    if (!f) return;
    if (f.size > 8 * 1024 * 1024) return toast("bad", "That file is too big", "Pictures can be up to 8 MB.");
    pending[target] = await new Promise(res => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.readAsDataURL(f); });
    pending["remove_" + target] = false;
    paint();
  };
  ["pf-name", "pf-about", "pf-text", "pf-url"].forEach(id => $("#" + id).oninput = paint);
  $("#pf-act").onchange = paint;
  wireSegs(root, paint);
  $("#pf-save").onclick = async () => {
    const b = $("#pf-save"); b.classList.add("busy"); b.disabled = true;
    const bodyOut = {about: val("pf-about"), presence: {status: segVal(root, "pfst"), activity: val("pf-act"), text: val("pf-text"), url: val("pf-url")}};
    if (val("pf-name") !== PROF.username) bodyOut.username = val("pf-name");
    for (const k of ["avatar", "banner"]) {
      if (pending[k]) bodyOut[k] = pending[k];
      if (pending["remove_" + k]) bodyOut["remove_" + k] = true;
    }
    try {
      const r = await api("/api/profile", {method: "POST", body: bodyOut});
      toast("ok", "Profile saved", r.done.length ? "Changed: " + r.done.join(", ") + "." : "Nothing had changed.");
      window.showProfile();
    } catch (e) { toast("bad", "Not saved", e.message); b.classList.remove("busy"); b.disabled = false; }
  };
  paint();
};

/* =====================================================================================
   The Commands guide (its own screen in the rail)
   ===================================================================================== */
const GUIDE = [
  {area: "Music", kanji: "音", how: "Join a voice channel and use /play with a YouTube or Spotify link, a playlist, or search words. The bot joins your channel. Only people in the bot's channel can control it.", cmds: [
    ["/play", "Play a song or playlist: a YouTube or Spotify link, or search words", "everyone"],
    ["/playnext", "Put a song at the front of the queue", "everyone"],
    ["/skip · /skipto", "Skip the song, or jump to a place in the queue", "everyone"],
    ["/pause · /resume · /stop", "Pause, carry on, or stop and leave", "everyone"],
    ["/queue · /nowplaying", "See the queue, or the song playing", "everyone"],
    ["/volume · /loop · /shuffle", "Volume 0-200%, loop the song or queue, shuffle", "everyone"],
    ["/remove · /move · /clear · /seek", "Edit the queue, or jump inside the song", "everyone"]]},
  {area: "Greetings", kanji: "迎", how: "Set the welcome and farewell messages on Servers → Welcome, with the embed builder. Autorole gives roles to new members, and can give people their roles back when they rejoin.", cmds: [
    ["/greetings testgreet", "Post the welcome message here, as if someone just joined", "Manage Server"],
    ["/greetings testfarewell", "Post the farewell message here", "Manage Server"],
    ["/greetings testdm", "Send yourself the welcome DM", "Manage Server"],
    ["/greetings status", "What's on: welcome, farewell, DM, autorole", "Manage Server"]]},
  {area: "Moderation", kanji: "守", how: "Every action becomes a numbered case. Set a mod-log channel on Servers → Moderation to get an entry for each. The bot won't act on someone whose role is at or above yours or its own.", cmds: [
    ["/ban", "Ban someone. Add a duration (1d, 2w) for a temporary ban, lifted by itself", "Ban Members"],
    ["/unban", "Lift a ban, by user ID", "Ban Members"],
    ["/kick", "Kick someone out", "Kick Members"],
    ["/timeout · /untimeout", "Mute someone for up to 28 days, or end it early", "Timeout Members"],
    ["/warn · /warnings", "Warn someone, or see someone's history", "Timeout Members"],
    ["/case · /reason", "Look up a case, change its reason", "Timeout Members"],
    ["/purge", "Delete up to 500 recent messages, optionally from one person", "Manage Messages"]]},
  {area: "Forum roles", kanji: "授", how: "Set up on Servers → Forum roles. When a post in a forum gets the tag you pick (for example Accepted), the person who made the post gets a role. Optionally the role goes again when the tag is removed, and the bot posts a message in the post or DMs the author. Make the tag moderator-only in the forum's settings, so authors can't tag their own posts.", cmds: []},
  {area: "VC maker", kanji: "室", how: "Set up on Servers → VC maker. Join a generator voice channel and the bot makes you your own channel and moves you in; it's deleted when everyone leaves. Control it with the panel in its chat or these commands. If the owner leaves, anyone inside can claim it.", cmds: [
    ["/voice name · limit · status", "Rename it, set how many can join, set the line under it", "the owner"],
    ["/voice lock · unlock", "Stop or allow new people joining", "the owner"],
    ["/voice ghost · unghost", "Hide or show it in the channel list", "the owner"],
    ["/voice permit · reject", "Let a person or role in, or keep them out", "the owner"],
    ["/voice invite · transfer", "DM someone an invite, or give the channel away", "the owner"],
    ["/voice bitrate · region · nsfw", "Sound quality, voice server, age-restricted", "the owner"],
    ["/voice text · lfm", "Add a text channel, or post Looking for members", "the owner"],
    ["/voice claim", "Take over when the owner left", "anyone in it"],
    ["/voice save · load", "Save your channel's settings for next time, or put them back", "the owner"],
    ["/voice panel", "Post the control panel in any channel", "Manage Channels"]]},
  {area: "Tickets", kanji: "票", how: "Make panels on Servers → Tickets: one button or dropdown option per kind of ticket, each with its own team, category, questions and first message. Editing a panel updates the posted message. In a ticket: Close (with or without a reason) and Claim; once closed: Reopen, Transcript, Delete.", cmds: [
    ["/ticket close", "Close this ticket, optionally with a reason", "its owner or the team"],
    ["/ticket closerequest", "Ask the owner if it can be closed", "the team"],
    ["/ticket reopen · delete", "Reopen or delete it", "the team"],
    ["/ticket claim", "Take the ticket (again to give it back)", "the team"],
    ["/ticket add · remove", "Let someone in, or take them out", "the team"],
    ["/ticket rename · transcript", "Rename it, or save a transcript", "the team"],
    ["/ticket panel", "Post a panel again in this channel", "Manage Server"]]},
  {area: "Tags", kanji: "札", how: "Saved messages anyone can post by name. Make text tags in Discord, or tags with embeds and images on Servers → Tags.", cmds: [
    ["/tag show", "Post a tag (type to search the names)", "everyone"],
    ["/tag list · /tag info", "Every tag, or who made one and how often it's used", "everyone"],
    ["/tag create · edit · delete", "Make, change or delete a text tag", "Manage Messages"]]},
  {area: "Community", kanji: "案", how: "Suggestions and the starboard are switched on per server on the dashboard. Auto-replies answer trigger words with a message you build.", cmds: [
    ["/suggest", "Suggest something; it's posted with vote reactions", "everyone"],
    ["/suggestion approve · deny · consider · implemented", "Decide on a suggestion", "Manage Server"]]},
  {area: "Reminders and info", kanji: "暦", how: "Reminders arrive in the channel you set them in, or by DM if that channel is gone.", cmds: [
    ["/remind", "A reminder later, e.g. in 2h", "everyone"],
    ["/reminders list · delete", "Your reminders", "everyone"],
    ["/userinfo · /serverinfo · /avatar", "Facts about someone or this server, a full-size avatar", "everyone"],
    ["/say", "Have the bot post a message", "Manage Messages"]]},
];
window.showGuide = () => {
  $("#crumb-t").textContent = "Commands";
  $("#guide-root").innerHTML = `<header class="fleet-head page-intro"><div class="fh-words">
      <p class="eyebrow">Guide · <b>${GUIDE.reduce((n, g) => n + g.cmds.length, 0)} entries</b></p>
      <h1 class="brush fh-title">Commands</h1>
      <p class="lede">Everything the bot does, by area. Type / in Discord to see them there too. Who can use each one is listed beside it; server admins can change that in Server Settings → Integrations → the bot.</p>
    </div><img class="mascot fh-fox" src="${M("nod")}" alt="" width="92" height="92"></header>
    <div class="stack">${GUIDE.map(g => `<section class="panel"><header><h3>${g.area}</h3><span class="sp">${g.cmds.length || ""}</span></header><div class="pad">
      <p>${esc(g.how)}</p>${g.cmds.length ? `<div class="tablewrap"><table class="tbl"><thead><tr><th>Command</th><th>What it does</th><th>Who can use it</th></tr></thead><tbody>
      ${g.cmds.map(([c, w, who]) => `<tr><td class="mono" style="white-space:nowrap">${esc(c)}</td><td>${esc(w)}</td><td>${who === "everyone" ? '<span class="pill ok">Everyone</span>' : `<span class="pill ghost">${esc(who)}</span>`}</td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">No commands: it works by itself once it is set up.</p>'}</div></section>`).join("")}</div>`;
};
})();
