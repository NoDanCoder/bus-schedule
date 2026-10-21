"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- identity & storage (every wrapped: storage can be blocked) ---------------------------
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
}
let clientId = store("busalert.client");
if (!clientId) {
  clientId = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/[^A-Za-z0-9_-]/g, "");
  store("busalert.client", clientId);
}
const H = { "X-Client-Id": clientId };

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...H, "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

// ---- map ----------------------------------------------------------------------------------
const map = L.map("map", { zoomControl: true }).setView([4.65, -74.09], 12);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const stopLayer = L.layerGroup().addTo(map);

// ---- state --------------------------------------------------------------------------------
let watches = [];                 // from /api/watches
let tick = null;                  // last SSE tick
let tickAt = 0;                   // performance.now() when received
let tickGap = 3000;               // ms between the last two ticks (drives marker glide time)
const markers = new Map();        // vehicle_id -> {marker, from, to, t0, dur, el, tip}
let picked = null;                // stop being configured
let followFit = true;             // auto-fit map until the user interacts
map.on("dragstart zoomstart", (e) => { if (e.originalEvent || e.type === "dragstart") followFit = false; });

// ---- connection status --------------------------------------------------------------------
let health = null;
function renderStatus() {
  const el = $("#status"), banner = $("#banner");
  let cls = "off", text = "offline", note = "";
  if (es && es.readyState === EventSource.OPEN && health) {
    if (!health.network_loaded) { cls = "stale"; text = "loading routes…"; }
    else if (health.feed_stale) {
      cls = "stale"; text = "feed delayed";
      note = health.demo ? "" : "TransMilenio’s live feed hasn’t updated recently — arrival times may be out of date.";
    } else { cls = "live"; text = "live" + (health.demo ? " · demo" : ""); }
  }
  el.className = "pill " + cls; el.textContent = text;
  if (health && health.last_error && cls !== "live") note = note || health.last_error;
  banner.hidden = !note; banner.textContent = note;
}
async function pollHealth() {
  try { health = await api("/api/health"); } catch { health = null; }
  renderStatus();
}

// ---- event stream -------------------------------------------------------------------------
let es = null;
function connect() {
  es = new EventSource("api/stream?client_id=" + encodeURIComponent(clientId));
  es.onopen = renderStatus;
  es.onerror = renderStatus;
  es.addEventListener("tick", (e) => {
    const now = performance.now();
    if (tickAt) tickGap = Math.min(8000, Math.max(1500, now - tickAt));
    tick = JSON.parse(e.data); tickAt = now; applyTick();
  });
  es.addEventListener("alert", (e) => onAlert(JSON.parse(e.data)));
}

// ---- watches ------------------------------------------------------------------------------
async function loadWatches() {
  watches = await api("/api/watches");
  drawGeometry();
  renderWatches();
}

function drawGeometry() {
  routeLayer.clearLayers(); stopLayer.clearLayers();
  const pts = [];
  for (const w of watches) {
    for (const g of w.geometry) {
      L.polyline(g.points, { color: "#d6001c", weight: 4, opacity: 0.35 }).addTo(routeLayer);
      pts.push(...g.points);
    }
    L.marker([w.stop.lat, w.stop.lon], { icon: L.divIcon({ className: "", html: '<div class="stop-icon"></div>', iconSize: [18, 18] }) })
      .bindTooltip(esc(w.stop.name)).addTo(stopLayer);
    pts.push([w.stop.lat, w.stop.lon]);
  }
  if (pts.length && followFit) map.fitBounds(L.latLngBounds(pts).pad(0.1), { maxZoom: 16 });
}

function etaFor(e) { return Math.max(0, e.eta_s - (performance.now() - tickAt) / 1000); }
function fmt(s) {
  if (s < 5) return "now";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m >= 1 ? `${m} min ${String(sec).padStart(2, "0")} s` : `${sec} s`;
}
const BASIS = {
  learned: ["●", "Based on speeds learned on this route at this time of week"],
  mixed: ["◐", "Partly learned, partly estimated"],
  default: ["○", "Estimated from average speed; still learning this route"],
};

