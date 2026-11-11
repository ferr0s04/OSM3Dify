// Aplicação de revisão: lista + mapa + 3D + edição de tags.
import { api } from "./api.js";
import { CHANGE_COLORS, CHANGE_LABELS, SHAPE_LABELS } from "./colors.js";
import { BuildingMap } from "./map.js";
import { renderResults } from "./results.js";
import { Viewer } from "./viewer.js";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (v, unit = "") => (v === null || v === undefined ? "—" : `${v}${unit}`);
const SHAPES = ["flat", "skillion", "gabled", "hipped", "side_hipped", "pyramidal", "unknown"];
const OVERRIDE_FIELDS = ["ground", "roof_shape", "height", "roof_height", "roof_orientation", "roof_direction", "roof_colour", "building_colour"];
const COLOUR_SOURCE = { "omissão": "sem tag: cor por omissão", OSM: "já no OSM", estimada: "estimada (LiDAR)", manual: "corrigida" };

// Converte qualquer cor CSS (p.ex. "red", do OSM) em #rrggbb para os campos de cor.
function toHex(css) {
  const ctx = toHex.ctx ?? (toHex.ctx = document.createElement("canvas").getContext("2d"));
  ctx.fillStyle = "#000000";
  ctx.fillStyle = css ?? "#000000";
  return ctx.fillStyle;
}

const state = {
  rows: [],
  filtered: [],
  selected: null,
  detail: null,
  touched: new Set(),
  token: 0,
};

const viewer = new Viewer($("#viewer"));
const map = new BuildingMap("map", $("#legend"), (key) => select(key));

function toast(msg, ms = 3500) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => el.classList.add("hidden"), ms);
}

// ------------------------------------------------------------------ lista

function priority(r) {
  // Sinalizados e de baixa confiança primeiro; revistos no fim.
  const reviewed = r.status !== "pending" ? 10 : 0;
  return reviewed + (r.needs_review ? 0 : 2) + (r.shape_conf ?? 0);
}

function applyFilters() {
  const q = $("#f-search").value.trim();
  const status = $("#f-status").value;
  const shape = $("#f-shape").value;
  const flagged = $("#f-flagged").checked;
  const mismatch = $("#f-mismatch").checked;
  const showArchived = $("#f-archived").checked;
  const osm = $("#f-osm").value;
  const sort = $("#f-sort").value;
  let rows = state.rows.filter(
    (r) =>
      (!q || r.key.includes(q)) &&
      (!status || r.status === status) &&
      (!shape || (r.shape ?? "unknown") === shape) &&
      (!flagged || r.needs_review) &&
      (!mismatch || r.match === false) &&
      (!osm || (osm === "osm3d" ? r.osm3d : r.change === osm)) &&
      (showArchived || !r.archived),
  );
  const by = {
    priority: (a, b) => priority(a) - priority(b),
    conf: (a, b) => (a.shape_conf ?? -1) - (b.shape_conf ?? -1),
    height: (a, b) => (b.height ?? 0) - (a.height ?? 0),
    area: (a, b) => (b.area ?? 0) - (a.area ?? 0),
    key: (a, b) => a.key.localeCompare(b.key, undefined, { numeric: true }),
  }[sort];
  rows = rows.sort(by);
  state.filtered = rows;
  renderList();
}

