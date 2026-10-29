"use strict";

// Operational workflows layered on top of the live-monitoring dashboard.
// Uses the same session cookie and CSRF token held by script.js.

const ops = {
  view: "overview",
  packages: [],
  shipments: [],
  devices: [],
  allAlerts: [],
  selectedPackage: null,
  selectedShipment: null,
  selectedTrackingPackage: null,
};

const op$ = (id) => document.getElementById(id);
const opViewTitles = {
  overview: ["Monitoring overview", "Real readings from your ColdGuardian API."],
  packages: ["Packages", "Create and manage packages, monitoring thresholds, tracker assignment and public tracking QR links."],
  shipments: ["Shipments", "Group packages, dispatch monitored shipments, and record deliveries."],
  devices: ["Devices", "MQTT-registered trackers and configuration status."],
  alerts: ["Alerts", "Review, acknowledge and resolve device-reported incidents."],
};

function opSetText(id, value) {
  const node = op$(id);
  if (node) node.textContent = value == null || value === "" ? "—" : String(value);
}

function opNotice(id, message, kind = "info") {
  const node = op$(id);
  if (!node) return;
  node.hidden = !message;
  node.textContent = message || "";
  node.className = `notice ${kind === "error" ? "error-notice" : "info-notice"}`;
}

function opButton(label, className, onClick, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className || "button secondary";
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", onClick);
  return button;
}

function opCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value == null || value === "" ? "—" : String(value);
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}

function opEmptyRow(tbody, message, columns) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = columns;
  cell.className = "empty-cell";
  cell.textContent = message;
  row.append(cell);
  tbody.replaceChildren(row);
}

function opDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

function opLocalDateTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function opIsoFromLocal(value) {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) throw new Error("Enter a valid date and time.");
  return d.toISOString();
}

function opStatusClass(status) {
  if (["online", "in_transit", "delivered", "resolved"].includes(status)) return "good";
  if (["open", "partially_delivered", "packed", "acknowledged"].includes(status)) return "warn";
  return "neutral";
}

function opStatus(status) {
  const span = document.createElement("span");
  span.className = `status-pill ${opStatusClass(status)}`;
  span.textContent = String(status || "unknown").replaceAll("_", " ").toUpperCase();
  return span;
}

function opSetView(name) {
  if (!opViewTitles[name]) name = "overview";
  ops.view = name;
  for (const key of Object.keys(opViewTitles)) {
    const pane = op$(`${key}View`);
    if (pane) pane.hidden = key !== name;
  }
  document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
    const active = item.dataset.view === name;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  opSetText("pageTitle", opViewTitles[name][0]);
  opSetText("pageSubtitle", opViewTitles[name][1]);
  opNotice("globalError", "");
  window.location.hash = name === "overview" ? "overview" : `${name}View`;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "packages") opsRefreshPackages();
  if (name === "shipments") opsRefreshShipments();
  if (name === "devices") opsLoadDevicesPage();
  if (name === "alerts") opsLoadAlertsPage();
}

document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
  item.addEventListener("click", (event) => {
    event.preventDefault();
    opSetView(item.dataset.view);
  });
});

// The main refresh button refreshes whichever operational view is open too.
op$("refreshButton")?.addEventListener("click", () => {
  if (ops.view === "packages") opsRefreshPackages();
  if (ops.view === "shipments") opsRefreshShipments();
  if (ops.view === "devices") opsLoadDevicesPage();
  if (ops.view === "alerts") opsLoadAlertsPage();
});
op$("refreshPackagesButton")?.addEventListener("click", opsRefreshPackages);
op$("refreshShipmentsButton")?.addEventListener("click", opsRefreshShipments);
op$("refreshDevicesButton")?.addEventListener("click", opsLoadDevicesPage);
op$("refreshAlertsButton")?.addEventListener("click", opsLoadAlertsPage);

async function opsRefreshPackages() {
  opNotice("packageMessage", "");
  try {
    const [packageResult, deviceResult] = await Promise.all([
      api("/api/v1/packages?limit=100&offset=0"),
      api("/api/v1/devices?limit=100&offset=0"),
    ]);
    ops.packages = packageResult.items || [];
    ops.devices = deviceResult.items || [];
    opsRenderPackages();
    opsPopulateDeviceSelect(op$("createDeviceId"), null, true);
    opsRenderAvailablePackageChoices();
  } catch (error) {
    opNotice("packageMessage", error.message, "error");
  }
}

