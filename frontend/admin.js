"use strict";

// Admin/account/history screens layered onto the existing ColdGuardian UI.
// Uses the same cookie-backed api() helper and in-memory csrfToken from script.js.

Object.assign(opViewTitles, {
  history: ["Package history", "Historical telemetry for a selected package."],
  users: ["User administration", "Invite accounts, adjust roles, and manage sessions."],
  account: ["Account settings", "Change your password and protect your session."],
  audit: ["Audit log", "Review recorded administrative and operational actions."],
});

const admin$ = (id) => document.getElementById(id);
let historySelectedPackage = "";
let historyNextBefore = null;
let historyRows = [];
let historyTemperatureChart = null;
let historyShockChart = null;
let auditOffset = 0;
let cachedAlertFilterOptions = false;

function adminMessage(id, message, kind = "info") {
  const el = admin$(id);
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || "";
  el.className = `notice ${kind === "error" ? "error-notice" : "info-notice"}`;
}

function adminCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
  row.append(cell);
  return cell;
}

function adminButton(label, onClick, className = "mini-action", disabled = false) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
  btn.textContent = label;
  btn.disabled = disabled;
  btn.addEventListener("click", onClick);
  return btn;
}

function adminEmpty(tbody, message, columns) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = columns;
  td.className = "empty-cell";
  td.textContent = message;
  tr.append(td);
  tbody.replaceChildren(tr);
}

function adminSetCellDate(row, value) {
  const d = value ? new Date(value) : null;
  return adminCell(row, d && !Number.isNaN(d.getTime()) ? d.toLocaleString() : "—");
}

function adminUpdateRoleVisibility() {
  const isAdmin = currentUser?.role === "admin";
  document.querySelectorAll("[data-admin-only]").forEach((el) => {
    el.hidden = !isAdmin;
  });
  const adminLabel = document.querySelector(".admin-nav-label");
  if (adminLabel) adminLabel.hidden = !isAdmin;
}

function adminLoadView(name) {
  if (name === "history") return adminPrepareHistory();
  if (name === "users") return adminLoadUsers();
  if (name === "account") return adminPrepareAccount();
  if (name === "audit") return adminLoadAudit(true);
}

document.querySelectorAll('.nav-item[data-view="history"], .nav-item[data-view="users"], .nav-item[data-view="account"], .nav-item[data-view="audit"]').forEach((link) => {
  link.addEventListener("click", () => {
    // operations.js registered its view-switch handler first; load the target next.
    window.setTimeout(() => adminLoadView(link.dataset.view), 0);
  });
});

// ----- Package history -----

async function adminPrepareHistory() {
  try {
    const result = await api("/api/v1/packages?limit=100&offset=0");
    const packages = result.items || [];
    const select = admin$("historyPackageSelect");
    const previous = historySelectedPackage || select.value;
    select.replaceChildren(new Option("Select a package", ""));
    for (const pkg of packages) {
      select.append(new Option(`${pkg.package_id} · ${pkg.product_name || pkg.product_sku || "Package"}`, pkg.package_id));
    }
    if (packages.some((pkg) => pkg.package_id === previous)) select.value = previous;
    else if (packages.length) select.value = packages[0].package_id;
    historySelectedPackage = select.value;
    if (historySelectedPackage) await adminLoadPackageHistory(true);
    else {
      historyRows = [];
      adminEmpty(admin$("packageHistoryRows"), "No packages yet. Create a package first.", 7);
      admin$("historyCount").textContent = "0 records";
      adminUpdateHistoryCharts();
    }
  } catch (error) {
    adminMessage("historyMessage", error.message, "error");
  }
}

function adminOpenPackageHistory(packageId) {
  opSetView("history");
  historySelectedPackage = packageId;
  admin$("historyPackageSelect").value = packageId;
  adminLoadPackageHistory(true);
}

admin$("historyPackageSelect")?.addEventListener("change", () => {
  historySelectedPackage = admin$("historyPackageSelect").value;
  adminLoadPackageHistory(true);
});
admin$("loadPackageHistory")?.addEventListener("click", () => adminLoadPackageHistory(true));
admin$("refreshPackageHistory")?.addEventListener("click", () => adminLoadPackageHistory(true));
admin$("loadOlderHistory")?.addEventListener("click", () => adminLoadPackageHistory(false));