function renderList() {
  const MAX = 2000;
  const rows = state.filtered.slice(0, MAX);
  $("#list-count").textContent =
    `${state.filtered.length} de ${state.rows.length} edifícios` + (state.filtered.length > MAX ? ` (a mostrar ${MAX})` : "");
  $("#list").innerHTML = rows
    .map((r) => {
      const shape = r.shape ?? "unknown";
      const flags = r.flags.length ? `<span class="chip warn">${r.flags.length} ⚑</span>` : "";
      const osmBadge = r.osm3d
        ? `<span class="chip osm" title="Já tem 3D no OSM: ${CHANGE_LABELS[r.change]}">OSM 3D ${{ diverge: "≠", complementa: "+", igual: "=" }[r.change] ?? ""}</span>`
        : "";
      const osm = r.osm_roof_shape ? ` · OSM ${esc(r.osm_roof_shape)}${r.match === false ? " ≠" : ""}` : "";
      return `<li data-key="${r.key}" class="${r.key === state.selected ? "sel" : ""}">
        <span class="dot ${r.status}"></span>
        <div><div class="key">${r.key}${r.edited ? " ✎" : ""}</div>
          <div class="meta"><span class="sw" style="background:${r.roof_colour}" title="roof:colour ${esc(r.roof_colour)}"></span> ${SHAPE_LABELS[shape]}
          ${r.shape_conf !== null ? `(${Math.round(r.shape_conf * 100)}%)` : ""} · ${fmt(r.height, " m")}${osm}</div></div>
        <div class="badges">${osmBadge}${flags}</div></li>`;
    })
    .join("");
}

function renderProgress() {
  const c = { pending: 0, accepted: 0, rejected: 0 };
  let archived = 0;
  state.rows.forEach((r) => (r.archived ? archived++ : c[r.status]++));
  $("#progress").innerHTML =
    `<span>por rever <b>${c.pending}</b></span><span style="color:var(--ok)">aceites <b>${c.accepted}</b></span>` +
    `<span style="color:var(--bad)">rejeitados <b>${c.rejected}</b></span>` +
    `<span class="muted" title="Aceites já submetidos e rejeitados arquivados">arquivados <b>${archived}</b></span>`;
}

function move(delta) {
  const list = state.filtered;
  if (!list.length) return;
  const i = list.findIndex((r) => r.key === state.selected);
  const next = list[Math.min(list.length - 1, Math.max(0, i + delta))] ?? list[0];
  select(next.key);
}

// ------------------------------------------------------------------ seleção

async function select(key) {
  if (!key) return;
  const token = ++state.token;
  state.selected = key;
  document.querySelectorAll("#list li").forEach((li) => li.classList.toggle("sel", li.dataset.key === key));
  document.querySelector(`#list li[data-key="${CSS.escape(key)}"]`)?.scrollIntoView({ block: "nearest" });
  $("#viewer-status").textContent = "a carregar…";
  viewer.clearAll();
  try {
    const [detail, mesh] = await Promise.all([api.detail(key), api.mesh(key)]);
    if (token !== state.token) return;
    state.detail = detail;
    state.touched = new Set(OVERRIDE_FIELDS.filter((f) => detail.overrides[f] !== null));
    map.select(key, detail.bbox);
    renderDetail();
    viewer.setModel(mesh);
    viewer.frame(mesh);
    $("#viewer-status").textContent = "a carregar pontos LiDAR… (a 1.ª vez por quadrícula demora)";
    const pts = await api.points(key);
    if (token !== state.token) return;
    viewer.setPoints(pts);
    $("#viewer-status").textContent = `${pts.count.toLocaleString("pt-PT")} pontos · rmse ${fmt(detail.effective?.rmse_m, " m")}`;
  } catch (e) {
    if (token === state.token) $("#viewer-status").textContent = `erro: ${e.message}`;
  }
}

function overrides() {
  const out = Object.fromEntries(OVERRIDE_FIELDS.map((f) => [f, null]));
  for (const f of state.touched) {
    const el = document.querySelector(`[name="${f}"]`);
    if (!el || el.value === "") continue;
    out[f] = ["height", "roof_height", "roof_direction"].includes(f) ? Number(el.value) : el.value;
  }
  return out;
}

function renderGround(d, val, t) {
  // Encosta/patamar: escolher a partir de que terreno se mede height.
  const g = d.ground;
  if (!g || !g.is_slope) return "";
  const sel = val("ground", g.choice);
  const opt = (c, label) =>
    `<option value="${c}" ${sel === c ? "selected" : ""}>${label}: chão ${g[c + "_m"]} m → height ${g.heights[c]} m</option>`;
  return `<label class="full ground ${t("ground")}">terreno de referência (desnível de ${g.step_m} m)
      <span class="est">${esc(g.reason)}</span>
      <select name="ground">
        ${opt("low", "baixo (ponto mais baixo, p.ex. estrada)")}
        ${opt("high", "lote (terreno dominante junto às paredes)")}
      </select></label>`;
}

