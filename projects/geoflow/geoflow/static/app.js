"use strict";

const $ = (id) => document.getElementById(id);
const state = { csv: "", report: null, appliedConfig: null, name: "", repair: false, busy: false, page: 0, plotPoints: [], view: "overview" };
const PAGE_SIZE = 7;
const statusLabels = { ready: "Ready", warning: "Review", invalid: "Invalid", estimated: "Estimated" };
const issueLabels = { missing_gps: "Missing GPS", missing_coordinate: "Incomplete coordinates", non_numeric: "Non-numeric values", out_of_range: "Out-of-range values", possible_swap: "Possible coordinate swap", duplicate_id: "Duplicate identifiers", duplicate_location: "Duplicate coordinates" };

function node(tag, text, className = "") {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function showError(message = "") {
  $("error").textContent = message;
  $("error").hidden = !message;
  if (message) $("error").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function busy(value) {
  state.busy = value;
  for (const element of document.querySelectorAll(".content button, .content select, .content input")) element.disabled = value;
  if (!value) {
    $("repair-button").disabled = !state.report || !$("filename").value;
    for (const element of document.querySelectorAll("[data-export], #download-audit")) element.disabled = !state.report;
    renderTable();
  }
}

function config() {
  return {
    csv: state.csv,
    columns: { latitude: $("latitude").value, longitude: $("longitude").value, identifier: $("identifier").value,
      filename: $("filename").value, groups: $("group").value ? [$("group").value] : [] },
    zero_missing: $("zero-missing").checked, repair: state.repair,
    max_gap: Number($("max-gap").value), max_distance: Number($("max-distance").value),
  };
}

async function api(path, payload, binary = false) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-GeoFlow": "1" }, body: JSON.stringify(payload) });
  if (!response.ok) {
    const failure = await response.json();
    throw new Error(failure.error || "The operation could not be completed.");
  }
  return binary ? response.blob() : response.json();
}

function options(id, headers, choices, optional = false) {
  const select = $(id);
  select.replaceChildren();
  if (optional) { const empty = node("option", "None"); empty.value = ""; select.append(empty); }
  for (const header of headers) { const option = node("option", header); option.value = header; select.append(option); }
  const normalized = (value) => value.toLowerCase().replace(/[\s_-]/g, "");
  const found = choices.map((choice) => headers.find((header) => normalized(header) === normalized(choice))).find(Boolean);
  if (found) select.value = found;
  else if (optional) select.value = "";
}

async function load(csv, name, isDemo) {
  if (state.busy) return;
  showError(); busy(true);
  try {
    const inspection = await api("/api/inspect", { csv });
    state.csv = csv; state.name = name; state.repair = false; state.page = 0;
    options("latitude", inspection.headers, ["latitude", "lat"]);
    options("longitude", inspection.headers, ["longitude", "lon", "lng", "long"]);
    options("identifier", inspection.headers, ["asset_id", "id", "image_id", "name"], true);
    options("filename", inspection.headers, ["filename", "file_path", "image_name", "image"], true);
    options("group", inspection.headers, ["route", "collection_id", "project_region_collection_id"], true);
    $("dataset-name").textContent = name;
    $("dataset-description").textContent = isDemo ? "Synthetic dataset · no real assets or client information" : "Your CSV · processed locally on this computer";
    $("search").value = ""; $("filter").value = "all";
    const settings = config();
    state.report = await api("/api/analyze", settings);
    state.appliedConfig = settings;
    render();
    $("announcement").textContent = `Loaded ${inspection.total} records. ${state.report.summary.invalid} have unusable coordinates.`;
  } catch (error) { state.report = null; state.appliedConfig = null; render(); showError(error.message); $("config-panel").hidden = false; $("toggle-config").setAttribute("aria-expanded", "true"); }
  finally { busy(false); }
}

async function analyze(repair = false) {
  if (state.busy || !state.csv) return;
  showError(); busy(true);
  const previousRepair = state.repair;
  state.repair = repair;
  try {
    const settings = config();
    const report = await api("/api/analyze", settings);
    state.report = report; state.appliedConfig = settings; state.page = 0; render();
    $("announcement").textContent = `${report.summary.valid} usable points. ${report.summary.estimated} estimated GPS values.`;
  } catch (error) { state.repair = previousRepair; showError(error.message); }
  finally { busy(false); }
}

function render() {
  const report = state.report;
  if (!report) {
    for (const id of ["metric-total", "metric-valid", "metric-flagged", "metric-estimated", "quality-score", "nav-total", "table-count"]) $(id).textContent = "—";
    $("metric-percent").textContent = "—"; $("quality-fill").style.width = "0%";
    $("issue-breakdown").replaceChildren(); $("reset-estimates").hidden = true;
    renderTable(); renderAudit(); drawPlot(); return;
  }
  const summary = report.summary;
  for (const key of ["total", "valid", "flagged", "estimated"]) $("metric-" + key).textContent = summary[key].toLocaleString();
  $("metric-percent").textContent = `${summary.quality_percent}%`;
  $("metric-errors").textContent = `${summary.invalid} unusable · ${summary.warning_rows} rows with warnings`;
  $("nav-total").textContent = summary.total;
  $("quality-score").textContent = summary.quality_percent;
  $("quality-fill").style.width = `${summary.quality_percent}%`;
  $("metric-total-note").textContent = `${report.crs} · decimal degrees`;
  $("reset-estimates").hidden = !state.repair;
  $("repair-note").textContent = state.repair ? `${summary.estimated} GPS gaps estimated. ${report.skipped_repairs.length} missing rows left unchanged. Review the audit before using estimates.` : "Estimate eligible missing GPS between original image anchors. Every change stays traceable.";
  $("issue-breakdown").replaceChildren();
  for (const [code, count] of Object.entries(report.issue_counts).sort((a, b) => b[1] - a[1]).slice(0, 4)) {
    const line = node("div", undefined, "issue-line"); const label = node("span");
    label.append(node("i"), node("span", issueLabels[code] || code)); line.append(label, node("strong", count)); $("issue-breakdown").append(line);
  }
  if (!Object.keys(report.issue_counts).length) $("issue-breakdown").append(node("div", "No coordinate issues found.", "issue-line"));
  renderTable(); renderAudit(); drawPlot();
}

function filteredRows() {
  if (!state.report) return [];
  const filter = $("filter").value; const query = $("search").value.trim().toLowerCase();
  return state.report.rows.filter((row) => (filter === "all" || filter === "issues" && row.issues.length || filter === "ready" && row.usable || filter === "estimated" && row.estimated)
    && (!query || Object.values(row.values).some((value) => value.toLowerCase().includes(query)) || row.status.includes(query)));
}

function renderTable() {
  const body = $("records-body"); body.replaceChildren();
  const rows = filteredRows(); const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)); state.page = Math.min(state.page, pages - 1);
  const start = state.page * PAGE_SIZE;
  $("identity-header").textContent = state.report?.columns.identifier || state.report?.columns.filename || "RECORD";
  for (const row of rows.slice(start, start + PAGE_SIZE)) {
    const tr = node("tr"); const columns = state.report.columns;
    const identity = row.values[columns.identifier] || row.values[columns.filename] || `Record ${row.row}`;
    const lat = row.values[columns.latitude]; const lon = row.values[columns.longitude];
    for (const value of [String(row.row).padStart(2, "0"), identity, lat || "—", lon || "—"]) tr.append(node("td", value));
    const status = node("td"); status.append(node("span", statusLabels[row.status], `status ${row.status}`)); tr.append(status);
    tr.append(node("td", row.issues.map((issue) => issue.message).join(" ") || (row.estimated ? "Estimated between original anchors; review the audit." : "Coordinates validated.")));
    body.append(tr);
  }
  if (!rows.length) { const td = node("td", state.report ? "No records match this filter." : "Import a CSV to review your records."); td.colSpan = 6; const tr = node("tr"); tr.append(td); body.append(tr); }
  $("table-count").textContent = rows.length;
  $("table-range").textContent = rows.length ? `Showing ${start + 1}–${Math.min(start + PAGE_SIZE, rows.length)} of ${rows.length} records` : "0 records";
  $("previous").disabled = state.busy || state.page === 0; $("next").disabled = state.busy || state.page >= pages - 1;
}

