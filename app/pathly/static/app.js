const TOKEN_KEY = "pathly.token";
const EMAIL_KEY = "pathly.email";
const TRACE_LIMIT = 30;

const state = {
  token: readStorage(TOKEN_KEY),
  email: readStorage(EMAIL_KEY),
  window: "1h",
  hash: "",
  links: [],
  totals: {},
  chart: null,
  streamController: null,
  timers: [],
};

const $ = (id) => document.getElementById(id);
const numberFormat = new Intl.NumberFormat("pl-PL");
const compactFormat = new Intl.NumberFormat("pl-PL", { notation: "compact", maximumFractionDigits: 1 });

function readStorage(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function writeStorage(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {}
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function recordTrace(method, path, response) {
  const route = response.headers.get("x-db-route") || "—";
  const pools = response.headers.get("x-db-pools") || "—";
  const item = el("li", {}, [
    el("span", { className: "req" }, [
      el("span", { className: "method", text: method }),
      ` ${path} → ${response.status}`,
    ]),
    el("span", { className: "route", text: `${route}` }),
    el("span", { className: "route", text: `pools: ${pools}` }),
  ]);
  const list = $("trace");
  list.prepend(item);
  while (list.children.length > TRACE_LIMIT) list.lastElementChild.remove();
}

async function api(method, path, body, { trace = true } = {}) {
  const headers = { Accept: "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (trace) recordTrace(method, path, response);
  if (response.status === 401 && state.token) {
    logout();
    throw new Error("Sesja wygasła");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function showAuth() {
  $("auth-view").hidden = false;
  $("app-view").hidden = true;
  $("session").hidden = true;
}

function showApp() {
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  $("session").hidden = false;
  $("session-email").textContent = state.email || "";
}

function logout() {
  state.token = null;
  state.email = null;
  writeStorage(TOKEN_KEY, null);
  writeStorage(EMAIL_KEY, null);
  stopLive();
  showAuth();
}

async function handleAuth(event) {
  event.preventDefault();
  const mode = event.submitter?.dataset.mode || "login";
  const form = new FormData(event.target);
  $("auth-error").textContent = "";
  try {
    const result = await api("POST", `/api/auth/${mode}`, {
      email: form.get("email"),
      password: form.get("password"),
    });
    state.token = result.access_token;
    state.email = result.email;
    writeStorage(TOKEN_KEY, state.token);
    writeStorage(EMAIL_KEY, state.email);
    await startApp();
  } catch (error) {
    $("auth-error").textContent = error.message;
  }
}

async function handleCreate(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const body = { target_url: form.get("target_url") };
  if (form.get("alias")) body.alias = form.get("alias");
  const result = $("create-result");
  result.textContent = "";
  try {
    const link = await api("POST", "/api/links", body);
    result.append("Gotowe: ", el("a", { href: link.short_url, target: "_blank", rel: "noopener", text: link.short_url }));
    event.target.reset();
    await loadLinks();
  } catch (error) {
    result.textContent = `Błąd: ${error.message}`;
  }
}

async function loadLinks() {
  const [links, totals] = await Promise.all([
    api("GET", "/api/links"),
    api("GET", "/api/analytics/totals"),
  ]);
  state.links = links;
  state.totals = totals;
  renderLinks();
  renderLinkFilter();
}

async function refreshTotals() {
  try {
    state.totals = await api("GET", "/api/analytics/totals", undefined, { trace: false });
    renderLinks();
  } catch {}
}

function renderLinkFilter() {
  const select = $("link-filter");
  const current = state.hash;
  select.replaceChildren(el("option", { value: "", text: "Wszystkie linki" }));
  for (const link of state.links) {
    select.append(el("option", { value: link.hash, text: `/${link.hash}` }));
  }
  select.value = state.links.some((link) => link.hash === current) ? current : "";
}

function renderLinks() {
  const body = $("links-body");
  body.replaceChildren();
  $("links-count").textContent = `${state.links.length} ${state.links.length === 1 ? "link" : "linków"}`;
  for (const link of state.links) {
    const status = link.is_active ? ["good", "aktywny"] : ["idle", "wyłączony"];
    const toggle = el("button", {
      className: "ghost",
      text: link.is_active ? "Wyłącz" : "Włącz",
      onclick: () => updateLink(link.hash, { is_active: !link.is_active }),
    });
    const remove = el("button", {
      className: "ghost",
      text: "Usuń",
      onclick: () => deleteLink(link.hash),
    });
    const stats = el("button", {
      className: "ghost",
      text: "Statystyki",
      onclick: () => selectLink(link.hash),
    });
    body.append(
      el("tr", {}, [
        el("td", {}, [el("a", { href: link.short_url, target: "_blank", rel: "noopener", text: `/${link.hash}` })]),
        el("td", { className: "target", title: link.target_url, text: link.target_url }),
        el("td", { className: "num", text: numberFormat.format(state.totals[link.hash] || 0) }),
        el("td", {}, [el("span", { className: `status ${status[0]}`, text: status[1] })]),
        el("td", {}, [el("div", { className: "actions" }, [stats, toggle, remove])]),
      ]),
    );
  }
  if (!state.links.length) {
    body.append(el("tr", {}, [el("td", { colspan: "5", className: "muted", text: "Brak linków — skróć pierwszy powyżej albo uruchom symulator." })]));
  }
}

async function updateLink(hash, changes) {
  try {
    await api("PATCH", `/api/links/${encodeURIComponent(hash)}`, changes);
    await loadLinks();
  } catch (error) {
    alert(error.message);
  }
}

async function deleteLink(hash) {
  if (!confirm(`Usunąć /${hash}? Historia kliknięć zostaje w bazie analitycznej.`)) return;
  try {
    await api("DELETE", `/api/links/${encodeURIComponent(hash)}`);
    if (state.hash === hash) state.hash = "";
    await loadLinks();
    restartStream();
  } catch (error) {
    alert(error.message);
  }
}

function selectLink(hash) {
  state.hash = hash;
  $("link-filter").value = hash;
  restartStream();
  $("series-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function formatBucket(iso, bucketSeconds) {
  const date = new Date(iso);
  if (bucketSeconds >= 86400) return date.toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit" });
  if (bucketSeconds >= 3600 * 6) {
    return date.toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  const options = { hour: "2-digit", minute: "2-digit" };
  if (bucketSeconds < 60) options.second = "2-digit";
  return date.toLocaleTimeString("pl-PL", options);
}

function ensureChart() {
  if (state.chart || !window.Chart) return state.chart;
  const ctx = $("series-chart");
  state.chart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [
      { label: "Kliknięcia", data: [], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, fill: true, tension: 0.25 },
      { label: "Unikalni", data: [], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, fill: false, tension: 0.25 },
    ] },
    options: {
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { displayColors: true, boxWidth: 12, boxHeight: 2, usePointStyle: false },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 8, maxRotation: 0 } },
        y: { beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 5 } },
      },
    },
  });
  applyChartTheme();
  return state.chart;
}