function renderOsmChange(d) {
  // Destaque da relação com o OSM: já há 3D mapeado? a análise acrescenta ou diverge?
  const c = d.osm_change;
  if (!c) return "";
  const kind = c.kind;
  const existing = Object.entries(c.existing_3d);
  const conflicts = Object.entries(d.tags.conflicts);
  const added = Object.keys(d.tags.added);
  const text = {
    novo: "Sem informação 3D no OSM: todas as tags propostas são novas.",
    complementa: "Já tem 3D no OSM. A análise acrescenta tags em falta sem contradizer as existentes.",
    diverge: "Já tem 3D no OSM e a análise <b>propõe valores diferentes</b>. A exportação mantém os do OSM; confirmar no terreno/imagem antes de alterar.",
    igual: "Já tem 3D no OSM e a análise não acrescenta nada.",
    "sem proposta": "Sem reconstrução para este edifício.",
  }[kind];
  return `<div class="osm-box" style="border-color:${CHANGE_COLORS[kind]}">
    <div><span class="chip" style="background:${CHANGE_COLORS[kind]};color:white">${esc(CHANGE_LABELS[kind])}</span> ${text}</div>
    ${existing.length ? `<div class="small">No OSM: ${existing.map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</div>` : ""}
    ${c.osm_parts.length ? `<div class="small">Já tem building:part no OSM: ${c.osm_parts.map(esc).join(", ")}</div>` : ""}
    ${conflicts.length ? `<div class="small">Diverge: ${conflicts.map(([k, v]) => `<code>${esc(k)}</code> OSM <b>${esc(v.osm)}</b> → proposto <b>${esc(v.proposed)}</b>`).join("; ")}</div>` : ""}
    ${added.length && existing.length ? `<div class="small">Acrescenta: ${added.map((k) => `<code>${esc(k)}</code>`).join(" ")}</div>` : ""}
  </div>`;
}

function renderTags(tags, existing) {
  const keys = [...new Set([...Object.keys(existing), ...Object.keys(tags.proposed)])]
    .filter((k) => k.startsWith("roof") || k.startsWith("building") || k.includes("height"))
    .sort();
  return `<table class="kv"><tr><th>tag</th><th>OSM atual</th><th>proposto</th><th>final</th></tr>
    ${keys
      .map((k) => {
        const cls = tags.conflicts[k] ? "conflict" : tags.added[k] !== undefined ? "added" : "";
        return `<tr class="${cls}"><td>${esc(k)}</td><td>${esc(existing[k] ?? "")}</td>
          <td>${esc(tags.proposed[k] ?? "")}</td><td class="final">${esc(tags.final[k] ?? "")}</td></tr>`;
      })
      .join("")}</table>
    ${Object.keys(tags.conflicts).length ? `<p class="small muted">Conflitos: mantém-se o valor do OSM (não sobrescrever trabalho de mapeadores).</p>` : ""}`;
}

function renderDetail() {
  const d = state.detail;
  const eff = d.effective ?? {};
  const est = d.estimated ?? {};
  const ov = d.overrides;
  const val = (f, fallback) => (ov[f] !== null ? ov[f] : fallback ?? "");
  const t = (f) => (state.touched.has(f) ? "touched" : "");
  const shapeOpts = (sel) => SHAPES.map((s) => `<option value="${s}" ${s === sel ? "selected" : ""}>${SHAPE_LABELS[s]} (${s})</option>`).join("");
  const statusChip = { pending: "", accepted: "ok", rejected: "bad" }[d.status];
  const josmType = d.osm_type === "way" ? "w" : "r";
  $("#detail").innerHTML = `
    <h2>${d.key} <span class="chip ${statusChip}">${{ pending: "por rever", accepted: "aceite", rejected: "rejeitado" }[d.status]}</span>
      ${d.archived ? '<span class="chip" title="Arquivado: fora da lista e das exportações">arquivado</span>' : ""}
      ${d.edited ? '<span class="chip">corrigido</span>' : ""}</h2>
    <div class="links">
      <a href="${d.osm_url}" target="_blank" rel="noopener">abrir no OSM ↗</a>
      <a href="#" id="josm-link" data-obj="${josmType}${d.osm_id}">carregar no JOSM</a>
      <span class="muted">v${d.osm_version} · ${esc(d.existing_tags.building ?? "")} · ${fmt(d.area, " m²")}</span>
    </div>
    ${renderOsmChange(d)}
    <div class="actions">
      <button class="btn ok" id="b-accept" title="A">Aceitar${state.touched.size ? " com correções" : ""}</button>
      <button class="btn bad" id="b-reject" title="R">Rejeitar</button>
      <button class="btn" id="b-reset" title="U" ${d.reviewed_at ? "" : "disabled"}>Repor</button>
    </div>

    <h3>Correções</h3>
    <form class="form" id="edit-form" autocomplete="off">
      ${renderGround(d, val, t)}
      <label class="${t("roof_shape")}">forma do telhado <span class="est">estimado: ${fmt(est.roof_shape)} (${fmt(est.roof_shape_conf)})</span>
        <select name="roof_shape">${shapeOpts(val("roof_shape", eff.roof_shape))}</select></label>
      <label class="${t("height")}">height (m) <span class="est">estimado: ${fmt(est.height)}</span>
        <input name="height" type="number" step="0.5" min="0" value="${val("height", eff.height)}"></label>
      <label class="${t("roof_height")}">roof:height (m) <span class="est">estimado: ${fmt(est.roof_height)}</span>
        <input name="roof_height" type="number" step="0.5" min="0" value="${val("roof_height", eff.roof_height)}"></label>
      <label class="${t("roof_orientation")}">roof:orientation <span class="est">estimado: ${fmt(est.roof_orientation)}</span>
        <select name="roof_orientation">
          <option value="">—</option>
          <option value="along" ${val("roof_orientation", eff.roof_orientation) === "along" ? "selected" : ""}>along (cumeeira ao longo)</option>
          <option value="across" ${val("roof_orientation", eff.roof_orientation) === "across" ? "selected" : ""}>across (cumeeira através)</option>
        </select></label>
      <label class="${t("roof_direction")}">roof:direction (°, uma água) <span class="est">estimado: ${fmt(est.roof_direction)}</span>
        <input name="roof_direction" type="number" step="1" min="0" max="359" value="${val("roof_direction", eff.roof_direction)}"></label>
      <label class="${t("roof_colour")}">roof:colour <span class="est">${COLOUR_SOURCE[d.colours.roof_source]}${est.roof_colour_conf !== null && est.roof_colour_conf !== undefined ? ` (${Math.round(est.roof_colour_conf * 100)}%)` : ""}</span>
        <input name="roof_colour" type="color" list="palette" value="${toHex(val("roof_colour", d.colours.roof))}"></label>
      <label class="${t("building_colour")}">building:colour <span class="est">${COLOUR_SOURCE[d.colours.walls_source]}</span>
        <input name="building_colour" type="color" value="${toHex(val("building_colour", d.colours.walls))}"></label>
      <label class="full">nota<textarea name="note" rows="2">${esc(d.note ?? "")}</textarea></label>
    </form>

    <h3>Tags</h3>
    <div id="tags">${renderTags(d.tags, d.existing_tags)}</div>

    <h3>Sinalizações</h3>
    <div class="flags">${d.qa_flags.length ? d.qa_flags.map((f) => `<div><span class="chip ${f.severity === "info" ? "" : f.severity === "error" ? "bad" : "warn"}">${esc(f.code)}</span> ${esc(f.message)}</div>`).join("") : '<span class="muted">nenhuma</span>'}</div>

    <h3>Diagnóstico</h3>
    <table class="kv">
      ${Object.entries(d.diagnostics).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${fmt(v)}</td></tr>`).join("")}
      <tr><th>cota do terreno</th><td>${fmt(d.ground_m, " m")}</td></tr>
      <tr><th>rmse do ajuste</th><td>${fmt(est.rmse_m, " m")}</td></tr>
      <tr><th>cumeeira (hipped)</th><td>${est.ridge_ratio !== null && est.ridge_ratio !== undefined ? `${Math.round(est.ridge_ratio * 100)} % do comprimento` : "—"}</td></tr>
      <tr><th>co-registo</th><td>dx ${d.offset[0]} m, dy ${d.offset[1]} m</td></tr>
    </table>`;

  $("#b-accept").onclick = () => review("accepted");
  $("#b-reject").onclick = () => review("rejected");
  $("#b-reset").onclick = () => resetReview();
  $("#josm-link").onclick = (e) => {
    e.preventDefault();
    const obj = e.target.dataset.obj;
    fetch(`http://127.0.0.1:8111/load_object?objects=${obj}&relation_members=true`, { mode: "no-cors" })
      .then(() => toast("Pedido enviado ao JOSM (requer o controlo remoto ativo)."))
      .catch(() => toast("JOSM não respondeu: está aberto com o controlo remoto ativo?"));
  };
  const form = $("#edit-form");
  form.addEventListener("input", (e) => {
    const name = e.target.name;
    if (!OVERRIDE_FIELDS.includes(name)) return;
    state.touched.add(name);
    e.target.closest("label").classList.add("touched");
    $("#b-accept").textContent = "Aceitar com correções";
    schedulePreview();
  });
}

