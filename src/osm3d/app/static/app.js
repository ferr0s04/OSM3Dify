// Aplicação de revisão: mapa (com filtros) + 3D + edição de tags e partes.
import { api } from "./api.js";
import { CHANGE_COLORS, CHANGE_LABELS, SHAPE_LABELS } from "./colors.js";
import { DataView } from "./data.js";
import { BuildingMap, basemapOptions } from "./map.js";
import { renderResults } from "./results.js";
import { Viewer } from "./viewer.js";
import { PART_COLORS, PartsEditor } from "./parts-editor.js";
import { SQUARE_MESSAGES, posKey, squareRings } from "./geom-editor.js";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
// Explicação longa num "?" (tooltip), para os campos do painel caberem sem scroll.
const help = (text) => `<span class="help" title="${esc(text)}">?</span>`;
const fmt = (v, unit = "") => (v === null || v === undefined ? "—" : `${v}${unit}`);
const SHAPES = ["flat", "skillion", "gabled", "hipped", "side_hipped", "pyramidal", "round", "unknown"];
const OVERRIDE_FIELDS = ["topology", "ground", "building", "roof_shape", "height", "roof_height", "roof_orientation", "roof_direction", "roof_colour", "building_colour"];
const COLOUR_SOURCE = { "omissão": "omissão", OSM: "já no OSM", estimada: "estimada", manual: "corrigida" };
const COLOUR_VIA = { orthophoto: "ortofoto + LiDAR", lidar: "só LiDAR" };

// Origem da cor estimada e confiança (abaixo do mínimo, a tag não é exportada).
function colourSource(c, conf) {
  let s = COLOUR_SOURCE[c.roof_source] ?? c.roof_source;
  if (c.roof_via) s += ` (${COLOUR_VIA[c.roof_via] ?? c.roof_via})`;
  if (conf !== null && conf !== undefined) {
    const min = state.summary?.min_colour_confidence ?? 0;
    s += ` · ${Math.round(conf * 100)}%${c.roof_source === "omissão" && conf < min ? ` < ${Math.round(min * 100)}%: não exportada` : ""}`;
  }
  if (!$("#opt-roof-colour").checked) s += " · não exportada";
  return s;
}

// Amostras da paleta de telhados para um campo de cor (clicar escolhe a cor).
function swatches(current, attrs) {
  const pal = state.summary?.palette ?? [];
  const cur = (current ?? "").toLowerCase();
  return `<div class="swatches" ${attrs}>${pal
    .map((c) => `<button type="button" class="swatch ${c.hex === cur ? "on" : ""}" data-hex="${c.hex}" title="${esc(c.label)} (${c.hex})" style="background:${c.hex}"></button>`)
    .join("")}</div>`;
}

// Liga as amostras ao campo de cor: muda o valor e dispara "input" (como o seletor).
function wireSwatches(root) {
  root.querySelectorAll(".swatches").forEach((box) =>
    box.addEventListener("click", (e) => {
      const btn = e.target.closest(".swatch");
      if (!btn) return;
      const input = box.dataset.for
        ? root.querySelector(`[name="${box.dataset.for}"]`)
        : root.querySelector(`[data-f="roof_colour"][data-i="${box.dataset.i}"]`);
      if (!input) return;
      input.value = btn.dataset.hex;
      box.querySelectorAll(".swatch").forEach((s) => s.classList.toggle("on", s === btn));
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }),
  );
}

function buildingTypeOptions(sel) {
  const types = state.summary?.building_types ?? [];
  const known = types.some((t) => t.value === sel);
  return (known ? "" : `<option value="${esc(sel)}" selected>${esc(sel)}</option>`) +
    types.map((t) => `<option value="${t.value}" ${t.value === sel ? "selected" : ""}>${esc(t.label)} (${t.value})</option>`).join("");
}

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
const map = new BuildingMap("map", $("#legend"), (key, shift) => (shift ? toggleMulti(key) : select(key)), (key) => selectWall(key));
map.onSelectLanduse = (key) => selectLanduse(key);
map.onSelectOsmBuilding = (key, shift) => (shift ? toggleMulti(key) : selectOsmBuilding(key));
map.onMultiMenu = (point) => {
  const n = state.multi?.items.size ?? 0;
  map.editor.openMenu(point, [{ label: `Esquadrar ${n} edifícios`, hint: "Q", enabled: n > 0, run: () => squareMulti(),
    title: "Esquadra todos os edifícios selecionados (os vértices partilhados entre eles movem-se juntos)" }]);
};
map.onEditCommit = (target, coords, ids, others) => commitEdit(target, coords, ids, others);
map.onStatus = (msg) => toast(msg, 4500);
map.onDeselect = () => deselect();
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
  // Opções marcadas num grupo de filtros (nenhuma = todos).
  return new Set([...document.querySelectorAll(`#f-menu [data-group="${group}"] input:checked`)].map((i) => i.value));
}

function renderFilterLabels() {
  // Resumo do menu "Filtros": o que está ativo ("Filtros: por rever, só sinalizados").
  const menu = $("#f-menu");
  const names = [...menu.querySelectorAll("input:checked")].map((b) => b.parentElement.textContent.trim());
  menu.querySelector("summary").innerHTML = `<span class="flabel">Filtros</span> ${names.length ? esc(names.join(", ")) : "todos"}`;
  menu.classList.toggle("on", names.length > 0);
}

// A lista tem só os edifícios da cartografia municipal (em falta no OSM); os do OSM são
// uma camada do mapa ("Camadas" → edifícios do OSM) e abrem-se clicando neles.
const municipalRows = (rows) => rows.filter((r) => r.candidate);
const municipalGeojson = (gj) => ({ ...gj, features: gj.features.filter((f) => f.properties.candidate) });

function setRows(rows) {
  state.rows = municipalRows(rows);
  // Edifícios do OSM com análise 3D (os outros só se editam como geometria).
  state.osmKeys = new Set(rows.filter((r) => !r.candidate).map((r) => r.key));
}

function applyFilters() {
  renderFilterLabels();
  const q = $("#f-search").value.trim();
  const status = chosen("status");
  const extra = chosen("extra");
  const flagged = extra.has("flagged");
  const showArchived = extra.has("archived");
  const rows = state.rows.filter(
    (r) =>
      (!q || r.key.includes(q)) &&
      (!status.size || status.has(r.status)) &&
      (!flagged || r.needs_review) &&
      (showArchived || !r.archived),
  );
  // Ordem de navegação (J/K): sinalizados e de baixa confiança primeiro.
  state.filtered = rows.sort((a, b) => priority(a) - priority(b));
  $("#f-count").textContent = `${rows.length} de ${state.rows.length}`;
  map.setVisibleKeys(new Set(rows.map((r) => r.key)));
  scheduleSaveFilters();
}

// ------------------------------------------------------- filtros guardados

// Todos os filtros (barra, cor do mapa, muros e camadas OSM) numa só chave do
// localStorage: lida uma vez ao arrancar e escrita no máximo uma vez a cada 300 ms, só
// quando muda. Sem armazenamento (modo privado, bloqueado), ficam os de omissão.
const FILTERS_KEY = "osm3d.filters";
const FILTERS_VERSION = 1;
let savedFilters = null;
let saveTimer = null;

function checkedValues(root) {
  return root ? [...root.querySelectorAll("input[type=checkbox]:checked")].map((i) => i.value) : [];
}

function filterSnapshot() {
  const groups = {};
  document.querySelectorAll("#f-menu [data-group]").forEach((d) => (groups[d.dataset.group] = checkedValues(d)));
  return JSON.stringify({
    v: FILTERS_VERSION,
    search: $("#f-search").value,
    groups,
    walls: checkedValues($("#m-walls")),
    osm: checkedValues($("#m-osm")),
    color: $("#m-color").value,
  });
}

function scheduleSaveFilters() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const snap = filterSnapshot();
    if (snap === savedFilters) return;
    try {
      localStorage.setItem(FILTERS_KEY, snap);
      savedFilters = snap;
    } catch { /* sem armazenamento: ficam só nesta página */ }
  }, 300);
}