function renderWatches() {
  const box = $("#watches");
  if (!watches.length) { box.innerHTML = '<p class="muted">Nothing yet. Search for the stop you’ll be waiting at.</p>'; return; }
  box.innerHTML = watches.map((w) => `
    <div class="card" data-w="${esc(w.id)}">
      <h3 data-focus="${esc(w.id)}"><span>${esc(w.label || w.stop.name)}</span><button data-del="${esc(w.id)}" aria-label="Stop watching ${esc(w.stop.name)}">Remove</button></h3>
      <div class="meta">${esc(w.stop.code ? "Stop " + w.stop.code + " · " : "")}alerts at ${w.thresholds.join(", ")} min${w.ntfy_topic ? " · push on" : ""}</div>
      <div class="buses"></div>
    </div>`).join("");
  paintEtas();
}

function paintEtas() {
  for (const card of document.querySelectorAll(".card")) {
    const data = tick && tick.watches.find((x) => x.watch_id === card.dataset.w);
    const buses = card.querySelector(".buses");
    const list = data ? data.etas.filter((e) => e.status === "approaching") : [];
    if (!tick) { buses.innerHTML = '<p class="muted">Waiting for data…</p>'; continue; }
    if (!list.length) { buses.innerHTML = '<p class="muted">No buses on the way right now.</p>'; continue; }
    buses.innerHTML = list.slice(0, 6).map((e) => {
      const s = etaFor(e), [dot, why] = BASIS[e.basis] || BASIS.default;
      const official = e.official_eta_s != null ? ` · official ${fmt(Math.max(0, e.official_eta_s - (performance.now() - tickAt) / 1000))}` : "";
      return `<div class="bus" data-v="${esc(e.vehicle_id)}">
        <span class="badge">${esc(e.route)}</span>
        <span>${esc(e.headsign || "")}<small>${(e.distance_m / 1000).toFixed(1)} km · ${e.stops_away} stop${e.stops_away === 1 ? "" : "s"}${esc(official)}</small></span>
        <span class="eta ${s < 120 ? "soon" : ""}">${fmt(s)}<small class="basis" title="${esc(why)}">${dot}</small></span>
      </div>`;
    }).join("");
  }
}