function opsPopulateDeviceSelect(select, currentPackageId, onlyUnassigned) {
  if (!select) return;
  const oldValue = select.value;
  const options = [new Option("No tracker assigned", "")];
  const currentDevice = currentPackageId
    ? ops.packages.find((p) => p.package_id === currentPackageId)?.device_id
    : null;
  for (const device of ops.devices) {
    const assignedElsewhere = device.package_id && device.package_id !== currentPackageId;
    const isCurrent = currentPackageId && device.package_id === currentPackageId;
    if (assignedElsewhere) continue;
    if (onlyUnassigned && device.package_id) continue;
    // For a package editor, keep its current tracker as a selectable option.
    if (!onlyUnassigned && currentDevice && device.device_id === currentDevice) {
      options.push(new Option(`${device.device_id} (current)`, device.device_id));
    } else {
      options.push(new Option(`${device.device_id} · ${device.status || "unknown"}`, device.device_id));
    }
  }
  select.replaceChildren(...options);
  if ([...select.options].some((option) => option.value === oldValue)) select.value = oldValue;
  else select.value = currentDevice || "";
}

function opsRenderPackages() {
  const tbody = op$("packagesRows");
  if (!tbody) return;
  opSetText("packageListCount", `${ops.packages.length} packages`);
  if (!ops.packages.length) {
    opEmptyRow(tbody, "No packages yet. Create the first package using the form above.", 8);
    return;
  }
  tbody.replaceChildren();
  for (const pkg of ops.packages) {
    const row = document.createElement("tr");
    opCell(row, pkg.package_id);
    const product = document.createElement("td");
    const strong = document.createElement("strong");
    strong.textContent = pkg.product_name || "Unknown product";
    const small = document.createElement("small");
    small.className = "table-subtext";
    small.textContent = pkg.product_sku || "No SKU";
    product.append(strong, small); row.append(product);
    const status = document.createElement("td"); status.append(opStatus(pkg.status)); row.append(status);
    opCell(row, pkg.device_id || "Unassigned");
    opCell(row, pkg.shipment_id || "—");
    opCell(row, pkg.latest_temperature_valid ? `${Number(pkg.latest_temperature_c).toFixed(2)} °C` : "No valid reading");
    opCell(row, pkg.open_alert_count ?? 0);
    const actions = document.createElement("td"); actions.className = "action-cell";
    actions.append(
      opButton("Tracking / QR", "mini-action", () => opsOpenTracking(pkg.package_id)),
      opButton("History", "mini-action", () => adminOpenPackageHistory(pkg.package_id)),
      opButton("Edit", "mini-action", () => opsEditPackage(pkg.package_id), pkg.status !== "packed"),
    );
    row.append(actions); tbody.append(row);
  }
}

op$("packageCreateForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const min = Number(op$("createMinTemp").value);
    const max = Number(op$("createMaxTemp").value);
    if (min > max) throw new Error("Minimum temperature cannot exceed maximum temperature.");
    const body = {
      product_sku: op$("createSku").value.trim(),
      product_name: op$("createProductName").value.trim(),
      quantity: Number(op$("createQuantity").value),
      unit: op$("createUnit").value.trim(),
      batch_number: op$("createBatch").value.trim() || null,
      expiry_date: op$("createExpiry").value || null,
      min_temperature_c: min,
      max_temperature_c: max,
      shock_threshold_g: Number(op$("createShock").value),
      device_id: op$("createDeviceId").value || null,
    };
    const result = await api("/api/v1/packages", { method: "POST", body });
    opNotice("packageMessage", `Created ${result.package.package_id}. The backend returned a tracking URL and published configuration to the assigned tracker, if any.`);
    if (result.tracking_url) opsShowTracking(result.package.package_id, result.tracking_url, "New tracking URL returned by package creation.");
    event.currentTarget.reset();
    op$("createQuantity").value = "1"; op$("createUnit").value = "package";
    op$("createMinTemp").value = "2"; op$("createMaxTemp").value = "8"; op$("createShock").value = "4";
    await opsRefreshPackages();
    if (typeof refreshAll === "function") await refreshAll(false);
    toast(`Package ${result.package.package_id} created.`);
  } catch (error) { opNotice("packageMessage", error.message, "error"); }
  finally { button.disabled = false; }
});

