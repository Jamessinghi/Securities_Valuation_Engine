// Securities Valuation Engine — wizard SPA
const S = {
  market: "", ticker: "", date: "", period: null,
  docs: {},        // slotId -> DocResult
  slots: [],       // slot definitions
  manual: {},      // field_key -> value
  marketOverrides: {},
  methodOverrides: {},
  summary: null,
  completingMethod: null,
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  setTimeout(() => t.classList.add("hidden"), 2600);
}
function goto(step) {
  $$(".screen").forEach((s) => s.classList.remove("active"));
  $(`#screen${step}`).classList.add("active");
  $$(".dot").forEach((d) => {
    const n = +d.dataset.step;
    d.classList.toggle("active", n === step);
    d.classList.toggle("done", n < step);
  });
  $("#exportWrap").classList.toggle("hidden", step !== 5);
}

// ---------- More information modal ----------
const moreInfoModal = $("#moreInfoModal");
const moreInfoBtn = $("#moreInfoBtn");
const closeMoreInfoBtn = $("#closeMoreInfo");
let modalReturnFocus = null;

function openMoreInfo() {
  modalReturnFocus = document.activeElement;
  moreInfoModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  closeMoreInfoBtn.focus();
}

function closeMoreInfo() {
  moreInfoModal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if (modalReturnFocus instanceof HTMLElement) modalReturnFocus.focus();
}

moreInfoBtn.addEventListener("click", openMoreInfo);
closeMoreInfoBtn.addEventListener("click", closeMoreInfo);
moreInfoModal.addEventListener("click", (e) => {
  if (e.target === moreInfoModal) closeMoreInfo();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !moreInfoModal.classList.contains("hidden")) closeMoreInfo();
});

// ---------- Row-level missing-input completion ----------
const completeModal = $("#completeModal");
const completeForm = $("#completeForm");
const closeCompleteBtn = $("#closeComplete");
const cancelCompleteBtn = $("#cancelComplete");

function openComplete(methodId) {
  const result = S.summary?.results?.find((r) => r.id === methodId);
  if (!result || !result.completion?.length) return;
  S.completingMethod = result;
  modalReturnFocus = document.activeElement;
  $("#completeTitle").textContent = result.name;
  $("#completeIntro").textContent = result.missing?.length
    ? `Still required: ${result.missing.join(", ")}.`
    : "Supply the external information required by this method.";
  $("#completeFields").innerHTML = result.completion.map((field, index) => `
    <div class="completion-field">
      <label for="completion-${index}">${field.label}</label>
      <small>${field.unit}</small>
      <input id="completion-${index}" type="number" step="any" inputmode="decimal"
        data-scope="${field.scope}" data-key="${field.key}" ${field.required ? "required" : ""} />
    </div>`).join("");
  completeModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  $("#completeFields input")?.focus();
}

function closeComplete() {
  completeModal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  S.completingMethod = null;
  if (modalReturnFocus instanceof HTMLElement) modalReturnFocus.focus();
}

closeCompleteBtn.addEventListener("click", closeComplete);
cancelCompleteBtn.addEventListener("click", closeComplete);
completeModal.addEventListener("click", (e) => { if (e.target === completeModal) closeComplete(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !completeModal.classList.contains("hidden")) closeComplete();
});

completeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const method = S.completingMethod;
  if (!method) return;
  const methodEntry = {};
  $$("#completeFields input").forEach((input) => {
    if (input.value === "") return;
    const value = Number(input.value);
    if (input.dataset.scope === "manual") S.manual[input.dataset.key] = value;
    else if (input.dataset.scope === "market") S.marketOverrides[input.dataset.key] = value;
    else methodEntry[input.dataset.key] = value;
  });
  if (Object.keys(methodEntry).length) S.methodOverrides[String(method.id)] = methodEntry;
  closeComplete();
  await compute({preserveTable: true});
  toast(`${method.name} updated; all methods recalculated.`);
});

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.status); }
  return r.json();
}

