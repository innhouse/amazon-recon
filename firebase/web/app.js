// Amazon invoices: Firebase Hosting front end.
// Reads Firestore (named database "amazon-recon") and Cloud Storage; builds zips in the browser.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup, onAuthStateChanged, signOut } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { getFirestore, collection, doc, getDoc, getDocs, query, where, orderBy, limit } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
import { getStorage, ref, getBlob } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-storage.js";

const DB_NAME = "amazon-recon";
const BUCKET = "gs://innprop-amazon-recon-data";
const $ = (s) => document.querySelector(s);

// Firebase Hosting serves the project's config at /__/firebase/init.json.
const config = await (await fetch("/__/firebase/init.json")).json();
const app = initializeApp(config);
const auth = getAuth(app);
const db = getFirestore(app, DB_NAME);
const storage = getStorage(app, BUCKET);

let summary = null;
let current = [];          // charges currently shown

// ---------- auth ----------
$("#btn-signin").onclick = async () => {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ hd: "innhouse.bc.ca", prompt: "select_account" });
  try { await signInWithPopup(auth, provider); }
  catch (e) { const el = $("#signin-error"); el.textContent = e.message; el.hidden = false; }
};
onAuthStateChanged(auth, async (user) => {
  const ok = user && /@innhouse\.bc\.ca$/i.test(user.email || "");
  $("#signin").hidden = !!ok;
  $("#nav").hidden = !ok;
  if (!ok) { if (user) await signOut(auth); showPage(null); return; }
  $("#who").innerHTML = `${user.email} · <a href="#" id="signout" style="color:#fff">sign out</a>`;
  $("#signout").onclick = (e) => { e.preventDefault(); signOut(auth); };
  await loadSummary();
  route();
});

// ---------- pages ----------
function showPage(name) {
  document.querySelectorAll(".page").forEach(p => p.hidden = true);
  document.querySelectorAll(".navbtn").forEach(a => a.classList.toggle("active", a.dataset.page === name));
  if (name) $(`#page-${name}`).hidden = false;
}
function route() {
  const h = location.hash.replace("#", "") || "home";
  if (h.startsWith("charges")) { showPage("charges"); const p = new URLSearchParams(h.split("?")[1] || ""); if (p.has("card") || p.has("from")) runFilter(p.get("card"), p.get("from"), p.get("to")); }
  else showPage("home");
}
window.addEventListener("hashchange", route);

async function loadSummary() {
  const snap = await getDoc(doc(db, "meta", "summary"));
  summary = snap.exists() ? snap.data() : null;
  const tiles = $("#tiles"); tiles.innerHTML = "";
  if (!summary) { tiles.innerHTML = `<p class="warn">No data has been synced yet. On the PC run: python -m app.cli sync</p>`; return; }
  const t = (n, l) => `<div class="tile"><b>${n ?? 0}</b><span>${l}</span></div>`;
  tiles.innerHTML = t(summary.charges, "card charges") + t(summary.orders, "orders") + t(summary.pdfs, "invoice PDFs stored") + t(summary.refunds, "refunds");
  $("#freshness").textContent = `Charges from ${summary.date_from || "?"} to ${summary.date_to || "?"} · last Amazon report ${summary.last_report_import || "never"} · last fetch ${summary.last_invoice_fetch || "never"} · synced ${summary.last_sync || "never"}`;
  const sel = $("#f-card"); sel.innerHTML = `<option value="">all cards</option>` + (summary.cards || []).map(c => `<option>${c}</option>`).join("");
}

// ---------- queries ----------
$("#form-filter").onsubmit = (e) => {
  e.preventDefault();
  const p = new URLSearchParams(); if ($("#f-card").value) p.set("card", $("#f-card").value); if ($("#f-from").value) p.set("from", $("#f-from").value); if ($("#f-to").value) p.set("to", $("#f-to").value);
  location.hash = "charges?" + p.toString();
};
$("#form-paste").onsubmit = async (e) => {
  e.preventDefault();
  const codes = [...$("#paste").value.matchAll(/AM(?:AZON|ZN)(?:\.CA|\.COM)?\s*(?:MKTP\s*CA)?\*\s*([A-Z0-9]{7,12})\b/gi)].map(m => m[1].toUpperCase());
  const amounts = [...$("#paste").value.matchAll(/\$?\s*(-?\d{1,3}(?:,\d{3})*\.\d{2})/g)].map(m => parseFloat(m[1].replace(/,/g, "")));
  showPage("charges");
  $("#charges-title").textContent = "Charges matching the pasted statement lines";
  const rows = [];
  for (let i = 0; i < codes.length; i += 30) {
    const chunk = codes.slice(i, i + 30);
    const qs = await getDocs(query(collection(db, "charges"), where("code", "in", chunk)));
    qs.forEach(d => rows.push(d.data()));
    const rs = await getDocs(query(collection(db, "refunds"), where("code", "in", chunk)));
    rs.forEach(d => rows.push({ ...d.data(), refund: true }));
  }
  const found = new Set(rows.map(r => r.code));
  const missing = codes.filter(c => !found.has(c));
  render(rows, missing.length ? `${missing.length} line(s) not found in the Business account (maybe personal account): ${missing.join(", ")}` : "");
  if (!codes.length) render([], `No statement codes recognised in the pasted text. Lines should look like "AMAZON* 5E66L7HX2 ... $106.39". ${amounts.length ? "Amounts alone are not enough to match reliably." : ""}`);
};

