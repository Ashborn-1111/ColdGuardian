"use strict";

// The default origin is intentionally localhost:8000 to match the backend's
// development CORS defaults when this page is served from localhost:5173.
const DEFAULT_API_PROTOCOL = window.location.protocol === "https:" ? "https:" : "http:";
const DEFAULT_API_HOST = window.location.hostname || "localhost";
const DEFAULT_API_BASE_URL = window.location.port === "5173"
  ? `${DEFAULT_API_PROTOCOL}//${DEFAULT_API_HOST}:8000`
  : window.location.origin;
const API_BASE_URL = (window.COLDGUARDIAN_API_BASE_URL || DEFAULT_API_BASE_URL).replace(/\/$/, "");
const REFRESH_INTERVAL_MS = 5000;
const TELEMETRY_LIMIT = 40;
const ALERTS_LIMIT = 50;

const $ = (id) => document.getElementById(id);
const els = {
  loginScreen: $("loginScreen"), loginForm: $("loginForm"), loginError: $("loginError"),
  email: $("emailInput"), password: $("passwordInput"), app: $("app"), logout: $("logoutButton"),
  refresh: $("refreshButton"), trackerSelect: $("trackerSelect"), userEmail: $("userEmail"), userRole: $("userRole"),
  userInitial: $("userInitial"), apiDot: $("apiDot"), apiStatusText: $("apiStatusText"), apiStatusDetail: $("apiStatusDetail"),
  globalError: $("globalError"), globalNotice: $("globalNotice"), toastRegion: $("toastRegion"),
  packagesTotal: $("packagesTotal"), packagesTransit: $("packagesTransit"), devicesOnline: $("devicesOnline"),
  devicesOffline: $("devicesOffline"), alertsOpen: $("alertsOpen"), alertSummaryBadge: $("alertSummaryBadge"),
  navAlertCount: $("navAlertCount"), selectedDeviceTitle: $("selectedDeviceTitle"), deviceStateDot: $("deviceStateDot"),
  deviceStateText: $("deviceStateText"), lastUpdate: $("lastUpdate"), temperatureValue: $("temperatureValue"),
  temperatureStatus: $("temperatureStatus"), temperatureLimits: $("temperatureLimits"), temperatureRangeMarker: $("temperatureRangeMarker"),
  temperatureHint: $("temperatureHint"), shockValue: $("shockValue"), shockStatus: $("shockStatus"), shockLimit: $("shockLimit"),
  shockBarFill: $("shockBarFill"), shockHint: $("shockHint"), gpsStatus: $("gpsStatus"), latitude: $("latitude"),
  longitude: $("longitude"), mapLink: $("mapLink"), gpsMeta: $("gpsMeta"), gpsSatellites: $("gpsSatellites"),
  packageStatus: $("packageStatus"), packageId: $("packageId"), packageProduct: $("packageProduct"), productSku: $("productSku"),
  shipmentId: $("shipmentId"), configVersion: $("configVersion"), reportInterval: $("reportInterval"), configSync: $("configSync"),
  alertsList: $("alertsList"), alertsListCount: $("alertsListCount"), loadMoreAlerts: $("loadMoreAlerts"),
  telemetryRows: $("telemetryRows"), footerApi: $("footerApi"), currentDate: $("currentDate"),
  tempChartFallback: $("temperatureChartFallback"),
};

let csrfToken = null; // Never persist CSRF tokens in localStorage.
let currentUser = null;
let selectedDeviceId = "";
let devicesCache = [];
let alertsCache = [];
let alertLimit = 10;
let refreshTimer = null;
let refreshInFlight = false;
let tempChart = null;
let shockChart = null;
let lastTelemetryFingerprint = "";
let chartJsWarned = false;