// ---------- STEP 1 ----------
$("#tickerInput").addEventListener("input", (e) => {
  const v = e.target.value.toUpperCase();
  const m = v.match(/^([A-Z]+)\s*:\s*([A-Z0-9.]+)$/);
  const hint = $("#tickerHint");
  if (m) { hint.textContent = `Market ${m[1]} · Ticker ${m[2]}`; hint.className = "hint ok"; }
  else { hint.textContent = "Format: MARKET:TICKER (e.g. ASX:STO)"; hint.className = "hint"; }
});
$("#next1").addEventListener("click", () => {
  const v = $("#tickerInput").value.toUpperCase();
  const m = v.match(/^([A-Z]+)\s*:\s*([A-Z0-9.]+)$/);
  if (!m) { $("#tickerHint").textContent = "Enter a valid MARKET:TICKER."; $("#tickerHint").className = "hint err"; return; }
  S.market = m[1]; S.ticker = m[2];
  goto(2);
  if (!$("#dateInput").value) $("#dateInput").value = new Date().toISOString().slice(0, 10);
  $("#dateInput").dispatchEvent(new Event("change"));
});

// ---------- STEP 2 ----------
$("#dateInput").addEventListener("change", async (e) => {
  const d = e.target.value; if (!d) return;
  try {
    const p = await api("/api/period", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ date: d }) });
    S.date = d; S.period = p;
    const chips = p.trailing.map((q) => `<span class="qchip">${q.label}</span>`).join("");
    $("#periodPreview").innerHTML = `
      <div class="row"><span class="muted">Valuation date</span><b>${p.valuation_date}</b></div>
      <div class="row"><span class="muted">Falls in</span><b>${p.current_label}</b></div>
      <div class="row"><span class="muted">Previous 4 completed quarters (order ${p.trailing_quarter_order.join(", ")})</span></div>
      <div class="qchips">${chips}</div>`;
    $("#periodPreview").classList.add("show");
  } catch (err) { toast("Could not resolve period: " + err.message); }
});
$("#next2").addEventListener("click", () => { if (!S.period) return toast("Pick a date first."); buildBubbles(); goto(3); });

// ---------- STEP 3: uploads ----------
function buildBubbles() {
  const p = S.period;
  const slots = [
    { id: "annual_report", title: "Annual Report / Appendix 4E", dt: "annual_report", sub: `${p.year - (p.quarter === 1 ? 1 : 0)} full-year` },
    { id: "half_year", title: "Half-Year Report / Appendix 4D", dt: "half_year", sub: "Interim financials" },
    { id: "results_presentation", title: "Full-Year Results Presentation", dt: "results_presentation", sub: "Guidance / FCF" },
  ];
  p.trailing.forEach((q, i) => slots.push({ id: `q${i}`, title: `${q.short} Quarterly Report`, dt: "quarterly", sub: `Trailing quarter ${i + 1} of 4` }));
  S.slots = slots;
  S.docs = {};
  $("#uploadSub").textContent = `${S.market}:${S.ticker} — the previous four quarters are ${p.trailing.map(q => q.short).join(", ")}. Drop each PDF; the OCR engine extracts the figures.`;
  const box = $("#bubbles");
  box.innerHTML = slots.map((s) => `
    <div class="bubble" id="bub-${s.id}" data-dt="${s.dt}">
      <h3>${s.title}</h3>
      <div class="dt">${s.sub}</div>
      <div class="drop" data-slot="${s.id}">⬆ Click or drop a PDF here</div>
      <input type="file" accept="application/pdf" class="file-in" data-slot="${s.id}" hidden />
      <div class="status" id="st-${s.id}"></div>
      <div class="fields" id="fl-${s.id}"></div>
    </div>`).join("");

  $$(".drop").forEach((d) => {
    const slot = d.dataset.slot;
    d.addEventListener("click", () => $(`.file-in[data-slot="${slot}"]`).click());
    const bub = $(`#bub-${slot}`);
    bub.addEventListener("dragover", (e) => { e.preventDefault(); bub.classList.add("drag"); });
    bub.addEventListener("dragleave", () => bub.classList.remove("drag"));
    bub.addEventListener("drop", (e) => {
      e.preventDefault(); bub.classList.remove("drag");
      if (e.dataTransfer.files[0]) uploadFile(slot, e.dataTransfer.files[0]);
    });
  });
  $$(".file-in").forEach((inp) => inp.addEventListener("change", (e) => {
    if (e.target.files[0]) uploadFile(e.target.dataset.slot, e.target.files[0]);
  }));
  refreshWarnings();
}