async function opsEditPackage(packageId) {
  try {
    const pkg = await api(`/api/v1/packages/${encodeURIComponent(packageId)}`);
    if (pkg.status !== "packed") throw new Error("Only packed packages can be edited.");
    ops.selectedPackage = pkg;
    op$("packageEditPanel").hidden = false;
    opSetText("packageEditTitle", `Edit ${pkg.package_id}`);
    op$("editPackageId").value = pkg.package_id;
    op$("editSku").value = pkg.product_sku || "";
    op$("editProductName").value = pkg.product_name || "";
    op$("editQuantity").value = pkg.quantity ?? 1;
    op$("editUnit").value = pkg.unit || "package";
    op$("editBatch").value = pkg.batch_number || "";
    op$("editExpiry").value = pkg.expiry_date || "";
    op$("editMinTemp").value = pkg.min_temperature_c;
    op$("editMaxTemp").value = pkg.max_temperature_c;
    op$("editShock").value = pkg.shock_threshold_g;
    opsPopulateDeviceSelect(op$("editDeviceId"), packageId, false);
    op$("editDeviceId").value = pkg.device_id || "";
    op$("packageEditPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message, "error"); }
}

op$("cancelPackageEdit")?.addEventListener("click", () => { op$("packageEditPanel").hidden = true; ops.selectedPackage = null; });
op$("packageEditForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    const min = Number(op$("editMinTemp").value); const max = Number(op$("editMaxTemp").value);
    if (min > max) throw new Error("Minimum temperature cannot exceed maximum temperature.");
    const id = op$("editPackageId").value;
    const body = {
      product_sku: op$("editSku").value.trim(), product_name: op$("editProductName").value.trim(),
      quantity: Number(op$("editQuantity").value), unit: op$("editUnit").value.trim(),
      batch_number: op$("editBatch").value.trim() || null, expiry_date: op$("editExpiry").value || null,
      min_temperature_c: min, max_temperature_c: max, shock_threshold_g: Number(op$("editShock").value),
      device_id: op$("editDeviceId").value || null,
    };
    await api(`/api/v1/packages/${encodeURIComponent(id)}`, { method: "PATCH", body });
    op$("packageEditPanel").hidden = true; ops.selectedPackage = null;
    toast(`Package ${id} updated. Device config refresh was requested by the backend.`);
    await opsRefreshPackages();
    if (typeof refreshAll === "function") await refreshAll(false);
  } catch (error) { opNotice("packageMessage", error.message, "error"); }
  finally { button.disabled = false; }
});

async function opsOpenTracking(packageId) {
  try {
    const result = await api(`/api/v1/packages/${encodeURIComponent(packageId)}/tracking-token`);
    let url = result.tracking_url;
    if (!url || result.needs_rotation) {
      if (!window.confirm(`Package ${packageId} has no recoverable current tracking URL. Rotate its token? Any older QR for this package will stop working.`)) return;
      const rotated = await api(`/api/v1/packages/${encodeURIComponent(packageId)}/tracking-token/rotate`, { method: "POST" });
      url = rotated.tracking_url;
    }
    if (!url) throw new Error("The backend did not return a tracking URL.");
    opsShowTracking(packageId, url, "The public tracking URL is a bearer secret. Anyone with it can view this package's sanitized tracking page.");
  } catch (error) {
    toast(error.message, "error");
  }
}

