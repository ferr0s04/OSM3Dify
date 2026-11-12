// Aplicação de revisão: mapa (com filtros) + 3D + edição de tags e partes.
import { api } from "./api.js";
import { CHANGE_COLORS, CHANGE_LABELS, SHAPE_LABELS } from "./colors.js";
import { DataView } from "./data.js";
import { BuildingMap, basemapOptions } from "./map.js";
import { renderResults } from "./results.js";
import { Viewer } from "./viewer.js";
import { PART_COLORS, PartsEditor } from "./parts-editor.js";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (v, unit = "") => (v === null || v === undefined ? "—" : `${v}${unit}`);
const SHAPES = ["flat", "skillion", "gabled", "hipped", "side_hipped", "pyramidal", "round", "unknown"];
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
  // Divisão em partes: vista do editor, divisão ativa e correções por parte.
  parts: { view: null, active: false, layout: null, overrides: [], info: null, selected: null },
  viewMode: "3d",
};

const viewer = new Viewer($("#viewer"));
const map = new BuildingMap("map", $("#legend"), (key) => select(key));
const dataView = new DataView({ mapEl: $("#data-map"), panelEl: $("#data-panel"), hoverEl: $("#data-hover"), baseEl: $("#d-base"), toast: (m, ms) => toast(m, ms) });

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

function chosen(group) {
  // Opções marcadas num dropdown de filtros (nenhuma = todos).
  return new Set([...document.querySelectorAll(`.msel[data-group="${group}"] input:checked`)].map((i) => i.value));
}

function renderFilterLabels() {
  // Texto de cada dropdown: "estado: todos", "estado: aceites", "estado: 2 de 3".
  document.querySelectorAll(".msel").forEach((d) => {
    const boxes = [...d.querySelectorAll("input")];
    const on = boxes.filter((b) => b.checked);
    const names = on.map((b) => b.parentElement.textContent.trim());
    const value = !on.length ? (d.dataset.group === "extra" ? "—" : "todos") : on.length === 1 ? names[0] : `${on.length} de ${boxes.length}`;
    d.querySelector("summary").innerHTML = `<span class="flabel">${d.dataset.label}</span> ${value}`;
    d.classList.toggle("on", on.length > 0);
    d.dataset.hint ??= d.title;
    d.title = on.length ? `${d.dataset.label}: ${names.join(", ")}` : d.dataset.hint;
  });
}

