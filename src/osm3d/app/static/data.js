// Separador "Dados": a área de estudo é um conjunto de quadrículas LiDAR de 1 km,
// escolhidas no mapa. As mesmas quadrículas definem o LAZ/MDT a descarregar e os
// edifícios OSM processados. Aqui também se descarrega, apaga e processa a área.
import { api } from "./api.js";
import { BASEMAP } from "./map.js";

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const mb = (bytes) => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`);
const ACTIVE = ["queued", "searching", "downloading"];

// Grelha só a partir deste zoom (a ~z9 o ecrã tem ~2–3 mil quadrículas de 1 km).
const GRID_MIN_ZOOM = 9;
// Tamanhos típicos por quadrícula (Vila do Conde, 2025), só para estimar o download.
const TYPICAL_MB = { lidar: 155, dtm: 13, dsm: 13 };
const SHORT = { lidar: "LAZ", dtm: "MDT", dsm: "MDS", orthophoto: "Orto" };
const REQUIRED = ["lidar", "dtm"];
const COLORS = { complete: "#16a34a", partial: "#d97706", area: "#2563eb" };
const TASK_LABELS = {
  queued: "em fila", searching: "a pesquisar", downloading: "a descarregar", done: "descarregado",
  exists: "já existe", missing: "sem dados na DGT", error: "erro", cancelled: "cancelado",
};

export class DataView {
  constructor({ mapEl, panelEl, hoverEl, toast }) {
    this.mapEl = mapEl;
    this.panelEl = panelEl;
    this.hoverEl = hoverEl;
    this.toast = toast;
    this.map = null;
    this.status = null;
    this.areas = null;
    this.known = new Map(); // nome → feature (propriedades do servidor)
    this.viewNames = new Set(); // quadrículas da vista atual do mapa
    // Edição da área: quadrículas em edição (draft) e, ao criar, o nome da área nova.
    this.draft = null;
    this.creating = null; // null = a editar a área ativa; string = nome da área nova
    this.extras = new Set(); // produtos opcionais (MDS, ortofotos)
    this.includeCache = true;
    this.processOpts = { candidates: false, refresh_osm: false };
    this.jobs = new Map(); // quadrícula → estado da tarefa de download ativa
    this.downloads = null;
    this.processing = null;
  }

  // ------------------------------------------------------------ arranque

  async show() {
    if (!this.map) await this.init();
    else this.map.resize();
    await this.refresh();
  }

  async init() {
    this.map = new maplibregl.Map({
      container: this.mapEl, style: BASEMAP, center: [-8.66, 41.284], zoom: 12,
      boxZoom: false, dragRotate: false, pitchWithRotate: false,
    });
    this.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    await new Promise((resolve) => this.map.on("load", resolve));
    const empty = { type: "FeatureCollection", features: [] };
    this.map.addSource("tiles", { type: "geojson", data: empty });
    this.map.addSource("others", { type: "geojson", data: empty });
    // Cor = ficheiros locais; contorno azul = quadrícula da área.
    this.map.addLayer({
      id: "t-fill", type: "fill", source: "tiles",
      paint: {
        "fill-color": ["match", ["get", "state"], "complete", COLORS.complete, "partial", COLORS.partial, ["case", ["get", "inArea"], COLORS.area, "#000000"]],
        "fill-opacity": ["case",
          ["all", ["get", "inArea"], ["!=", ["get", "state"], "none"]], 0.45,
          ["get", "inArea"], 0.2,
          ["!=", ["get", "state"], "none"], 0.2,
          0.02],
      },
    });
    this.map.addLayer({
      id: "t-line", type: "line", source: "tiles",
      paint: {
        "line-color": ["case", ["to-boolean", ["get", "job"]], "#7c3aed", ["get", "inArea"], "#1d4ed8", ["==", ["get", "disponivel"], "nao"], "#9ca3af", "#4b5563"],
        "line-width": ["case", ["to-boolean", ["get", "job"]], 2.5, ["get", "inArea"], 1.8, 0.5],
      },
    });
    this.map.addLayer({
      id: "others-line", type: "line", source: "others",
      paint: { "line-color": "#6b7380", "line-width": 1.2, "line-dasharray": [3, 2] },
    });
    this.map.on("click", "t-fill", (e) => {
      // Shift/Alt: tratado pela seleção por retângulo (o fim do arrasto também é um clique).
      if (e.originalEvent.shiftKey || e.originalEvent.altKey) return;
      const f = e.features[0];
      if (f) this.edit([f.properties.name], "toggle");
    });
    this.map.on("mousemove", "t-fill", (e) => this.hover(e.features[0]?.properties.name));
    this.map.on("mouseleave", "t-fill", () => this.hover(null));
    this.map.on("moveend", () => this.loadViewport());
    this.setupBoxSelect();
    this.renderLegend();
  }

  // ----------------------------------------------------------- dados

  active() {
    return this.areas?.areas.find((a) => a.active) ?? null;
  }

  dirty() {
    if (this.creating !== null) return true;
    const a = this.active();
    return a && (this.draft.size !== a.tiles.length || a.tiles.some((t) => !this.draft.has(t)));
  }

  async refresh() {
    try {
      [this.status, this.areas] = await Promise.all([api.dataStatus(), api.areas()]);
    } catch (e) {
      this.toast(`Dados: ${e.message}`);
      return;
    }
    for (const f of this.status.local.features) this.known.set(f.properties.name, f);
    const a = this.active();
    if (this.draft === null || (!this.dirty() && this.creating === null)) this.draft = new Set(a.tiles);
    await this.ensureKnown([...this.draft]);
    this.setDownloads(this.status.downloads);
    this.setProcessing(this.areas.processing);
    this.renderOthers();
    if (!this.framed) {
      this.framed = true;
      this.fitBbox(a.bbox, 0);
    }
    await this.loadViewport();
    this.renderPanel();
  }

  async ensureKnown(names) {
    const missing = names.filter((n) => !this.known.has(n));
    for (let i = 0; i < missing.length; i += 400) {
      const fc = await api.dataTilesByName(missing.slice(i, i + 400));
      for (const f of fc.features) this.known.set(f.properties.name, f);
    }
  }

  async reloadTiles(names) {
    // Estado local atualizado (depois de downloads ou limpeza).
    for (const n of names) this.known.delete(n);
    await this.ensureKnown(names);
  }

  async loadViewport() {
    if (this.map.getZoom() < GRID_MIN_ZOOM) {
      this.viewNames = new Set();
      this.renderTiles();
      this.renderLegend();
      return;
    }
    const b = this.map.getBounds();
    const bbox = [Math.max(b.getWest(), -10), Math.max(b.getSouth(), 36.5), Math.min(b.getEast(), -6), Math.min(b.getNorth(), 42.3)];
    if (!(bbox[0] < bbox[2] && bbox[1] < bbox[3])) return;
    const token = (this.viewToken = (this.viewToken ?? 0) + 1);
    try {
      const fc = await api.dataTiles(bbox);
      if (token !== this.viewToken) return;
      this.viewNames = new Set(fc.features.map((f) => f.properties.name));
      for (const f of fc.features) this.known.set(f.properties.name, f);
      this.renderTiles();
    } catch (e) {
      this.toast(`Grelha: ${e.message}`);
    }
    this.renderLegend();
  }

  fitBbox([w, s, e, n], duration = 500) {
    this.map.fitBounds([[w, s], [e, n]], { padding: 40, duration });
  }

  renderTiles() {
    if (!this.map?.getSource("tiles")) return;
    const localNames = (this.status?.local.features ?? []).map((f) => f.properties.name);
    const names = new Set([...this.viewNames, ...(this.draft ?? []), ...localNames, ...this.jobs.keys()]);
    const features = [];
    for (const n of names) {
      const f = this.known.get(n);
      if (!f) continue;
      features.push({ ...f, properties: { ...f.properties, inArea: this.draft?.has(n) ?? false, job: this.jobs.get(n) ?? null } });
    }
    this.map.getSource("tiles").setData({ type: "FeatureCollection", features });
  }

  renderOthers() {
    const poly = ([w, s, e, n]) => ({ type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] });
    this.map.getSource("others").setData({
      type: "FeatureCollection",
      features: this.areas.areas.filter((a) => !a.active).map((a) => ({ type: "Feature", properties: { name: a.name }, geometry: poly(a.bbox) })),
    });
  }

  // ------------------------------------------------------- edição da área

  async edit(names, mode) {
    // Acrescenta/retira quadrículas da área em edição (fica por guardar).
    await this.ensureKnown(names);
    for (const n of names) {
      const on = mode === "add" || (mode === "toggle" && !this.draft.has(n));
      if (on) this.draft.add(n);
      else this.draft.delete(n);
    }
    this.renderTiles();
    this.renderPanel();
  }

  setupBoxSelect() {
    // Shift+arrastar: acrescenta as quadrículas do retângulo; Alt+arrastar: retira.
    const canvas = this.map.getCanvasContainer();
    let start = null, box = null, mode = "add";
    const pos = (e) => {
      const r = canvas.getBoundingClientRect();
      return new maplibregl.Point(e.clientX - r.left, e.clientY - r.top);
    };
    const finish = async (end) => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      box?.remove();
      box = null;
      this.map.dragPan.enable();
      if (!end || Math.abs(end.x - start.x) < 4 || Math.abs(end.y - start.y) < 4) {
        // Shift/Alt+clique sem arrastar: só a quadrícula clicada.
        const hit = end && this.map.queryRenderedFeatures(end, { layers: ["t-fill"] })[0];
        if (hit) this.edit([hit.properties.name], mode);
        return;
      }
      const a = this.map.unproject(start), b = this.map.unproject(end);
      const bbox = [Math.min(a.lng, b.lng), Math.min(a.lat, b.lat), Math.max(a.lng, b.lng), Math.max(a.lat, b.lat)];
      try {
        const fc = await api.dataTiles(bbox);
        for (const f of fc.features) this.known.set(f.properties.name, f);
        await this.edit(fc.features.map((f) => f.properties.name), mode);
      } catch (e) {
        this.toast(e.message);
      }
    };
    const onMove = (e) => {
      const p = pos(e);
      if (!box) {
        box = document.createElement("div");
        box.className = `select-box ${mode}`;
        canvas.appendChild(box);
      }
      const x = Math.min(start.x, p.x), y = Math.min(start.y, p.y);
      Object.assign(box.style, { left: `${x}px`, top: `${y}px`, width: `${Math.abs(p.x - start.x)}px`, height: `${Math.abs(p.y - start.y)}px` });
    };
    const onUp = (e) => finish(pos(e));
    canvas.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || !(e.shiftKey || e.altKey)) return;
      e.preventDefault();
      e.stopPropagation();
      mode = e.altKey ? "remove" : "add";
      this.map.dragPan.disable();
      start = pos(e);
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }, true);
  }

  async addRelation() {
    const input = this.panelEl.querySelector("#a-relation");
    const id = Number(input?.value);
    if (!id) return this.toast("Indicar o id da relação OSM do limite (p.ex. 5989576).");
    this.toast("A obter o limite no OSM…", 2000);
    try {
      const r = await api.osmRelation(id);
      if (this.creating !== null && !this.creating) this.creating = r.label ?? "";
      await this.edit(r.tiles, "add");
      const fc = { features: r.tiles.map((t) => this.known.get(t)).filter(Boolean) };
      const pts = fc.features.flatMap((f) => f.geometry.coordinates[0]);
      if (pts.length) this.fitBbox([Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1]))]);
      this.toast(`${r.label ?? `Relação ${id}`}: ${r.tiles.length} quadrículas acrescentadas à área`, 4000);
    } catch (e) {
      this.toast(`Relação ${id}: ${e.message}`, 6000);
    }
  }

  startNew() {
    if (this.dirty() && !confirm("Descartar as alterações por guardar?")) return;
    this.creating = "";
    this.draft = new Set();
    this.renderTiles();
    this.renderPanel();
    this.panelEl.querySelector("#a-name")?.focus();
  }

  discard() {
    this.creating = null;
    this.draft = new Set(this.active().tiles);
    this.renderTiles();
    this.renderPanel();
  }

  async save() {
    const tiles = [...this.draft];
    if (!tiles.length) return this.toast("A área precisa de pelo menos uma quadrícula.");
    try {
      if (this.creating !== null) {
        // O id (area-<n>) é gerado pelo servidor; o nome é opcional.
        await api.createArea({ label: this.creating.trim() || null, tiles, activate: true });
        return this.reloadPage();
      }
      const a = this.active();
      await api.updateArea(a.name, { label: a.label, tiles });
      this.toast(a.processed ? "Área guardada. Processar de novo para atualizar os resultados." : "Área guardada.", 5000);
      await this.refresh();
    } catch (e) {
      this.toast(`Guardar: ${e.message}`, 6000);
    }
  }

  async rename() {
    const a = this.active();
    const label = prompt(`Nome a mostrar da área ${a.name} (vazio = sem nome):`, a.label ?? "");
    if (label === null) return;
    try {
      await api.updateArea(a.name, { label: label.trim() || null, tiles: a.tiles });
      // O nome também aparece na barra de topo: recarregar.
      this.reloadPage();
    } catch (e) {
      this.toast(`Mudar o nome: ${e.message}`, 6000);
    }
  }

  async activate(name) {
    if (this.dirty() && !confirm("Descartar as alterações por guardar?")) return;
    try {
      await api.activateArea(name);
      this.reloadPage();
    } catch (e) {
      this.toast(`Ativar: ${e.message}`, 6000);
    }
  }

  reloadPage() {
    // A revisão, o mapa 3D e os filtros dependem da área: recomeçar a página.
    location.hash = "data";
    location.reload();
  }

  async removeArea(name) {
    if (!confirm(`Remover a área "${name}" da lista?\n\nOs ficheiros raw e os resultados em data/processed/${name} não são apagados.`)) return;
    try {
      await api.deleteArea(name);
      await this.refresh();
    } catch (e) {
      this.toast(`Remover: ${e.message}`, 6000);
    }
  }

  // -------------------------------------------------------- downloads

  setDownloads(state) {
    const prevVersion = this.downloads?.version;
    this.downloads = state;
    this.jobs = new Map(state.tasks.filter((t) => ACTIVE.includes(t.status)).map((t) => [t.tile, t.status]));
    clearTimeout(this.pollTimer);
    if (state.running) this.pollTimer = setTimeout(() => this.poll(), 1000);
    return prevVersion !== undefined && prevVersion !== state.version;
  }

  async poll() {
    let state;
    try {
      state = await api.downloads();
    } catch {
      this.pollTimer = setTimeout(() => this.poll(), 3000);
      return;
    }
    const wasRunning = this.downloads?.running;
    const changed = this.setDownloads(state);
    if (changed || (wasRunning && !state.running)) {
      const names = state.tasks.map((t) => t.tile);
      await this.reloadTiles([...new Set(names)]);
      await this.refresh();
      if (wasRunning && !state.running) this.announceFinished(state);
    } else {
      this.renderTiles();
      this.renderDownloads();
    }
  }

  announceFinished(state) {
    const c = state.counts;
    const parts = [
      c.done && `${c.done} descarregados`, c.exists && `${c.exists} já existiam`,
      c.missing && `${c.missing} sem dados`, c.error && `${c.error} com erro`, c.cancelled && `${c.cancelled} cancelados`,
    ].filter(Boolean);
    this.toast(`Downloads terminados: ${parts.join(", ")}`, 6000);
  }

  products() {
    return [...REQUIRED, ...this.extras];
  }

  missingTiles() {
    const a = this.active();
    return a.tiles.filter((t) => {
      const local = this.known.get(t)?.properties.local ?? {};
      return this.products().some((p) => !local[p] || local[p].partial);
    });
  }

  estimate(tiles, products) {
    let bytes = 0, unknown = false;
    for (const n of tiles) {
      const local = this.known.get(n)?.properties.local ?? {};
      for (const p of products) {
        if (local[p] && !local[p].partial) continue;
        if (TYPICAL_MB[p]) bytes += TYPICAL_MB[p] * 1e6;
        else unknown = true;
      }
    }
    return { bytes, unknown };
  }

  async download() {
    const tiles = this.missingTiles();
    if (!tiles.length) return this.toast("A área já tem todos os ficheiros.");
    if (!this.status.credentials) {
      return this.toast("Faltam as credenciais da DGT: definir DGT_USER e DGT_PASSWORD no .env e reiniciar.", 8000);
    }
    const est = this.estimate(tiles, this.products());
    const what = this.products().map((p) => SHORT[p]).join(" + ");
    const ortho = est.unknown
      ? "\n\nOrtofotos: a DGT distribui-as em folhas de ~8 × 5 km (~480 MB cada, 25 cm). Cada folha cobre ~40 quadrículas e só é descarregada uma vez."
      : "";
    if (!confirm(`Descarregar ${what} de ${tiles.length} quadrícula(s) da área (~${mb(est.bytes)}${est.unknown ? " + ortofotos" : ""})?${ortho}`)) return;
    try {
      const state = await api.startDownloads(tiles, this.products());
      this.setDownloads(state);
      this.renderTiles();
      this.renderPanel();
    } catch (e) {
      this.toast(`Download: ${e.message}`);
    }
  }

  async cancelDownloads() {
    this.setDownloads(await api.cancelDownloads());
    this.toast("A cancelar downloads…", 2000);
    this.renderDownloads();
  }

  bytesOf(tiles) {
    // Bytes em disco das quadrículas: uma folha partilhada (ortofoto) conta uma vez.
    const files = new Map();
    for (const n of tiles) {
      for (const v of Object.values(this.known.get(n)?.properties.local ?? {})) {
        for (const f of v.files) files.set(f, v.size / v.files.length);
      }
    }
    return [...files.values()].reduce((s, b) => s + b, 0);
  }

  async removeFiles(tiles, label) {
    const products = Object.keys(SHORT);
    const withFiles = tiles.filter((n) => this.known.get(n)?.properties.size > 0);
    if (!withFiles.length) return this.toast("Não há ficheiros locais para apagar.");
    const bytes = this.bytesOf(withFiles);
    const sheets = withFiles.some((n) => (this.known.get(n).properties.local.orthophoto?.shared ?? 1) > 1);
    const shared = this.areas.areas
      .filter((a) => !a.active)
      .map((a) => [a.name, a.tiles.filter((t) => withFiles.includes(t))])
      .filter(([, t]) => t.length);
    const msg = `Apagar os ficheiros raw de ${label} (${withFiles.length} quadrícula(s), ${mb(bytes)})?\n\n` +
      `${withFiles.slice(0, 12).join(", ")}${withFiles.length > 12 ? "…" : ""}\n\n` +
      (shared.length ? `Também usadas por: ${shared.map(([n, t]) => `${n} (${t.length})`).join(", ")}.\n` : "") +
      (sheets ? "As folhas de ortofoto cobrem também quadrículas vizinhas: deixam de existir para elas.\n" : "") +
      (this.includeCache ? "As caches de pontos da revisão destes LAZ também são apagadas.\n" : "") +
      "Não pode ser desfeito: será preciso voltar a descarregá-los.";
    if (!confirm(msg)) return;
    try {
      const r = await api.deleteRaw(withFiles, products, this.includeCache);
      this.toast(`${r.n_files} ficheiros apagados (${mb(r.freed_bytes)} libertados)`, 5000);
      await this.reloadTiles(withFiles);
      await this.refresh();
    } catch (e) {
      this.toast(`Eliminar: ${e.message}`, 6000);
    }
  }

  // ---------------------------------------------------------- processamento

  async process() {
    const a = this.active();
    const nMissing = a.tiles.length - a.tiles_complete.length;
    const warn = nMissing
      ? `\n\nAtenção: ${nMissing} de ${a.tiles.length} quadrículas não têm LAZ + MDT. Os edifícios nessas quadrículas ficam sem reconstrução.`
      : "";
    const stages = `edifícios OSM${this.processOpts.refresh_osm ? " (obtidos de novo)" : ""} → preparar → telhados → cores → QA${this.processOpts.candidates ? " → candidatos" : ""}`;
    if (!confirm(`Processar a área "${a.label || a.name}" (${a.tiles.length} quadrículas)?\n\n${stages}${warn}\n\nPode demorar vários minutos; a revisão é recarregada no fim.`)) return;
    try {
      this.setProcessing(await api.startProcessing(this.processOpts));
      this.renderProcessing();
    } catch (e) {
      this.toast(`Processar: ${e.message}`, 6000);
    }
  }

  setProcessing(state) {
    const wasRunning = this.processing?.running;
    this.processing = state;
    clearTimeout(this.procTimer);
    if (state.running) this.procTimer = setTimeout(() => this.pollProcessing(), 1500);
    return wasRunning && !state.running;
  }

  async pollProcessing() {
    try {
      if (this.setProcessing(await api.processing())) {
        const p = this.processing;
        this.toast(p.ok ? `Área ${p.area} processada.` : `Processamento de ${p.area} terminou ${p.error ? "com erro" : "(cancelado)"}.`, 8000);
        await this.refresh();
      } else {
        this.renderProcessing();
      }
    } catch {
      this.procTimer = setTimeout(() => this.pollProcessing(), 4000);
    }
  }

  // ------------------------------------------------------------ painel

  hover(name) {
    this.hoverName = name;
    this.renderLegend();
  }

  renderLegend() {
    const sw = (c, o = 0.5) => `<span class="sw" style="background:${c};opacity:${o}"></span>`;
    const line = (style) => `<span class="sw" style="background:none;border:${style}"></span>`;
    let info = "";
    const f = this.hoverName && this.known.get(this.hoverName);
    if (f) {
      const p = f.properties;
      const local = Object.entries(p.local).map(([k, v]) => `${SHORT[k]}${v.partial ? " (incompleto)" : ""}`).join(", ");
      const job = this.jobs.get(p.name);
      info = `<div class="hover-info"><b>${esc(p.name)}</b>${this.draft?.has(p.name) ? " · na área" : ""}${p.lote ? ` · lote ${p.lote}` : ""}<br>` +
        `local: ${local ? `${esc(local)} (${mb(p.size)})` : "nada"}${job ? ` · <b>${TASK_LABELS[job]}</b>` : ""}</div>`;
    }
    const zoomHint = this.map && this.map.getZoom() < GRID_MIN_ZOOM ? `<div class="muted">aproxime o mapa para ver a grelha</div>` : "";
    this.hoverEl.innerHTML = info +
      `<div>${line("2px solid #1d4ed8")}quadrícula da área</div>` +
      `<div>${sw(COLORS.complete)}LAZ + MDT locais</div>` +
      `<div>${sw(COLORS.partial)}ficheiros incompletos</div>` +
      `<div>${line("2px solid #7c3aed")}download em curso</div>` +
      `<div>${line("1px dashed #6b7380")}outras áreas</div>` + zoomHint;
  }

  renderPanel() {
    const st = this.status, a = this.active();
    if (!st || !a) return;
    const others = this.areas.areas.filter((x) => !x.active);
    const when = (t) => new Date(t * 1000).toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "short" });
    const dirty = this.dirty();
    const added = [...this.draft].filter((t) => !a.tiles.includes(t)).length;
    const removed = a.tiles.filter((t) => !this.draft.has(t)).length;
    const missing = this.missingTiles();
    const est = this.estimate(missing, this.products());
    const areaBytes = this.bytesOf(a.tiles);
    const running = this.processing?.running;
    const warnings = [
      !st.index.available && `Grelha oficial não encontrada (<code>${esc(st.index.path)}</code>): usa-se a malha de 1 km calculada.`,
      !st.credentials && "Sem credenciais da DGT: definir <code>DGT_USER</code> e <code>DGT_PASSWORD</code> no <code>.env</code> para descarregar.",
    ].filter(Boolean);
    const lock = dirty ? 'disabled title="Guardar ou descartar primeiro as alterações à área"' : "";

    this.panelEl.innerHTML = `
      ${warnings.map((w) => `<div class="warn-box small">${w}</div>`).join("")}
      <h2>${this.creating !== null
        ? "Nova área"
        : `${a.label ? esc(a.label) : "Área"} <span class="chip" title="Identificador (pasta data/processed/${esc(a.name)})">${esc(a.name)}</span> <span class="chip ok">ativa</span>
           <button class="btn mini-light" id="a-rename" title="Mudar o nome a mostrar">✎</button>`}</h2>
      ${this.creating !== null
        ? `<div class="form"><label class="full">nome (opcional; o id area-… é gerado)<input id="a-name" value="${esc(this.creating)}" placeholder="p.ex. Vila do Conde"></label></div>`
        : `<div class="small area-meta">
            <b>${a.tiles.length}</b> quadrículas (${a.tiles.length} km²) · LAZ + MDT em <b>${a.tiles_complete.length}</b> ·
            ${a.processed ? `processada em ${when(a.processed_at)}` : "<b>ainda não processada</b>"}
            ${a.stale ? ' · <span class="warn-text">quadrículas alteradas depois do processamento</span>' : ""}
          </div>`}
      <p class="small muted">A área é o conjunto de quadrículas a azul: o LAZ/MDT descarregado e os edifícios OSM processados são sempre os dessas quadrículas.
        Clique numa quadrícula para a pôr ou tirar; <kbd>Shift</kbd>+arrastar acrescenta, <kbd>Alt</kbd>+arrastar retira.</p>
      <div class="area-row">
        <input id="a-relation" type="number" min="1" placeholder="relação OSM do limite (p.ex. 3892769)" title="Acrescenta as quadrículas que cobrem um limite administrativo do OSM">
        <button class="btn" id="a-add-relation">Juntar limite</button>
        <button class="btn" id="a-clear" ${this.draft.size ? "" : "disabled"}>Limpar</button>
      </div>
      ${dirty ? `<div class="area-changes small">
          ${this.creating !== null ? `<b>${this.draft.size}</b> quadrículas selecionadas` : `Alterações por guardar: <b>+${added}</b> / <b>−${removed}</b> quadrículas (${this.draft.size} no total)`}
          <span class="actions">
            <button class="btn primary" id="a-save">${this.creating !== null ? "Criar e ativar" : "Guardar área"}</button>
            <button class="btn" id="a-discard">${this.creating !== null ? "Cancelar" : "Descartar"}</button>
          </span></div>` : ""}

      <h3>Dados LiDAR da área</h3>
      <div class="products">
        <span class="small">LAZ + MDT (necessários)</span>
        ${["dsm", "orthophoto"].map((p) => `<label class="check small" title="${p === "dsm" ? "MDS 50 cm, por quadrícula (~13 MB)" : "Ortofotos 25 cm em folhas de ~8 × 5 km (~480 MB cada, partilhadas por ~40 quadrículas)"}"><input type="checkbox" data-extra="${p}" ${this.extras.has(p) ? "checked" : ""}> ${p === "dsm" ? "MDS" : "ortofotos (folhas ~480 MB)"}</label>`).join("")}
      </div>
      <div class="actions">
        <button class="btn primary" id="d-download" ${dirty || !missing.length ? "disabled" : ""} ${lock}>Descarregar em falta${missing.length ? ` (${missing.length} quadr., ~${mb(est.bytes)})` : " — nada em falta"}</button>
        <button class="btn bad" id="d-delete" ${dirty || !areaBytes ? "disabled" : ""} ${lock}>Eliminar ficheiros da área${areaBytes ? ` (${mb(areaBytes)})` : ""}</button>
      </div>
      <label class="check small"><input type="checkbox" id="d-cache" ${this.includeCache ? "checked" : ""}> ao eliminar LAZ, apagar também a cache de pontos da revisão</label>
      <div id="d-downloads"></div>

      <h3>Processamento</h3>
      <div class="actions">
        <button class="btn primary" id="p-start" ${dirty || running ? "disabled" : ""} ${lock}>Processar área</button>
      </div>
      <label class="check small"><input type="checkbox" id="p-candidates" ${this.processOpts.candidates ? "checked" : ""}> incluir edifícios em falta no OSM (candidatos)</label>
      <label class="check small"><input type="checkbox" id="p-refresh" ${this.processOpts.refresh_osm ? "checked" : ""}> voltar a obter os edifícios do OSM</label>
      <div id="a-processing"></div>

      <h3>Áreas</h3>
      ${others.length ? `<table class="kv areas-table">${others.map((x) => `<tr>
          <td><b>${esc(x.label || x.name)}</b>${x.label ? ` <span class="chip">${esc(x.name)}</span>` : ""}<div class="muted small">${x.tiles.length} quadr. · LAZ+MDT ${x.tiles_complete.length}${x.processed ? " · ✔ processada" : ""}</div></td>
          <td class="right"><button class="btn mini-light" data-activate="${esc(x.name)}" ${running ? "disabled" : ""}>Ativar</button>
            ${x.from_config ? "" : `<button class="btn mini-light" data-remove="${esc(x.name)}" title="Remover da lista (os dados ficam)">✕</button>`}</td></tr>`).join("")}</table>`
        : '<p class="muted small">Só existe esta área.</p>'}
      <div class="actions"><button class="btn" id="a-new" ${this.creating !== null ? "disabled" : ""}>Nova área</button></div>

      <h3>Ficheiros locais <span class="muted small">${mb(st.totals.bytes)}</span></h3>
      ${this.renderLocal()}`;

    const $p = (s) => this.panelEl.querySelector(s);
    $p("#a-name")?.addEventListener("input", (e) => (this.creating = e.target.value));
    $p("#a-rename")?.addEventListener("click", () => this.rename());
    $p("#a-add-relation").onclick = () => this.addRelation();
    $p("#a-relation").onkeydown = (e) => e.key === "Enter" && this.addRelation();
    $p("#a-clear").onclick = () => { this.draft.clear(); this.renderTiles(); this.renderPanel(); };
    $p("#a-save")?.addEventListener("click", () => this.save());
    $p("#a-discard")?.addEventListener("click", () => this.discard());
    this.panelEl.querySelectorAll("[data-extra]").forEach((el) => (el.oninput = () => {
      if (el.checked) this.extras.add(el.dataset.extra);
      else this.extras.delete(el.dataset.extra);
      this.renderPanel();
    }));
    $p("#d-download").onclick = () => this.download();
    $p("#d-delete").onclick = () => this.removeFiles(a.tiles, `"${a.label || a.name}"`);
    $p("#d-cache").oninput = (e) => (this.includeCache = e.target.checked);
    $p("#p-start").onclick = () => this.process();
    $p("#p-candidates").oninput = (e) => (this.processOpts.candidates = e.target.checked);
    $p("#p-refresh").oninput = (e) => (this.processOpts.refresh_osm = e.target.checked);
    $p("#a-new").onclick = () => this.startNew();
    this.panelEl.querySelectorAll("[data-activate]").forEach((el) => (el.onclick = () => this.activate(el.dataset.activate)));
    this.panelEl.querySelectorAll("[data-remove]").forEach((el) => (el.onclick = () => this.removeArea(el.dataset.remove)));
    this.panelEl.querySelectorAll("tr[data-tile] td.t-go").forEach((el) => (el.onclick = () => this.focusTile(el.parentElement.dataset.tile)));
    this.panelEl.querySelectorAll("[data-delete-tile]").forEach((el) => (el.onclick = () => this.removeFiles([el.dataset.deleteTile], `a quadrícula ${el.dataset.deleteTile}`)));
    this.renderDownloads();
    this.renderProcessing();
  }

  renderLocal() {
    const feats = this.status.local.features;
    if (!feats.length) return '<p class="muted small">Ainda não há ficheiros descarregados.</p>';
    const cell = (p, k) => {
      const v = p.local[k];
      if (!v) return "<td class='muted'>—</td>";
      const shared = v.shared > 1 ? `\nfolha partilhada por ${v.shared} quadrículas` : "";
      return `<td class="${v.partial ? "warn" : "ok"}" title="${esc(v.files.join("\n") + shared)}">${v.partial ? "incompleto" : v.shared > 1 ? "folha" : mb(v.size)}</td>`;
    };
    return `<table class="kv local-table"><tr><th>quadrícula</th>${Object.values(SHORT).map((s) => `<th>${s}</th>`).join("")}<th>total</th><th></th></tr>
      ${feats.map((f) => {
        const p = f.properties;
        const busy = this.jobs.has(p.name);
        return `<tr data-tile="${p.name}" class="${this.draft?.has(p.name) ? "sel" : ""}">
          <td class="t-go" title="ver no mapa"><b>${p.name}</b></td>${Object.keys(SHORT).map((k) => cell(p, k)).join("")}<td>${mb(p.size)}</td>
          <td><button class="btn mini-light" data-delete-tile="${p.name}" ${busy ? "disabled" : ""} title="Apagar os ficheiros desta quadrícula">✕</button></td></tr>`;
      }).join("")}</table>`;
  }

  renderDownloads() {
    const el = this.panelEl.querySelector("#d-downloads");
    if (!el) return;
    const d = this.downloads;
    if (!d || !d.tasks.length) {
      el.innerHTML = "";
      return;
    }
    const active = d.tasks.filter((t) => ACTIVE.includes(t.status)).length;
    const summary = Object.entries(d.counts).map(([k, v]) => `${TASK_LABELS[k]} ${v}`).join(" · ");
    el.innerHTML = `<div class="small dl-summary">${d.running ? `<b>${active} por concluir</b> · ` : ""}${summary}
        ${d.running ? '<button class="btn mini-light" id="d-cancel">Cancelar</button>' : ""}</div>
      <div class="tasks">${d.tasks.map((t) => {
        const pct = t.total_bytes ? Math.min(100, (100 * t.done_bytes) / t.total_bytes) : 0;
        return `<div class="task ${t.status}">
          <span class="t-name">${t.tile} ${SHORT[t.product] ?? t.product}</span>
          <span class="t-status">${TASK_LABELS[t.status]}</span>
          ${t.status === "downloading" ? `<span class="t-bar"><span style="width:${pct.toFixed(1)}%"></span></span>` : ""}
          ${t.message ? `<span class="t-msg small muted" title="${esc(t.message)}">${esc(t.message)}</span>` : ""}
        </div>`;
      }).join("")}</div>`;
    el.querySelector("#d-cancel")?.addEventListener("click", () => this.cancelDownloads());
  }

  renderProcessing() {
    const el = this.panelEl.querySelector("#a-processing");
    const p = this.processing;
    if (!el) return;
    if (!p || !p.area) {
      el.innerHTML = "";
      return;
    }
    const LABEL = { pending: "", running: "…", done: "✔", error: "✖", skipped: "–", cancelled: "–" };
    const review = !p.running && p.ok && p.area === this.areas?.active
      ? '<button class="btn ok" id="p-open">Abrir revisão</button>' : "";
    el.innerHTML = `
      <div class="proc-head small"><b>${p.running ? "A processar" : p.ok ? "Processada" : p.error ? "Falhou" : "Interrompido"}: ${esc(p.area)}</b>
        ${p.running ? '<button class="btn mini-light" id="p-cancel" title="Termina no fim da fase em curso">Cancelar</button>' : review}</div>
      <div class="stages">${p.stages.map((s) => `<span class="stage ${s.status}">${s.stage} ${LABEL[s.status] ?? ""}</span>`).join("")}</div>
      ${p.error ? `<div class="problems">${esc(p.error)}</div>` : ""}
      <pre class="proc-log">${esc(p.log.slice(-12).join("\n"))}</pre>`;
    el.querySelector("#p-cancel")?.addEventListener("click", async () => {
      this.setProcessing(await api.cancelProcessing());
      this.renderProcessing();
    });
    el.querySelector("#p-open")?.addEventListener("click", () => { location.hash = "review"; location.reload(); });
  }

  focusTile(name) {
    const f = this.known.get(name);
    if (!f) return;
    const ring = f.geometry.coordinates[0];
    const lons = ring.map((c) => c[0]), lats = ring.map((c) => c[1]);
    this.map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: 120, duration: 500, maxZoom: 14 });
  }
}