async function uploadFile(slot, file) {
  const bub = $(`#bub-${slot}`), st = $(`#st-${slot}`), fl = $(`#fl-${slot}`);
  bub.className = "bubble";
  st.innerHTML = `<span class="spinner"></span> Extracting with OCR…`;
  fl.innerHTML = "";
  const fd = new FormData();
  fd.append("file", file);
  fd.append("doc_type", bub.dataset.dt);
  try {
    const res = await api("/api/extract", { method: "POST", body: fd });
    S.docs[slot] = res;
    const nFields = Object.keys(res.fields || {}).length;
    if (res.missing_required && res.missing_required.length) {
      bub.classList.add("warn");
      st.className = "status warn";
      st.innerHTML = `⚠ Extracted ${nFields} fields · missing ${res.missing_required.length}
        <div class="reupload" data-slot="${slot}">↻ Re-upload</div>`;
    } else {
      bub.classList.add("ok");
      st.className = "status ok";
      st.innerHTML = `✓ ${res.filename} · ${nFields} fields extracted
        <div class="reupload" data-slot="${slot}">↻ Replace</div>`;
    }
    fl.innerHTML = Object.entries(res.fields || {}).slice(0, 8)
      .map(([k, v]) => `${k}: ${Number(v.value).toLocaleString()}`).join(" · ");
    $(`.reupload[data-slot="${slot}"]`)?.addEventListener("click", () => $(`.file-in[data-slot="${slot}"]`).click());
  } catch (err) {
    st.className = "status warn"; st.textContent = "Extraction failed: " + err.message;
  }
  refreshWarnings();
}