function renderAudit() {
  const target = $("audit-content"); target.replaceChildren(); $("skipped-content").replaceChildren();
  const repairs = state.report?.repairs || [];
  if (!repairs.length) target.append(node("div", state.repair ? "No rows met the configured estimation rules." : "Run “Estimate missing GPS” to view the complete change log.", "audit-empty"));
  else {
    const wrapper = node("div", undefined, "audit-table"); const table = node("table"); const head = node("thead"); const header = node("tr");
    for (const label of ["ROW", "ORIGINAL ANCHORS", "FRACTION", "ANCHOR DISTANCE", "ESTIMATED LAT / LON"]) header.append(node("th", label));
    head.append(header); const body = node("tbody");
    for (const repair of repairs) { const tr = node("tr");
      for (const value of [repair.row, `Rows ${repair.before_row} → ${repair.after_row}`, repair.fraction, `${repair.anchor_distance_m} m`, `${repair.latitude.toFixed(8)}, ${repair.longitude.toFixed(8)}`]) tr.append(node("td", value));
      body.append(tr);
    }
    table.append(head, body); wrapper.append(table); target.append(wrapper);
  }
  const skipped = state.report?.skipped_repairs || [];
  if (skipped.length) {
    const list = node("div", undefined, "skipped-list"); list.append(node("h3", "Missing GPS left unchanged"));
    for (const item of skipped) list.append(node("p", `Row ${item.row}: ${item.reason}`));
    $("skipped-content").append(list);
  }
}