function opsShowTracking(packageId, url, message) {
  ops.selectedTrackingPackage = packageId;
  opSetText("trackingPanelTitle", `${packageId} · Tracking QR`);
  op$("trackingUrlInput").value = url;
  op$("openTrackingLink").href = url;
  op$("downloadQrLink").href = `${API_BASE_URL}/api/v1/packages/${encodeURIComponent(packageId)}/tracking-qr.png`;
  opSetText("trackingPanelHint", message || "The ESP32 receives this URL over MQTT and generates its OLED QR locally. The PNG endpoint is for printing/downloading a label.");
  op$("trackingPanel").hidden = false;
  op$("trackingPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

op$("closeTrackingPanel")?.addEventListener("click", () => { op$("trackingPanel").hidden = true; ops.selectedTrackingPackage = null; });
op$("rotateTrackingToken")?.addEventListener("click", async () => {
  const id = ops.selectedTrackingPackage;
  if (!id) return;
  if (!window.confirm(`Rotate the tracking token for ${id}? Old printed QR codes will no longer work. The backend will publish the new URL to the assigned ESP32 through MQTT.`)) return;
  try {
    const result = await api(`/api/v1/packages/${encodeURIComponent(id)}/tracking-token/rotate`, { method: "POST" });
    opsShowTracking(id, result.tracking_url, `Token version ${result.tracking_token_version} created. The backend republished config to the assigned tracker via MQTT.`);
    toast("Tracking token rotated.");
  } catch (error) { toast(error.message, "error"); }
});

async function opsRefreshShipments() {
  opNotice("shipmentMessage", "");
  try {
    const [shipmentsResult, packagesResult, devicesResult] = await Promise.all([
      api("/api/v1/shipments?limit=100&offset=0"),
      api("/api/v1/packages?limit=100&offset=0"),
      api("/api/v1/devices?limit=100&offset=0"),
    ]);
    ops.shipments = shipmentsResult.items || [];
    ops.packages = packagesResult.items || [];
    ops.devices = devicesResult.items || [];
    opsRenderShipments();
    opsRenderAvailablePackageChoices();
  } catch (error) { opNotice("shipmentMessage", error.message, "error"); }
}

function opsRenderAvailablePackageChoices() {
  const container = op$("shipmentPackageChoices");
  if (!container) return;
  container.replaceChildren();
  const eligible = ops.packages.filter((pkg) => pkg.status === "packed" && !pkg.shipment_id);
  if (!eligible.length) {
    const note = document.createElement("p"); note.className = "muted";
    note.textContent = "No available packed packages. Create a package first or check whether existing packages are already attached to a shipment.";
    container.append(note); return;
  }
  for (const pkg of eligible) {
    const label = document.createElement("label"); label.className = "package-choice";
    const check = document.createElement("input"); check.type = "checkbox"; check.name = "shipmentPackageId"; check.value = pkg.package_id;
    const text = document.createElement("span");
    const strong = document.createElement("strong"); strong.textContent = `${pkg.package_id} · ${pkg.product_name}`;
    const small = document.createElement("small"); small.textContent = `${pkg.product_sku} · tracker ${pkg.device_id || "not assigned"}`;
    text.append(strong, small); label.append(check, text); container.append(label);
  }
}

op$("shipmentCreateForm")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    const packageIds = [...document.querySelectorAll('input[name="shipmentPackageId"]:checked')].map((node) => node.value);
    if (!packageIds.length) throw new Error("Select at least one available packed package.");
    const body = {
      origin: op$("shipmentOrigin").value.trim(), destination: op$("shipmentDestination").value.trim(),
      expected_delivery_at: opIsoFromLocal(op$("shipmentExpected").value), package_ids: packageIds,
    };
    const result = await api("/api/v1/shipments", { method: "POST", body });
    opNotice("shipmentMessage", `Created ${result.shipment_id} with ${result.package_count} package(s). Trackers assigned to these packages receive refreshed retained configuration over MQTT.`);
    event.currentTarget.reset();
    await opsRefreshShipments();
    if (typeof refreshAll === "function") await refreshAll(false);
    toast(`Shipment ${result.shipment_id} created.`);
  } catch (error) { opNotice("shipmentMessage", error.message, "error"); }
  finally { button.disabled = false; }
});

function opsRenderShipments() {
  const tbody = op$("shipmentsRows"); if (!tbody) return;
  opSetText("shipmentListCount", `${ops.shipments.length} shipments`);
  if (!ops.shipments.length) { opEmptyRow(tbody, "No shipments yet. Create one from packed packages above.", 8); return; }
  tbody.replaceChildren();
  for (const sh of ops.shipments) {
    const row = document.createElement("tr");
    opCell(row, sh.shipment_id); opCell(row, `${sh.origin} → ${sh.destination}`);
    const statusCell = document.createElement("td"); statusCell.append(opStatus(sh.status)); row.append(statusCell);
    opCell(row, sh.package_count ?? 0); opCell(row, sh.delivered_package_count ?? 0); opCell(row, sh.open_alert_count ?? 0);
    opCell(row, sh.expected_delivery_at ? opDate(sh.expected_delivery_at) : "Not set");
    const actions = document.createElement("td"); actions.className = "action-cell";
    actions.append(opButton("View", "mini-action", () => opsViewShipment(sh.shipment_id)));
    if (sh.status === "packed") {
      actions.append(opButton("Edit", "mini-action", () => opsEditShipment(sh.shipment_id)));
      actions.append(opButton("Dispatch", "mini-action primary-mini", () => opsDispatchShipment(sh.shipment_id)));
    }
    row.append(actions); tbody.append(row);
  }
}

