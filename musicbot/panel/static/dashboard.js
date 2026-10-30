/* The server dashboard: a server picker, then one server's settings on the ofuda shelf.
   Uses the helpers from index.html ($, esc, api, toast, svg, M). Every save is validated again by
   the bot, which also checks that the signed-in person has Manage Server there. */
(() => {
const TABS = [
  {id: "welcome", kanji: "迎", label: "Welcome", group: "People"},
  {id: "roles", kanji: "役", label: "Roles", group: "People"},
  {id: "moderation", kanji: "守", label: "Moderation", group: "Safety"},
  {id: "logging", kanji: "記", label: "Logging", group: "Safety"},
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
const ACTION_PILL = {ban: "bad", unban: "ok", kick: "warn", timeout: "warn", untimeout: "ok", warn: "gold"};
const ACTION_WORD = {ban: "Ban", unban: "Unban", kick: "Kick", timeout: "Timeout", untimeout: "Timeout ended", warn: "Warning"};
const COMMANDS = [
  ["/ban", "Ban, for good or for a while (e.g. 1d, 2w)", "Ban Members"],
  ["/unban", "Lift a ban by user ID", "Ban Members"],
  ["/kick", "Kick someone out", "Kick Members"],
  ["/timeout · /untimeout", "Mute someone for up to 28 days, or end it early", "Timeout Members"],
  ["/warn · /warnings", "Warn someone, see someone's history", "Timeout Members"],
  ["/case · /reason", "Look up a case, change its reason", "Timeout Members"],
  ["/purge", "Delete recent messages, optionally from one person", "Manage Messages"],
];

let G = null;           // the open server: {guild, channels, roles, emojis, config, menus, ...}
let tab = "welcome";
let editing = null;     // the reaction-role menu being edited (a copy), or null
let casePage = 0, caseUser = "";

const root = () => $("#dash-root");
const field = (id, label, inner, help = "") =>
  `<div class="field"><label for="${id}">${label}</label>${inner}${help ? `<div class="help">${help}</div>` : ""}</div>`;
const sw = (id, label, on) =>
  `<label class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span class="track"></span>${label}</label>`;
const btn = (id, label, cls = "", icon = "") =>
  `<button class="btn ${cls}" type="button" id="${id}">${cls.includes("ghost") ? "" : '<span class="p5-hl" aria-hidden="true"></span>'}${icon ? svg(icon) : ""}<span>${esc(label)}</span></button>`;

function channelSelect(id, value, none = "Not set") {
  const groups = {};
  for (const c of G.channels) (groups[c.category || "No category"] ||= []).push(c);
  const opts = Object.entries(groups).map(([cat, list]) => `<optgroup label="${esc(cat)}">${list.map(c =>
    `<option value="${c.id}" ${c.id === value ? "selected" : ""} ${c.can_send ? "" : "disabled"}>#${esc(c.name)}${c.can_send ? "" : " (the bot can't post here)"}</option>`).join("")}</optgroup>`).join("");
  return `<select id="${id}"><option value="">${esc(none)}</option>${opts}</select>`;
}
function roleChips(name, selected) {
  return `<div class="chiprow" data-roles="${name}">${G.roles.map(r =>
    `<button class="chip" type="button" data-id="${r.id}" aria-pressed="${selected.includes(r.id)}" ${r.problem ? "disabled" : ""}
      title="${esc(r.problem || "")}">${r.color ? `<span aria-hidden="true" style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${esc(r.color)};margin-right:6px"></span>` : ""}@${esc(r.name)}</button>`).join("")}</div>`;
}
const pickedRoles = name => [...root().querySelectorAll(`[data-roles="${name}"] .chip[aria-pressed="true"]`)].map(b => b.dataset.id);
function wireChips() {
  root().querySelectorAll(".chiprow .chip").forEach(b => b.onclick = () => b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")));
}
const val = id => $("#" + id).value;
const on = id => $("#" + id).checked;

async function save(section, data, okText) {
  try {
    const r = await api(`/api/dash/guild/${G.guild.id}/config`, {method: "POST", body: {section, data}});
    G.config[section] = r.data;
    toast("ok", "Saved", okText);
    return true;
  } catch (e) { toast("bad", "Not saved", e.message); return false; }
}

/* ---------- entry points (index.html calls these) ---------- */
window.dashRoute = async parts => {
  const [gid, t] = parts;
  if (!gid) return showPicker();
  if (!G || G.guild.id !== gid) {
    root().innerHTML = `<div class="loading-fox"><img class="mascot" src="${M("run")}" alt="" width="56" height="56"><span>Reading the server's channels and roles<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span></span></div>`;
    try { G = await api(`/api/dash/guild/${gid}`); }
    catch (e) { G = null; return showProblem(e.message); }
    editing = null; casePage = 0; caseUser = "";
  }
  tab = TABS.some(x => x.id === t) ? t : "welcome";
  showGuild();
};

function showProblem(text) {
  root().innerHTML = `<div class="empty"><div style="position:relative"><div class="fox-halo"></div><img class="mascot fox" src="${M("hurt")}" alt="" width="132" height="132"></div>
    <h3>This server can't be opened</h3><p>${esc(text)}</p><div class="btnrow" style="justify-content:center"><a class="btn" href="#servers"><span class="p5-hl" aria-hidden="true"></span><span>All servers</span></a></div></div>`;
}

async function showPicker() {
  G = null;
  $("#crumb-t").textContent = "Servers";
  root().innerHTML = `<div class="loading-fox"><img class="mascot" src="${M("run")}" alt="" width="56" height="56"><span>Finding the servers you can manage<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span></span></div>`;
  let data;
  try { data = await api("/api/dash/guilds"); } catch (e) { return showProblem(e.message); }
  const owner = window.ME && window.ME.kind === "owner";
  const head = `<header class="fleet-head page-intro"><div class="fh-words">
      <p class="eyebrow">Dashboard · <b>${data.guilds.length} server${data.guilds.length === 1 ? "" : "s"}</b></p>
      <h1 class="brush fh-title">Your servers</h1>
      <p class="lede">${owner ? "As the panel's owner you can open every server the bot is in." : "Servers where you have the Manage Server permission and the bot is a member."}</p>
    </div><img class="mascot fh-fox" src="${M("fox-pounce")}" alt="" width="92" height="92"></header>${intentsNote(data.intents)}`;
  if (!data.guilds.length) {
    root().innerHTML = head + `<div class="empty"><div style="position:relative"><div class="fox-halo"></div><img class="mascot fox" src="${M("placeblock")}" alt="" width="132" height="132"></div>
      <h3>No servers to manage yet</h3><p>${owner ? "The bot isn't in any server. Add it with the invite on the Overview." : "You need the Manage Server permission in a server the bot is in. Ask that server's owner to add the bot, or to give you the permission."}</p></div>`;
    return;
  }
  root().innerHTML = head + `<div class="grid gauto bleed">${data.guilds.map(g => `
    <a class="card hoverable" href="#servers/${g.id}" style="text-decoration:none;color:inherit"><span class="p5-hl" aria-hidden="true"></span>
      <header><h3>${esc(g.name)}</h3><span class="k">${g.members ?? "?"} members</span></header>
      <div style="display:flex;align-items:center;gap:14px">
        ${g.icon ? `<img src="${esc(g.icon)}" alt="" width="56" height="56" style="border-radius:50%;border:2.5px solid var(--slab-bg)">` : `<span class="seal dead" role="img" aria-label="No icon"><b>${esc(g.name.slice(0, 1))}</b></span>`}
        <p class="card-sub" style="margin:0">Welcome messages, roles, moderation and logs.</p>
      </div></a>`).join("")}</div>`;
}

function intentsNote(i) {
  if (!i || (i.members && i.message_content)) return "";
  return `<div class="note bad" style="margin-bottom:18px"><b>Two Discord switches are off</b>Welcome messages, autorole, member logs and permission checks need <i>Server Members Intent</i>, and edit/delete logs need <i>Message Content Intent</i>. Turn both on in the Discord Developer Portal → your application → Bot → Privileged Gateway Intents, then restart the bot from the Overview.</div>`;
}

function showGuild() {
  const g = G.guild;
  $("#crumb-t").textContent = g.name;
  const missing = G.missing.length ? `<div class="note warn" style="margin-bottom:18px"><b>The bot is missing ${G.missing.length === 1 ? "a permission" : "some permissions"}</b>${G.missing.map(m => `${esc(m.perm.replace(/_/g, " "))} (for ${esc(m.for)})`).join(" · ")}. Give its role these in Server Settings → Roles → ${esc(G.bot_role)}.</div>` : "";
  const groups = [...new Set(TABS.map(t => t.group))];
  root().innerHTML = `
    <header class="fleet-head page-intro"><div class="fh-words">
      <p class="eyebrow"><a href="#servers">All servers</a> · <b>${esc(g.id)}</b></p>
      <h1 class="brush fh-title">${esc(g.name)}</h1>
      <p class="lede">${g.members ?? "?"} members. Each panel saves on its own.</p>
    </div>${g.icon ? `<img class="fh-fox" src="${esc(g.icon)}" alt="" width="92" height="92" style="border-radius:50%;border:2.5px solid var(--slab-bg)">` : ""}</header>
    ${intentsNote(G.intents)}${missing}
    <nav class="shelf" role="tablist" aria-label="Server settings" style="margin-bottom:24px"><span class="shelf-rope" aria-hidden="true"></span>
      ${groups.map(gr => `<div class="shelf-group"><span class="sg-label">${gr}</span>${TABS.filter(t => t.group === gr).map(t =>
        `<button class="slipbtn" type="button" role="tab" aria-selected="${t.id === tab}" data-tab="${t.id}"><span class="sl-cord" aria-hidden="true"></span><span class="sl-kanji" aria-hidden="true">${t.kanji}</span><span class="sl-label">${t.label}</span>${t.id === "roles" && G.menus.length ? `<span class="n">${G.menus.length}</span>` : ""}</button>`).join("")}</div>`).join("")}
    </nav>
    <div id="tab-body"></div>`;
  root().querySelectorAll(".slipbtn").forEach(b => b.onclick = () => { location.hash = `#servers/${g.id}/${b.dataset.tab}`; });
  ({welcome: tabWelcome, roles: tabRoles, moderation: tabModeration, logging: tabLogging})[tab]();
}
const body = () => $("#tab-body");

/* ---------- Welcome ---------- */
function placeholderHelp() {
  return "Placeholders: " + Object.entries(G.placeholders).map(([k, v]) => `<code>${esc(k)}</code> ${esc(v)}`).join(" · ");
}
function tabWelcome() {
  const w = G.config.welcome, f = G.config.farewell, a = G.config.autorole;
  body().innerHTML = `<div class="grid g2">
    <div class="stack">
      <section class="panel"><header><h3>Welcome message</h3><span class="sp">${w.enabled ? "On" : "Off"}</span></header><div class="pad">
        <div class="switches" style="margin-bottom:16px">${sw("w-enabled", "Post a message when someone joins", w.enabled)}</div>
        ${field("w-channel", "Channel", channelSelect("w-channel", w.channel_id))}
        ${field("w-message", "Message", `<textarea id="w-message" rows="4" style="font-family:var(--body);font-size:15px">${esc(w.message)}</textarea>`, placeholderHelp())}
        <div class="switches" style="margin-bottom:16px">${sw("w-embed", "Send it as an embed with their avatar", w.embed)}${sw("w-ping", "Mention them above the embed, so they get a ping", w.ping)}</div>
        ${field("w-color", "Embed colour", `<input type="color" id="w-color" value="${esc(w.color)}" style="height:44px;padding:4px">`)}
        <div class="switches" style="margin-bottom:12px">${sw("w-dm", "Also send them a direct message", w.dm_enabled)}</div>
        ${field("w-dm-message", "Direct message", `<textarea id="w-dm-message" rows="3" style="font-family:var(--body);font-size:15px">${esc(w.dm_message)}</textarea>`, "Sent only if their DMs are open.")}
        <div class="btnrow">${btn("w-save", "Save", "primary", "save")}${btn("w-test", "Send a test", "")}</div>
      </div></section>
    </div>
    <div class="stack">
      <section class="panel"><header><h3>Farewell message</h3><span class="sp">${f.enabled ? "On" : "Off"}</span></header><div class="pad">
        <div class="switches" style="margin-bottom:16px">${sw("f-enabled", "Post a message when someone leaves", f.enabled)}</div>
        ${field("f-channel", "Channel", channelSelect("f-channel", f.channel_id))}
        ${field("f-message", "Message", `<textarea id="f-message" rows="3" style="font-family:var(--body);font-size:15px">${esc(f.message)}</textarea>`, "The same placeholders work here. {user} can't ping someone who left.")}
        <div class="switches" style="margin-bottom:16px">${sw("f-embed", "Send it as an embed", f.embed)}</div>
        ${field("f-color", "Embed colour", `<input type="color" id="f-color" value="${esc(f.color)}" style="height:44px;padding:4px">`)}
        <div class="btnrow">${btn("f-save", "Save", "", "save")}${btn("f-test", "Send a test", "ghost")}</div>
      </div></section>
      <section class="panel"><header><h3>Autorole</h3><span class="sp">${a.enabled ? "On" : "Off"}</span></header><div class="pad">
        <div class="switches" style="margin-bottom:16px">${sw("a-enabled", "Give roles to everyone who joins", a.enabled)}</div>
        <div class="field"><label>Roles for new members</label>${roleChips("a-roles", a.role_ids)}</div>
        <div class="field"><label>Roles for new bots</label>${roleChips("a-bots", a.bot_role_ids)}<div class="help">Greyed-out roles are above the bot's own role (${esc(G.bot_role)}) or belong to an integration. If the server uses rules screening, roles are given once the member accepts the rules.</div></div>
        <div class="btnrow">${btn("a-save", "Save", "", "save")}</div>
      </div></section>
    </div></div>`;
  wireChips();
  $("#w-save").onclick = () => save("welcome", {enabled: on("w-enabled"), channel_id: val("w-channel") || null, message: val("w-message"),
    embed: on("w-embed"), ping: on("w-ping"), color: val("w-color"), dm_enabled: on("w-dm"), dm_message: val("w-dm-message")},
    "New members are greeted with this from now on.");
  $("#f-save").onclick = () => save("farewell", {enabled: on("f-enabled"), channel_id: val("f-channel") || null, message: val("f-message"),
    embed: on("f-embed"), color: val("f-color")}, "It posts when someone leaves.");
  $("#a-save").onclick = () => save("autorole", {enabled: on("a-enabled"), role_ids: pickedRoles("a-roles"), bot_role_ids: pickedRoles("a-bots")},
    "New members get these roles from now on.");
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
  body().innerHTML = `<div class="btnrow" style="margin-bottom:18px">${btn("rr-new", "New menu", "primary", "")}</div>` + (G.menus.length
    ? `<div class="grid gauto">${G.menus.map(m => `<div class="card"><header><h3>${esc(m.title)}</h3><span class="k">${KIND_WORD[m.kind]}</span></header>
        <p class="card-sub">#${esc(ch(m.channel_id))} · ${m.roles.length} role${m.roles.length === 1 ? "" : "s"} · ${esc(MODE_WORD[m.mode])}</p>
        ${m.message_id ? "" : `<div class="note warn" style="margin:8px 0"><b>Its message was deleted</b>Save it again to post it again.</div>`}
        <div class="btnrow">${btn("rr-edit-" + m.id, "Edit", "sm")}${btn("rr-del-" + m.id, "Delete", "sm ghost")}</div></div>`).join("")}</div>`
    : `<div class="empty"><div style="position:relative"><div class="fox-halo"></div><img class="mascot fox" src="${M("placeblock")}" alt="" width="132" height="132"></div>
        <h3>No role menus yet</h3><p>A menu is a message with buttons, a dropdown or reactions. Members click to give themselves roles: colours, pings, regions, game roles.</p></div>`);
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
      <div class="help">Emoji: paste one like 🎮, or a custom one from this server: ${G.emojis.length ? G.emojis.slice(0, 12).map(e => `<button class="chip rr-emo" type="button" data-e="${esc(e.text)}" title=":${esc(e.name)}:"><img src="${esc(e.url)}" alt=":${esc(e.name)}:" width="16" height="16" style="vertical-align:-3px"></button>`).join(" ") : "this server has none"}.</div>
    </div>
    <div class="btnrow">${btn("rr-add", "Add a role", "sm", "")}</div>
    <hr>
    <div class="btnrow">${btn("rr-save", m.id ? "Save and update the message" : "Post the menu", "primary", "save")}${btn("rr-cancel", "Cancel", "ghost")}</div>
  </div></section>`;
  const grab = () => {   // copy the form back into `editing`, so re-rendering keeps what was typed
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
      const path = `/api/dash/guild/${G.guild.id}/menus` + (m.id ? `/${m.id}` : "");
      const r = await api(path, {method: m.id ? "PUT" : "POST", body: m});
      G.menus = G.menus.filter(x => x.id !== r.menu.id).concat([r.menu]).sort((a, b) => a.id - b.id);
      toast("ok", m.id ? "Menu updated" : "Menu posted", "It's live in Discord now.");
      editing = null;
      showGuild();
    } catch (e) { toast("bad", "Not posted", e.message); b.classList.remove("busy"); b.disabled = false; }
  };
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
      <section class="panel"><header><h3>Commands</h3></header><div class="pad"><div class="tablewrap"><table class="tbl"><tbody>${COMMANDS.map(([c, what, perm]) =>
        `<tr><td class="mono" style="white-space:nowrap">${esc(c)}</td><td>${esc(what)}<br><span class="muted" style="font-size:12.5px">needs ${esc(perm)}</span></td></tr>`).join("")}</tbody></table></div>
        <p class="muted" style="margin-top:10px;font-size:12.5px">Who sees these is set in Discord: Server Settings → Integrations → the bot.</p></div></section>
    </aside></div>`;
  $("#m-save").onclick = () => save("moderation", {modlog_channel_id: val("m-log") || null, dm_users: on("m-dm")}, "Moderation uses these from the next case on.");
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
      <div class="field"><label>Channels whose messages aren't logged</label>
        <div class="chiprow" data-roles="l-ignored">${G.channels.map(c => `<button class="chip" type="button" data-id="${c.id}" aria-pressed="${l.ignored_channel_ids.includes(c.id)}">#${esc(c.name)}</button>`).join("")}</div>
        <div class="help">The log channels themselves are always left out. Deleted messages can be quoted only if they were sent while the bot was running.</div></div>
    </div></section></div>`;
  wireChips();
  $("#l-save").onclick = () => save("logging", {enabled: on("l-enabled"), ignore_bots: on("l-bots"), ignored_channel_ids: pickedRoles("l-ignored"),
    channels: Object.fromEntries(LOGS.map(([k]) => [k, val("l-" + k) || null]))}, "Events are logged where you picked.");
}
})();