async function adminLoadPackageHistory(reset) {
  const packageId = admin$("historyPackageSelect")?.value || historySelectedPackage;
  if (!packageId) {
    adminMessage("historyMessage", "Select a package first.", "error");
    return;
  }
  historySelectedPackage = packageId;
  historySelectedPackage = packageId;
  if (reset) {
    historyRows = [];
    historyNextBefore = null;
  }
  const params = new URLSearchParams({ limit: "100" });
  if (!reset && historyNextBefore) params.set("before", historyNextBefore);
  try {
    adminMessage("historyMessage", "Loading package telemetry…");
    const result = await api(`/api/v1/packages/${encodeURIComponent(packageId)}/telemetry?${params.toString()}`);
    const items = result.items || [];
    // API pages are newest-first. Prevent duplicate rows when a timestamp cursor overlaps.
    const existing = new Set(historyRows.map((r) => `${r.device_id}:${r.boot_id}:${r.message_seq}`));
    for (const item of items) {
      const key = `${item.device_id}:${item.boot_id}:${item.message_seq}`;
      if (!existing.has(key)) historyRows.push(item);
    }
    historyNextBefore = result.next_before || null;
    historyRows.sort((a, b) => new Date(b.observed_at) - new Date(a.observed_at));
    adminRenderHistoryRows();
    adminUpdateHistoryCharts();
    adminMessage("historyMessage", items.length ? `Loaded ${items.length} record(s) from ${packageId}.` : "No more telemetry records were returned.");
    admin$("loadOlderHistory").hidden = !historyNextBefore;
  } catch (error) {
    adminMessage("historyMessage", error.message, "error");
  }
}

function adminRenderHistoryRows() {
  const tbody = admin$("packageHistoryRows");
  tbody.replaceChildren();
  admin$("historyCount").textContent = `${historyRows.length} records`;
  for (const item of historyRows) {
    const row = document.createElement("tr");
    const d = new Date(item.observed_at);
    adminCell(row, Number.isNaN(d.getTime()) ? "Unknown" : d.toLocaleString());
    adminCell(row, item.device_id);
    adminCell(row, item.temperature_valid ? `${Number(item.temperature_c).toFixed(2)} °C` : "Invalid");
    adminCell(row, item.motion_valid ? `${Number(item.peak_acceleration_magnitude_g).toFixed(3)} g` : "Invalid");
    adminCell(row, item.shock_count_since_last_report ?? 0);
    adminCell(row, item.location_fix_valid ? `${Number(item.latitude).toFixed(5)}, ${Number(item.longitude).toFixed(5)}` : "No fix");
    adminCell(row, item.config_version ?? 0);
    tbody.append(row);
  }
  if (!historyRows.length) adminEmpty(tbody, "No telemetry recorded for this package yet.", 7);
}

function adminUpdateHistoryCharts() {
  const sorted = [...historyRows].reverse();
  if (!window.Chart) return;
  if (!historyTemperatureChart) {
    historyTemperatureChart = new Chart(admin$("packageHistoryTemperatureChart"), {
      type: "line",
      data: { labels: [], datasets: [{ label: "Temperature °C", data: [], borderColor: "#c98b00", backgroundColor: "rgba(255,217,44,.20)", fill: true, tension: .2, pointRadius: 2, spanGaps: false }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } } },
    });
    historyShockChart = new Chart(admin$("packageHistoryShockChart"), {
      type: "line",
      data: { labels: [], datasets: [{ label: "Acceleration g", data: [], borderColor: "#c64439", backgroundColor: "rgba(198,68,57,.10)", fill: true, tension: .2, pointRadius: 2, spanGaps: false }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } } },
    });
  }
  const temps = sorted.filter((x) => x.temperature_valid && Number.isFinite(x.temperature_c));
  const shocks = sorted.filter((x) => x.motion_valid && Number.isFinite(x.peak_acceleration_magnitude_g));
  const label = (x) => {
    const d = new Date(x.observed_at);
    return Number.isNaN(d.getTime()) ? "?" : d.toLocaleTimeString();
  };
  historyTemperatureChart.data.labels = temps.map(label);
  historyTemperatureChart.data.datasets[0].data = temps.map((x) => x.temperature_c);
  historyTemperatureChart.update("none");
  historyShockChart.data.labels = shocks.map(label);
  historyShockChart.data.datasets[0].data = shocks.map((x) => x.peak_acceleration_magnitude_g);
  historyShockChart.update("none");
}

// ----- Alert filters and detail -----