function applyChartTheme() {
  const chart = state.chart;
  if (!chart) return;
  const [clicks, visitors] = chart.data.datasets;
  clicks.borderColor = cssVar("--series-1");
  clicks.backgroundColor = cssVar("--series-1-wash");
  clicks.pointBackgroundColor = cssVar("--series-1");
  visitors.borderColor = cssVar("--series-2");
  visitors.backgroundColor = cssVar("--series-2");
  visitors.pointBackgroundColor = cssVar("--series-2");
  const grid = cssVar("--grid");
  const muted = cssVar("--text-muted");
  chart.options.scales.x.ticks.color = muted;
  chart.options.scales.y.ticks.color = muted;
  chart.options.scales.y.grid.color = grid;
  chart.options.scales.y.border = { display: false };
  chart.options.scales.x.border = { color: cssVar("--baseline") };
  chart.options.plugins.tooltip.backgroundColor = cssVar("--surface-raised");
  chart.options.plugins.tooltip.titleColor = cssVar("--text-secondary");
  chart.options.plugins.tooltip.bodyColor = cssVar("--text-primary");
  chart.options.plugins.tooltip.borderColor = cssVar("--baseline");
  chart.options.plugins.tooltip.borderWidth = 1;
  chart.update("none");
}

function renderSeries(stats) {
  const labels = stats.series.map((point) => formatBucket(point.t, stats.bucket_seconds));
  const chart = ensureChart();
  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = stats.series.map((point) => point.clicks);
    chart.data.datasets[1].data = stats.series.map((point) => point.visitors);
    chart.update("none");
  }
  const rows = stats.series.slice().reverse().map((point, index, list) =>
    el("tr", {}, [
      el("td", { text: labels[list.length - 1 - index] }),
      el("td", { className: "num", text: numberFormat.format(point.clicks) }),
      el("td", { className: "num", text: numberFormat.format(point.visitors) }),
    ]),
  );
  $("series-table").replaceChildren(...rows);
}