function restoreFilters() {
  let saved = null;
  try {
    savedFilters = localStorage.getItem(FILTERS_KEY);
    saved = savedFilters ? JSON.parse(savedFilters) : null;
  } catch {
    saved = null;
  }
  if (!saved || saved.v !== FILTERS_VERSION) return;
  const setChecked = (root, values) => {
    if (!root || !Array.isArray(values)) return;
    const want = new Set(values);
    root.querySelectorAll("input[type=checkbox]").forEach((i) => (i.checked = want.has(i.value)));
  };
  for (const [group, values] of Object.entries(saved.groups ?? {})) {
    setChecked(document.querySelector(`#f-menu [data-group="${group}"]`), values);
  }
  setChecked($("#m-walls"), saved.walls);
  setChecked($("#m-osm"), saved.osm);
  if (typeof saved.search === "string") $("#f-search").value = saved.search;
  if ([...$("#m-color").options].some((o) => o.value === saved.color)) $("#m-color").value = saved.color;
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

// Detalhe, malha e pontos pedidos antes de serem precisos: o edifício para onde se
// avança ao aceitar/rejeitar o atual já está carregado quando se carrega na tecla. Cada
// entrada serve uma vez (ao abrir, sai); as que uma revisão ou edição pode ter mudado
// saem antes (forgetPrefetched).
const prefetched = new Map();
const PREFETCH_MAX = 4;

function loadBuilding(key) {
  const hit = prefetched.get(key);
  if (hit) {
    prefetched.delete(key);
    return hit;
  }
  return requestBuilding(key);
}

function requestBuilding(key) {
  const e = { detail: api.detail(key), mesh: api.mesh(key), points: api.points(key) };
  for (const p of Object.values(e)) p.catch(() => {}); // falha tratada por quem o usar
  return e;
}

function prefetch(key) {
  if (!key || prefetched.has(key) || key === state.selected) return;
  prefetched.set(key, requestBuilding(key));
  while (prefetched.size > PREFETCH_MAX) prefetched.delete(prefetched.keys().next().value);
}

function forgetPrefetched(keys = null) {
  if (keys === null) prefetched.clear();
  else for (const k of keys) prefetched.delete(k);
}

function prefetchNext(key, token) {
  // O edifício para onde review() avança a partir deste (ver nearestRow).
  if (token !== state.token || !state.filtered.some((r) => r.key === key)) return;
  const row = state.rows.find((r) => r.key === key);
  prefetch(nearestRow(key, row)?.key);
}

async function select(key, { keepView = false } = {}) {
  if (!key) return;
  clearMulti();
  const token = ++state.token;
  state.selected = key;
  state.wall = null;
  state.landuse = null;
  map.stopEditing();
  map.selectWall(null);
  $("#viewer-status").textContent = "a carregar…";
  if (!keepView) viewer.clearAll();
  map.showOsmAdjustments(null);
  try {
    const load = loadBuilding(key);
    const [detail, mesh] = await Promise.all([load.detail, load.mesh]);
    if (token !== state.token) return;
    state.detail = detail;
    map.showOsmAdjustments(detail.osm_adjustments);
    state.touched = new Set(OVERRIDE_FIELDS.filter((f) => detail.overrides[f] !== null));
    state.parts = { view: null, active: false, layout: null, overrides: [], info: null, selected: null };
    map.select(key, detail.bbox);
    renderDetail();
    startBuildingEdit();
    if (detail.has_layout || state.viewMode === "plan") loadParts(key, token);
    viewer.setModel(mesh);
    if (!keepView) viewer.frame(mesh);
    $("#viewer-status").textContent = "a carregar pontos LiDAR… (a 1.ª vez por quadrícula demora)";
    const pts = await load.points;
    if (token !== state.token) return;
    viewer.setPoints(pts);
    $("#viewer-status").textContent = `${pts.count.toLocaleString("pt-PT")} pontos · rmse ${fmt(detail.effective?.rmse_m, " m")}`;
    prefetchNext(key, token); // depois dos pontos deste, para não lhes tirar o servidor
  } catch (e) {
    if (token === state.token) $("#viewer-status").textContent = `erro: ${e.message}`;
  }
}

function deselect() {
  // Clique no fundo do mapa ou dentro de um landuse (ou Esc): nada selecionado, como no iD.
  if (state.multi) clearMulti();
  else if (!state.selected && !state.wall && !state.landuse) return;
  state.token++;
  state.selected = null;
  state.detail = null;
  state.wall = null;
  state.landuse = null;
  state.touched = new Set();
  state.parts = { view: null, active: false, layout: null, overrides: [], info: null, selected: null };
  map.stopEditing();
  map.select(null);
  map.selectWall(null);
  map.showOsmAdjustments(null);
  viewer.clearAll();
  $("#viewer-status").textContent = "";
  $("#detail").innerHTML = '<p class="muted pad">Selecione um edifício na lista ou no mapa.</p>';
}

// ------------------------------------------------------- seleção múltipla

function selectAny(key) {
  // Um só edifício: o detalhe completo (com análise 3D) ou só o contorno (fora dela).
  if (key.startsWith("new/") || state.osmKeys?.has(key)) return select(key);
  return selectLanduse(key);
}

function currentBuildingItem() {
  // O edifício selecionado agora (para começar uma seleção múltipla com ele).
  const d = state.detail;
  if (state.selected && d?.geometry) {
    const g = d.geometry;
    return { key: d.key, nodes: g.nodes, shared: new Set(Object.keys(g.shared ?? {}).map(Number)), inReview: true, editable: g.editable, reason: g.reason };
  }
  const l = state.landuse;
  if (l?.is_building) {
    return { key: l.key, nodes: l.nodes, shared: new Set(Object.keys(l.shared ?? {}).map(Number)), inReview: false, editable: true, reason: null };
  }
  return null;
}

async function fetchRing(key) {
  if (key.startsWith("new/") || state.osmKeys?.has(key)) {
    const g = await api.buildingGeometry(key);
    return { key, nodes: g.nodes, shared: new Set(Object.keys(g.shared ?? {}).map(Number)), inReview: true, editable: g.editable, reason: g.reason };
  }
  const d = await api.landuse(Number(key.slice(4)));
  return { key, nodes: d.nodes, shared: new Set(Object.keys(d.shared ?? {}).map(Number)), inReview: false, editable: true, reason: null };
}

async function toggleMulti(key) {
  // Shift + clique: junta o edifício à seleção (ou tira-o, se já lá está), como no iD.
  if (!state.multi) {
    const current = currentBuildingItem();
    if (!current) return key.startsWith("way/") ? selectOsmBuilding(key) : select(key);
    if (current.key === key) return;
    state.token++;
    state.selected = null;
    state.detail = null;
    state.wall = null;
    state.landuse = null;
    state.parts = { view: null, active: false, layout: null, overrides: [], info: null, selected: null };
    map.stopEditing();
    map.select(null);
    map.selectWall(null);
    viewer.clearAll();
    $("#viewer-status").textContent = "seleção múltipla: sem vista 3D";
    state.multi = { items: new Map([[current.key, current]]), undo: [] };
  }
  const items = state.multi.items;
  if (items.has(key)) items.delete(key);
  else {
    try {
      items.set(key, await fetchRing(key));
    } catch (e) {
      return toast(`${key}: ${e.message}`);
    }
  }
  if (items.size === 1) {
    const [only] = items.keys();
    return selectAny(only);
  }
  if (!items.size) return deselect();
  renderMulti();
}

function clearMulti() {
  if (!state.multi) return;
  state.multi = null;
  map.setMulti([]);
}

function renderMulti() {
  const m = state.multi;
  if (!m) return;
  const items = [...m.items.values()];
  map.setMulti(items.filter((i) => i.nodes?.length >= 3).map((i) => ({ key: i.key, coords: i.nodes.map((n) => [n[1], n[2]]) })));
  const blocked = items.filter((i) => !i.editable);
  $("#detail").innerHTML = `
    <h2>${items.length} edifícios selecionados</h2>
    <p class="small muted">Shift + clique junta ou tira edifícios da seleção. Q, o botão abaixo ou o botão direito
      dentro de um deles esquadram-nos todos; os vértices partilhados entre eles (casas geminadas) movem-se juntos.
      Ctrl+Z desfaz. Esc ou clique no fundo do mapa tira a seleção. As edições são gravadas logo e vão na exportação.</p>
    <div class="actions"><button class="btn" id="m-square" ${items.length > blocked.length ? "" : "disabled"}>⊾ Esquadrar todos</button></div>
    <table class="kv">${items.map((i) => `<tr><th><a href="#" data-key="${esc(i.key)}">${esc(i.key)}</a></th>
      <td>${i.key.startsWith("new/") ? "cartografia municipal" : i.inReview ? "OSM" : "OSM (sem análise 3D)"}${i.editable ? "" : ` · <span class="warn-text">${esc(i.reason ?? "não editável")}</span>`}</td></tr>`).join("")}</table>`;
  $("#m-square").onclick = () => squareMulti();
  document.querySelectorAll("#detail [data-key]").forEach((a) => (a.onclick = (e) => {
    e.preventDefault();
    selectAny(a.dataset.key);
  }));
}

function squareMulti() {
  const m = state.multi;
  if (!m) return;
  const items = [...m.items.values()].filter((i) => i.editable && i.nodes?.length >= 3);
  if (!items.length) return toast("Nenhum dos edifícios selecionados se edita aqui.");
  const owners = map.vertexOwners();
  const rings = items.map((i) => ({
    key: i.key,
    coords: i.nodes.map((n) => [n[1], n[2]]),
    ids: i.nodes.map((n) => n[0]),
    // Vértice ligado a outro elemento (nó partilhado do OSM ou o mesmo ponto): não sai.
    keep: (k) => {
      const [id, lon, lat] = i.nodes[k];
      if (id != null && i.shared.has(id)) return true;
      return [...(owners.get(posKey([lon, lat])) ?? [])].some((o) => o !== i.key);
    },
  }));
  const { rings: out, errors } = squareRings(rings);
  const changed = out.filter((r) => r.changed);
  if (errors.length) {
    const why = [...new Set(errors.map((e) => e.error))].map((e) => SQUARE_MESSAGES[e] ?? e).join(" ");
    toast(`${errors.length} de ${items.length} não esquadrados (${errors.map((e) => e.key).join(", ")}): ${why}`, 7000);
  }
  if (!changed.length) return;
  m.undo.push(changed.map((r) => ({ key: r.key, nodes: m.items.get(r.key).nodes })));
  const saves = changed.map((r) => ({ key: r.key, nodes: r.coords.map((c, k) => [r.ids?.[k] ?? null, c[0], c[1]]) }));
  for (const sv of saves) m.items.get(sv.key).nodes = sv.nodes;
  renderMulti();
  saveMulti(saves);
}

function undoMulti() {
  const m = state.multi;
  const prev = m?.undo.pop();
  if (!prev) return;
  for (const p of prev) if (m.items.has(p.key)) m.items.get(p.key).nodes = p.nodes;
  renderMulti();
  saveMulti(prev);
}

function saveMulti(saves) {
  // Os contornos ficam logo no mapa; gravam-se na fila com as outras edições do mapa (a
  // exportação espera por elas). O 3D de cada edifício é reajustado ao LiDAR no servidor.
  localEdit(saves.map(({ key, nodes }) => ({ key, coords: nodes.map((n) => [n[1], n[2]]), ids: nodes.map((n) => n[0]) })));
  enqueueSave({
    kind: "multi",
    label: `${saves.length} contornos`,
    run: async () => {
      let failed = 0;
      for (const { key, nodes } of saves) {
        try {
          if (isReviewBuilding(key)) await api.saveBuildingGeometry(key, nodes);
          else await api.saveLanduse(Number(key.slice(4)), nodes);
        } catch (e) {
          failed++;
          toast(`${key}: não gravado (${e.message})`, 7000);
        }
        for (const l of layersFor(key)) reloadLayers.add(l);
      }
      if (failed) for (const l of ["buildings", "ctx", "walls"]) reloadLayers.add(l);
    },
  });
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
  const tip = `${g.reason}\n\nbaixo: ponto mais baixo (p.ex. estrada) · lote: terreno dominante junto às paredes`;
  return `<label class="full ground ${t("ground")}"><span>terreno de referência (desnível ${g.step_m} m) ${help(tip)}</span>
      <select name="ground">
        ${opt("low", "baixo")}
        ${opt("high", "lote")}
      </select></label>`;
}

const TOPOLOGY_MODES = {
  osm: "ajustar o OSM a este contorno",
  trim: "recortar este edifício",
  keep: "manter (sobreposição verdadeira)",
};

function renderTopology(d, val, t) {
  // Edifício novo sobreposto a landuse/edifícios do OSM: o que fazer na exportação.
  const topo = d.topology;
  if (!topo || !topo.relevant) return "";
  const sel = val("topology", topo.mode);
  const opts = Object.entries(TOPOLOGY_MODES)
    .map(([k, label]) => `<option value="${k}" ${sel === k ? "selected" : ""}>${label}${k === topo.default ? " — padrão" : ""}</option>`)
    .join("");
  const msgs = d.qa_flags.filter((f) => ["adjusts_osm", "topology_trimmed", "straddles_landuse", "overlaps_osm_building", "crosses_highway", "overlaps_new_building"].includes(f.code));
  const tip = [
    ...msgs.map((f) => f.message),
    `Padrão: ${topo.default === "osm" ? "o contorno municipal manda" : "este edifício é recortado"}. Ver as camadas OSM no mapa (landuse, muros, vias). Muda ao aceitar.`,
  ].join("\n");
  return `<div class="full topology-box ${t("topology")}">
      <b>Sobreposição com o OSM</b> ${help(tip)}
      <select name="topology">${opts}</select>
      <div class="small muted" id="topo-adj">${topologyStatus(d.osm_adjustments)}</div>
    </div>`;
}

function topologyStatus(adjs) {
  // Modo "ajustar o OSM": o que muda no OSM (já desenhado no mapa).
  if (!adjs?.length) return "";
  const done = adjs.filter((a) => a.geometry).length;
  const failed = adjs.length - done;
  const tip = adjs.map((a) => `${a.geometry ? "✓" : "✗"} ${a.description}`).join("\n");
  return `<span title="${esc(tip)}">${done ? `${done} elemento${done > 1 ? "s" : ""} do OSM ajustado${done > 1 ? "s" : ""} no mapa` : ""}${done && failed ? " · " : ""}${failed ? `${failed} não ajustáve${failed > 1 ? "is" : "l"}` : ""}</span>`;
}

function renderTopologyStatus(adjs) {
  const el = $("#topo-adj");
  if (el) el.innerHTML = topologyStatus(adjs);
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
    novo: "Sem 3D no OSM: as tags são todas novas.",
    complementa: "Acrescenta tags em falta ao 3D do OSM.",
    diverge: `<b>Valores diferentes</b> do 3D do OSM (fica o do OSM) ${help("Confirmar no terreno/imagem antes de alterar.")}`,
    igual: "O 3D do OSM já tem tudo.",
    "sem proposta": "Sem reconstrução.",
  }[kind];
  return `<div class="osm-box" style="border-color:${CHANGE_COLORS[kind]}">
    <div><span class="chip" style="background:${CHANGE_COLORS[kind]};color:white">${esc(CHANGE_LABELS[kind])}</span> ${text}</div>
    ${existing.length ? `<div class="small">No OSM: ${existing.map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</div>` : ""}
    ${c.osm_parts.length ? `<div class="small">Já tem building:part no OSM: ${c.osm_parts.map(esc).join(", ")}</div>` : ""}
    ${conflicts.length ? `<div class="small">Diverge: ${conflicts.map(([k, v]) => `<code>${esc(k)}</code> OSM <b>${esc(v.osm)}</b> → proposto <b>${esc(v.proposed)}</b>`).join("; ")}</div>` : ""}
    ${added.length && existing.length ? `<div class="small">Acrescenta: ${added.map((k) => `<code>${esc(k)}</code>`).join(" ")}</div>` : ""}
  </div>`;
}

// Tags já no OSM: num edifício novo não há nenhuma (building/source são propostas).
function osmTags(d) {
  return d.candidate ? {} : d.existing_tags;
}

function renderTags(tags, existing) {
  const keys = [...new Set([...Object.keys(existing), ...Object.keys(tags.proposed), ...Object.keys(tags.final)])]
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
    ${Object.keys(tags.conflicts).length ? `<p class="small muted">Conflitos: fica o valor do OSM ${help("Não se sobrescreve o trabalho dos mapeadores.")}</p>` : ""}`;
}

function renderGeometry(d) {
  // Contorno do edifício: edita-se no mapa como no iD (vértices, esquadrar).
  const g = d.geometry;
  if (!g) return "";
  const shared = Object.values(g.shared ?? {}).flat();
  const others = [...new Set(shared)];
  const where = g.kind === "osm"
    ? "Vai no .osm da exportação, aplicado aos dados atuais do OSM (como os landuse), mesmo sem aceitar as tags 3D."
    : "Vai no edifício novo, ao aceitar.";
  const tip = `${BUILDING_EDIT_TIP}\n\n${where} Ao mudar o contorno, o telhado, a altura e a cor são reajustados ao LiDAR.`;
  return `<h3>Contorno ${g.editable ? help(tip) : ""} ${g.edited ? '<span class="chip">editado à mão</span>' : ""}</h3>
    ${g.editable
      ? `${others.length ? `<p class="small" title="Os vértices cinzentos movem-se também nesses elementos.">Vértices partilhados com: ${others.map(esc).join(", ")}</p>` : ""}
         <div class="actions">
           <button class="btn" id="g-square" title="Esquadrar o contorno inteiro (Q no mapa; com um vértice selecionado, só esse canto)">⊾ Esquadrar</button>
           <button class="btn" id="g-reset" ${g.edited ? "" : "disabled"}>Repor contorno ${g.kind === "osm" ? "do OSM" : "municipal"}</button>
         </div>`
      : `<p class="small muted">${esc(g.reason ?? "Contorno não editável aqui.")}</p>`}`;
}

function startBuildingEdit() {
  // Edifício selecionado: o contorno fica logo editável no mapa (como os muros e landuse),
  // exceto com uma divisão em partes ativa (as partes dependem do contorno).
  const d = state.detail;
  if (!d || state.parts.active || !d.geometry?.editable) return map.stopEditing();
  map.editBuilding(d.key, d.geometry);
}

function rerenderDetail() {
  // Volta a desenhar o detalhe sem perder as correções ainda não guardadas.
  if (!state.touched.has("topology")) map.showOsmAdjustments(state.detail?.osm_adjustments);
  const values = {};
  for (const f of state.touched) {
    const el = document.querySelector(`[name="${f}"]`);
    if (el) values[f] = el.value;
  }
  const note = document.querySelector('[name="note"]')?.value;
  renderDetail();
  for (const [f, v] of Object.entries(values)) {
    const el = document.querySelector(`[name="${f}"]`);
    if (el) {
      el.value = v;
      el.closest("label")?.classList.add("touched");
    }
  }
  if (note !== undefined && document.querySelector('[name="note"]')) document.querySelector('[name="note"]').value = note;
  if (state.parts.view) renderPartsSection();
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
        : `<span class="chip candidate" title="Contorno da cartografia municipal (confirmado pelo LiDAR) sem edifício no OSM: ao aceitar, é exportado como edifício novo">em falta no OSM</span>
           <span class="muted" title="gid do contorno municipal · parte da área com LiDAR · área">gid ${fmt(d.diagnostics?.municipal_gid)} · LiDAR ${Math.round((d.diagnostics?.municipal_lidar_cover ?? 0) * 100)}% · ${fmt(d.area, " m²")}</span>`}
    </div>
    ${renderOsmChange(d)}
    <div class="actions">
      <button class="btn ok" id="b-accept" title="A">Aceitar${state.touched.size ? " com correções" : ""}</button>
      <button class="btn bad" id="b-reject" title="R">Rejeitar</button>
      <button class="btn" id="b-reset" title="U" ${d.reviewed_at ? "" : "disabled"}>Repor</button>
    </div>

    <h3>Correções</h3>
    <form class="form" id="edit-form" autocomplete="off">
      ${renderTopology(d, val, t)}
      ${renderGround(d, val, t)}
      ${d.candidate ? `<label class="full ${t("building")}">building <span class="est">municipal: ${esc(d.estimated_building ?? "yes")}</span>
        <select name="building">${buildingTypeOptions(val("building", d.building_type ?? "yes"))}</select></label>` : ""}
      <label class="${t("roof_shape")}">roof:shape <span class="est">est. ${fmt(est.roof_shape)} (${fmt(est.roof_shape_conf)})</span>
        <select name="roof_shape">${shapeOpts(val("roof_shape", eff.roof_shape))}</select></label>
      <label class="${t("height")}">height (m) <span class="est">est. ${fmt(est.height)}</span>
        <input name="height" type="number" step="0.5" min="0" value="${val("height", eff.height)}"></label>
      <label class="${t("roof_height")}">roof:height (m) <span class="est">est. ${fmt(est.roof_height)}</span>
        <input name="roof_height" type="number" step="0.5" min="0" value="${val("roof_height", eff.roof_height)}"></label>
      <label class="${t("roof_orientation")}">roof:orientation <span class="est">est. ${fmt(est.roof_orientation)}</span>
        <select name="roof_orientation">
          <option value="">—</option>
          <option value="along" ${val("roof_orientation", eff.roof_orientation) === "along" ? "selected" : ""}>along (cumeeira ao longo)</option>
          <option value="across" ${val("roof_orientation", eff.roof_orientation) === "across" ? "selected" : ""}>across (cumeeira através)</option>
        </select></label>
      <label class="${t("roof_direction")}">roof:direction (°) <span class="est">est. ${fmt(est.roof_direction)}</span>
        <input name="roof_direction" type="number" step="1" min="0" max="359" value="${val("roof_direction", eff.roof_direction)}"></label>
      <label class="${t("roof_colour")}">roof:colour <span class="est" title="${esc(colourSource(d.colours, est.roof_colour_conf))}">${colourSource(d.colours, est.roof_colour_conf)}</span>
        <input name="roof_colour" type="color" list="palette" value="${toHex(val("roof_colour", d.colours.roof))}">
        ${swatches(toHex(val("roof_colour", d.colours.roof)), 'data-for="roof_colour"')}</label>
      <label class="${t("building_colour")}">building:colour <span class="est">${COLOUR_SOURCE[d.colours.walls_source]}</span>
        <input name="building_colour" type="color" value="${toHex(val("building_colour", d.colours.walls))}"></label>
      <label class="full">nota<textarea name="note" rows="1">${esc(d.note ?? "")}</textarea></label>
    </form>

    ${renderGeometry(d)}

    <h3>Partes (building:part)</h3>
    <div id="parts-section"></div>

    <h3>Tags</h3>
    <div id="tags">${renderTags(d.tags, osmTags(d))}</div>

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
  if ($("#g-square")) $("#g-square").onclick = () => map.editor.editing(d.key) && map.editor.square("way");
  if ($("#g-reset")) $("#g-reset").onclick = () => resetGeometry();
  if ($("#josm-link")) $("#josm-link").onclick = (e) => {
    e.preventDefault();
    const obj = e.target.dataset.obj;
    fetch(`http://127.0.0.1:8111/load_object?objects=${obj}&relation_members=true`, { mode: "no-cors" })
      .then(() => toast("Pedido enviado ao JOSM (requer o controlo remoto ativo)."))
      .catch(() => toast("JOSM não respondeu: está aberto com o controlo remoto ativo?"));
  };
  const form = $("#edit-form");
  wireSwatches(form);
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
      map.showOsmAdjustments(p.osm_adjustments);
      renderTopologyStatus(p.osm_adjustments);
      $("#tags").innerHTML = renderTags(p.tags, osmTags(state.detail));
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

function patchRowFromFeature(f) {
  // Vizinho cuja topologia mudou com a revisão (recorte, sinalizações): a linha da lista
  // fica com o que o mapa mostra.
  const row = state.rows.find((r) => r.key === f.properties.key);
  if (!row) return;
  for (const [k, v] of Object.entries(f.properties)) {
    if (k in row) row[k] = k === "flags" ? (v ? v.split(",") : []) : v;
  }
}

function applyReviewResult(detail, seq) {
  // Resposta do servidor (pela ordem da fila): o estado oficial deste edifício e dos
  // edifícios novos sobrepostos a ele, cuja topologia pode ter mudado. Os que foram
  // editados no mapa depois desta revisão (seq) ficam como estão (a edição é mais recente).
  patchRow(detail);
  const feats = detail.map_features?.filter((f) => isLatest(f.properties.key, seq));
  if (feats) {
    for (const f of feats) patchRowFromFeature(f);
    map.patchFeatures(feats.filter((f) => f.properties.candidate));
    forgetPrefetched(feats.map((f) => f.properties.key));
  } else {
    // A topologia da área toda vai ser refeita: o mapa recarrega-se quando a fila esvaziar.
    forgetPrefetched();
    reloadLayers.add("buildings");
  }
  renderProgress();
  applyFilters();
  // Telhados 3D deste edifício no mapa (forma/cores podem ter mudado).
  if (map.roofs) api.sceneOne(detail.key).then((b) => map.updateBuilding(b)).catch(() => {});
  // O edifício aberto agora (o seguinte) é um dos que mudaram: atualizar o detalhe.
  const sel = state.selected;
  if (sel && sel !== detail.key && (!feats || feats.some((f) => f.properties.key === sel))) refreshSelected();
}

async function refreshSelected() {
  // Detalhe do edifício aberto, sem o fechar nem perder as correções por guardar; a
  // malha só se não há correções (com elas, a pré-visualização é que manda).
  const key = state.selected;
  const token = state.token;
  try {
    const detail = await api.detail(key);
    if (token !== state.token || JSON.stringify(detail) === JSON.stringify(state.detail)) return;
    state.detail = detail;
    rerenderDetail();
    if (state.touched.size) return schedulePreview();
    const mesh = await api.mesh(key);
    if (token === state.token) viewer.setModel(mesh);
  } catch { /* fica o que está */ }
}

function nearestRow(key, from) {
  // O mais perto de `from` (lon/lat) na lista filtrada, sem `key`; os por rever antes dos
  // já revistos (com o filtro "todos", o vizinho acabado de aceitar não é o seguinte).
  if (from?.lon == null || from?.lat == null) return null;
  const k = Math.cos((from.lat * Math.PI) / 180);
  let best = null;
  for (const r of state.filtered) {
    if (r.key === key) continue;
    const d = ((r.lon - from.lon) * k) ** 2 + (r.lat - from.lat) ** 2;
    const rank = r.status === "pending" && !r.archived ? 0 : 1;
    if (!best || rank < best.rank || (rank === best.rank && d < best.d)) best = { r, d, rank };
  }
  return best?.r ?? null;
}

function review(status) {
  // Aceitar/rejeitar (status null: repor, volta a "por rever" e fica no mesmo edifício).
  // A decisão aparece logo (lista, contagens, cor no mapa) e passa-se já ao seguinte; a
  // gravação segue em segundo plano, na fila das edições.
  const d = state.detail;
  if (!d || state.selected !== d.key) return;
  const key = d.key;
  // O formulário é lido já: ao avançar, é substituído pelo do edifício seguinte.
  const note = document.querySelector('[name="note"]')?.value ?? null;
  const ov = overrides();
  const parts = state.parts.active
    ? { layout: state.parts.layout, overrides: state.parts.overrides.map((o) => o ?? {}) }
    : null;
  const save = status ? () => api.saveReview(key, status, note, ov, parts) : () => api.resetReview(key);
  saveDecision(key, status ?? "pending", save, !!status);
  toast(`${key}: ${status === "accepted" ? "aceite" : status === "rejected" ? "rejeitado" : "revisão reposta"}`, 1500);
}

function resetReview() {
  review(null);
}

function saveDecision(key, status, save, advance) {
  const row = state.rows.find((r) => r.key === key);
  const before = row ? { ...row } : null;
  // Avança para o edifício da lista filtrada mais perto deste (por rever primeiro), e não
  // para o seguinte na ordem da lista, que pode estar do outro lado da área. Edifício do
  // OSM (aberto no mapa, fora da lista) ou repor: fica neste.
  const inList = state.filtered.some((r) => r.key === key);
  const target = advance && inList ? nearestRow(key, row ?? state.detail) : null;
  if (row) row.status = status;
  const seq = ++editSeq; // uma recarga do mapa ainda a caminho já não tem esta decisão
  map.setFeatureProps(key, { status });
  forgetPrefetched([key]);
  renderProgress();
  applyFilters();
  if (target) select(target.key); // normalmente já pré-carregado
  enqueueSave({
    kind: "review",
    label: "Revisão",
    run: async () => {
      const detail = await save();
      applyReviewResult(detail, seq);
      // Ficou neste edifício (do OSM, fora da lista, ou repor): mostra-o como ficou.
      if (!target && state.selected === key) select(key, { keepView: true });
    },
    failed: () => {
      if (row && before) Object.assign(row, before);
      if (before) map.setFeatureProps(key, { status: before.status });
      renderProgress();
      applyFilters();
    },
  });
}

// ------------------------------------------------------------- arranque

function wire() {
  // Filtros guardados primeiro: o mapa cria as camadas já com eles.
  restoreFilters();
  map.mode = $("#m-color").value;
  map.wallFilter = wallFilter();
  map.osmKinds = new Set(checkedValues($("#m-osm")));
  document.addEventListener("input", (e) => {
    if (e.target.closest("#m-layers, #m-color")) scheduleSaveFilters();
  });
  document.querySelectorAll("#f-menu input").forEach((i) => i.addEventListener("input", applyFilters));
  document.querySelectorAll("#m-osm input").forEach((i) =>
    i.addEventListener("input", () => map.setOsmLayers(new Set([...document.querySelectorAll("#m-osm input:checked")].map((x) => x.value)))),
  );
  $("#m-osm-refresh").addEventListener("click", refreshOsmContext);
  document.querySelectorAll("#m-walls input").forEach((i) => i.addEventListener("input", () => map.setWallFilter(wallFilter())));
  $("#m-walls-fetch").addEventListener("click", fetchWalls);
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
    document.querySelectorAll("#f-menu input").forEach((i) => (i.checked = false));
    $("#f-search").value = "";
    applyFilters();
  });
  $("#f-default").addEventListener("click", () => {
    document.querySelectorAll("#f-menu input").forEach((i) => (i.checked = i.defaultChecked));
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

  $("#opt-roof-colour").addEventListener("input", (e) => setRoofColourOption(e.target));
  $("#btn-export").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      await editQueue; // as edições do mapa ainda por gravar entram nesta exportação
      const r = await api.export();
      const topo = r.topology ? ` · Topologia: ${r.topology} (pormenor no CSV)` : "";
      toast(`${r.exported} edifícios exportados (${r.conflicts} com conflitos de tags) → ${r.path}${topo}`, 14000);
    } catch (err) {
      toast(`Exportação falhou: ${err.message}`);
    } finally {
      e.target.disabled = false;
    }
  });

  document.addEventListener("keydown", (e) => {
    // Seleção múltipla: Q esquadra todos, Ctrl+Z desfaz, Esc tira a seleção.
    if (!state.multi || e.target.closest("input, select, textarea")) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); undoMulti(); }
    else if (!e.ctrlKey && !e.metaKey && !e.altKey && k === "q") { e.preventDefault(); squareMulti(); }
    else if (e.key === "Escape") deselect();
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (state.multi) return; // só as teclas da seleção múltipla
    const k = e.key.toLowerCase();
    if (state.landuse && !state.selected) return; // landuse: só as teclas do editor
    if (state.wall && !state.selected) {
      // Muro selecionado: as mesmas teclas revêem os muros.
      if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); moveWall(1); }
      else if (k === "k" || e.key === "ArrowUp") { e.preventDefault(); moveWall(-1); }
      else if (k === "a") reviewWall("accepted");
      else if (k === "r") reviewWall("rejected");
      else if (k === "u") reviewWall(null);
      return;
    }
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
    // O editor da planta só conhece a geometria: o modo da divisão mantém-se.
    state.parts.layout = { ...layout, split: state.parts.layout?.split ?? "parts" };
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
  if (state.parts.active && state.detail) renderPartsSection(); // o botão "Editar na Planta"
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
      map.stopEditing(); // dividido em partes: o contorno fica fixo
    }
    editor.load({ outline: view.outline, points: view.points, layout: state.parts.active ? state.parts.layout : null });
    renderPartsSection();
    if (state.parts.active) schedulePartsPreview(0);
  } catch (e) {
    toast(`Partes: ${e.message}`);
  }
}