function applyFilters() {
  renderFilterLabels();
  const q = $("#f-search").value.trim();
  const change = chosen("change");
  const status = chosen("status");
  const origin = chosen("origin");
  const extra = chosen("extra");
  const flagged = extra.has("flagged");
  const showArchived = extra.has("archived");
  const rows = state.rows.filter(
    (r) =>
      (!q || r.key.includes(q)) &&
      (!origin.size || origin.has(r.candidate ? "new" : "osm")) &&
      (!change.size || change.has(r.change)) &&
      (!status.size || status.has(r.status)) &&
      (!flagged || r.needs_review) &&
      (showArchived || !r.archived),
  );
  // Ordem de navegação (J/K): sinalizados e de baixa confiança primeiro.
  state.filtered = rows.sort((a, b) => priority(a) - priority(b));
  $("#f-count").textContent = `${rows.length} de ${state.rows.length}`;
  map.setVisibleKeys(new Set(rows.map((r) => r.key)));
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
  $("#viewer-status").textContent = "a carregar…";
  viewer.clearAll();
  try {
    const [detail, mesh] = await Promise.all([api.detail(key), api.mesh(key)]);
    if (token !== state.token) return;
    state.detail = detail;
    state.touched = new Set(OVERRIDE_FIELDS.filter((f) => detail.overrides[f] !== null));
    state.parts = { view: null, active: false, layout: null, overrides: [], info: null, selected: null };
    map.select(key, detail.bbox);
    renderDetail();
    if (detail.has_layout || state.viewMode === "plan") loadParts(key, token);
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
  if (!c || d.candidate) return ""; // edifício novo: não há nada no OSM para comparar
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
      ${d.osm_url
        ? `<a href="${d.osm_url}" target="_blank" rel="noopener">abrir no OSM ↗</a>
           <a href="#" id="josm-link" data-obj="${josmType}${d.osm_id}">carregar no JOSM</a>
           <span class="muted">v${d.osm_version} · ${esc(d.existing_tags.building ?? "")} · ${fmt(d.area, " m²")}</span>`
        : `<span class="chip candidate" title="Pontos de edifício no LiDAR sem contorno no OSM">em falta no OSM</span>
           <span class="muted">${d.diagnostics?.ortho_offset_dx !== undefined ? "contorno do ortofoto" : "contorno do LiDAR (inclui beirais)"} · ${fmt(d.area, " m²")} · ao aceitar, é exportado como edifício novo</span>`}
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

    <h3>Partes (building:part)</h3>
    <div id="parts-section"></div>

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
  if ($("#josm-link")) $("#josm-link").onclick = (e) => {
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
  map.setData(await api.geojson());
  applyFilters();
  // Atualiza os telhados 3D deste edifício no mapa (forma/cores podem ter mudado).
  if (map.roofs) map.updateBuilding(await api.sceneOne(current));
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
    const parts = state.parts.active
      ? { layout: state.parts.layout, overrides: state.parts.overrides.map((o) => o ?? {}) }
      : null;
    const detail = await api.saveReview(state.selected, status, note, overrides(), parts);
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
  document.querySelectorAll(".msel input").forEach((i) => i.addEventListener("input", applyFilters));
  document.querySelectorAll(".msel").forEach((d) =>
    d.addEventListener("toggle", () => {
      // Só um menu aberto de cada vez.
      if (d.open) document.querySelectorAll(".msel[open]").forEach((o) => o !== d && (o.open = false));
    }),
  );
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".msel")) document.querySelectorAll(".msel[open]").forEach((o) => (o.open = false));
  });
  $("#f-clear").addEventListener("click", () => {
    document.querySelectorAll(".msel input").forEach((i) => (i.checked = false));
    $("#f-search").value = "";
    applyFilters();
  });
  $("#archive-action").addEventListener("input", (e) => archiveAction(e.target));
  $("#f-search").addEventListener("input", applyFilters);
  $("#m-color").addEventListener("input", (e) => map.setColorMode(e.target.value));
  basemapOptions($("#m-base")).then((kind) => map.setBasemap(kind));
  $("#m-base").addEventListener("input", (e) => map.setBasemap(e.target.value));
  $("#m-3d").addEventListener("input", async (e) => {
    const on = e.target.checked;
    if (on) toast("A preparar os telhados 3D de toda a área…", 2500);
    try {
      await map.set3D(on, () => api.scene());
    } catch (err) {
      toast(`3D: ${err.message}`);
    }
  });
  $("#v-color").addEventListener("input", (e) => viewer.setColorMode(e.target.value));
  $("#v-points").addEventListener("input", (e) => viewer.setVisible("points", e.target.checked));
  $("#v-context").addEventListener("input", (e) => viewer.setVisible("context", e.target.checked));
  $("#v-model").addEventListener("input", (e) => viewer.setVisible("model", e.target.checked));
  $("#v-transparent").addEventListener("input", (e) => viewer.setTransparent(e.target.checked));
  $("#vt-3d").addEventListener("click", () => setViewMode("3d"));
  $("#vt-plan").addEventListener("click", () => setViewMode("plan"));
  $("#pe-split").addEventListener("click", () => editor.startSplit());
  $("#pe-merge").addEventListener("click", () => mergeSelectedPart());
  $("#pe-square").addEventListener("click", () => editor.squarePart(editor.selectedPart));
  $("#pe-undo").addEventListener("click", () => editor.undo());

  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => showTab(tab.dataset.tab)),
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