async function opsEditShipment(shipmentId) {
  try {
    const sh = await api(`/api/v1/shipments/${encodeURIComponent(shipmentId)}`);
    if (sh.status !== "packed") throw new Error("Only packed shipments can be edited.");
    op$("editShipmentId").value = shipmentId; op$("editShipmentOrigin").value = sh.origin || "";
    op$("editShipmentDestination").value = sh.destination || "";
    op$("editShipmentExpected").value = opLocalDateTime(sh.expected_delivery_at);
    opSetText("shipmentEditTitle", `Edit ${shipmentId}`); op$("shipmentEditPanel").hidden = false;
    op$("shipmentEditPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message, "error"); }
}

op$("cancelShipmentEdit")?.addEventListener("click", () => { op$("shipmentEditPanel").hidden = true; });
op$("shipmentEditForm")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    const id = op$("editShipmentId").value;
    const body = {
      origin: op$("editShipmentOrigin").value.trim(),
      destination: op$("editShipmentDestination").value.trim(),
      expected_delivery_at: opIsoFromLocal(op$("editShipmentExpected").value),
    };
    await api(`/api/v1/shipments/${encodeURIComponent(id)}`, { method: "PATCH", body });
    op$("shipmentEditPanel").hidden = true; toast(`Shipment ${id} updated.`); await opsRefreshShipments();
  } catch (error) { opNotice("shipmentMessage", error.message, "error"); }
  finally { button.disabled = false; }
});

async function opsDispatchShipment(shipmentId) {
  if (!window.confirm(`Dispatch ${shipmentId}? The backend requires every package to have a registered tracker and the latest package config to be applied.`)) return;
  const note = window.prompt("Dispatch note (optional):", "Released for transport");
  if (note === null) return;
  try {
    await api(`/api/v1/shipments/${encodeURIComponent(shipmentId)}/dispatch`, { method: "POST", body: { note: note.trim() || null } });
    toast(`${shipmentId} dispatched.`); await opsRefreshShipments(); await opsRefreshPackages();
    if (typeof refreshAll === "function") await refreshAll(false);
  } catch (error) { opNotice("shipmentMessage", error.message, "error"); }
}