function drawPlot() {
  const canvas = $("coordinate-plot"); const rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = rect.width * ratio; canvas.height = rect.height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const width = rect.width, height = rect.height; const rows = state.report?.rows.filter((row) => row.usable) || [];
  ctx.clearRect(0, 0, width, height); state.plotPoints = []; $("plot-empty").hidden = Boolean(rows.length);
  if (!rows.length) return;
  const left = 62, right = 21, top = 28, bottom = 35;
  const xs = rows.map((row) => row.longitude), ys = rows.map((row) => row.latitude);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const sx = Math.max(maxX - minX, 0.0002), sy = Math.max(maxY - minY, 0.0002);
  const bounds = { x0: minX - sx * .18, x1: maxX + sx * .18, y0: minY - sy * .18, y1: maxY + sy * .18 };
  ctx.lineWidth = .7; ctx.strokeStyle = "#dde4d5"; ctx.font = "8px system-ui"; ctx.fillStyle = "#9ba58f";
  for (let tick = 0; tick <= 4; tick++) {
    const x = left + (width - left - right) * tick / 4; const y = top + (height - top - bottom) * tick / 4;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, height - bottom); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - right, y); ctx.stroke();
    ctx.textAlign = "center"; ctx.fillText((bounds.x0 + (bounds.x1 - bounds.x0) * tick / 4).toFixed(4), x, height - 17);
    ctx.textAlign = "right"; ctx.fillText((bounds.y1 - (bounds.y1 - bounds.y0) * tick / 4).toFixed(4), left - 9, y + 3);
  }
  ctx.font = "7px system-ui"; ctx.textAlign = "center"; ctx.fillText("LONGITUDE", width / 2, height - 4);
  ctx.save(); ctx.translate(11, height / 2); ctx.rotate(-Math.PI / 2); ctx.fillText("LATITUDE", 0, 0); ctx.restore();
  for (const row of rows) {
    const x = left + (row.longitude - bounds.x0) / (bounds.x1 - bounds.x0) * (width - left - right);
    const y = top + (bounds.y1 - row.latitude) / (bounds.y1 - bounds.y0) * (height - top - bottom);
    const color = row.estimated ? "#8967c4" : row.issues.length ? "#ca994a" : "#628358";
    ctx.fillStyle = color + "15"; ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = color; ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, 3.6, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    state.plotPoints.push({ x, y, row });
  }
}

function switchView(view) {
  state.view = view;
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const nav of document.querySelectorAll("[data-view]")) nav.classList.toggle("active", nav.dataset.view === view);
  $("breadcrumb").textContent = { overview: "Overview", data: "Dataset", audit: "Repair audit" }[view];
  $("records-card").hidden = view === "audit";
  if (view === "overview") requestAnimationFrame(drawPlot);
}

async function download(format) {
  if (state.busy || !state.report) return;
  showError(); busy(true);
  try {
    const blob = await api("/api/export", { ...state.appliedConfig, format }, true);
    const url = URL.createObjectURL(blob); const anchor = node("a");
    anchor.href = url; anchor.download = { gpkg: "geoflow_points.gpkg", geojson: "geoflow_points.geojson", csv: "geoflow_audited.csv", report: "geoflow_qa_report.json" }[format];
    document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    $("announcement").textContent = "Download ready.";
  } catch (error) { showError(error.message); }
  finally { busy(false); }
}

async function loadDemo() {
  try { const response = await fetch("/demo.csv"); if (!response.ok) throw new Error("The demo could not be loaded."); await load(await response.text(), "synthetic_gps_survey.csv", true); }
  catch (error) { showError(error.message); }
}

$("choose-file").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 5 * 1024 * 1024) { showError("The maximum CSV size is 5 MiB."); event.target.value = ""; return; }
  await load(await file.text(), file.name, false); event.target.value = "";
});
$("load-demo").addEventListener("click", loadDemo);
$("toggle-config").addEventListener("click", () => { const visible = $("config-panel").hidden; $("config-panel").hidden = !visible; $("toggle-config").setAttribute("aria-expanded", String(visible)); });
$("apply-config").addEventListener("click", () => analyze(false));
$("repair-button").addEventListener("click", () => analyze(true));
$("reset-estimates").addEventListener("click", () => analyze(false));
$("search").addEventListener("input", () => { state.page = 0; renderTable(); });
$("filter").addEventListener("change", () => { state.page = 0; renderTable(); });
$("previous").addEventListener("click", () => { state.page--; renderTable(); });
$("next").addEventListener("click", () => { state.page++; renderTable(); });
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => switchView(button.dataset.view));
for (const button of document.querySelectorAll("[data-export]")) button.addEventListener("click", () => download(button.dataset.export));
$("download-audit").addEventListener("click", () => download("report"));
$("coordinate-plot").addEventListener("pointermove", (event) => {
  const rect = event.target.getBoundingClientRect(); const x = event.clientX - rect.left, y = event.clientY - rect.top;
  const point = state.plotPoints.find((item) => Math.hypot(item.x - x, item.y - y) < 9);
  const tooltip = $("plot-tooltip"); tooltip.hidden = !point;
  if (point) { tooltip.textContent = `Row ${point.row.row} · ${statusLabels[point.row.status]}`; tooltip.style.left = `${Math.min(x + 12, rect.width - 135)}px`; tooltip.style.top = `${Math.max(3, y - 28)}px`; }
});
$("coordinate-plot").addEventListener("pointerleave", () => $("plot-tooltip").hidden = true);
new ResizeObserver(() => drawPlot()).observe($("coordinate-plot"));
loadDemo();