async function runFilter(card, from, to) {
  $("#charges-title").textContent = `Charges${card ? " on card " + card : ""}${from || to ? `, ${from || "start"} to ${to || "today"}` : ""}`;
  const clauses = [];
  if (card) clauses.push(where("card", "==", card));
  if (from) clauses.push(where("date", ">=", from));
  if (to) clauses.push(where("date", "<=", to));
  const qs = await getDocs(query(collection(db, "charges"), ...clauses, orderBy("date", "desc"), limit(1000)));
  const rows = []; qs.forEach(d => rows.push(d.data()));
  render(rows, "");
}

// ---------- rendering ----------
const money = (v) => v == null ? "" : v.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function render(rows, note) {
  current = rows;
  const tb = $("#charges-table tbody"); tb.innerHTML = "";
  for (const r of rows) {
    const item = (r.items && r.items[0]) ? `${r.items[0].quantity}x ${r.items[0].description.slice(0, 60)}${r.items.length > 1 ? ` (+${r.items.length - 1} more)` : ""}` : "";
    const inv = (r.invoice_files || []).length;
    tb.insertAdjacentHTML("beforeend", `<tr>
      <td>${r.date || ""}</td><td>${r.card || ""}</td><td class="mono">${r.code ? "AMAZON* " + r.code : ""}</td>
      <td class="num">${r.refund ? "-" : ""}${money(r.amount)} ${r.currency || ""}</td>
      <td class="mono">${r.order_id}</td><td class="small">${item}</td>
      <td>${inv ? `<span class="pill ok">${inv} stored</span>` : `<span class="pill none">none yet</span>`}</td></tr>`);
  }
  $("#charges-empty").hidden = rows.length > 0;
  const total = rows.reduce((a, r) => a + (r.refund ? -r.amount : r.amount || 0), 0);
  const orders = new Set(rows.map(r => r.order_id));
  const files = new Set(rows.flatMap(r => r.invoice_files || []));
  $("#result-summary").innerHTML = `<b>${rows.length}</b> charges, ${money(total)} total, <b>${orders.size}</b> orders, <b>${files.size}</b> invoice PDFs available.` + (note ? `<br><span class="warn">${note}</span>` : "");
  $("#result-actions").hidden = rows.length === 0;
  $("#btn-zip").disabled = files.size === 0;
}

// ---------- downloads ----------
$("#btn-zip").onclick = async () => {
  const files = [...new Set(current.flatMap(r => r.invoice_files || []))];
  const status = $("#zip-status"); const zip = new JSZip(); let n = 0;
  for (const path of files) {
    status.textContent = `fetching ${++n} of ${files.length}…`;
    try { zip.file(path.split("/").slice(1).join("/"), await getBlob(ref(storage, path))); }
    catch (e) { console.warn("missing", path, e); }
  }
  status.textContent = "building zip…";
  const blob = await zip.generateAsync({ type: "blob" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `amazon-invoices-${new Date().toISOString().slice(0, 10)}.zip`; a.click();
  status.textContent = `${files.length} invoices zipped.`;
};
$("#btn-csv").onclick = () => {
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [["charge_date", "card", "statement_code", "amount", "currency", "order_id", "item", "invoices_stored"].join(",")];
  for (const r of current) lines.push([r.date, r.card, r.code, (r.refund ? -r.amount : r.amount), r.currency, r.order_id, r.items?.[0]?.description || "", (r.invoice_files || []).length].map(esc).join(","));
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  a.download = "amazon-charges.csv"; a.click();
};