async function reviewChanged() {
  // A revisão foi recarregada no servidor (outra área ou novos resultados) depois de a
  // página a ter carregado? Então os edifícios, o mapa e o 3D desta página estão velhos.
  try {
    const a = await api.areas();
    return state.reviewGeneration !== undefined && a.review_generation !== state.reviewGeneration;
  } catch {
    return false;
  }
}

async function showTab(name) {
  if ((name === "review" || name === "results") && (await reviewChanged())) {
    location.hash = name === "review" ? "" : name;
    return location.reload();
  }
  // O separador fica no URL: ao mudar de área a página recarrega e volta a ele.
  history.replaceState(null, "", name === "review" ? location.pathname : `#${name}`);
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  for (const v of ["review", "results", "data"]) $(`#view-${v}`).classList.toggle("hidden", v !== name);
  if (name === "results") {
    $("#view-results").innerHTML = "<p class='muted'>A calcular…</p>";
    try {
      renderResults($("#view-results"), await api.summary());
    } catch (e) {
      $("#view-results").innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    }
  } else if (name === "data") {
    await dataView.show();
  } else {
    map.map.resize(); // o mapa pode ter mudado de tamanho enquanto estava escondido
  }
}

// --------------------------------------------------------------- partes

const editor = new PartsEditor($("#planner"), {
  onChange: (layout) => {
    state.parts.layout = layout;
    schedulePartsPreview();
  },
  onSelectPart: (i) => {
    state.parts.selected = i;
    document.querySelectorAll(".part-card").forEach((c) => c.classList.toggle("sel", Number(c.dataset.part) === i));
  },
  onSplit: (i, a, b) => partsOp(() => api.partsSplit(state.selected, state.parts.layout, i, a, b)),
  onStatus: (msg) => toast(msg),
});

function setViewMode(mode) {
  state.viewMode = mode;
  const plan = mode === "plan";
  $("#viewer").classList.toggle("hidden", plan);
  $("#planner").classList.toggle("hidden", !plan);
  $("#controls-3d").classList.toggle("hidden", plan);
  $("#controls-plan").classList.toggle("hidden", !plan);
  $("#vt-3d").classList.toggle("active", !plan);
  $("#vt-plan").classList.toggle("active", plan);
  if (plan && state.selected && !state.parts.view) loadParts(state.selected, state.token);
  if (plan) requestAnimationFrame(() => editor.render());
}

async function loadParts(key, token) {
  try {
    const view = await api.parts(key);
    if (token !== state.token) return;
    state.parts.view = view;
    if (view.layout) {
      state.parts.active = true;
      state.parts.layout = view.layout;
      state.parts.overrides = view.overrides ?? [];
    }
    editor.load({ outline: view.outline, points: view.points, layout: state.parts.active ? state.parts.layout : null });
    renderPartsSection();
    if (state.parts.active) schedulePartsPreview(0);
  } catch (e) {
    toast(`Partes: ${e.message}`);
  }
}

function activateParts(layout) {
  state.parts.active = true;
  state.parts.layout = layout;
  state.parts.overrides = [];
  state.parts.info = null;
  editor.load({ outline: state.parts.view.outline, layout }, true);
  setViewMode("plan");
  renderPartsSection();
  schedulePartsPreview(0);
}

async function startParts(kind) {
  if (!state.parts.view) await loadParts(state.selected, state.token);
  if (kind === "suggested" && state.parts.view?.suggested) return activateParts(state.parts.view.suggested);
  try {
    const { layout } = await api.partsStart(state.selected);
    activateParts(layout);
    editor.selectPart(0);
    editor.startSplit();
  } catch (e) {
    toast(e.message);
  }
}

async function deactivateParts() {
  state.parts.active = false;
  state.parts.layout = null;
  state.parts.overrides = [];
  state.parts.info = null;
  editor.load({ outline: state.parts.view.outline, layout: null }, true);
  renderPartsSection();
  viewer.setModel(await api.mesh(state.selected));
  toast("Divisão descartada (fica em vigor ao aceitar/rejeitar).");
}