async function adminPrepareAlertFilters() {
  if (cachedAlertFilterOptions) return;
  try {
    const [packages, shipments] = await Promise.all([
      api("/api/v1/packages?limit=100&offset=0"),
      api("/api/v1/shipments?limit=100&offset=0"),
    ]);
    const packageSelect = admin$("alertPackageFilter");
    const shipmentSelect = admin$("alertShipmentFilter");
    const oldPackage = packageSelect.value;
    const oldShipment = shipmentSelect.value;
    packageSelect.replaceChildren(new Option("All packages", ""));
    for (const pkg of packages.items || []) packageSelect.append(new Option(`${pkg.package_id} · ${pkg.product_sku || pkg.product_name}`, pkg.package_id));
    shipmentSelect.replaceChildren(new Option("All shipments", ""));
    for (const shipment of shipments.items || []) shipmentSelect.append(new Option(`${shipment.shipment_id} · ${shipment.origin} → ${shipment.destination}`, shipment.shipment_id));
    packageSelect.value = oldPackage;
    shipmentSelect.value = oldShipment;
    cachedAlertFilterOptions = true;
  } catch (error) {
    toast(`Could not load alert filter options: ${error.message}`, "error");
  }
}

// Filter selectors are already wired by operations.js; populate options once then refresh.
const originalOpsLoadAlertsPage = opsLoadAlertsPage;
opsLoadAlertsPage = async function () {
  await adminPrepareAlertFilters();
  return originalOpsLoadAlertsPage();
};