function setText(element, value) { if (element) element.textContent = value == null ? "—" : String(value); }
function fmtNumber(value, digits = 1) { return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—"; }
function parseDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function fmtTime(value, includeDate = false) {
  const date = parseDate(value);
  if (!date) return "Unknown time";
  return date.toLocaleString([], includeDate
    ? { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (m) => m.toUpperCase());
}
function setBadge(element, message, type = "neutral") {
  if (!element) return;
  element.textContent = message;
  element.className = `status-pill ${type}`;
}
function showError(message) {
  els.globalError.hidden = !message;
  setText(els.globalError, message || "");
}
function showNotice(message) {
  els.globalNotice.hidden = !message;
  setText(els.globalNotice, message || "");
}
function toast(message, kind = "") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`.trim();
  item.textContent = message;
  els.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 4500);
}
function readErrorBody(data, status) {
  if (typeof data?.detail === "string") return data.detail;
  if (data?.detail?.message) return data.detail.message;
  if (Array.isArray(data?.detail)) return data.detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("; ");
  return `Request failed (HTTP ${status}).`;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const request = { method, headers, credentials: "include", cache: "no-store" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }
  if (["POST", "PATCH", "DELETE"].includes(method) && options.csrf !== false) {
    if (!csrfToken) throw new Error("Your session has no CSRF token. Sign in again.");
    headers["X-CSRF-Token"] = csrfToken;
  }
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, request);
  } catch (error) {
    throw new Error(`Cannot reach ${API_BASE_URL}. Check that main.py is running and this frontend origin is allowed by CORS.`);
  }
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json().catch(() => null) : null;
  if (response.status === 401 && path !== "/api/v1/auth/login") {
    handleSessionExpired();
    throw new Error("Session expired. Please sign in again.");
  }
  if (!response.ok) throw new Error(readErrorBody(data, response.status));
  return data;
}

function showLogin(message = "") {
  els.app.hidden = true;
  els.loginScreen.hidden = false;
  els.loginError.textContent = message;
  if (message) els.password.value = "";
  window.clearInterval(refreshTimer);
  refreshTimer = null;
}
function showApp() {
  els.loginScreen.hidden = true;
  els.app.hidden = false;
  const email = currentUser?.email || "Signed in";
  setText(els.userEmail, email);
  setText(els.userRole, currentUser?.role || "user");
  setText(els.userInitial, email.charAt(0).toUpperCase() || "U");
  if (typeof adminUpdateRoleVisibility === "function") adminUpdateRoleVisibility();
}
function handleSessionExpired() {
  csrfToken = null;
  currentUser = null;
  showLogin("Your session expired. Sign in again.");
}

async function checkSession() {
  try {
    const response = await api("/api/v1/auth/me", { csrf: false });
    currentUser = response.user;
    csrfToken = response.csrf_token;
    showApp();
    await refreshAll(true);
    startPolling();
  } catch (error) {
    if (!String(error.message).includes("Sign in again")) showLogin();
    else showLogin(error.message);
  }
}

els.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.loginError.textContent = "Signing in…";
  const submitButton = els.loginForm.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  try {
    const response = await api("/api/v1/auth/login", {
      method: "POST", csrf: false,
      body: { email: els.email.value.trim(), password: els.password.value },
    });
    currentUser = response.user;
    csrfToken = response.csrf_token;
    els.password.value = "";
    els.loginError.textContent = "";
    showApp();
    await refreshAll(true);
    startPolling();
  } catch (error) {
    els.loginError.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
});

els.logout.addEventListener("click", async () => {
  try {
    await api("/api/v1/auth/logout", { method: "POST" });
  } catch (error) {
    console.warn("Logout request failed:", error.message);
  } finally {
    csrfToken = null;
    currentUser = null;
    showLogin();
  }
});

els.refresh.addEventListener("click", async () => {
  els.refresh.disabled = true;
  els.refresh.querySelector(".refresh-symbol").textContent = "…";
  try { await refreshAll(true); toast("Dashboard refreshed."); }
  catch (error) { toast(error.message, "error"); }
  finally {
    els.refresh.disabled = false;
    els.refresh.querySelector(".refresh-symbol").textContent = "↻";
  }
});

els.trackerSelect.addEventListener("change", async () => {
  selectedDeviceId = els.trackerSelect.value;
  lastTelemetryFingerprint = "";
  resetSelectedTrackerView();
  await refreshSelectedTracker();
});
els.loadMoreAlerts.addEventListener("click", () => {
  alertLimit = alertLimit >= 50 ? 10 : Math.min(50, alertLimit + 10);
  renderAlerts(alertsCache);
});

function resetSelectedTrackerView() {
  setText(els.selectedDeviceTitle, selectedDeviceId || "Choose a tracker");
  setText(els.temperatureValue, "—");
  setText(els.shockValue, "—");
  setText(els.latitude, "—");
  setText(els.longitude, "—");
  setText(els.lastUpdate, "Loading telemetry…");
  setBadge(els.temperatureStatus, "NO DATA", "neutral");
  setBadge(els.shockStatus, "NO DATA", "neutral");
  setBadge(els.gpsStatus, "NO FIX", "neutral");
  els.mapLink.hidden = true;
  els.temperatureRangeMarker.hidden = true;
  if (tempChart) { tempChart.data.labels = []; tempChart.data.datasets[0].data = []; tempChart.update("none"); }
  if (shockChart) { shockChart.data.labels = []; shockChart.data.datasets[0].data = []; shockChart.update("none"); }
  els.telemetryRows.replaceChildren(makeCellRow("Select a tracker to see telemetry.", 6));
}

async function refreshReadiness() {
  try {
    const response = await fetch(`${API_BASE_URL}/readyz`, { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.status !== "ok") {
      els.apiDot.className = "dot bad";
      setText(els.apiStatusText, "API not ready");
      setText(els.apiStatusDetail, `HTTP ${response.status} · check database/MQTT`);
      return false;
    }
    const ready = data.postgresql === "ready" && data.schema === "ready" && data.mqtt_consumer === "ready";
    els.apiDot.className = `dot ${ready ? "good" : "warn"}`;
    setText(els.apiStatusText, ready ? "System operational" : "Partially ready");
    setText(els.apiStatusDetail, `DB ${data.postgresql || "?"} · MQTT ${data.mqtt_consumer || "?"}`);
    return ready;
  } catch {
    els.apiDot.className = "dot bad";
    setText(els.apiStatusText, "API unreachable");
    setText(els.apiStatusDetail, API_BASE_URL);
    return false;
  }
}

async function refreshAll(showErrors = false) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const ready = await refreshReadiness();
    const [summary, devicesResult] = await Promise.all([
      api("/api/v1/inventory/summary"),
      api("/api/v1/devices?limit=100&offset=0"),
    ]);
    renderSummary(summary);
    devicesCache = Array.isArray(devicesResult.items) ? devicesResult.items : [];
    renderDeviceSelect(devicesCache);
    if (!selectedDeviceId && devicesCache.length) {
      selectedDeviceId = devicesCache[0].device_id;
      els.trackerSelect.value = selectedDeviceId;
      resetSelectedTrackerView();
    } else if (selectedDeviceId && !devicesCache.some((d) => d.device_id === selectedDeviceId) && devicesCache.length) {
      selectedDeviceId = devicesCache[0].device_id;
      els.trackerSelect.value = selectedDeviceId;
      resetSelectedTrackerView();
    }
    await refreshSelectedTracker();
    showError("");
    if (!devicesCache.length) showNotice("No device has registered yet. Power on the ESP32 and check that it connects to MQTT.");
    else if (!ready) showNotice("The API is reachable, but one or more dependencies are not ready. Live data may be delayed.");
    else showNotice("");
    setText(els.footerApi, `API: ${new URL(API_BASE_URL).host}`);
  } catch (error) {
    if (showErrors) showError(error.message);
    console.error("Dashboard refresh failed:", error);
  } finally {
    refreshInFlight = false;
  }
}

function renderSummary(summary) {
  setText(els.packagesTotal, summary.packages_total ?? "—");
  setText(els.packagesTransit, `${summary.packages_in_transit ?? 0} in transit · ${summary.packages_packed ?? 0} packed`);
  setText(els.devicesOnline, summary.devices_online ?? "—");
  setText(els.devicesOffline, `${summary.devices_offline ?? 0} offline`);
  setText(els.alertsOpen, summary.packages_with_open_alerts ?? "—");
  const count = Number(summary.packages_with_open_alerts || 0);
  setBadge(els.alertSummaryBadge, count ? "NEEDS ATTENTION" : "ALL CLEAR", count ? "warn" : "good");
}

function renderDeviceSelect(devices) {
  const current = selectedDeviceId || els.trackerSelect.value;
  els.trackerSelect.replaceChildren();
  if (!devices.length) {
    const option = document.createElement("option");
    option.value = ""; option.textContent = "No registered trackers";
    els.trackerSelect.append(option);
    selectedDeviceId = "";
    setText(els.selectedDeviceTitle, "No registered trackers");
    showNotice("No device has registered yet. Power on the ESP32 and check that it connects to MQTT.");
    return;
  }
  for (const device of devices) {
    const option = document.createElement("option");
    option.value = device.device_id;
    option.textContent = `${device.device_id} · ${device.status || "unknown"}`;
    els.trackerSelect.append(option);
  }
  if (devices.some((d) => d.device_id === current)) selectedDeviceId = current;
  else selectedDeviceId = devices[0].device_id;
  els.trackerSelect.value = selectedDeviceId;
}

async function refreshSelectedTracker() {
  if (!selectedDeviceId) return;
  try {
    const id = encodeURIComponent(selectedDeviceId);
    const [device, settings, telemetryResult, alertResult] = await Promise.all([
      api(`/api/v1/devices/${id}`),
      api(`/api/v1/devices/${id}/settings`),
      api(`/api/v1/devices/${id}/telemetry?limit=${TELEMETRY_LIMIT}&offset=0`),
      api(`/api/v1/alerts?device_id=${id}&limit=${ALERTS_LIMIT}&offset=0`),
    ]);
    const telemetry = Array.isArray(telemetryResult.items) ? telemetryResult.items : [];
    const alerts = Array.isArray(alertResult.items) ? alertResult.items : [];
    renderDevice(device, settings);
    renderTelemetry(telemetry, settings);
    alertsCache = alerts;
    renderAlerts(alertsCache);
  } catch (error) {
    if (!String(error.message).includes("Session expired")) showError(error.message);
    console.error("Selected tracker refresh failed:", error);
  }
}

function renderDevice(device, settings) {
  setText(els.selectedDeviceTitle, device.device_id);
  const online = device.status === "online";
  els.deviceStateDot.className = `dot ${online ? "good" : "bad"}`;
  setText(els.deviceStateText, online ? "Online" : "Offline");
  setText(els.lastUpdate, device.last_seen_at ? `Last seen ${fmtTime(device.last_seen_at, true)}` : "Never seen");
  setText(els.packageId, settings.package_id || "No package assigned");
  setText(els.shipmentId, settings.shipment_id || "None");
  setText(els.configVersion, `v${settings.config_version ?? device.config_version ?? 0} · applied v${device.config_applied_version ?? 0}`);
  setText(els.reportInterval, settings.reporting_interval_seconds ? `${settings.reporting_interval_seconds} seconds` : "—");
  if (settings.assigned) {
    const tempMinimum = settings.min_temperature_c;
    const tempMaximum = settings.max_temperature_c;
    setText(els.temperatureLimits, `${fmtNumber(tempMinimum, 1)}–${fmtNumber(tempMaximum, 1)} °C`);
    setText(els.shockLimit, `${fmtNumber(settings.shock_threshold_g, 1)} g`);
    setBadge(els.packageStatus, "ASSIGNED", "good");
    const applied = Number(device.config_applied_version ?? 0) >= Number(settings.config_version ?? 0);
    els.configSync.className = `sync-box ${applied ? "good" : "warn"}`;
    els.configSync.querySelector("span:last-child").textContent = applied
      ? `Configuration v${settings.config_version} is applied by the tracker.`
      : `Waiting for tracker to apply configuration v${settings.config_version}.`;
  } else {
    setText(els.temperatureLimits, "No package limits");
    setText(els.shockLimit, "No threshold");
    setBadge(els.packageStatus, "UNASSIGNED", "neutral");
    els.configSync.className = "sync-box warn";
    els.configSync.querySelector("span:last-child").textContent = "No package assigned. Monitoring thresholds are unavailable.";
  }
  // Fetch authoritative package metadata without duplicating threshold state on the device.
  if (settings.package_id) loadPackageSummary(settings.package_id);
  else {
    lastPackageLoaded = "";
    setText(els.packageProduct, "This tracker is not currently linked to a package.");
    setText(els.productSku, "—");
  }
}

let lastPackageLoaded = "";
async function loadPackageSummary(packageId) {
  if (lastPackageLoaded === packageId) return;
  lastPackageLoaded = packageId;
  try {
    const packageInfo = await api(`/api/v1/packages/${encodeURIComponent(packageId)}`);
    setText(els.packageProduct, `${packageInfo.product_name || "Unknown product"} · qty ${packageInfo.quantity ?? "—"} ${packageInfo.unit || ""}`.trim());
    setText(els.productSku, packageInfo.product_sku || "—");
    if (packageInfo.status) setBadge(els.packageStatus, titleCase(packageInfo.status), packageInfo.status === "delivered" ? "neutral" : "good");
  } catch (error) {
    lastPackageLoaded = "";
    console.warn("Could not load package metadata:", error.message);
  }
}

function renderTelemetry(items, settings) {
  const newest = items[0];
  if (!newest) {
    setText(els.temperatureValue, "—");
    setText(els.shockValue, "—");
    setBadge(els.temperatureStatus, "NO READINGS", "neutral");
    setBadge(els.shockStatus, "NO READINGS", "neutral");
    setText(els.lastUpdate, "No telemetry received yet");
    setText(els.temperatureHint, "The backend has no telemetry rows for this tracker yet.");
    setText(els.shockHint, "The backend has no accelerometer readings for this tracker yet.");
    setText(els.latitude, "No GPS fix");
    setText(els.longitude, "No GPS fix");
    setBadge(els.gpsStatus, "NO FIX", "neutral");
    els.mapLink.hidden = true;
    updateCharts([]);
    els.telemetryRows.replaceChildren(makeCellRow("No telemetry rows for this tracker.", 6));
    return;
  }
  const tempValid = newest.temperature_valid === true && Number.isFinite(newest.temperature_c);
  const shockValid = newest.motion_valid === true && Number.isFinite(newest.peak_acceleration_magnitude_g);
  const gpsValid = newest.location_fix_valid === true && Number.isFinite(newest.latitude) && Number.isFinite(newest.longitude);
  const min = settings.min_temperature_c;
  const max = settings.max_temperature_c;
  setText(els.temperatureValue, tempValid ? fmtNumber(newest.temperature_c, 2) : "—");
  if (!tempValid) {
    setBadge(els.temperatureStatus, "SENSOR ERROR", "bad");
    setText(els.temperatureHint, "The latest temperature value is invalid. Temperature excursions cannot be assessed from this reading.");
  } else if (settings.assigned && typeof min === "number" && typeof max === "number" && newest.temperature_c > max) {
    setBadge(els.temperatureStatus, "ABOVE LIMIT", "bad");
    setText(els.temperatureHint, `Reading is ${fmtNumber(newest.temperature_c - max, 2)} °C above the package maximum.`);
  } else if (settings.assigned && typeof min === "number" && typeof max === "number" && newest.temperature_c < min) {
    setBadge(els.temperatureStatus, "BELOW LIMIT", "bad");
    setText(els.temperatureHint, `Reading is ${fmtNumber(min - newest.temperature_c, 2)} °C below the package minimum.`);
  } else {
    setBadge(els.temperatureStatus, tempValid ? "IN RANGE" : "NO DATA", tempValid ? "good" : "neutral");
    setText(els.temperatureHint, tempValid ? "Latest valid sensor reading." : "Waiting for a valid temperature sample.");
  }
  if (tempValid && settings.assigned && typeof min === "number" && typeof max === "number") {
    const span = Math.max(1, max - min);
    const relative = Math.max(0, Math.min(1, (newest.temperature_c - min) / span));
    els.temperatureRangeMarker.hidden = false;
    els.temperatureRangeMarker.style.left = `${relative * 100}%`;
  } else els.temperatureRangeMarker.hidden = true;

  setText(els.shockValue, shockValid ? fmtNumber(newest.peak_acceleration_magnitude_g, 3) : "—");
  if (!shockValid) {
    setBadge(els.shockStatus, "SENSOR ERROR", "bad");
    setText(els.shockHint, "The accelerometer reading is invalid or unavailable.");
    els.shockBarFill.style.width = "0%";
  } else {
    const peak = newest.peak_acceleration_magnitude_g;
    const threshold = settings.shock_threshold_g;
    const above = settings.assigned && typeof threshold === "number" && peak >= threshold;
    setBadge(els.shockStatus, above ? "OVER THRESHOLD" : "VALID READING", above ? "bad" : "good");
    setText(els.shockHint, above ? "Peak acceleration meets or exceeds the configured shock threshold." : `Shock count in this report: ${newest.shock_count_since_last_report ?? 0}.`);
    const denominator = typeof threshold === "number" && threshold > 0 ? threshold * 1.5 : Math.max(10, peak);
    els.shockBarFill.style.width = `${Math.max(2, Math.min(100, peak / denominator * 100))}%`;
    els.shockBarFill.style.background = above ? "var(--red)" : "var(--green)";
  }

  if (gpsValid) {
    setBadge(els.gpsStatus, "GPS FIX", "good");
    setText(els.latitude, newest.latitude.toFixed(6));
    setText(els.longitude, newest.longitude.toFixed(6));
    els.mapLink.href = `https://www.openstreetmap.org/?mlat=${encodeURIComponent(newest.latitude)}&mlon=${encodeURIComponent(newest.longitude)}#map=16/${newest.latitude}/${newest.longitude}`;
    els.mapLink.hidden = false;
    setText(els.gpsMeta, Number.isFinite(newest.accuracy_m) ? `Reported accuracy: ${fmtNumber(newest.accuracy_m, 1)} m` : "Valid coordinates received.");
    setText(els.gpsSatellites, Number.isFinite(newest.satellites_used) ? `${newest.satellites_used} sats` : "");
  } else {
    setBadge(els.gpsStatus, "NO FIX", "neutral");
    setText(els.latitude, "No GPS fix");
    setText(els.longitude, "No GPS fix");
    els.mapLink.hidden = true;
    setText(els.gpsMeta, "Coordinates are unavailable until the receiver gets a valid fix.");
    setText(els.gpsSatellites, Number.isFinite(newest.satellites_used) ? `${newest.satellites_used} sats` : "");
  }
  setText(els.lastUpdate, `Observed ${fmtTime(newest.observed_at, true)}`);
  updateCharts(items);
  renderTelemetryTable(items);
}