function collectMissing() {
  const map = {};
  Object.entries(S.docs).forEach(([slot, res]) => {
    (res.missing_required || []).forEach((m) => {
      if (!map[m.key]) map[m.key] = { key: m.key, label: m.label, docs: [] };
      map[m.key].docs.push(res.filename);
    });
  });
  return Object.values(map);
}
function refreshWarnings() {
  const missing = collectMissing();
  const box = $("#warnBox");
  if (!missing.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  $("#warnList").innerHTML = missing.map((m) =>
    `<li><b>${m.label}</b> — not found in ${m.docs.join(", ")}</li>`).join("");
}
$("#manualBtn").addEventListener("click", () => { buildManualForm(); goto(4); });
$("#next3").addEventListener("click", () => compute());

// ---------- STEP 4: manual ----------
function buildManualForm() {
  const missing = collectMissing();
  const form = $("#manualForm");
  if (!missing.length) { form.innerHTML = `<p class="muted">Nothing missing — you can compute directly.</p>`; return; }
  form.innerHTML = missing.map((m) => `
    <div class="field">
      <label for="mi-${m.key}">${m.label}</label>
      <input id="mi-${m.key}" data-key="${m.key}" type="number" step="any" placeholder="enter value" />
    </div>`).join("");
}
$("#next4").addEventListener("click", () => {
  S.manual = {};
  $$("#manualForm input").forEach((i) => { if (i.value !== "") S.manual[i.dataset.key] = i.value; });
  compute();
});

// ---------- compute + STEP 5 ----------
async function compute({preserveTable = false} = {}) {
  goto(5);
  $("#intrinsicBox").innerHTML = `<span class="spinner"></span> Running 65 valuation methods & fetching market data…`;
  if (!preserveTable) $("#resultsTable").querySelector("tbody").innerHTML = "";
  try {
    const body = {market: S.market, ticker: S.ticker, date: S.date, docs: Object.values(S.docs),
      manual: S.manual, market_overrides: S.marketOverrides, method_overrides: S.methodOverrides, use_market: true};
    const sum = await api("/api/compute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    S.summary = sum;
    renderSummary(sum);
  } catch (err) { $("#intrinsicBox").innerHTML = `<span class="lbl">Compute failed: ${err.message}</span>`; }
}

function fmt(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return v;
}
function renderSummary(sum) {
  $("#summaryTitle").textContent = `${sum.meta.market}:${sum.meta.ticker}`;
  const fxNote = (sum.meta.reporting_currency && sum.meta.reporting_currency !== sum.meta.currency)
    ? ` · ${sum.meta.reporting_currency}→${sum.meta.currency} @ ${sum.meta.fx}${sum.meta.fx_live ? "" : " (fallback)"}`
    : "";
  $("#summaryMeta").textContent = `Valuation date ${sum.meta.date} · ${sum.meta.current_label} · priced in ${sum.meta.currency || "AUD"}${fxNote}`;
  const c = sum.counts || {};
  $("#counts").innerHTML =
    `<span class="pill ok">${c.ok || 0} computed</span>
     <span class="pill partial">${c.partial || 0} partial</span>
     <span class="pill na">${c.na || 0} needs external data</span>`;
  const tb = $("#resultsTable").querySelector("tbody");
  tb.innerHTML = sum.results.map((r) => `
    <tr>
      <td>${r.id}</td>
      <td>${r.name}</td>
      <td class="note">${r.section.replace(/^\d+\.\s*/, "")}</td>
      <td><span class="badge ${r.status}">${r.status === "na" ? "N/A" : r.status.toUpperCase()}</span></td>
      <td>${fmt(r.value)}${r.unit ? " <span class='note'>" + r.unit + "</span>" : ""}</td>
      <td>${fmt(r.intrinsic_ps)}</td>
      <td class="note">${r.note || (r.missing && r.missing.length ? "needs: " + r.missing.join(", ") : "")}</td>
      <td>${r.completion && r.completion.length ? `<button class="complete-btn" data-method-id="${r.id}">Complete?</button>` : ""}</td>
    </tr>`).join("");
  $$(".complete-btn").forEach((button) => button.addEventListener("click", () => openComplete(Number(button.dataset.methodId))));

  const iv = sum.intrinsic_value_per_share, cur = sum.intrinsic_currency;
  let verdict = "";
  if (sum.verdict) {
    const up = (sum.verdict.upside * 100).toFixed(1);
    const dir = sum.verdict.upside >= 0 ? "undervalued" : "overvalued";
    verdict = `<div class="verdict">Market price ${sum.verdict.currency_price} ${fmt(sum.verdict.price)}<br>
      <b>${up}% ${dir}</b>${sum.verdict.comparable ? "" : "<br><small>⚠ currency mismatch — convert to compare</small>"}</div>`;
  } else {
    verdict = `<div class="verdict"><small>Add a market price (enter a valid ticker & connect API keys)<br>to compare against market.</small></div>`;
  }
  $("#intrinsicBox").innerHTML = `
    <div><div class="lbl">Triangulated intrinsic value (median of ${sum.n_intrinsic_families || 0} independent families; ${sum.n_intrinsic_models} models)</div>
      <div class="big">${cur} ${fmt(iv)} / share</div>
      ${sum.assumptions && sum.assumptions.length ? "<div class='lbl'>Assumptions: " + sum.assumptions.join("; ") + "</div>" : ""}
    </div>${verdict}`;
}

// ---------- export dropdown ----------
$("#exportBtn").addEventListener("click", () => $("#exportMenu").classList.toggle("hidden"));
document.addEventListener("click", (e) => {
  if (!e.target.closest("#exportWrap")) $("#exportMenu").classList.add("hidden");
});
// Debug/testing hook (harmless): lets an automated check render a summary.
window.SVE = { get state() { return S; }, renderSummary, goto,
  setSummary(s) { S.summary = s; renderSummary(s); goto(5); } };

$$("#exportMenu button").forEach((b) => b.addEventListener("click", async () => {
  if (!S.summary) return;
  $("#exportMenu").classList.add("hidden");
  toast("Preparing " + b.dataset.fmt.toUpperCase() + "…");
  const r = await fetch("/api/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format: b.dataset.fmt, summary: S.summary, meta: S.summary.meta }),
  });
  if (!r.ok) return toast("Export failed");
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `valuation-${S.ticker}-${S.date}.${b.dataset.fmt === "pdf" ? "pdf" : "jpg"}`;
  a.click(); URL.revokeObjectURL(url);
}));