async function adminViewAlert(alertId) {
  try {
    const alert = await api(`/api/v1/alerts/${encodeURIComponent(alertId)}`);
    admin$("alertDetailTitle").textContent = `${alert.alert_id} · ${String(alert.type).replaceAll("_", " ")}`;
    const fields = [
      ["Status", alert.status], ["Severity", alert.severity], ["Device", alert.device_id],
      ["Package", alert.package_id], ["Shipment", alert.shipment_id],
      ["Observed", adminDate(alert.observed_at)], ["Received", adminDate(alert.received_at)],
      ["Acknowledged by", alert.acknowledged_by], ["Acknowledged at", adminDate(alert.acknowledged_at)],
      ["Resolved by", alert.resolved_by], ["Resolved at", adminDate(alert.resolved_at)],
      ["Resolution note", alert.resolution_note], ["Source boot ID", alert.source_boot_id],
      ["Source message sequence", alert.source_message_seq],
    ];
    const summary = admin$("alertDetailSummary");
    summary.replaceChildren();
    for (const [label, value] of fields) {
      const row = document.createElement("div"); row.className = "detail-row";
      const key = document.createElement("span"); key.textContent = label;
      const val = document.createElement("strong"); val.textContent = value == null || value === "" ? "—" : String(value);
      row.append(key, val); summary.append(row);
    }
    admin$("alertDetailJson").textContent = JSON.stringify(alert.details || {}, null, 2);
    admin$("alertDetailPanel").hidden = false;
    admin$("alertDetailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(error.message, "error");
  }
}

function adminDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
admin$("closeAlertDetail")?.addEventListener("click", () => { admin$("alertDetailPanel").hidden = true; });

// ----- User administration (Admin only) -----

async function adminLoadUsers() {
  if (currentUser?.role !== "admin") {
    adminMessage("usersMessage", "User management is restricted to Admin accounts.", "error");
    return;
  }
  adminMessage("usersMessage", "Loading users and invitations…");
  try {
    const status = admin$("invitationStatusFilter").value;
    const invParams = new URLSearchParams({ limit: "100", offset: "0" });
    if (status) invParams.set("status", status);
    const [users, invitations] = await Promise.all([
      api("/api/v1/auth/users?limit=100&offset=0"),
      api(`/api/v1/auth/users/invitations?${invParams}`),
    ]);
    adminRenderUsers(users.items || []);
    adminRenderInvitations(invitations.items || []);
    admin$("usersCount").textContent = `${users.total ?? users.items?.length ?? 0} users`;
    admin$("invitationsCount").textContent = `${invitations.total ?? invitations.items?.length ?? 0} invitations`;
    adminMessage("usersMessage", "Account data loaded.");
  } catch (error) {
    adminMessage("usersMessage", error.message, "error");
  }
}

function adminRenderUsers(users) {
  const tbody = admin$("usersRows"); tbody.replaceChildren();
  for (const user of users) {
    const row = document.createElement("tr");
    adminCell(row, user.email);
    const roleCell = document.createElement("td");
    const role = document.createElement("select"); role.className = "admin-role-select";
    role.append(new Option("Operator", "operator"), new Option("Admin", "admin"));
    role.value = user.role;
    roleCell.append(role); row.append(roleCell);
    adminCell(row, user.is_active ? "Active" : "Disabled");
    adminCell(row, user.active_session_count ?? 0);
    adminCell(row, adminDate(user.created_at));
    const actions = document.createElement("td"); actions.className = "admin-table-actions";
    actions.append(adminButton("Save role", async () => {
      if (role.value === user.role) return toast("Role is unchanged.");
      if (!window.confirm(`Change ${user.email} from ${user.role} to ${role.value}?`)) return;
      try { await api(`/api/v1/auth/users/${encodeURIComponent(user.user_id)}`, { method: "PATCH", body: { role: role.value } }); toast("User role updated."); await adminLoadUsers(); }
      catch (error) { toast(error.message, "error"); }
    }));
    actions.append(adminButton(user.is_active ? "Disable" : "Enable", async () => {
      if (user.user_id === currentUser?.user_id && user.is_active) return toast("You cannot disable your own current account from this screen.", "error");
      const next = !user.is_active;
      if (!window.confirm(`${next ? "Enable" : "Disable"} ${user.email}? Disabling revokes their sessions.`)) return;
      try { await api(`/api/v1/auth/users/${encodeURIComponent(user.user_id)}`, { method: "PATCH", body: { is_active: next } }); toast(next ? "User enabled." : "User disabled and sessions revoked."); await adminLoadUsers(); }
      catch (error) { toast(error.message, "error"); }
    }, "mini-action", user.user_id === currentUser?.user_id && user.is_active));
    actions.append(adminButton("Reset invite", async () => {
      if (user.user_id === currentUser?.user_id) {
        if (!window.confirm("Create a password-reset invitation for your own account? Your current session will remain, but keep the link private.")) return;
      }
      try {
        const result = await api(`/api/v1/auth/users/${encodeURIComponent(user.user_id)}/password-reset-invitations`, { method: "POST" });
        adminShowInvitation(result, "Password-reset link created. It is shown only once; copy it now.");
        toast("Password-reset invitation created.");
      } catch (error) { toast(error.message, "error"); }
    }));
    actions.append(adminButton("Revoke sessions", async () => {
      if (user.user_id === currentUser?.user_id && !window.confirm("This revokes every session for your own account, including the current session. Continue?")) return;
      if (user.user_id !== currentUser?.user_id && !window.confirm(`Revoke all active sessions for ${user.email}?`)) return;
      try { const result = await api(`/api/v1/auth/users/${encodeURIComponent(user.user_id)}/revoke-sessions`, { method: "POST" }); toast(`Revoked ${result.revoked_sessions} session(s).`); if (user.user_id === currentUser?.user_id) handleSessionExpired(); else await adminLoadUsers(); }
      catch (error) { toast(error.message, "error"); }
    }, "mini-action", user.user_id === currentUser?.user_id));
    row.append(actions); tbody.append(row);
  }
  if (!users.length) adminEmpty(tbody, "No users found.", 6);
}

function adminRenderInvitations(invitations) {
  const tbody = admin$("invitationsRows"); tbody.replaceChildren();
  for (const invitation of invitations) {
    const row = document.createElement("tr");
    adminCell(row, invitation.email); adminCell(row, invitation.role); adminCell(row, invitation.purpose);
    const status = invitation.accepted_at ? "accepted" : invitation.revoked_at ? "revoked" : new Date(invitation.expires_at) <= new Date() ? "expired" : "pending";
    adminCell(row, status); adminCell(row, adminDate(invitation.expires_at));
    const actions = document.createElement("td");
    if (status === "pending") actions.append(adminButton("Revoke", async () => {
      if (!window.confirm(`Revoke the pending invitation for ${invitation.email}?`)) return;
      try { await api(`/api/v1/auth/users/invitations/${encodeURIComponent(invitation.invitation_id)}`, { method: "DELETE", body: { reason: "Revoked from frontend" } }); toast("Invitation revoked."); await adminLoadUsers(); }
      catch (error) { toast(error.message, "error"); }
    }));
    else actions.textContent = "—";
    row.append(actions); tbody.append(row);
  }
  if (!invitations.length) adminEmpty(tbody, "No invitations match this filter.", 6);
}

function adminShowInvitation(result, message) {
  admin$("invitationResult").hidden = false;
  admin$("invitationUrl").value = result.activation_url || "";
  admin$("openInvitationUrl").href = result.activation_url || "#";
  adminMessage("usersMessage", `${message} Expiry: ${adminDate(result.expires_at)}.`);
  admin$("invitationResult").scrollIntoView({ behavior: "smooth", block: "center" });
}

admin$("inviteUserForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const result = await api("/api/v1/auth/users/invitations", { method: "POST", body: { email: admin$("inviteUserEmail").value.trim(), role: admin$("inviteUserRole").value } });
    adminShowInvitation(result, "Invitation created.");
    admin$("inviteUserForm").reset();
    toast(`Invitation created for ${result.email}.`);
    await adminLoadUsers();
  } catch (error) { adminMessage("usersMessage", error.message, "error"); }
  finally { button.disabled = false; }
});
admin$("copyInvitationUrl")?.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(admin$("invitationUrl").value);
    toast("Invitation link copied.");
  } catch {
    admin$("invitationUrl").select();
    toast("Select and copy the invitation link manually.", "error");
  }
});
admin$("refreshUsersButton")?.addEventListener("click", adminLoadUsers);
admin$("invitationStatusFilter")?.addEventListener("change", adminLoadUsers);