function activateParts(layout, split = "parts", plan = true) {
  // plan: abrir a vista Planta (para desenhar a divisão); aplicar a sugestão fica no 3D.
  state.parts.active = true;
  state.parts.layout = { ...layout, split };
  state.parts.overrides = [];
  state.parts.info = null;
  map.stopEditing(); // as partes estão em coordenadas do contorno: este fica fixo
  editor.load({ outline: state.parts.view.outline, layout }, true);
  if (plan) setViewMode("plan");
  renderPartsSection();
  schedulePartsPreview(0);
}

async function startParts(kind, split = "parts") {
  if (!state.parts.view) await loadParts(state.selected, state.token);
  if (kind === "suggested") {
    if (!state.parts.view?.suggested) return toast("Sem divisão sugerida para este edifício.");
    activateParts(state.parts.view.suggested, split, false);
    return toast(`Divisão sugerida aplicada (${split === "buildings" ? "edifícios separados" : "building:part"}): fica ao aceitar.`, 3000);
  }
  try {
    const { layout } = await api.partsStart(state.selected);
    activateParts(layout, split);
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
  startBuildingEdit();
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
      if (res.tags) $("#tags").innerHTML = renderTags(res.tags, osmTags(state.detail)) + renderPartTags(res.parts);
    } catch (e) {
      toast(`Partes: ${e.message}`);
    }
  }, delay);
}

