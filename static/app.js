"use strict";
const $ = (id) => document.getElementById(id);
let busy = false;
let selectedTable = null;
let availableTables = [];
let previewVersion = 0;
let currentAnswer = "";
let history = [];
const storageKey = "data-agent-requests-v1";
const pendingKey = "data-agent-pending-v1";

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function remember(key, value) {
  try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value)); } catch { /* Storage may be unavailable. */ }
}
function recall(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch { return fallback; }
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Please check your request and try again.");
  return data;
}
function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  setTimeout(() => { $("toast").hidden = true; }, 3500);
}
function setBusy(value) {
  busy = value;
  $("run-button").disabled = value;
  $("run-button").textContent = value ? "Working…" : "Run Request ↗";
  $("clear-request").disabled = value;
  $("new-request").disabled = value;
  $("question").disabled = value;
  $("remove-table").disabled = value;
  $("close-preview").disabled = value;
  document.querySelectorAll(".example,.table-select,.history-item,.extraction-delete").forEach(button => { button.disabled = value; });
  $("result-card").setAttribute("aria-busy", String(value));
}
function clearResult() {
  ["empty-result", "running-result", "error-result", "answer", "result-files", "result-footer", "copy-result", "result-agent"].forEach(id => { $(id).hidden = true; });
  $("result-files").replaceChildren();
  currentAnswer = "";
}
function clearRequest() {
  if (busy) return;
  $("question").value = "";
  chooseTable(null);
  clearResult();
  $("result-card").hidden = true;
  remember(pendingKey, null);
  $("question").focus();
}
function showRunningResult() {
  $("result-card").hidden = false;
  clearResult();
  $("running-result").hidden = false;
  $("running-note").textContent = "Understanding your request and choosing the right tools…";
}
async function chooseTable(table) {
  if (busy) return;
  const version = ++previewVersion;
  selectedTable = table;
  $("selected-table").hidden = !table;
  $("selected-table-label").textContent = table ? `Using ${table.schema}.${table.name}` : "";
  document.querySelectorAll(".table-select").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.table === table?.name));
  });
  $("table-preview").hidden = !table;
  $("empty-result").hidden = Boolean(table) || ["running-result", "error-result", "answer"].some(id => !$(id).hidden);
  $("preview-data").replaceChildren();
  $("preview-scroll").hidden = true;
  if (!table) return;
  $("preview-title").textContent = `${table.schema}.${table.name}`;
  $("preview-note").textContent = "Loading rows from PostgreSQL…";
  try {
    const data = await api("/api/database/preview?table=" + encodeURIComponent(table.name));
    if (version !== previewVersion) return;
    const caption = el("caption", `${data.schema}.${data.name} — up to ${data.limit} rows`, "sr-only");
    const head = el("thead");
    const headings = el("tr");
    data.columns.forEach(name => { const th = el("th", name); th.scope = "col"; headings.append(th); });
    head.append(headings);
    const body = el("tbody");
    data.rows.forEach(row => {
      const tr = el("tr");
      row.forEach(value => {
        const td = el("td", value === null ? "NULL" : value, value === null ? "null-value" : "");
        tr.append(td);
      });
      body.append(tr);
    });
    $("preview-data").append(caption, head, body);
    $("preview-scroll").hidden = false;
    $("preview-note").textContent = data.rows.length
      ? `${data.rows.length} rows shown${data.has_more ? ` · Preview limited to ${data.limit} rows` : ""}. Ask an agent to filter, count, or summarize this table.`
      : "This table is empty. Its column names are shown below.";
  } catch (error) {
    if (version === previewVersion) $("preview-note").textContent = error.message;
  }
}
function showResult(result) {
  $("result-card").hidden = false;
  clearResult();
  currentAnswer = result.answer;
  $("answer").textContent = currentAnswer;
  $("answer").hidden = false;
  $("copy-result").hidden = false;
  $("result-agent").textContent = result.agent === "sql" ? "SQL analyst" : result.agent === "etl" ? "ETL analyst" : "Data agent";
  $("result-agent").hidden = false;
  $("elapsed").textContent = `${result.seconds}s`;
  $("result-footer").hidden = false;
  const files = result.files || [];
  files.forEach(file => $("result-files").append(extractionLink(file)));
  $("result-files").hidden = !files.length;
}
function extractionLink(file) {
  const link = el("a", `↓ ${file.name}`, "extraction-download");
  link.href = "/api/extractions/download?path=" + encodeURIComponent(file.path);
  link.setAttribute("download", file.name);
  link.setAttribute("aria-label", `Download ${file.name}`);
  link.dataset.extractionPath = file.path;
  return link;
}
async function deleteExtraction(file, button) {
  if (busy || button.disabled) return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    await api("/api/extractions?path=" + encodeURIComponent(file.path), {method: "DELETE"});
    history = history.map(item => ({...item, result: {...item.result, files: (item.result.files || []).filter(saved => saved.path !== file.path)}}));
    remember(storageKey, history);
    drawHistory();
    document.querySelectorAll("[data-extraction-path]").forEach(link => {
      if (link.dataset.extractionPath === file.path) link.remove();
    });
    $("result-files").hidden = !$("result-files").children.length;
    await refreshExtractions();
  } catch (error) {
    toast(error.message);
    button.disabled = busy;
    button.setAttribute("aria-busy", "false");
  }
}
async function refreshExtractions() {
  $("refresh-extractions").disabled = true;
  try {
    const data = await api("/api/extractions");
    $("extractions-list").replaceChildren();
    $("extraction-count").textContent = data.files.length;
    if (!data.files.length) $("extractions-list").append(el("p", "Ask to extract table data as CSV to create your first file.", "tables-empty"));
    data.files.forEach(file => {
      const row = el("div", undefined, "extraction-item");
      const size = file.size < 1024 ? `${file.size} B` : file.size < 1048576 ? `${(file.size / 1024).toFixed(1)} KB` : `${(file.size / 1048576).toFixed(1)} MB`;
      const heading = el("div", undefined, "extraction-heading");
      const remove = el("button", undefined, "extraction-delete");
      remove.type = "button";
      remove.disabled = busy;
      remove.setAttribute("aria-label", `Delete ${file.name}`);
      remove.onclick = () => deleteExtraction(file, remove);
      heading.append(extractionLink(file), remove);
      row.append(heading, el("small", `${size} · ${new Date(file.modified / 1e6).toLocaleString()}`));
      $("extractions-list").append(row);
    });
  } catch (error) {
    $("extraction-count").textContent = "—";
    $("extractions-list").replaceChildren(el("p", `Could not load extracted data. ${error.message}`, "tables-empty"));
  } finally { $("refresh-extractions").disabled = false; }
}
function drawHistory() {
  $("history").replaceChildren();
  if (!history.length) $("history").append(el("p", "Your recent requests will appear here.", "sidebar-empty"));
  history.slice(0, 8).forEach(item => {
    const button = el("button", item.question, "history-item");
    button.title = item.question;
    button.disabled = busy;
    button.onclick = () => { if (busy) return; $("question").value = item.question; chooseTable(availableTables.find(table => table.name === item.table) || null); showResult(item.result); };
    $("history").append(button);
  });
}
async function refreshTables() {
  $("refresh-tables").disabled = true;
  try {
    const data = await api("/api/database/tables");
    availableTables = data.tables;
    $("tables-list").replaceChildren();
    $("table-count").textContent = data.tables.length;
    $("database-label").textContent = `${data.database} · ${data.schema} schema`;
    $("connection-label").textContent = "PostgreSQL connected";
    $("connection").classList.remove("warn");
    if (!data.tables.length) $("tables-list").append(el("p", "No readable tables in the public schema. Check the selected database and table permissions.", "tables-empty"));
    data.tables.forEach(item => {
      const row = el("div", undefined, "table-item");
      row.append(el("span", "▥", "table-icon"));
      const use = el("button", undefined, "table-select");
      use.disabled = busy;
      use.dataset.table = item.name;
      use.setAttribute("aria-pressed", String(selectedTable?.name === item.name));
      use.append(el("strong", item.name), el("small", `${item.schema} · ${item.column_count} columns`));
      use.title = `Preview ${item.schema}.${item.name}`;
      use.onclick = () => chooseTable(item);
      row.append(use);
      $("tables-list").append(row);
    });
    if (selectedTable && !busy) {
      const table = availableTables.find(item => item.name === selectedTable.name);
      await chooseTable(table || null);
    }
  } catch (error) {
    availableTables = [];
    $("connection-label").textContent = "PostgreSQL unavailable";
    $("connection").classList.add("warn");
    $("table-count").textContent = "—";
    $("database-label").textContent = "Connection could not be established.";
    $("tables-list").replaceChildren(el("p", error.message, "tables-empty"));
    if (!busy) await chooseTable(null);
  } finally { $("refresh-tables").disabled = false; }
}
async function poll(pending) {
  setBusy(true);
  showRunningResult();
  const start = Date.now();
  let failures = 0;
  try {
    while (Date.now() - start < 600_000) {
      let job;
      try { job = await api(`/api/requests/${pending.id}`); failures = 0; }
      catch (error) { if (++failures >= 3) throw error; await new Promise(resolve => setTimeout(resolve, 1200)); continue; }
      if (job.status === "failed") throw new Error(job.error);
      if (job.status === "completed") {
        showResult(job.result);
        history = [{question: pending.question, table: pending.table, result: job.result}, ...history].slice(0, 8);
        remember(storageKey, history);
        drawHistory();
        remember(pendingKey, null);
        return;
      }
      if (Date.now() - start > 20_000) $("running-note").textContent = "Still working. Requests can take a minute or two as agents call their tools.";
      await new Promise(resolve => setTimeout(resolve, 800));
    }
    throw new Error("This request is taking longer than expected. Reload the page to check it again.");
  } catch (error) {
    clearResult();
    $("error-result").textContent = error.message;
    $("error-result").hidden = false;
    // Preserve the ID on transport failures/timeouts so a refresh can resume polling.
    if (error.message.includes("could not be completed") || error.message.includes("no longer available")) remember(pendingKey, null);
  } finally { setBusy(false); await refreshExtractions(); }
}
$("request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const question = $("question").value.trim();
  if (!question) { $("question").focus(); return; }
  setBusy(true);
  showRunningResult();
  try {
    const table = selectedTable?.name || null;
    const accepted = await api("/api/requests", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({question, table})});
    const pending = {id: accepted.id, question, table};
    remember(pendingKey, pending);
    await poll(pending);
  } catch (error) { clearResult(); $("error-result").textContent = error.message; $("error-result").hidden = false; setBusy(false); }
});
$("question").addEventListener("keydown", event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("request-form").requestSubmit(); } });
$("new-request").onclick = clearRequest;
$("clear-request").onclick = clearRequest;
$("remove-table").onclick = () => chooseTable(null);
$("close-preview").onclick = () => chooseTable(null);
$("refresh-tables").onclick = refreshTables;
$("refresh-extractions").onclick = refreshExtractions;
$("clear-history").onclick = () => { history = []; remember(storageKey, history); drawHistory(); };
$("copy-result").onclick = async () => { try { await navigator.clipboard.writeText(currentAnswer); toast("Answer copied"); } catch { toast("Select the answer text and copy it with Ctrl+C."); } };
document.querySelectorAll(".example").forEach(button => button.onclick = () => {
  if (busy) return;
  const kind = button.dataset.example;
  if (kind === "sql") chooseTable(null);
  $("question").value = {
    sql: "What are the different payment methods available in the database?",
    extract: `Extract top 5 data from ${selectedTable ? JSON.stringify(selectedTable.name) : "ratings"} table and save it as CSV in data/extract.`,
    summary: selectedTable ? "How many rows are in the selected table? Show a summary of its data." : "Show completed payment totals, grouped by payment method."
  }[kind];
  $("question").focus();
});
async function init() {
  const saved = recall(storageKey, []);
  history = Array.isArray(saved) ? saved.filter(item => typeof item?.question === "string" && typeof item?.result?.answer === "string").slice(0, 8) : [];
  drawHistory();
  try {
    const status = await api("/api/status");
    if (!status.configured) { $("setup-note").textContent = `Add these settings to .env and restart the server: ${status.missing.join(", ")}.`; $("setup-note").hidden = false; }
  } catch { $("setup-note").textContent = "Could not check agent settings. Check the server and reload this page."; $("setup-note").hidden = false; }
  await Promise.allSettled([refreshTables(), refreshExtractions()]);
  const pending = recall(pendingKey, null);
  if (pending && typeof pending.id === "string" && typeof pending.question === "string") {
    $("question").value = pending.question;
    await chooseTable(availableTables.find(table => table.name === pending.table) || null);
    await poll(pending);
  }
}
init();