// ----- Account settings -----

function adminPrepareAccount() {
  adminMessage("accountMessage", `Signed in as ${currentUser?.email || "—"} (${currentUser?.role || "—"}).`);
}

admin$("changePasswordForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const current = admin$("currentPassword").value;
  const next = admin$("newPassword").value;
  const confirm = admin$("confirmNewPassword").value;
  if (next.length < 12) return adminMessage("accountMessage", "New password must have at least 12 characters.", "error");
  if (next !== confirm) return adminMessage("accountMessage", "New password and confirmation do not match.", "error");
  const button = event.currentTarget.querySelector('button[type="submit"]'); button.disabled = true;
  try {
    await api("/api/v1/auth/password/change", { method: "POST", body: { current_password: current, new_password: next } });
    event.currentTarget.reset();
    adminMessage("accountMessage", "Password changed. The backend revoked your other sessions.");
    toast("Password changed successfully.");
  } catch (error) { adminMessage("accountMessage", error.message, "error"); }
  finally { button.disabled = false; }
});

// ----- Audit log -----

async function adminLoadAudit(reset) {
  if (currentUser?.role !== "admin") {
    adminMessage("auditMessage", "Audit log is restricted to Admin accounts.", "error");
    return;
  }
  if (reset) auditOffset = 0;
  try {
    adminMessage("auditMessage", "Loading audit events…");
    const result = await api(`/api/v1/auth/audit-log?limit=100&offset=${auditOffset}`);
    const tbody = admin$("auditRows");
    if (reset || auditOffset === 0) tbody.replaceChildren();
    for (const item of result.items || []) {
      const row = document.createElement("tr");
      adminSetCellDate(row, item.created_at); adminCell(row, item.actor_email); adminCell(row, item.action);
      adminCell(row, item.target_type); adminCell(row, item.target_id); adminCell(row, JSON.stringify(item.metadata || {}));
      tbody.append(row);
    }
    if (!tbody.children.length) adminEmpty(tbody, "No audit events recorded.", 6);
    admin$("auditCount").textContent = `${result.total ?? result.items?.length ?? 0} total events · showing ${tbody.querySelectorAll("tr").length}`;
    admin$("loadOlderAudit").hidden = auditOffset + (result.items || []).length >= (result.total || 0) || !(result.items || []).length;
    adminMessage("auditMessage", "Audit log loaded.");
  } catch (error) { adminMessage("auditMessage", error.message, "error"); }
}
admin$("refreshAuditButton")?.addEventListener("click", () => adminLoadAudit(true));
admin$("loadOlderAudit")?.addEventListener("click", async () => {
  auditOffset += 100;
  await adminLoadAudit(false);
});

// If the user navigates directly to a view hash after login, the existing app still starts at Overview.
window.addEventListener("hashchange", () => {
  const name = location.hash.replace(/^#/, "").replace(/View$/, "");
  if (opViewTitles[name] && ops.view !== name) {
    opSetView(name);
    adminLoadView(name);
  }
});