function schedulePreview() {
  clearTimeout(schedulePreview.t);
  schedulePreview.t = setTimeout(async () => {
    const key = state.selected;
    const token = state.token;
    try {
      const p = await api.preview(key, overrides());
      if (token !== state.token) return;
      viewer.setModel(p.mesh);
      $("#tags").innerHTML = renderTags(p.tags, state.detail.existing_tags);
      // Mudar o terreno de referência recalcula a altura (se não foi corrigida à mão).
      const h = document.querySelector('[name="height"]');
      if (h && !state.touched.has("height") && p.effective) h.value = p.effective.height;
    } catch (e) {
      toast(`Pré-visualização falhou: ${e.message}`);
    }
  }, 250);
}

function patchRow(detail) {
  const i = state.rows.findIndex((r) => r.key === detail.key);
  if (i >= 0) state.rows[i] = { ...state.rows[i], ...Object.fromEntries(Object.keys(state.rows[i]).map((k) => [k, detail[k]])) };
}

async function afterReviewChange(detail, advance) {
  patchRow(detail);
  renderProgress();
  const current = state.selected;
  const idx = state.filtered.findIndex((r) => r.key === current);
  applyFilters();
  map.setData(await api.geojson());
  if (advance) {
    // Avança para o próximo da lista filtrada (o atual pode ter saído do filtro).
    const next = state.filtered[Math.min(idx, state.filtered.length - 1)];
    const target = next && next.key !== current ? next : state.filtered[idx + 1];
    if (target) return select(target.key);
  }
  select(current);
}