function renderBars(id, items, total) {
  const list = $(id);
  if (!items.length) {
    list.replaceChildren(el("li", {}, [el("span", { className: "empty", text: "Brak danych w tym oknie" })]));
    return;
  }
  const max = Math.max(...items.map((item) => item.clicks));
  list.replaceChildren(
    ...items.map((item) => {
      const share = total ? Math.round((item.clicks / total) * 100) : 0;
      const bar = el("span", { className: "bar" });
      bar.style.width = `${Math.max(1, (item.clicks / max) * 100)}%`;
      const row = el("li", { tabindex: "0" }, [
        el("span", { className: "label", title: item.key, text: item.key }),
        el("span", { className: "track" }, [bar]),
        el("span", { className: "val" }, [numberFormat.format(item.clicks), el("small", { text: `${share}%` })]),
      ]);
      const show = (event) => showTooltip(event, numberFormat.format(item.clicks), `${item.key} · ${share}% kliknięć`);
      row.addEventListener("pointermove", show);
      row.addEventListener("focus", show);
      row.addEventListener("pointerleave", hideTooltip);
      row.addEventListener("blur", hideTooltip);
      return row;
    }),
  );
}

function showTooltip(event, value, label) {
  const tip = $("tooltip");
  tip.replaceChildren(el("strong", { text: value }), el("span", { text: label }));
  tip.hidden = false;
  const rect = event.currentTarget.getBoundingClientRect();
  const x = event.clientX ?? rect.left + rect.width / 2;
  const y = event.clientY ?? rect.top;
  tip.style.left = `${Math.min(x + 12, window.innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${Math.max(8, y - tip.offsetHeight - 10)}px`;
}

function hideTooltip() {
  $("tooltip").hidden = true;
}

function renderTiles(stats) {
  const { clicks, visitors, bot_clicks, previous_clicks, last_minute } = stats.totals;
  $("tile-clicks").textContent = clicks >= 10000 ? compactFormat.format(clicks) : numberFormat.format(clicks);
  $("tile-visitors").textContent = visitors >= 10000 ? compactFormat.format(visitors) : numberFormat.format(visitors);
  $("tile-minute").textContent = numberFormat.format(last_minute);
  $("tile-bots").textContent = numberFormat.format(bot_clicks);
  const delta = $("tile-delta");
  if (previous_clicks > 0) {
    const change = ((clicks - previous_clicks) / previous_clicks) * 100;
    delta.textContent = `${change >= 0 ? "▲ +" : "▼ "}${change.toFixed(1)}% vs poprzednie okno`;
    delta.className = change >= 0 ? "delta up" : "delta";
  } else {
    delta.textContent = "brak danych z poprzedniego okna";
    delta.className = "delta";
  }
}

function renderStats(stats) {
  const total = stats.totals.clicks;
  renderTiles(stats);
  renderSeries(stats);
  renderBars("bars-referrers", stats.referrers, total);
  renderBars("bars-devices", stats.devices, total);
  renderBars("bars-browsers", stats.browsers, total);
  renderBars("bars-countries", stats.countries, total);
  document.querySelectorAll(".loading").forEach((node) => node.classList.remove("loading"));
}

function setLive(on, label) {
  $("live-indicator").classList.toggle("on", on);
  $("live-label").textContent = label;
}

function streamUrl() {
  const params = new URLSearchParams({ window: state.window });
  if (state.hash) params.set("hash", state.hash);
  return `/api/analytics/stream?${params}`;
}

async function openStream() {
  const controller = new AbortController();
  state.streamController = controller;
  document.querySelectorAll("#series-card, .breakdowns .card, .tiles").forEach((node) => node.classList.add("loading"));
  try {
    const response = await fetch(streamUrl(), {
      headers: { Authorization: `Bearer ${state.token}`, Accept: "text/event-stream" },
      signal: controller.signal,
    });
    recordTrace("GET", streamUrl(), response);
    if (response.status === 401) return logout();
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
    setLive(true, "na żywo · SSE");
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = block.split("\n").filter((line) => line.startsWith("data: ")).map((line) => line.slice(6)).join("\n");
        if (data) renderStats(JSON.parse(data));
      }
    }
  } catch (error) {
    if (controller.signal.aborted) return;
    console.warn("stream error", error);
  }
  if (state.streamController === controller && state.token) {
    setLive(false, "ponowne łączenie…");
    setTimeout(() => state.streamController === controller && openStream(), 2000);
  }
}