async function partsOp(fn) {
  try {
    const { layout } = await fn();
    state.parts.layout = layout;
    state.parts.overrides = []; // a numeração das partes mudou
    editor.setLayout(layout);
    renderPartsSection();
    schedulePartsPreview(0);
  } catch (e) {
    toast(e.message);
  }
}

function mergeSelectedPart() {
  const i = editor.selectedPart;
  if (i === null || !state.parts.active) return toast("Seleciona a parte a juntar à vizinha.");
  partsOp(() => api.partsMerge(state.selected, state.parts.layout, i));
}

function schedulePartsPreview(delay = 300) {
  clearTimeout(schedulePartsPreview.t);
  schedulePartsPreview.t = setTimeout(async () => {
    if (!state.parts.active) return;
    const key = state.selected, token = state.token;
    try {
      const res = await api.partsPreview(key, state.parts.layout, state.parts.overrides.map((o) => o ?? {}));
      if (token !== state.token || !state.parts.active) return;
      state.parts.info = res;
      if (res.mesh) viewer.setModel(res.mesh);
      updatePartsInfo();
      if (res.tags) $("#tags").innerHTML = renderTags(res.tags, state.detail.existing_tags) + renderPartTags(res.parts);
    } catch (e) {
      toast(`Partes: ${e.message}`);
    }
  }, delay);
}

function renderPartTags(parts) {
  if (!parts?.length) return "";
  return parts
    .map((p, i) => `<div class="small" style="margin-top:6px"><b style="color:${PART_COLORS[i % PART_COLORS.length]}">building:part ${i + 1}</b> ${Object.entries(p.tags)
      .map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</div>`)
    .join("");
}

const PART_FIELDS = ["roof_shape", "height", "min_height", "roof_height", "roof_direction", "roof_colour"];

function renderPartsSection() {
  const el = $("#parts-section");
  if (!el) return;
  const d = state.detail;
  if (!d.parts_editable) {
    el.innerHTML = `<p class="muted small">Divisão em partes indisponível para este edifício.</p>`;
    return;
  }
  if (!state.parts.active) {
    el.innerHTML = `<div class="actions">
        ${d.has_suggestion ? `<button class="btn" id="p-suggested" title="Volume principal + volumes secundários detetados no LiDAR">Dividir em partes (sugerido)</button>` : ""}
        <button class="btn" id="p-manual">Dividir manualmente</button>
      </div>
      ${d.has_suggestion ? `<p class="small muted">A análise encontrou volumes com alturas diferentes neste contorno.</p>` : ""}`;
    $("#p-suggested")?.addEventListener("click", () => startParts("suggested"));
    $("#p-manual").addEventListener("click", () => startParts("manual"));
    return;
  }
  const n = state.parts.layout.parts.length;
  const ov = (i, f) => state.parts.overrides[i]?.[f] ?? "";
  const shapeOpts = (i) => ["", "flat", "skillion", "gabled", "hipped", "side_hipped", "pyramidal", "round"]
    .map((s) => `<option value="${s}" ${ov(i, "roof_shape") === s ? "selected" : ""}>${s ? SHAPE_LABELS[s] : "— estimado —"}</option>`).join("");
  el.innerHTML = `
    <div id="parts-problems" class="problems"></div>
    <div class="parts-list">
      ${Array.from({ length: n }, (_, i) => `
        <div class="part-card ${i === state.parts.selected ? "sel" : ""}" data-part="${i}" style="border-left-color:${PART_COLORS[i % PART_COLORS.length]}">
          <h4><span>Parte ${i + 1}</span><span class="est small muted" data-est="${i}"></span></h4>
          <div class="form">
            <label>forma<select data-f="roof_shape" data-i="${i}">${shapeOpts(i)}</select></label>
            <label>height (m)<input data-f="height" data-i="${i}" type="number" step="0.5" min="0" value="${ov(i, "height")}"></label>
            <label>min_height (m)<input data-f="min_height" data-i="${i}" type="number" step="0.5" min="0" value="${ov(i, "min_height")}"></label>
            <label>roof:height (m)<input data-f="roof_height" data-i="${i}" type="number" step="0.5" min="0" value="${ov(i, "roof_height")}"></label>
            <label>roof:direction (°)<input data-f="roof_direction" data-i="${i}" type="number" step="1" min="0" max="359" value="${ov(i, "roof_direction")}"></label>
            <label>roof:colour<input data-f="roof_colour" data-i="${i}" type="color" list="palette" value="${ov(i, "roof_colour") || "#b5533c"}"></label>
          </div>
        </div>`).join("")}
    </div>
    <p class="small muted">Campos vazios = valor estimado (indicado ao lado do título). Edita a geometria na vista <b>Planta</b>.</p>
    <div class="actions"><button class="btn" id="p-single">Voltar a edifício único</button></div>`;
  el.querySelectorAll("[data-f]").forEach((input) =>
    input.addEventListener("input", (e) => {
      const i = Number(e.target.dataset.i), f = e.target.dataset.f;
      const o = (state.parts.overrides[i] = { ...(state.parts.overrides[i] ?? {}) });
      const v = e.target.value;
      o[f] = v === "" ? null : ["height", "min_height", "roof_height", "roof_direction"].includes(f) ? Number(v) : v;
      schedulePartsPreview();
    }),
  );
  el.querySelectorAll(".part-card").forEach((c) =>
    c.addEventListener("click", (e) => {
      if (!e.target.closest("input, select")) editor.selectPart(Number(c.dataset.part));
    }),
  );
  $("#p-single").addEventListener("click", deactivateParts);
  updatePartsInfo();
}