// ---- live map markers (smoothly interpolated between feed updates) ------------------------
function busIcon(e) {
  const rot = e.bearing != null ? `style="transform:rotate(${e.bearing}deg)"` : 'style="display:none"';
  return L.divIcon({
    className: "", iconSize: [34, 34], iconAnchor: [17, 17],
    html: `<div class="bus-icon ${e.status === "passed" ? "passed" : ""}"><div class="orbit" ${rot}><span class="arrow"></span></div><div class="dot">${esc(e.route.slice(0, 4))}</div></div>`,
  });
}
function applyTick() {
  const seen = new Set();
  const dur = tickGap;
  for (const w of tick.watches) {
    for (const e of w.etas) {
      if (seen.has(e.vehicle_id)) continue;
      seen.add(e.vehicle_id);
      let m = markers.get(e.vehicle_id);
      if (!m) {
        const marker = L.marker([e.lat, e.lon], { icon: busIcon(e), zIndexOffset: 500 }).addTo(map);
        m = { marker, from: [e.lat, e.lon], to: [e.lat, e.lon], t0: 0, dur, status: e.status };
        marker.bindTooltip(() => (m.e ? `${m.e.route} · ${fmt(etaFor(m.e))}` : ""), { direction: "top", offset: [0, -16] });
        markers.set(e.vehicle_id, m);
      } else {
        const p = m.marker.getLatLng();
        m.from = [p.lat, p.lng]; m.to = [e.lat, e.lon]; m.t0 = performance.now(); m.dur = dur;
        if (m.status !== e.status) { m.marker.setIcon(busIcon(e)); m.status = e.status; }
        else {
          const orbit = m.marker.getElement() && m.marker.getElement().querySelector(".orbit");
          if (orbit && e.bearing != null) { orbit.style.display = ""; orbit.style.transform = `rotate(${e.bearing}deg)`; }
        }
      }
      m.e = e;
    }
  }
  for (const [id, m] of markers) if (!seen.has(id)) { m.marker.remove(); markers.delete(id); }
  paintEtas();
  if (followFit && markers.size && !watches.some((w) => w.geometry.length)) fitAll();
}
function fitAll() {
  const pts = [...markers.values()].map((m) => m.marker.getLatLng());
  watches.forEach((w) => pts.push(L.latLng(w.stop.lat, w.stop.lon)));
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.2), { maxZoom: 16 });
}
function animate(now) {
  for (const m of markers.values()) {
    if (!m.t0) continue;
    const k = Math.min(1, (now - m.t0) / m.dur);
    m.marker.setLatLng([m.from[0] + (m.to[0] - m.from[0]) * k, m.from[1] + (m.to[1] - m.from[1]) * k]);
    if (m.marker.isTooltipOpen()) m.marker.setTooltipContent(`${m.e.route} · ${fmt(etaFor(m.e))}`);
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);
setInterval(paintEtas, 1000); // countdown between server updates

// ---- alerts: toast + sound + vibration + system notification ------------------------------
let audioCtx = null;
function beep(urgent) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const n = urgent ? 3 : 2;
    for (let i = 0; i < n; i++) {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.frequency.value = urgent ? 1040 : 760; o.connect(g); g.connect(audioCtx.destination);
      const t = audioCtx.currentTime + i * 0.22;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.25, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
      o.start(t); o.stop(t + 0.2);
    }
  } catch { /* audio blocked until a user gesture */ }
}
function onAlert(a) {
  addLog(a);
  const t = document.createElement("div");
  t.className = "toast"; t.innerHTML = `${esc(a.title)}<small>${esc(a.message)}</small>`;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), a.threshold <= 2 ? 20000 : 10000);
  beep(a.threshold <= 2);
  if (navigator.vibrate) navigator.vibrate(a.threshold <= 2 ? [300, 120, 300, 120, 300] : [250]);
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification(a.title, { body: a.message, tag: `${a.watch_id}:${a.vehicle_id}`, requireInteraction: a.threshold <= 2 }); } catch { /* ignore */ }
  }
}
function addLog(a) {
  const ul = $("#log");
  if (ul.querySelector(".muted")) ul.innerHTML = "";
  const li = document.createElement("li");
  li.innerHTML = `${esc(a.message)}<time>${new Date(a.ts * 1000 > Date.now() + 864e5 ? Date.now() : a.ts * 1000).toLocaleTimeString()}</time>`;
  ul.prepend(li);
  while (ul.children.length > 30) ul.lastChild.remove();
}
async function loadLog() {
  try {
    const rows = (await api("/api/alerts")).reverse();
    for (const r of rows) addLog({ message: r.message, ts: r.fired_at });
  } catch { /* ignore */ }
}
function setupNotifyButton() {
  const b = $("#notify");
  b.hidden = !("Notification" in window) || Notification.permission === "granted";
  b.onclick = async () => {
    beep(false);
    if ("Notification" in window) await Notification.requestPermission();
    b.hidden = Notification.permission === "granted";
  };
}