function restartStream() {
  state.streamController?.abort();
  openStream();
}

function stopLive() {
  state.streamController?.abort();
  state.streamController = null;
  state.timers.forEach(clearInterval);
  state.timers = [];
  setLive(false, "rozłączono");
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function poolCard(title, healthy, rows, error) {
  const statusClass = healthy === null ? "idle" : healthy ? "good" : "bad";
  const statusText = healthy === null ? "brak danych" : healthy ? "zdrowa" : "niedostępna";
  const list = el("dl");
  for (const [term, value] of rows) list.append(el("dt", { text: term }), el("dd", { text: String(value) }));
  const card = el("div", { className: "pool" }, [
    el("h3", { text: title }),
    el("span", { className: `status ${statusClass}`, text: statusText }),
    list,
  ]);
  if (error) card.append(el("span", { className: "err", text: error }));
  return card;
}

async function refreshSystem() {
  let system;
  try {
    system = await api("GET", "/api/system", undefined, { trace: false });
  } catch {
    return;
  }
  const cards = system.pools.map((pool) => {
    if (pool.role === "olap") {
      return poolCard(pool.name, true, [
        ["silnik", pool.engine],
        ["wierszy", numberFormat.format(pool.rows)],
        ["zapytań", numberFormat.format(pool.served)],
      ]);
    }
    const rows = [
      ["rola", pool.role],
      ["obsłużono", numberFormat.format(pool.served)],
      ["połączenia", `${pool.checked_out ?? "—"} / ${pool.size ?? "—"}`],
    ];
    if (pool.role === "replica") rows.push(["lag", formatBytes(pool.lag_bytes)]);
    return poolCard(pool.name, pool.healthy, rows, pool.last_error);
  });
  const cache = system.cache;
  cards.push(poolCard("redis_cache", cache.errors === 0 ? true : null, [
    ["trafienia", numberFormat.format(cache.hits + cache.negative_hits)],
    ["chybienia", numberFormat.format(cache.misses)],
    ["hit ratio", cache.hit_ratio === null ? "—" : `${(cache.hit_ratio * 100).toFixed(1)}%`],
  ]));
  const ingest = system.ingestor;
  const fresh = ingest.last_flush_at ? Date.now() / 1000 - ingest.last_flush_at : null;
  cards.push(poolCard("redis_stream → duckdb", ingest.errors === 0 ? true : null, [
    ["w strumieniu", ingest.length === null ? "—" : numberFormat.format(ingest.length)],
    ["oczekujące", ingest.pending ?? "—"],
    ["lag grupy", ingest.lag ?? "—"],
    ["zapisane", numberFormat.format(ingest.ingested_total)],
    ["ost. batch", `${ingest.last_batch_size} w ${ingest.last_batch_ms} ms`],
    ["ost. flush", fresh === null ? "—" : `${fresh.toFixed(1)} s temu`],
  ]));
  $("pools").replaceChildren(...cards);
}

function bindWindowFilter() {
  for (const button of document.querySelectorAll("#window-filter button")) {
    button.addEventListener("click", () => {
      document.querySelectorAll("#window-filter button").forEach((other) => other.setAttribute("aria-checked", String(other === button)));
      state.window = button.dataset.window;
      restartStream();
    });
  }
  $("link-filter").addEventListener("change", (event) => {
    state.hash = event.target.value;
    restartStream();
  });
}

async function startApp() {
  showApp();
  stopLive();
  try {
    await loadLinks();
  } catch (error) {
    if (!state.token) return;
    console.warn(error);
  }
  openStream();
  refreshSystem();
  state.timers.push(setInterval(refreshSystem, 2000));
  state.timers.push(setInterval(refreshTotals, 5000));
}

function init() {
  $("auth-form").addEventListener("submit", handleAuth);
  $("create-form").addEventListener("submit", handleCreate);
  $("logout").addEventListener("click", logout);
  bindWindowFilter();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyChartTheme);
  if (state.token) startApp();
  else showAuth();
}

init();