function updateCharts(items) {
  if (!window.Chart) {
    if (!chartJsWarned) { els.tempChartFallback.hidden = false; chartJsWarned = true; }
    return;
  }
  const chronological = [...items].reverse();
  const tempRows = chronological.filter((item) => item.temperature_valid === true && Number.isFinite(item.temperature_c));
  const shockRows = chronological.filter((item) => item.motion_valid === true && Number.isFinite(item.peak_acceleration_magnitude_g));
  if (!tempChart || !shockChart) createCharts();
  const labelsFor = (rows) => rows.map((item) => {
    const date = parseDate(item.observed_at);
    return date ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "?";
  });
  tempChart.data.labels = labelsFor(tempRows);
  tempChart.data.datasets[0].data = tempRows.map((item) => item.temperature_c);
  tempChart.update("none");
  shockChart.data.labels = labelsFor(shockRows);
  shockChart.data.datasets[0].data = shockRows.map((item) => item.peak_acceleration_magnitude_g);
  shockChart.update("none");
  if (tempRows.length) els.tempChartFallback.hidden = true;
}

function createCharts() {
  const commonOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { intersect: false, mode: "index" },
    plugins: { legend: { display: false }, tooltip: { displayColors: false } },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 6, color: "#858475", font: { size: 10 } }, border: { display: false } },
      y: { grid: { color: "#eeece2" }, ticks: { color: "#858475", font: { size: 10 } }, border: { display: false } },
    },
  };
  tempChart = new Chart($("temperatureChart"), {
    type: "line", data: { labels: [], datasets: [{ data: [], borderColor: "#c98b00", backgroundColor: "rgba(255, 217, 44, .22)", fill: true, tension: .25, pointRadius: 2, pointHoverRadius: 4, borderWidth: 2, spanGaps: false }] }, options: structuredClone(commonOptions),
  });
  shockChart = new Chart($("shockChart"), {
    type: "line", data: { labels: [], datasets: [{ data: [], borderColor: "#c64439", backgroundColor: "rgba(198, 68, 57, .10)", fill: true, tension: .25, pointRadius: 2, pointHoverRadius: 4, borderWidth: 2, spanGaps: false }] }, options: structuredClone(commonOptions),
  });
}