// ---- stop search & add form ---------------------------------------------------------------
let searchTimer = null;
$("#q").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#results").innerHTML = ""; return; }
  searchTimer = setTimeout(async () => {
    try { showResults(await api("/api/stops?q=" + encodeURIComponent(q))); }
    catch (err) { $("#results").innerHTML = `<li class="muted">${esc(err.message)}</li>`; }
  }, 250);
});
$("#near").addEventListener("click", () => {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    async (p) => showResults(await api(`/api/stops?lat=${p.coords.latitude}&lon=${p.coords.longitude}`)),
    () => { $("#results").innerHTML = '<li class="muted">Location unavailable.</li>'; },
  );
});
function showResults(stops) {
  const ul = $("#results");
  if (!stops.length) { ul.innerHTML = '<li class="muted">No stops found.</li>'; return; }
  ul.innerHTML = stops.map((s, i) => `<li tabindex="0" data-i="${i}">${esc(s.name)}${s.code ? " · " + esc(s.code) : ""}
    <small>${s.distance_m != null ? s.distance_m + " m · " : ""}${s.routes.map((r) => esc(r.name)).join(", ")}</small></li>`).join("");
  ul.onclick = ul.onkeydown = (e) => {
    if (e.type === "keydown" && e.key !== "Enter") return;
    const li = e.target.closest("li[data-i]");
    if (li) pick(stops[+li.dataset.i]);
  };
}
function pick(s) {
  picked = { stop: s, routes: new Set() };
  $("#results").innerHTML = "";
  $("#picked").textContent = s.name + (s.code ? " · " + s.code : "");
  $("#routes").innerHTML = s.routes.map((r) => `<span class="chip" role="button" tabindex="0" aria-pressed="false" data-r="${esc(r.id)}">${esc(r.name)}</span>`).join("")
    + '<small class="muted" style="width:100%">None selected = every route at this stop.</small>';
  $("#add").hidden = false;
  map.setView([s.lat, s.lon], 16);
}
$("#routes").addEventListener("click", toggleChip);
$("#routes").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleChip(e); } });
function toggleChip(e) {
  const c = e.target.closest(".chip");
  if (!c || !picked) return;
  const on = c.getAttribute("aria-pressed") !== "true";
  c.setAttribute("aria-pressed", on);
  on ? picked.routes.add(c.dataset.r) : picked.routes.delete(c.dataset.r);
}
$("#add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const thresholds = $("#thresholds").value.split(/[ ,;]+/).map(Number).filter((n) => n > 0 && n <= 60);
  try {
    await api("/api/watches", { method: "POST", body: JSON.stringify({
      stop_id: picked.stop.id, route_ids: [...picked.routes], thresholds: thresholds.length ? thresholds : [10, 5, 2, 1],
      ntfy_topic: $("#ntfy").value.trim(),
    }) });
    store("busalert.ntfy", $("#ntfy").value.trim());
    $("#add").hidden = true; $("#q").value = ""; picked = null; followFit = true;
    setupNotifyButton();
    await loadWatches();
  } catch (err) { alert(err.message); }
});
$("#watches").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) { await api("/api/watches/" + del.dataset.del, { method: "DELETE" }); await loadWatches(); if (tick) { tick.watches = tick.watches.filter((x) => x.watch_id !== del.dataset.del); applyTick(); } return; }
  const f = e.target.closest("[data-focus]");
  const v = e.target.closest("[data-v]");
  if (v && markers.has(v.dataset.v)) { map.setView(markers.get(v.dataset.v).marker.getLatLng(), Math.max(map.getZoom(), 15)); markers.get(v.dataset.v).marker.openTooltip(); }
  else if (f) { const w = watches.find((x) => x.id === f.dataset.focus); if (w) map.setView([w.stop.lat, w.stop.lon], 16); }
});

// ---- boot ---------------------------------------------------------------------------------
$("#ntfy").value = store("busalert.ntfy") || "";
setupNotifyButton();
(async () => {
  await pollHealth();
  setInterval(pollHealth, 5000);
  try { await loadWatches(); } catch { /* network still loading: the retry below handles it */ }
  loadLog();
  connect();
  const retry = setInterval(async () => {
    if (health && health.network_loaded) { clearInterval(retry); loadWatches(); }
  }, 2000);
})();