function updatePartsInfo() {
  const info = state.parts.info;
  if (!info) return;
  const probs = $("#parts-problems");
  if (probs) probs.innerHTML = (info.problems ?? []).map((p) => `<div>⚠ ${esc(p)}</div>`).join("");
  (info.parts ?? []).forEach((p, i) => {
    const span = document.querySelector(`[data-est="${i}"]`);
    if (span) {
      span.textContent = `${SHAPE_LABELS[p.roof_shape] ?? p.roof_shape} · ${fmt(p.height, " m")}` +
        (p.min_height ? ` (base ${p.min_height} m)` : "") + ` · ${fmt(p.area, " m²")}`;
    }
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
  const initial = location.hash.slice(1);
  // Geração da revisão carregada por esta página (ver reviewChanged).
  try {
    const a = await api.areas();
    state.reviewGeneration = a.review_generation;
    const active = a.areas.find((x) => x.active);
    $("#area-name").textContent = active?.label ? `${active.label} · ${a.active}` : a.active;
    $("#area-name").title = `Área ativa: ${a.active}, ${active?.tiles.length ?? "?"} quadrículas (mudar no separador Dados)`;
  } catch { /* sem áreas: o erro aparece abaixo */ }
  if (initial === "data" || initial === "results") showTab(initial);
  try {
    const [rows, geojson, summary] = await Promise.all([api.buildings(), api.geojson(), api.summary()]);
    $("#palette").innerHTML = (summary.palette ?? []).map((c) => `<option value="${c.hex}">${esc(c.label)}</option>`).join("");
    map.setPalette(summary.palette ?? [], summary.default_colours ?? { roof: "#c8c8c8" });
    state.rows = rows;
    renderProgress();
    applyFilters();
    await map.setData(geojson);
    if (state.filtered.length) select(state.filtered[0].key);
  } catch (e) {
    if (e.status === 503) {
      // Sem resultados do pipeline (área nova): começar pelos dados.
      toast(`${e.message}. No separador Dados: descarregar e processar a área.`, 8000);
      if (initial !== "data") showTab("data");
      return;
    }
    toast(`Erro ao carregar: ${e.message}`, 10000);
  }
}

init();