async function opsViewShipment(shipmentId) {
  try {
    const [detail, events] = await Promise.all([
      api(`/api/v1/shipments/${encodeURIComponent(shipmentId)}`),
      api(`/api/v1/shipments/${encodeURIComponent(shipmentId)}/events`),
    ]);
    ops.selectedShipment = detail;
    opSetText("shipmentDetailTitle", `${detail.shipment_id} · ${detail.origin} → ${detail.destination}`);
    const summary = op$("shipmentDetailSummary"); summary.replaceChildren();
    const fields = [
      ["Status", detail.status], ["Packages", `${detail.delivered_package_count || 0}/${detail.package_count || 0} delivered`],
      ["Open alerts", detail.open_alert_count ?? 0], ["Expected delivery", opDate(detail.expected_delivery_at)],
      ["Dispatched", opDate(detail.dispatched_at)], ["Delivered", opDate(detail.delivered_at)],
    ];
    for (const [label, value] of fields) {
      const row = document.createElement("div"); row.className = "detail-row";
      const name = document.createElement("span"); name.textContent = label;
      const val = document.createElement("strong"); val.textContent = value == null || value === "Unknown time" ? "—" : String(value);
      row.append(name, val); summary.append(row);
    }
    const tbody = op$("shipmentPackageRows"); tbody.replaceChildren();
    for (const pkg of detail.packages || []) {
      const row = document.createElement("tr"); opCell(row, pkg.package_id);
      opCell(row, `${pkg.product_name || ""} (${pkg.product_sku || "—"})`); opCell(row, pkg.device_id || "Unassigned");
      const status = document.createElement("td"); status.append(opStatus(pkg.status)); row.append(status);
      opCell(row, pkg.latest_temperature_valid ? `${Number(pkg.latest_temperature_c).toFixed(2)} °C` : "No valid reading");
      opCell(row, pkg.open_alert_count ?? 0);
      const action = document.createElement("td");
      if (pkg.status === "in_transit") action.append(opButton("Mark delivered", "mini-action", () => opsDeliverPackage(pkg.package_id, detail.shipment_id)));
      else action.textContent = pkg.status === "delivered" ? "Delivered" : "—";
      row.append(action); tbody.append(row);
    }
    if (!(detail.packages || []).length) opEmptyRow(tbody, "No packages attached.", 7);
    const timeline = op$("shipmentEvents"); timeline.replaceChildren();
    for (const event of events.items || []) {
      const node = document.createElement("div"); node.className = "timeline-item";
      const title = document.createElement("strong"); title.textContent = String(event.event_type || "event").replaceAll("_", " ");
      const details = document.createElement("p"); details.textContent = `${opDate(event.occurred_at)} · ${event.actor || "system"}${event.package_id ? ` · ${event.package_id}` : ""}`;
      node.append(title, details); timeline.append(node);
    }
    if (!(events.items || []).length) { const n = document.createElement("p"); n.className = "muted"; n.textContent = "No timeline events yet."; timeline.append(n); }
    op$("shipmentDetailPanel").hidden = false;
    op$("shipmentDetailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message, "error"); }
}

op$("closeShipmentDetail")?.addEventListener("click", () => { op$("shipmentDetailPanel").hidden = true; ops.selectedShipment = null; });

async function opsDeliverPackage(packageId, shipmentId) {
  if (!window.confirm(`Confirm that package ${packageId} has been received? This updates the package and shipment lifecycle.`)) return;
  const note = window.prompt("Delivery note (optional):", "Package received");
  if (note === null) return;
  try {
    await api(`/api/v1/packages/${encodeURIComponent(packageId)}/deliver`, { method: "POST", body: { note: note.trim() || null } });
    toast(`${packageId} marked delivered.`);
    await opsViewShipment(shipmentId); await opsRefreshShipments(); await opsRefreshPackages();
    if (typeof refreshAll === "function") await refreshAll(false);
  } catch (error) { toast(error.message, "error"); }
}

async function opsLoadDevicesPage() {
  try {
    const result = await api("/api/v1/devices?limit=100&offset=0"); ops.devices = result.items || [];
    opSetText("deviceListCount", `${result.total ?? ops.devices.length} registered devices`);
    const tbody = op$("devicesRows"); tbody.replaceChildren();
    for (const device of ops.devices) {
      const row = document.createElement("tr"); opCell(row, device.device_id);
      const status = document.createElement("td"); status.append(opStatus(device.status)); row.append(status);
      opCell(row, device.firmware_version || "—"); opCell(row, device.package_id || "Unassigned"); opCell(row, device.shipment_id || "—");
      opCell(row, `v${device.config_version ?? 0} / applied v${device.config_applied_version ?? 0}`); opCell(row, opDate(device.last_seen_at));
      const actions = document.createElement("td"); actions.append(opButton("Inspect", "mini-action", () => opsInspectDevice(device.device_id))); row.append(actions); tbody.append(row);
    }
    if (!ops.devices.length) opEmptyRow(tbody, "No devices registered. A tracker appears here after publishing its MQTT register message.", 8);
    const filter = op$("alertDeviceFilter");
    if (filter) {
      const chosen = filter.value;
      filter.replaceChildren(new Option("All devices", ""));
      for (const device of ops.devices) filter.append(new Option(device.device_id, device.device_id));
      filter.value = chosen;
    }
    opsPopulateDeviceSelect(op$("createDeviceId"), null, true);
  } catch (error) { toast(error.message, "error"); }
}

async function opsInspectDevice(deviceId) {
  try {
    const result = await api(`/api/v1/devices/${encodeURIComponent(deviceId)}`);
    opSetText("deviceDetailTitle", `${deviceId} · full diagnostics`);
    opSetText("deviceDetailJson", JSON.stringify(result, null, 2));
    op$("deviceDetailPanel").hidden = false;
    op$("deviceDetailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message, "error"); }
}
op$("closeDeviceDetail")?.addEventListener("click", () => { op$("deviceDetailPanel").hidden = true; });

async function opsLoadAlertsPage() {
  try {
    const params = new URLSearchParams({ limit: "100", offset: "0" });
    const status = op$("alertStatusFilter").value; const type = op$("alertTypeFilter").value; const deviceId = op$("alertDeviceFilter").value;
    const packageId = op$("alertPackageFilter")?.value || ""; const shipmentId = op$("alertShipmentFilter")?.value || "";
    if (status) params.set("status", status); if (type) params.set("type", type); if (deviceId) params.set("device_id", deviceId);
    if (packageId) params.set("package_id", packageId); if (shipmentId) params.set("shipment_id", shipmentId);
    const result = await api(`/api/v1/alerts?${params.toString()}`); ops.allAlerts = result.items || [];
    opSetText("allAlertsCount", `${result.total ?? ops.allAlerts.length} alerts`);
    const list = op$("allAlertsList"); list.replaceChildren();
    for (const alert of ops.allAlerts) {
      const row = document.createElement("div"); row.className = "alert-row";
      const icon = document.createElement("span"); icon.className = `alert-icon ${alert.type === "shock_detected" ? "shock" : ""}`; icon.textContent = alert.type === "shock_detected" ? "↯" : "!";
      const main = document.createElement("div"); main.className = "alert-main";
      const title = document.createElement("strong"); title.textContent = String(alert.type).replaceAll("_", " ");
      const d = alert.details || {}; const desc = document.createElement("p");
      desc.textContent = alert.type === "temperature_breach"
        ? `${d.temperature_c ?? "—"} °C · ${d.breach_direction || "excursion"} · package ${alert.package_id || "—"}`
        : `${d.peak_acceleration_magnitude_g ?? "—"} g peak · threshold ${d.threshold_g ?? "—"} g · package ${alert.package_id || "—"}`;
      const meta = document.createElement("div"); meta.className = "alert-time"; meta.textContent = `${alert.device_id} · ${opDate(alert.observed_at)}`;
      main.append(title, desc, meta);
      const actions = document.createElement("div"); actions.className = "alert-actions"; const state = document.createElement("span"); state.className = `alert-state ${alert.status}`; state.textContent = alert.status; actions.append(state);
      actions.append(opButton("Details", "mini-action", () => adminViewAlert(alert.alert_id)));
      if (alert.status === "open") actions.append(opButton("Ack", "mini-action", () => opsAlertAction(alert.alert_id, "acknowledge")));
      if (alert.status !== "resolved") actions.append(opButton("Resolve", "mini-action", () => opsAlertAction(alert.alert_id, "resolve")));
      row.append(icon, main, actions); list.append(row);
    }
    if (!ops.allAlerts.length) { const empty = document.createElement("div"); empty.className = "empty-state"; empty.textContent = "No alerts match these filters."; list.append(empty); }
  } catch (error) { toast(error.message, "error"); }
}

async function opsAlertAction(alertId, action) {
  let body = {};
  if (action === "resolve") {
    const note = window.prompt("Resolution note (optional):", "Reviewed by operator");
    if (note === null) return;
    body = { resolution_note: note.trim() || null };
  }
  try {
    await api(`/api/v1/alerts/${encodeURIComponent(alertId)}/${action}`, { method: "POST", ...(action === "resolve" ? { body } : {}) });
    toast(action === "acknowledge" ? "Alert acknowledged." : "Alert resolved.");
    await opsLoadAlertsPage();
    if (typeof refreshAll === "function") await refreshAll(false);
  } catch (error) { toast(error.message, "error"); }
}

["alertStatusFilter", "alertTypeFilter", "alertDeviceFilter", "alertPackageFilter", "alertShipmentFilter"].forEach((id) => {
  op$(id)?.addEventListener("change", opsLoadAlertsPage);
});

// On an existing session the original dashboard bootstrap already runs.
// Keep the operational data fresh while its page is visible.
window.setInterval(() => {
  if (typeof currentUser === "undefined" || !currentUser || document.visibilityState === "hidden") return;
  if (ops.view === "packages") opsRefreshPackages();
  else if (ops.view === "shipments") opsRefreshShipments();
  else if (ops.view === "devices") opsLoadDevicesPage();
  else if (ops.view === "alerts") opsLoadAlertsPage();
}, 15000);