function pieceName(i) {
  // Parte (building:part) ou edifício (divisão em vários edifícios), com a sua cor.
  if (state.parts.layout?.split !== "buildings") return `building:part ${i + 1}`;
  return i === 0 ? "edifício 1 (fica com a way original)" : `edifício ${i + 1} (novo)`;
}

function renderPartTags(parts) {
  if (!parts?.length) return "";
  return parts
    .map((p, i) => `<div class="small" style="margin-top:6px"><b style="color:${PART_COLORS[i % PART_COLORS.length]}">${esc(pieceName(i))}</b> ${Object.entries(p.tags)
      .map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</div>`)
    .join("");
}

const PART_NUMERIC = ["height", "min_height", "roof_height", "roof_direction"];

function partValue(i, f) {
  // Valor de um campo de uma parte: a correção, senão o valor em vigor (o estimado).
  const ov = state.parts.overrides[i]?.[f];
  if (ov !== null && ov !== undefined && ov !== "") return ov;
  const v = state.parts.info?.parts?.[i]?.[f];
  return v === null || v === undefined ? "" : v;
}

function renderPartsSection() {
  const el = $("#parts-section");
  if (!el) return;
  const d = state.detail;
  if (!d.parts_editable) {
    el.innerHTML = `<p class="muted small">Divisão em partes indisponível: ${esc(d.parts_reason ?? "este edifício não se divide aqui")}.</p>`;
    return;
  }
  if (!state.parts.active) {
    const sug = d.suggestion ?? [];
    el.innerHTML = `
      ${sug.length ? `<div class="suggestion">
        <p class="small" title="Volumes com alturas ou telhados diferentes neste contorno">Sugestão: <b>${sug.length} volumes</b>
          ${sug.map((p, i) => `<span class="chip" style="border-left:3px solid ${PART_COLORS[i % PART_COLORS.length]}">${fmt(p.height, " m")} · ${esc(SHAPE_LABELS[p.roof_shape] ?? p.roof_shape)}${p.area ? ` · ${p.area} m²` : ""}</span>`).join(" ")}</p>
        <div class="actions">
          <button class="btn primary" id="p-suggested" title="Aplica a divisão sugerida como building:part do mesmo edifício (fica ao aceitar; a geometria edita-se na vista Planta)">Aplicar: building:part</button>
          <button class="btn" id="p-suggested-buildings" title="Aplica a divisão sugerida como edifícios separados, cada um com as suas tags 3D (fica ao aceitar)">Aplicar: edifícios separados</button>
        </div></div>` : ""}
      <div class="actions">
        <button class="btn" id="p-manual" title="Desenhar a divisão na vista Planta: partes do mesmo edifício (building:part), com alturas e telhados próprios">Dividir em building:part</button>
        <button class="btn" id="p-buildings" title="Desenhar a divisão na vista Planta: cada peça passa a ser um edifício (building=*) com as suas tags 3D">Dividir em edifícios</button>
      </div>`;
    $("#p-suggested")?.addEventListener("click", () => startParts("suggested"));
    $("#p-suggested-buildings")?.addEventListener("click", () => startParts("suggested", "buildings"));
    $("#p-manual").addEventListener("click", () => startParts("manual"));
    $("#p-buildings").addEventListener("click", () => startParts("manual", "buildings"));
    return;
  }
  const n = state.parts.layout.parts.length;
  const split = state.parts.layout.split ?? "parts";
  const touched = (i, f) => (state.parts.overrides[i]?.[f] ?? null) !== null;
  const t = (i, f) => (touched(i, f) ? "touched" : "");
  const shapeOpts = (i) => ["flat", "skillion", "gabled", "hipped", "side_hipped", "pyramidal", "round", "unknown"]
    .map((sh) => `<option value="${sh}" ${partValue(i, "roof_shape") === sh ? "selected" : ""}>${SHAPE_LABELS[sh]} (${sh})</option>`).join("");
  const orientOpts = (i) => `<option value="">—</option>` + [["along", "along (cumeeira ao longo)"], ["across", "across (cumeeira através)"]]
    .map(([v, l]) => `<option value="${v}" ${partValue(i, "roof_orientation") === v ? "selected" : ""}>${l}</option>`).join("");
  const colour = (i) => toHex(partValue(i, "roof_colour") || "#b5533c");
  el.innerHTML = `
    <div class="split-mode small">
      <label class="check" title="Partes do mesmo edifício: o contorno fica e as partes são ways building:part dentro dele">
        <input type="radio" name="p-split" value="parts" ${split === "parts" ? "checked" : ""}> partes do mesmo edifício (building:part)</label>
      <label class="check" title="O edifício é dividido: cada peça é um edifício (building=*) com as tags do original (sem as 3D) e as suas tags 3D. No OSM, a way original fica com a 1.ª peça.">
        <input type="radio" name="p-split" value="buildings" ${split === "buildings" ? "checked" : ""}> vários edifícios (dividir o edifício)</label>
    </div>
    <div id="parts-problems" class="problems"></div>
    <div class="parts-list">
      ${Array.from({ length: n }, (_, i) => `
        <div class="part-card ${i === state.parts.selected ? "sel" : ""}" data-part="${i}" style="border-left-color:${PART_COLORS[i % PART_COLORS.length]}">
          <h4><span>${esc(pieceName(i))}</span><span class="est small muted" data-est="${i}"></span>${i === 0 ? help("Os campos mostram o valor em vigor (estimado, ou corrigido a laranja); mudar um campo corrige-o. A divisão fica ao aceitar.") : ""}</h4>
          <div class="form">
            <label class="${t(i, "roof_shape")}">roof:shape<select data-f="roof_shape" data-i="${i}">${shapeOpts(i)}</select></label>
            <label class="${t(i, "height")}">height (m)<input data-f="height" data-i="${i}" type="number" step="0.5" min="0" value="${partValue(i, "height")}"></label>
            <label class="${t(i, "min_height")}">min_height (m)<input data-f="min_height" data-i="${i}" type="number" step="0.5" min="0" value="${partValue(i, "min_height")}"></label>
            <label class="${t(i, "roof_height")}">roof:height (m)<input data-f="roof_height" data-i="${i}" type="number" step="0.5" min="0" value="${partValue(i, "roof_height")}"></label>
            <label class="${t(i, "roof_orientation")}">roof:orientation<select data-f="roof_orientation" data-i="${i}">${orientOpts(i)}</select></label>
            <label class="${t(i, "roof_direction")}">roof:direction (°)<input data-f="roof_direction" data-i="${i}" type="number" step="1" min="0" max="359" value="${partValue(i, "roof_direction")}"></label>
            <label class="${t(i, "roof_colour")}">roof:colour<input data-f="roof_colour" data-i="${i}" type="color" list="palette" value="${colour(i)}">
              ${swatches(colour(i), `data-i="${i}"`)}</label>
          </div>
        </div>`).join("")}
    </div>
    <div class="actions">
      ${state.viewMode === "plan" ? "" : `<button class="btn" id="p-plan" title="Mover, dividir ou juntar as partes na vista Planta">Editar a divisão na Planta</button>`}
      <button class="btn" id="p-single">Voltar a edifício único</button></div>`;
  $("#p-plan")?.addEventListener("click", () => {
    setViewMode("plan");
    renderPartsSection();
  });
  el.querySelectorAll('[name="p-split"]').forEach((r) =>
    r.addEventListener("input", (e) => {
      state.parts.layout = { ...state.parts.layout, split: e.target.value };
      renderPartsSection();
      schedulePartsPreview(0);
    }),
  );
  el.querySelectorAll("[data-f]").forEach((input) =>
    input.addEventListener("input", (e) => {
      const i = Number(e.target.dataset.i), f = e.target.dataset.f;
      const o = (state.parts.overrides[i] = { ...(state.parts.overrides[i] ?? {}) });
      const v = e.target.value;
      o[f] = v === "" ? null : PART_NUMERIC.includes(f) ? Number(v) : v;
      e.target.closest("label")?.classList.toggle("touched", o[f] !== null);
      schedulePartsPreview();
    }),
  );
  wireSwatches(el);
  el.querySelectorAll(".part-card").forEach((c) =>
    c.addEventListener("click", (e) => {
      if (!e.target.closest("input, select, .swatches")) editor.selectPart(Number(c.dataset.part));
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
    const e = p.estimated ?? p;
    const span = document.querySelector(`[data-est="${i}"]`);
    if (span) {
      span.textContent = `est. ${SHAPE_LABELS[e.roof_shape] ?? e.roof_shape} · ${fmt(e.height, " m")}` +
        (e.min_height ? ` (base ${e.min_height} m)` : "") + ` · ${fmt(p.area, " m²")}`;
    }
    // Campos sem correção: o valor em vigor (o que vai nas tags), como no edifício único.
    document.querySelectorAll(`#parts-section [data-i="${i}"][data-f]`).forEach((el) => {
      const f = el.dataset.f;
      if ((state.parts.overrides[i]?.[f] ?? null) !== null) return;
      const v = p[f];
      if (f === "roof_colour") {
        if (v) {
          el.value = toHex(v);
          el.closest("label")?.querySelectorAll(".swatch").forEach((s) => s.classList.toggle("on", s.dataset.hex === el.value));
        }
      } else el.value = v === null || v === undefined ? "" : v;
    });
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

async function loadSettings() {
  try {
    const s = await api.settings();
    $("#opt-roof-colour").checked = !!s.export_roof_colour;
  } catch { /* sem servidor de opções: fica o valor por omissão */ }
}

async function setRoofColourOption(input) {
  // A revisão é recarregada no servidor com a nova política de tags.
  input.disabled = true;
  try {
    const s = await api.saveSettings({ export_roof_colour: input.checked });
    input.checked = !!s.export_roof_colour;
    const a = await api.areas();
    state.reviewGeneration = a.review_generation;
    state.summary = await api.summary();
    await reload();
    await map.reload3D(() => api.scene());
    toast(input.checked
      ? "roof:colour volta a ser proposto e exportado."
      : "roof:colour estimado deixa de ser acrescentado ou alterado (o do OSM fica; a cor escolhida à mão vai na mesma).", 6000);
  } catch (err) {
    input.checked = !input.checked;
    toast(`Opção roof:colour: ${err.message}`);
  } finally {
    input.disabled = false;
  }
}

async function loadOsmContext() {
  // Landuse, vias, muros e edifícios do OSM (camada do mapa). Avisa se faltam dados.
  try {
    await showOsmContext(await api.osmContext());
  } catch (e) {
    toast(`Camadas OSM: ${e.message}`);
  }
}

async function showOsmContext(ctx) {
  await map.setOsmContext(ctx);
  {
    const date = ctx.date ? `dados OSM de ${ctx.date.slice(0, 10)}` : "sem contexto OSM";
    $("#m-osm-date").textContent = ctx.missing.length ? `${date} · em falta: ${ctx.missing.join(", ")}` : date;
    $("#m-osm").classList.toggle("warn", ctx.missing.length > 0);
    layersWarn();
    if (ctx.missing.length) {
      toast(`Contexto OSM incompleto (obtido por uma versão anterior): faltam ${ctx.missing.join(", ")}. Usar "camadas OSM → atualizar do OSM".`, 9000);
    }
  }
}

// ------------------------------------------------------------------ muros

const WALL_TYPES = { wall: "muro", fence: "vedação" };

async function loadWalls() {
  // Muros propostos e o estado das fontes; avisa se faltam dados do OSM ou do geoportal.
  try {
    await showWalls(await api.walls());
  } catch (e) {
    toast(`Muros: ${e.message}`);
  }
}

async function showWalls({ info, geojson }) {
  state.walls = geojson.features.filter((f) => f.properties.kind === "wall").map((f) => f.properties);
  await map.setWalls(geojson);
  renderWallsInfo(info);
  if (info.warnings.length && !loadWalls.warned) {
    loadWalls.warned = true;
    toast(`Muros: ${info.warnings.join(" ")}`, 9000);
  }
}

function renderWallsInfo(info) {
  const c = info.counts ?? {};
  const counts = `${c.pending ?? 0} por rever · ${c.accepted ?? 0} aceites · ${c.rejected ?? 0} rejeitados${c.archived ? ` · ${c.archived} arquivados` : ""}`;
  const src = info.fetched ? `${esc(info.layers.join(" + "))} (${(info.fetched_at ?? "").slice(0, 10)})` : "por obter";
  $("#m-walls-info").innerHTML = `${counts}<br>fonte: ${src}${info.warnings.map((w) => `<div class="warn-text">⚠ ${esc(w)}</div>`).join("")}`;
  $("#m-walls").classList.toggle("warn", info.warnings.length > 0);
  $("#m-walls-count").textContent = `· ${c.pending ?? 0} por rever`;
  layersWarn();
}

function layersWarn() {
  // Aviso no menu "Camadas" se faltam muros ou dados do OSM.
  $("#m-layers").classList.toggle("warn", $("#m-walls").classList.contains("warn") || $("#m-osm").classList.contains("warn"));
}

function wallFilter() {
  return new Set([...document.querySelectorAll("#m-walls input:checked")].map((x) => x.value));
}

async function selectWall(key) {
  if (!key) return;
  clearMulti();
  const token = ++state.token;
  try {
    const d = await api.wall(key);
    if (token !== state.token) return;
    map.showOsmAdjustments(null);
    state.wall = d;
    state.landuse = null;
    state.selected = null;
    map.select(null);
    map.selectWall(key, d.bbox);
    if (Array.isArray(d.coords)) map.editWall(d);
    else toast("Para editar a geometria, reiniciar o servidor (osm3d review): está a correr uma versão anterior.", 8000);
    renderWallDetail();
  } catch (e) {
    toast(`Muro: ${e.message}`);
  }
}

function renderWallDetail() {
  const d = state.wall;
  const statusChip = { pending: "", accepted: "ok", rejected: "bad" }[d.status];
  const type = WALL_TYPES[d.tags.barrier] ?? d.tags.barrier;
  $("#viewer-status").textContent = "muro: sem vista 3D";
  viewer.clearAll();
  $("#detail").innerHTML = `
    <h2>${esc(type)} <span class="chip ${statusChip}">${{ pending: "por rever", accepted: "aceite", rejected: "rejeitado" }[d.status]}</span>
      ${d.archived ? '<span class="chip">arquivado</span>' : ""}</h2>
    <div class="links"><span class="chip candidate">proposta</span> <span class="muted">${esc(d.layer)} · ${d.length} m · ${d.sources.length} linha(s) municipais</span></div>
    <div class="actions">
      <button class="btn ok" id="w-accept" title="A">Aceitar</button>
      <button class="btn bad" id="w-reject" title="R">Rejeitar</button>
      <button class="btn" id="w-reset" title="U" ${d.status === "pending" ? "disabled" : ""}>Repor</button>
    </div>
    <h3>Tags</h3>
    <table class="kv">${Object.entries(d.tags).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table>
    <h3>Ligação ao landuse</h3>
    ${d.runs.length
      ? `<p class="small">Na exportação, estes limites passam para a posição do muro (tracejado laranja no mapa) e partilham os nós com ele:</p>
         <table class="kv">${d.runs.map((r) => `<tr><th>${esc(r.label)}</th><td>${r.length} m, até ${r.max_offset} m do muro</td></tr>`).join("")}</table>`
      : '<p class="small muted">Não corre ao longo de nenhum limite de landuse: vai como linha própria.</p>'}
    <h3>Geometria ${d.edited ? '<span class="chip">editada à mão</span>' : ""}</h3>
    ${EDIT_HINT}
    <div class="actions"><button class="btn" id="w-geom-reset" ${d.edited ? "" : "disabled"}>Repor geometria municipal</button></div>
    <label class="full small">nota<textarea id="w-note" rows="2">${esc(d.note ?? "")}</textarea></label>
    <p class="small muted">Ver as camadas OSM (landuse, muros já mapeados) e o ortofoto para confirmar. J/K: muro seguinte/anterior.</p>`;
  $("#w-accept").onclick = () => reviewWall("accepted");
  $("#w-reject").onclick = () => reviewWall("rejected");
  $("#w-reset").onclick = () => reviewWall(null);
  $("#w-geom-reset").onclick = () => resetGeometry();
}

// ------------------------------------------------------- edição de geometria

const EDIT_HINT = `<p class="small muted">No mapa: arrastar um vértice (encaixa em muros, landuse e edifícios;
  largado noutro muro, liga-se a ele); um vértice cinzento está ligado a outros elementos e move-os também.
  Arrastar o triângulo a meio de uma aresta ou duplo clique cria um vértice. Clique (ou botão direito) num vértice ou
  numa aresta abre as opções: apagar (Delete), desligar (D), esquadrar (Q), alinhar o muro (S). Ctrl+Z desfaz.</p>`;

const BUILDING_EDIT_TIP = "No mapa, como no iD: arrastar um vértice (encaixa em edifícios, landuse e muros); " +
  "arrastar o triângulo a meio de uma aresta ou duplo clique cria um vértice. Clique (ou botão direito) num vértice " +
  "ou numa aresta abre as opções: apagar (Delete), desligar (D), esquadrar o canto, a aresta ou o edifício " +
  "(Q: o edifício; com um vértice selecionado, o canto). Ctrl+Z desfaz.";

async function selectLanduse(key) {
  // Limite de um landuse do OSM: edita-se como no iD (os nós partilhados movem-se em
  // todas as ways que os usam).
  const id = Number(key.replace("way/", ""));
  clearMulti();
  const token = ++state.token;
  try {
    const d = await api.landuse(id);
    if (token !== state.token) return;
    map.showOsmAdjustments(null);
    state.landuse = d;
    state.wall = null;
    state.selected = null;
    map.select(null);
    map.selectWall(null);
    map.editLanduse(d);
    renderLanduseDetail();
  } catch (e) {
    toast(`Landuse: ${e.message}`);
  }
}

function renderLanduseDetail() {
  const d = state.landuse;
  $("#viewer-status").textContent = d.is_building ? "edifício fora da revisão: sem vista 3D" : "landuse: sem vista 3D";
  viewer.clearAll();
  const shared = Object.entries(d.shared ?? {});
  const others = [...new Set(shared.flatMap(([, ways]) => ways))];
  $("#detail").innerHTML = `
    <h2>${esc(d.label)} ${d.edited ? '<span class="chip">editado à mão</span>' : ""}</h2>
    <div class="links"><a href="${d.osm_url}" target="_blank" rel="noopener">abrir no OSM ↗</a>
      <span class="muted">${d.nodes.length} nós${shared.length ? ` · ${shared.length} partilhados` : ""}</span></div>
    ${d.is_building ? `<p class="small">Edifício do OSM sem análise 3D (fora da área processada ou sem LiDAR): aqui só se edita o contorno.</p>` : ""}
    <h3>Tags</h3>
    <table class="kv">${Object.entries(d.tags).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table>
    <h3>Geometria</h3>
    ${EDIT_HINT}
    ${others.length ? `<p class="small">Nós cinzentos são partilhados com: ${others.map(esc).join(", ")}. Movê-los move-os também nesses elementos.</p>` : ""}
    <p class="small muted">A edição vai no .osm da exportação, aplicada aos dados atuais do OSM (se a way mudou no OSM entretanto, não se faz e fica no relatório).</p>
    <div class="actions"><button class="btn" id="l-reset" ${d.edited ? "" : "disabled"}>Repor como no OSM</button></div>`;
  $("#l-reset").onclick = () => resetGeometry();
}

// Gravações em fila: cada edição só sai depois da anterior (sem corridas no servidor).
// A edição aparece logo no mapa (map.applyLocalEdit) e grava-se em segundo plano; as
// camadas só se recarregam do servidor (recortes, nós novos, ligações dos muros) quando a
// fila esvazia, e essa recarga fica de parte se entretanto houve outra edição.
const saveJobs = []; // por gravar (ainda não começadas)
let reloadLayers = new Set(); // camadas a recarregar quando a fila esvaziar
let editSeq = 0; // muda a cada edição local
const latestEdit = new Map(); // chave → editSeq da última edição local desse elemento
let editQueue = Promise.resolve(); // resolve quando tudo o que está na fila foi gravado
let saving = false;
window.addEventListener("beforeunload", (e) => {
  if (saving || saveJobs.length) e.preventDefault(); // edições/revisões ainda a gravar
});

function enqueueSave(job) {
  // Duas edições seguidas do mesmo elemento (vários arrastos rápidos) gravam-se de uma vez:
  // cada uma leva a geometria inteira, a última basta (com os ligados de ambas).
  const last = saveJobs.at(-1);
  if (job.kind === "edit" && last?.kind === "edit" && last.target.key === job.target.key && last.review === job.review) {
    Object.assign(last, { coords: job.coords, ids: job.ids, seq: job.seq, others: { ...last.others, ...job.others } });
  } else saveJobs.push(job);
  if (!saving) editQueue = runSaves();
  setSaveStatus();
  return editQueue;
}

async function runSaves() {
  saving = true;
  setSaveStatus();
  try {
    while (saveJobs.length || reloadLayers.size) {
      if (saveJobs.length) {
        const job = saveJobs.shift();
        setSaveStatus();
        try {
          await job.run(job);
          // Uma geometria nova muda os recortes dos vizinhos: o pré-carregado já não serve.
          if (job.kind !== "review") forgetPrefetched();
        } catch (e) {
          toast(`${job.label ?? "Edição"} não guardada: ${e.message}`, 6000);
          job.failed?.(e);
          for (const l of ["buildings", "ctx", "walls"]) reloadLayers.add(l); // o mapa volta a ser o do servidor
        }
        continue;
      }
      const layers = reloadLayers;
      reloadLayers = new Set();
      try {
        await reloadEditedMap(layers);
      } catch (e) {
        toast(`Camadas do mapa não recarregadas: ${e.message}`, 6000);
      }
    }
  } finally {
    saving = false;
    setSaveStatus();
  }
}

function setSaveStatus() {
  // Indicador discreto no mapa enquanto há edições por gravar.
  const el = $("#save-status");
  if (!el) return;
  const pending = saveJobs.length;
  el.classList.toggle("hidden", !saving && !pending);
  el.textContent = pending > 1 ? `a gravar… (+${pending} na fila)` : "a gravar…";
}

function localEdit(changes) {
  // Mostra já a edição no mapa e regista-a (respostas do servidor a edições anteriores
  // deixam de mexer na geometria do editor).
  editSeq++;
  for (const c of changes) latestEdit.set(c.key, editSeq);
  map.applyLocalEdit(changes);
  map.editor.refreshLinks();
  return editSeq;
}

// Edifício em revisão (municipal ou do OSM com análise 3D): grava-se pelo contorno do
// edifício; os outros (landuse, edifícios do OSM sem análise) pela way.
const isReviewBuilding = (key) => key.startsWith("new/") || !!state.osmKeys?.has(key);

const isLatest = (key, seq) => (latestEdit.get(key) ?? 0) <= seq;

function layersFor(key) {
  // O que muda no servidor com a geometria deste elemento: muro → muros; edifício
  // municipal → edifícios (recortes); way do OSM → contexto, edifícios (topologia) e muros
  // (ligações ao landuse).
  if (key.startsWith("wall/")) return ["walls"];
  if (key.startsWith("new/")) return ["buildings"];
  return ["ctx", "buildings", "walls"];
}

function commitEdit(target, coords, ids, others = {}) {
  const review = !!state.selected && target.key === state.selected;
  if (review && state.parts.active) {
    // As partes estão em coordenadas do contorno: mudá-lo desfazia a divisão.
    toast("Divisão em partes ativa: voltar a edifício único para editar o contorno.", 6000);
    return startBuildingEdit();
  }
  const changes = [{ key: target.key, coords, ids }, ...Object.entries(others).map(([key, o]) => ({ key, coords: o.coords, ids: o.ids }))];
  const seq = localEdit(changes);
  if (review) $("#viewer-status").textContent = "a reajustar o telhado ao contorno novo… (LiDAR)";
  enqueueSave({ kind: "edit", target, coords, ids, others, review, seq, run: saveEdit, failed: () => revertEdit(target) });
}

function revertEdit(target) {
  // Volta à geometria guardada.
  if (state.selected && target.key === state.selected) startBuildingEdit();
  else if (target.key.startsWith("wall/") && state.wall?.key === target.key) map.editWall(state.wall);
  else if (state.landuse?.key === target.key) map.editLanduse(state.landuse);
}

async function saveEdit(job) {
  // Cada alteração terminada no mapa é gravada logo (vai na exportação a partir daí).
  const { target, coords, ids, others, seq } = job;
  const layers = new Set(layersFor(target.key));
  if (target.key.startsWith("wall/")) {
    // O muro e os ligados a ele que mudaram (vértice partilhado, ligação, alinhamento).
    const walls = Object.fromEntries(Object.entries(others).map(([k, o]) => [k, o.coords]));
    const res = await api.saveWallsGeometry({ ...walls, [target.key]: coords });
    const d = res.details[target.key];
    if (state.wall?.key === d.key) {
      state.wall = d;
      renderWallDetail();
    }
  } else {
    const nodes = coords.map((c, i) => [ids?.[i] ?? null, c[0], c[1]]);
    if (job.review) {
      // Contorno do edifício em revisão (do OSM: com os nós; municipal: só o anel).
      await saveBuildingGeometry(target.key, nodes, seq);
    } else if (isReviewBuilding(target.key)) {
      // Edifício em revisão que já não está selecionado (passou-se a outro antes de gravar).
      await api.saveBuildingGeometry(target.key, nodes);
    } else {
      const d = await api.saveLanduse(Number(target.key.replace("way/", "")), nodes);
      if (state.landuse?.key === d.key) {
        state.landuse = d;
        renderLanduseDetail();
        if (map.editor.editing(d.key) && isLatest(d.key, seq)) map.editor.setGeometry(d.nodes.map((n) => [n[1], n[2]]), d.nodes.map((n) => n[0]));
      }
    }
    // Os landuse e edifícios ligados a ele que mudaram com ele (vértice partilhado
    // arrastado, ou um vértice novo onde se ligou a uma aresta deles).
    for (const k of await saveLinked(others)) for (const l of layersFor(k)) layers.add(l);
  }
  for (const l of layers) reloadLayers.add(l);
}

async function saveLinked(others) {
  const keys = [];
  for (const [key, o] of Object.entries(others ?? {})) {
    const nodes = o.coords.map((c, i) => [o.ids?.[i] ?? null, c[0], c[1]]);
    try {
      if (key.startsWith("new/")) await api.saveBuildingGeometry(key, nodes);
      else if (key.startsWith("way/")) await api.saveLanduse(Number(key.slice(4)), nodes);
      else continue;
      keys.push(key);
    } catch (e) {
      toast(`${key} (ligado ao elemento editado) não foi gravado: ${e.message}`, 7000);
      for (const l of layersFor(key)) reloadLayers.add(l);
    }
  }
  return keys;
}

async function reloadEditedMap(layers = new Set(["buildings", "ctx", "walls"])) {
  // As camadas como ficaram no servidor (o contexto OSM com as edições, os municipais com
  // os recortes, os muros com as ligações); depois, as ligações do elemento em edição aos
  // vizinhos. Se entretanto houve outra edição, o que veio já está desatualizado: fica o
  // que está no mapa (a edição nova volta a pedir a recarga).
  const seq = editSeq;
  const [gj, ctx, walls] = await Promise.all([
    layers.has("buildings") ? api.geojson() : null,
    layers.has("ctx") ? api.osmContext() : null,
    layers.has("walls") ? api.walls() : null,
  ]);
  if (seq !== editSeq) return false;
  if (gj) {
    forgetPrefetched(); // a topologia pode ter mudado (e o que se pré-carregou entretanto)
    await map.setData(municipalGeojson(gj));
  }
  if (ctx) await showOsmContext(ctx);
  if (walls) await showWalls(walls);
  map.editor.refreshLinks();
  return true;
}

async function saveBuildingGeometry(key, nodes, seq) {
  const token = state.token;
  const detail = await api.saveBuildingGeometry(key, nodes);
  if (token !== state.token) return;
  await afterFootprintChange(detail, token, seq);
}

async function afterFootprintChange(detail, token, seq = Infinity) {
  // Contorno novo: o detalhe (tags, sinalizações), o modelo e os pontos mudam (o mapa
  // recarrega-o quem chama). seq: a edição local gravada (se já houve outra depois, o
  // editor fica com a geometria local, que é mais recente).
  // A topologia da área é refeita: o pré-carregado pode ter mudado.
  forgetPrefetched();
  state.detail = detail;
  rerenderDetail();
  patchRow(detail);
  const g = detail.geometry;
  if (g?.editable && map.editor.editing(detail.key) && isLatest(detail.key, seq)) {
    map.editor.setGeometry(g.nodes.map((n) => [n[1], n[2]]), g.kind === "osm" ? g.nodes.map((n) => n[0]) : null,
      new Set(Object.keys(g.shared ?? {}).map(Number)));
  }
  const key = detail.key;
  const [mesh, pts] = await Promise.all([state.touched.size ? null : api.mesh(key), api.points(key)]);
  if (token !== state.token) return;
  if (mesh) viewer.setModel(mesh);
  else schedulePreview(); // com as correções por guardar
  viewer.setPoints(pts);
  $("#viewer-status").textContent = `${pts.count.toLocaleString("pt-PT")} pontos · rmse ${fmt(detail.effective?.rmse_m, " m")}`;
  if (map.roofs) map.updateBuilding(await api.sceneOne(key));
}

function selectOsmBuilding(key) {
  // Clique num edifício da camada do OSM: com análise 3D, abre-se como os da lista;
  // sem ela (fora da área processada), edita-se só o contorno, como um landuse.
  if (key === state.selected || map.editor.editing(key)) return;
  if (state.osmKeys?.has(key)) return select(key);
  selectLanduse(key);
}

function resetGeometry() {
  // Na fila, depois das edições ainda por gravar (senão uma delas desfazia a reposição).
  const sel = state.selected && state.detail?.geometry ? state.selected : null;
  const wall = sel ? null : state.wall?.key ?? null;
  const landuse = sel || wall ? null : state.landuse ?? null;
  const key = sel ?? wall ?? landuse?.key;
  if (!key) return;
  editSeq++; // as recargas e respostas anteriores já não servem
  latestEdit.set(key, editSeq);
  if (sel) $("#viewer-status").textContent = "a repor o contorno…";
  enqueueSave({
    kind: "reset",
    label: "Reposição",
    run: async () => {
      if (sel) {
        const token = state.token;
        const detail = await api.resetBuildingGeometry(sel);
        for (const l of layersFor(sel)) reloadLayers.add(l);
        if (token !== state.token) return;
        await afterFootprintChange(detail, token);
        startBuildingEdit(); // o histórico de edição do contorno antigo deixa de servir
        toast("Contorno reposto.", 1500);
      } else if (wall) {
        const d = await api.resetWallGeometry(wall);
        reloadLayers.add("walls");
        if (state.wall?.key !== wall) return;
        state.wall = d;
        map.editWall(d);
        renderWallDetail();
      } else {
        const d = await api.resetLanduse(landuse.id);
        for (const l of layersFor(landuse.key)) reloadLayers.add(l);
        if (state.landuse?.key !== d.key) return;
        state.landuse = d;
        map.editLanduse(d);
        renderLanduseDetail();
      }
    },
  });
}

async function reviewWall(status) {
  const d = state.wall;
  if (!d) return;
  try {
    const note = $("#w-note")?.value ?? null;
    state.wall = status ? await api.saveWallReview(d.key, status, note) : await api.resetWallReview(d.key);
    await loadWalls();
    if (status) return moveWall(1);
    renderWallDetail();
  } catch (e) {
    toast(`Erro ao guardar: ${e.message}`);
  }
}

function moveWall(delta) {
  // Muros visíveis pelo filtro, por rever primeiro.
  const show = wallFilter();
  const list = (state.walls ?? []).filter((w) => show.has(w.archived ? "archived" : w.status));
  if (!list.length) return;
  const i = list.findIndex((w) => w.key === state.wall?.key);
  const next = list[(i + delta + list.length) % list.length];
  selectWall(next.key);
}

async function fetchWalls(e) {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "a obter do geoportal…";
  try {
    const info = await api.fetchWalls();
    toast(`Muros obtidos do geoportal (${info.counts.pending ?? 0} por rever)`, 4000);
    await loadWalls();
  } catch (err) {
    toast(`Não foi possível obter os muros: ${err.message}`, 8000);
  } finally {
    btn.disabled = false;
    btn.textContent = "obter do geoportal";
  }
}

async function refreshOsmContext(e) {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "a obter do Overpass…";
  try {
    const r = await api.refreshOsmContext();
    toast(`Contexto OSM atualizado (${r.n} elementos, dados de ${(r.date ?? "?").slice(0, 10)})`, 4000);
    await loadOsmContext();
    await reload();
  } catch (err) {
    toast(`Não foi possível atualizar do OSM: ${err.message}`, 8000);
  } finally {
    btn.disabled = false;
    btn.textContent = "atualizar do OSM";
  }
}

async function reload() {
  forgetPrefetched();
  const [rows, geojson] = await Promise.all([api.buildings(), api.geojson()]);
  setRows(rows);
  renderProgress();
  applyFilters();
  await map.setData(municipalGeojson(geojson));
  const current = state.filtered.find((r) => r.key === state.selected) ?? state.filtered[0];
  if (current) select(current.key);
}

function warnCandidates(c) {
  // A lista é a dos edifícios da cartografia municipal: sem a fase candidates feita para
  // as quadrículas desta área, fica vazia (os muros não contam para isto).
  if (!c) return;
  const where = "no separador Dados, Processar com \"incluir edifícios da cartografia municipal em falta no OSM\"";
  if (!c.configured) {
    if (!c.n) toast("Sem câmaras configuradas (config/local.yaml): a lista de edifícios municipais fica vazia. Os do OSM abrem-se clicando no mapa.", 9000);
  } else if (c.state !== "done") {
    toast(`Edifícios da cartografia municipal desta área por processar (${c.reason || c.state}): ${where}.`, 12000);
  }
}

async function init() {
  wire();
  loadSettings();
  const initial = location.hash.slice(1);
  // Geração da revisão carregada por esta página (ver reviewChanged).
  try {
    const a = await api.areas();
    state.reviewGeneration = a.review_generation;
    const active = a.areas.find((x) => x.active);
    $("#area-name").textContent = active?.label || a.active;
    $("#area-name").title = `Área ativa: ${a.active}, ${active?.tiles.length ?? "?"} quadrículas (mudar no separador Dados)`;
  } catch { /* sem áreas: o erro aparece abaixo */ }
  if (initial === "data" || initial === "results") showTab(initial);
  try {
    const [rows, geojson, summary] = await Promise.all([api.buildings(), api.geojson(), api.summary()]);
    state.summary = summary;
    $("#palette").innerHTML = (summary.palette ?? []).map((c) => `<option value="${c.hex}">${esc(c.label)}</option>`).join("");
    map.setPalette(summary.palette ?? [], summary.default_colours ?? { roof: "#c8c8c8" });
    setRows(rows);
    renderProgress();
    applyFilters();
    await map.setData(municipalGeojson(geojson));
    loadOsmContext();
    loadWalls();
    warnCandidates(summary.candidates);
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