function renderTelemetryTable(items) {
  els.telemetryRows.replaceChildren();
  for (const item of items.slice(0, 8)) {
    const row = document.createElement("tr");
    appendCell(row, fmtTime(item.observed_at, true));
    appendCell(row, item.temperature_valid && Number.isFinite(item.temperature_c) ? `${fmtNumber(item.temperature_c, 2)} °C` : "Invalid");
    appendCell(row, item.motion_valid && Number.isFinite(item.peak_acceleration_magnitude_g) ? `${fmtNumber(item.peak_acceleration_magnitude_g, 3)} g` : "Invalid");
    appendCell(row, item.shock_count_since_last_report ?? "—");
    appendCell(row, item.location_fix_valid === true ? "FIX" : "No fix");
    appendCell(row, item.config_version ?? "—");
    els.telemetryRows.append(row);
  }
  if (!items.length) els.telemetryRows.append(makeCellRow("No telemetry rows.", 6));
}
function appendCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value == null ? "—" : String(value);
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}
function makeCellRow(message, span) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = span; cell.className = "empty-cell"; cell.textContent = message;
  row.append(cell);
  return row;
}

function renderAlerts(alerts) {
  els.alertsList.replaceChildren();
  const visible = alerts.slice(0, alertLimit);
  const unresolved = alerts.filter((item) => item.status !== "resolved").length;
  setText(els.alertsListCount, `${alerts.length} loaded · ${unresolved} unresolved`);
  setText(els.navAlertCount, unresolved);
  els.navAlertCount.hidden = unresolved === 0;
  els.loadMoreAlerts.hidden = alerts.length <= 10;
  setText(els.loadMoreAlerts, alertLimit >= Math.min(alerts.length, ALERTS_LIMIT) ? "Show fewer alerts ↑" : "Show more alerts →");
  for (const alert of visible) {
    const row = document.createElement("div"); row.className = "alert-row";
    const icon = document.createElement("span"); icon.className = `alert-icon ${alert.type === "shock_detected" ? "shock" : ""}`; icon.textContent = alert.type === "shock_detected" ? "↯" : "!";
    const main = document.createElement("div"); main.className = "alert-main";
    const title = document.createElement("strong"); title.textContent = titleCase(alert.type);
    const detail = document.createElement("p");
    const d = alert.details || {};
    if (alert.type === "temperature_breach") detail.textContent = `${fmtNumber(d.temperature_c, 2)} °C · ${d.breach_direction === "above_maximum" ? `max ${fmtNumber(d.max_temperature_c, 1)} °C` : `min ${fmtNumber(d.min_temperature_c, 1)} °C`}`;
    else detail.textContent = `${fmtNumber(d.peak_acceleration_magnitude_g, 2)} g peak · threshold ${fmtNumber(d.threshold_g, 1)} g`;
    const time = document.createElement("div"); time.className = "alert-time"; time.textContent = fmtTime(alert.observed_at, true);
    main.append(title, detail, time);
    const actions = document.createElement("div"); actions.className = "alert-actions";
    const state = document.createElement("span"); state.className = `alert-state ${alert.status}`; state.textContent = alert.status;
    actions.append(state);
    if (alert.status === "open") actions.append(makeAlertButton("Ack", alert.alert_id, "acknowledge"));
    if (alert.status !== "resolved") actions.append(makeAlertButton("Resolve", alert.alert_id, "resolve"));
    row.append(icon, main, actions);
    els.alertsList.append(row);
  }
  if (!visible.length) {
    const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "No alerts recorded for this tracker.";
    els.alertsList.append(empty);
  }
}
function makeAlertButton(label, alertId, action) {
  const button = document.createElement("button"); button.className = "mini-action"; button.type = "button"; button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      let body;
      if (action === "resolve") {
        const note = window.prompt("Resolution note (optional):", "Reviewed by operator");
        if (note === null) { button.disabled = false; return; }
        body = note.trim() ? { resolution_note: note.trim() } : {};
      }
      await api(`/api/v1/alerts/${encodeURIComponent(alertId)}/${action}`, {
        method: "POST",
        ...(action === "acknowledge" ? {} : { body: body || {} }),
      });
      toast(`Alert ${action === "acknowledge" ? "acknowledged" : "resolved"}.`);
      await refreshSelectedTracker();
      const summary = await api("/api/v1/inventory/summary"); renderSummary(summary);
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  });
  return button;
}

function startPolling() {
  window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(async () => {
    if (document.visibilityState === "hidden") return;
    await refreshAll(false);
  }, REFRESH_INTERVAL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !els.app.hidden) refreshAll(false);
});
window.addEventListener("online", () => { if (!els.app.hidden) refreshAll(false); });

setText(els.currentDate, new Date().toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }).toUpperCase());
setText(els.footerApi, `API: ${new URL(API_BASE_URL).host}`);
if (!window.Chart) {
  els.tempChartFallback.hidden = false;
  chartJsWarned = true;
} else {
  // Chart.js is loaded with defer alongside this script; both defer scripts run in document order.
  createCharts();
}
checkSession();