async function review(status) {
  if (!state.detail) return;
  const note = document.querySelector('[name="note"]')?.value ?? null;
  try {
    const detail = await api.saveReview(state.selected, status, note, overrides());
    toast(`${detail.key}: ${status === "accepted" ? "aceite" : "rejeitado"}`, 1500);
    await afterReviewChange(detail, true);
  } catch (e) {
    toast(`Erro ao guardar: ${e.message}`);
  }
}

async function resetReview() {
  if (!state.detail) return;
  const detail = await api.resetReview(state.selected);
  toast(`${detail.key}: revisão reposta`, 1500);
  await afterReviewChange(detail, false);
}

// ------------------------------------------------------------- arranque

function wire() {
  $("#f-archived").addEventListener("input", (e) => {
    map.setShowArchived(e.target.checked);
    applyFilters();
  });
  $("#archive-action").addEventListener("input", (e) => archiveAction(e.target));
  ["#f-search", "#f-status", "#f-shape", "#f-osm", "#f-flagged", "#f-mismatch", "#f-sort"].forEach((s) =>
    $(s).addEventListener("input", applyFilters),
  );
  $("#list").addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (li) select(li.dataset.key);
  });
  $("#m-color").addEventListener("input", (e) => map.setColorMode(e.target.value));
  $("#m-3d").addEventListener("input", (e) => map.set3D(e.target.checked));
  $("#v-color").addEventListener("input", (e) => viewer.setColorMode(e.target.value));
  $("#v-points").addEventListener("input", (e) => viewer.setVisible("points", e.target.checked));
  $("#v-context").addEventListener("input", (e) => viewer.setVisible("context", e.target.checked));
  $("#v-model").addEventListener("input", (e) => viewer.setVisible("model", e.target.checked));
  $("#v-transparent").addEventListener("input", (e) => viewer.setTransparent(e.target.checked));

  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      const results = tab.dataset.tab === "results";
      $("#view-review").classList.toggle("hidden", results);
      $("#view-results").classList.toggle("hidden", !results);
      if (results) {
        $("#view-results").innerHTML = "<p class='muted'>A calcular…</p>";
        renderResults($("#view-results"), await api.summary());
      }
    }),
  );

  $("#btn-export").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api.export();
      toast(`${r.exported} edifícios exportados (${r.conflicts} com conflitos) → ${r.path}`, 8000);
    } catch (err) {
      toast(`Exportação falhou: ${err.message}`);
    } finally {
      e.target.disabled = false;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (k === "k" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
    else if (k === "a") review("accepted");
    else if (k === "r") review("rejected");
    else if (k === "u") resetReview();
  });
}

async function archiveAction(select) {
  const action = select.value;
  select.value = "";
  if (!action) return;
  const unarchive = action.startsWith("unarchive-");
  const status = unarchive ? action.slice("unarchive-".length) : action;
  const n = state.rows.filter((r) => r.status === status && r.archived === unarchive).length;
  const label = status === "accepted" ? "aceites" : "rejeitados";
  if (!n) return toast(`Não há ${label} para ${unarchive ? "desarquivar" : "arquivar"}.`);
  const msg = unarchive
    ? `Desarquivar ${n} ${label}? Voltam à lista e ao mapa${status === "accepted" ? " e às exportações" : ""}.`
    : status === "accepted"
      ? `Arquivar ${n} aceites?\n\nFaça-o só depois de os ter exportado e submetido ao OSM: deixam de aparecer na lista e no mapa e de ser exportados.`
      : `Arquivar ${n} rejeitados? Deixam de aparecer na lista e no mapa.`;
  if (!confirm(msg)) return;
  try {
    const r = await api.archive(status, !unarchive);
    toast(`${r.changed} ${label} ${unarchive ? "desarquivados" : "arquivados"}.`);
    await reload();
  } catch (e) {
    toast(`Erro: ${e.message}`);
  }
}

async function reload() {
  const [rows, geojson] = await Promise.all([api.buildings(), api.geojson()]);
  state.rows = rows;
  renderProgress();
  applyFilters();
  await map.setData(geojson);
  const current = state.filtered.find((r) => r.key === state.selected) ?? state.filtered[0];
  if (current) select(current.key);
}

async function init() {
  wire();
  try {
    const [rows, geojson, summary] = await Promise.all([api.buildings(), api.geojson(), api.summary()]);
    $("#area-name").textContent = summary.area;
    $("#palette").innerHTML = (summary.palette ?? []).map((c) => `<option value="${c.hex}">${esc(c.label)}</option>`).join("");
    map.setPalette(summary.palette ?? [], summary.default_colours ?? { roof: "#c8c8c8" });
    state.rows = rows;
    renderProgress();
    applyFilters();
    await map.setData(geojson);
    if (state.filtered.length) select(state.filtered[0].key);
  } catch (e) {
    toast(`Erro ao carregar: ${e.message}`, 10000);
  }
}

init();
