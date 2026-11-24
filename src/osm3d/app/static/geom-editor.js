// Edição de geometria no mapa (muros propostos e landuse do OSM), ao estilo do iD:
//  - arrastar um vértice move-o; encaixa em vértices e arestas próximos (muros, landuse,
//    edifícios). Um vértice ligado a outros muros (o mesmo ponto) move-os a todos;
//  - largar um vértice em cima de outro muro liga-os (numa aresta, cria lá o vértice);
//    em cima de outro vértice do mesmo elemento, os dois juntam-se (o arrastado sai);
//    um landuse largado num nó de outra way do OSM passa a usar esse nó;
//  - arrastar o ponto a meio de uma aresta, ou duplo clique numa aresta, cria um vértice;
//  - clique (ou botão direito) num vértice ou numa aresta abre o menu de opções: apagar o
//    vértice (Delete), desligar (D), acrescentar um vértice, alinhar o muro (S);
//  - Ctrl+Z desfaz; Esc fecha o menu ou tira a seleção.
// Cada alteração terminada é entregue a onCommit(target, coords, ids, others), em que
// others são os outros muros que mudaram com ela ({ chave: coords }).

const SNAP_PX = 10; // encaixe num vértice
const EDGE_SNAP_PX = 7; // encaixe numa aresta
const MID_MIN_PX = 26; // arestas mais curtas (no ecrã) não mostram o ponto a meio
const HIT_PX = 7; // clique/duplo clique numa aresta
const SAME_DEG = 1e-8; // ~1 mm: o mesmo ponto (= o mesmo nó na exportação)
const DISCONNECT_M = 0.05; // desligar: o vértice afasta-se 5 cm ao longo do próprio muro
const EMPTY = { type: "FeatureCollection", features: [] };
const LAYERS = ["edit-line", "edit-mid", "edit-vertex"];

const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));
const same = (a, b) => Math.abs(a[0] - b[0]) <= SAME_DEG && Math.abs(a[1] - b[1]) <= SAME_DEG;

function closestOnSegment(p, a, b) {
  // Ponto de [a, b] mais perto de p (em píxeis) e o parâmetro t.
  const dx = b.x - a.x, dy = b.y - a.y;
  const l2 = dx * dx + dy * dy || 1;
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2));
  const q = { x: a.x + t * dx, y: a.y + t * dy };
  return { q, t, d: Math.hypot(q.x - p.x, q.y - p.y) };
}

function projectOnLine(c, a, b) {
  // Projeção de c na reta a–b (lon/lat como plano: distâncias de poucos metros).
  const k = Math.cos((a[1] * Math.PI) / 180);
  const ax = a[0] * k, bx = b[0] * k, cx = c[0] * k;
  const dx = bx - ax, dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy || 1;
  const t = ((cx - ax) * dx + (c[1] - a[1]) * dy) / l2;
  return [(ax + t * dx) / k, a[1] + t * dy];
}

function towards(a, b, meters) {
  // Ponto a `meters` de a, na direção de b.
  const k = Math.cos((a[1] * Math.PI) / 180);
  const dx = (b[0] - a[0]) * k * 111320, dy = (b[1] - a[1]) * 110540;
  const d = Math.hypot(dx, dy);
  if (d < 1e-6) return [a[0] + meters / (111320 * k), a[1]];
  const f = Math.min(meters, d / 2) / d;
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
}

export class GeometryEditor {
  // snapTargets(point, px): elementos à volta ({ key, source, coords }); wallsIndex():
  // Map chave → geometria dos muros (para as ligações); onCommit: alteração terminada.
  constructor(map, { snapTargets, wallsIndex, onCommit }) {
    this.map = map;
    this.snapTargets = snapTargets;
    this.wallsIndex = wallsIndex;
    this.onCommit = onCommit;
    this.target = null;
    this.history = [];
    this.selected = null;
    this.drag = null;
    this.frame = null;
    this.menu = null;
  }

  install() {
    const m = this.map;
    m.addSource("edit", { type: "geojson", data: EMPTY });
    m.addLayer({ id: "edit-line", type: "line", source: "edit", filter: ["==", ["get", "kind"], "line"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["case", ["get", "other"], "#60a5fa", "#2563eb"], "line-width": ["case", ["get", "other"], 2, 3] } });
    m.addLayer({ id: "edit-mid", type: "circle", source: "edit", filter: ["==", ["get", "kind"], "mid"],
      paint: { "circle-radius": 4, "circle-color": "#ffffff", "circle-opacity": 0.85,
        "circle-stroke-color": "#2563eb", "circle-stroke-width": 1.5 } });
    m.addLayer({ id: "edit-vertex", type: "circle", source: "edit", filter: ["==", ["get", "kind"], "vertex"],
      paint: {
        "circle-radius": ["case", ["get", "selected"], 7, 5.5],
        "circle-color": ["case", ["get", "selected"], "#2563eb", ["get", "snapped"], "#fde047", "#ffffff"],
        // Vértice ligado a outros elementos (muros, landuse, vias): mexe-lhes também.
        "circle-stroke-color": ["case", ["get", "shared"], "#ea580c", "#1d4ed8"],
        "circle-stroke-width": ["case", ["get", "shared"], 3, 2],
      } });
    m.on("mousedown", "edit-vertex", (e) => this.startDrag(e, e.features[0].properties.idx, false));
    m.on("mousedown", "edit-mid", (e) => this.startDrag(e, e.features[0].properties.idx, true));
    for (const id of ["edit-vertex", "edit-mid"]) {
      m.on("mouseenter", id, () => (m.getCanvas().style.cursor = id === "edit-vertex" ? "move" : "copy"));
      m.on("mouseleave", id, () => (m.getCanvas().style.cursor = ""));
    }
    m.on("mousemove", (e) => this.onMove(e));
    m.on("mouseup", (e) => this.endDrag(e));
    // Largou fora do mapa: depois do mouseup do mapa (que trata o clique simples).
    window.addEventListener("mouseup", () => setTimeout(() => this.drag && this.endDrag(null), 0));
    m.on("click", (e) => this.onClick(e));
    m.on("contextmenu", (e) => this.onContextMenu(e));
    m.getCanvas().addEventListener("contextmenu", (ev) => this.target && ev.preventDefault());
    m.on("dblclick", (e) => this.onDblClick(e));
    m.on("movestart", () => this.closeMenu());
    m.on("zoomend", () => this.render()); // os pontos a meio dependem do zoom
    document.addEventListener("keydown", (e) => this.onKey(e));
    document.addEventListener("mousedown", (e) => {
      if (this.menu && !this.menu.contains(e.target)) this.closeMenu();
    });
  }

  // ------------------------------------------------------------- estado

  start(target) {
    this.closeMenu();
    const t = {
      key: target.key,
      kind: target.key.startsWith("wall/") ? "wall" : "landuse",
      closed: !!target.closed,
      coords: clone(target.coords),
      ids: target.ids ? clone(target.ids) : null,
      shared: target.shared ?? new Set(),
      min: target.min ?? (target.closed ? 3 : 2),
      links: [], // muros: por vértice, [{ key, i }] dos outros muros com o mesmo ponto
      others: new Map(), // muros ligados: chave → coords (atualizadas ao arrastar)
    };
    this.target = t;
    if (t.kind === "wall") this.findLinks();
    this.history = [];
    this.selected = null;
    this.drag = null;
    this.map.doubleClickZoom.disable(); // duplo clique cria vértices
    this.raise();
    this.render();
  }

  stop() {
    this.closeMenu();
    this.target = null;
    this.drag = null;
    this.selected = null;
    this.map.doubleClickZoom.enable();
    this.map.getSource("edit")?.setData(EMPTY);
  }

  editing(key) {
    return this.target?.key === key;
  }

  linkedKeys() {
    return this.target ? [...this.target.others.keys()] : [];
  }

  findLinks() {
    // Muros com um vértice no mesmo ponto que um vértice do muro em edição.
    const t = this.target;
    t.links = t.coords.map(() => []);
    t.others = new Map();
    for (const [key, g] of this.wallsIndex()) {
      if (key === t.key || g?.type !== "LineString") continue;
      g.coordinates.forEach((c, i) => {
        t.coords.forEach((v, j) => {
          if (!same(c, v)) return;
          t.links[j].push({ key, i });
          if (!t.others.has(key)) t.others.set(key, clone(g.coordinates));
        });
      });
    }
  }

  setGeometry(coords, ids = null) {
    // Geometria confirmada pelo servidor (sem perder a seleção nem o histórico).
    if (!this.target || this.drag) return;
    this.target.coords = clone(coords);
    if (ids) this.target.ids = clone(ids);
    if (this.target.kind === "wall") this.findLinks();
    if (this.selected !== null && this.selected >= coords.length) this.selected = null;
    this.render();
  }

  raise() {
    // Por cima das outras camadas (as que forem acrescentadas depois também).
    for (const id of LAYERS) if (this.map.getLayer(id)) this.map.moveLayer(id);
  }

  hitTest(point) {
    // O clique foi num vértice, ponto a meio ou aresta em edição (não seleciona outra coisa)?
    if (!this.target) return false;
    const box = [[point.x - HIT_PX, point.y - HIT_PX], [point.x + HIT_PX, point.y + HIT_PX]];
    return this.map.queryRenderedFeatures(box, { layers: LAYERS }).length > 0;
  }

  isShared(i) {
    const t = this.target;
    if (t.kind === "wall") return t.links[i]?.length > 0;
    const id = t.ids?.[i];
    return id != null && t.shared.has(id);
  }

  // ------------------------------------------------------------- desenho

  segments() {
    const n = this.target.coords.length;
    const out = [];
    for (let i = 0; i < n - 1; i++) out.push([i, i + 1]);
    if (this.target.closed && n > 2) out.push([n - 1, 0]);
    return out;
  }

  render() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.draw();
    });
  }

  draw() {
    const src = this.map.getSource("edit");
    if (!src) return;
    if (!this.target) return src.setData(EMPTY);
    const { coords, closed, others } = this.target;
    const line = closed ? [...coords, coords[0]] : coords;
    const feats = [{ type: "Feature", geometry: { type: "LineString", coordinates: line }, properties: { kind: "line", other: false } }];
    for (const c of others.values()) {
      feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: c }, properties: { kind: "line", other: true } });
    }
    for (const [i, j] of this.segments()) {
      const a = this.map.project(coords[i]), b = this.map.project(coords[j]);
      if (Math.hypot(a.x - b.x, a.y - b.y) < MID_MIN_PX) continue;
      const mid = [(coords[i][0] + coords[j][0]) / 2, (coords[i][1] + coords[j][1]) / 2];
      feats.push({ type: "Feature", geometry: { type: "Point", coordinates: mid }, properties: { kind: "mid", idx: i } });
    }
    coords.forEach((c, i) => {
      feats.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: c },
        properties: { kind: "vertex", idx: i, selected: i === this.selected, shared: this.isShared(i), snapped: this.drag?.idx === i && !!this.drag.snap },
      });
    });
    src.setData({ type: "FeatureCollection", features: feats });
  }

  // ------------------------------------------------------------- histórico

  snapshot() {
    const t = this.target;
    return { coords: clone(t.coords), ids: clone(t.ids), links: clone(t.links), others: clone([...t.others]) };
  }

  pushHistory() {
    this.history.push(this.snapshot());
    if (this.history.length > 100) this.history.shift();
  }

  restore(s) {
    const t = this.target;
    const before = new Map(t.others);
    t.coords = s.coords;
    t.ids = s.ids;
    t.links = s.links;
    t.others = new Map(s.others);
    // Os muros ligados que mudaram desde então voltam também (são gravados).
    return [...before.keys()].filter((k) => JSON.stringify(before.get(k)) !== JSON.stringify(t.others.get(k)));
  }

  // ------------------------------------------------------------- edição

  insertVertex(i, c) {
    // Novo vértice na posição i do elemento em edição.
    const t = this.target;
    t.coords.splice(i, 0, c);
    if (t.ids) t.ids.splice(i, 0, null);
    if (t.kind === "wall") t.links.splice(i, 0, []);
  }

  removeVertex(i) {
    const t = this.target;
    t.coords.splice(i, 1);
    if (t.ids) t.ids.splice(i, 1);
    if (t.kind === "wall") {
      const gone = t.links.splice(i, 1)[0] ?? [];
      // Os muros que só estavam ligados por este vértice deixam de acompanhar a edição.
      for (const { key } of gone) if (!t.links.some((ls) => ls.some((l) => l.key === key))) t.others.delete(key);
    }
  }

  moveVertex(i, c) {
    const t = this.target;
    t.coords[i] = c;
    for (const { key, i: j } of t.links[i] ?? []) t.others.get(key)[j] = [...c];
  }

  mergeOnDrop(i, snap) {
    // Largado num vértice: como no iD, os dois passam a ser um só e o arrastado sai.
    // Devolve o índice do vértice que fica (ou null se não juntou).
    const t = this.target;
    if (!snap || snap.vertex === undefined) return null;
    let j = null;
    if (snap.source === "self") j = snap.vertex;
    else if (t.kind === "landuse" && snap.nodes?.[snap.vertex] > 0) {
      // Nó de outra way do OSM: o landuse passa a usá-lo (ou junta-se ao seu, se já o tem).
      const node = snap.nodes[snap.vertex];
      const k = t.ids.indexOf(node);
      if (k < 0) {
        t.ids[i] = node;
        t.shared.add(node);
        return i;
      }
      j = k;
    }
    if (j === null || j === i) return null;
    const n = t.coords.length;
    // Pontas de um muro aberto: juntá-las fecha-o (fica o ponto repetido, como no iD).
    if (!t.closed && n > 2 && ((i === 0 && j === n - 1) || (i === n - 1 && j === 0))) return null;
    if (n <= t.min) return null;
    t.coords[j] = [...t.coords[i]];
    if (t.kind === "wall") {
      // Os muros ligados ao arrastado passam a estar ligados ao que fica.
      const keep = t.links[j];
      for (const l of t.links[i]) if (!keep.some((x) => x.key === l.key && x.i === l.i)) keep.push(l);
      t.links[i] = [];
    }
    this.removeVertex(i);
    this.selected = j > i ? j - 1 : j;
    return this.selected;
  }

  connectToWall(i, snap) {
    // Largado em cima de outro muro: liga-se a ele (num vértice ou, numa aresta, cria-o).
    const t = this.target;
    if (t.kind !== "wall" || !snap || snap.source !== "walls" || snap.key === t.key) return [];
    if (t.links[i].some((l) => l.key === snap.key)) return [];
    const coords = t.others.get(snap.key) ?? clone(snap.coords);
    let j;
    if (snap.vertex !== undefined) j = snap.vertex;
    else {
      j = snap.seg + 1;
      coords.splice(j, 0, [...t.coords[i]]);
      // Índices do outro muro depois do vértice novo andam uma posição.
      for (const ls of t.links) for (const l of ls) if (l.key === snap.key && l.i >= j) l.i += 1;
    }
    t.others.set(snap.key, coords);
    t.links[i].push({ key: snap.key, i: j });
    return snap.vertex !== undefined ? [] : [snap.key];
  }

  disconnect(i) {
    // Desliga o vértice i dos outros elementos (como o "Desligar" do iD).
    const t = this.target;
    if (!this.isShared(i)) return;
    this.pushHistory();
    if (t.kind === "wall") {
      // O muro passa a ter o seu próprio ponto, 5 cm para dentro dele (os outros ficam).
      const n = t.coords.length;
      const neighbour = i > 0 ? t.coords[i - 1] : t.coords[Math.min(1, n - 1)];
      const gone = t.links[i];
      t.links[i] = [];
      for (const { key } of gone) if (!t.links.some((ls) => ls.some((l) => l.key === key))) t.others.delete(key);
      t.coords[i] = towards(t.coords[i], neighbour, DISCONNECT_M);
    } else {
      t.ids[i] = null; // nó novo só deste landuse, no mesmo sítio
    }
    this.commit([]);
    this.render();
  }

  straighten() {
    // Muro reto do início ao fim: os vértices intermédios saem, exceto os ligados a outros
    // muros, que passam para cima da reta (e levam esses muros com eles).
    const t = this.target;
    if (t.kind !== "wall" || t.coords.length < 3) return;
    this.pushHistory();
    const a = t.coords[0], b = t.coords[t.coords.length - 1];
    const changed = new Set();
    for (let i = t.coords.length - 2; i >= 1; i--) {
      if (t.links[i].length) {
        this.moveVertex(i, projectOnLine(t.coords[i], a, b));
        t.links[i].forEach((l) => changed.add(l.key));
      } else this.removeVertex(i);
    }
    this.selected = null;
    this.commit([...changed]);
    this.render();
  }

  // ------------------------------------------------------------- interação

  startDrag(e, idx, fromMid) {
    if (!this.target || e.originalEvent.button !== 0) return;
    e.preventDefault(); // sem arrastar o mapa
    this.closeMenu();
    this.pushHistory();
    const t = this.target;
    if (fromMid) {
      // Ponto a meio da aresta idx → idx+1: passa a vértice (fica se for arrastado).
      const j = (idx + 1) % t.coords.length;
      this.insertVertex(idx + 1, [(t.coords[idx][0] + t.coords[j][0]) / 2, (t.coords[idx][1] + t.coords[j][1]) / 2]);
      idx += 1;
    }
    this.drag = { idx, fromMid, moved: false, snap: null, start: e.point, origin: [...t.coords[idx]] };
    this.selected = idx;
    this.render();
  }

  onMove(e) {
    const d = this.drag;
    if (!d) return;
    if (!d.moved && Math.hypot(e.point.x - d.start.x, e.point.y - d.start.y) < 3) return;
    const { lngLat, snap } = this.snap(e.point, e.lngLat, d.origin);
    this.moveVertex(d.idx, lngLat);
    d.moved = true;
    d.snap = snap;
    this.render();
  }

  endDrag(e) {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    if (d.moved) {
      const linked = (this.target.links[d.idx] ?? []).map((l) => l.key);
      const merged = this.mergeOnDrop(d.idx, d.snap);
      const extra = merged === null ? this.connectToWall(d.idx, d.snap) : [];
      this.commit([...new Set([...linked, ...extra])]);
    } else {
      // Só clicou: o ponto a meio não fica; o vértice fica selecionado e abre o menu.
      this.restore(this.history.pop());
      if (!d.fromMid && e?.point) this.openVertexMenu(d.idx, e.point);
      else if (d.fromMid) this.selected = null;
    }
    this.render();
  }

  onClick(e) {
    // Clique numa aresta (fora dos vértices): menu da aresta.
    if (!this.target || this.drag) return;
    const hit = this.edgeAt(e.point);
    if (hit && this.vertexAt(e.point) === null) this.openEdgeMenu(hit, e.point);
  }

  onContextMenu(e) {
    if (!this.target) return;
    const v = this.vertexAt(e.point);
    if (v !== null) {
      e.preventDefault();
      this.selected = v;
      this.render();
      return this.openVertexMenu(v, e.point);
    }
    const hit = this.edgeAt(e.point);
    if (hit) {
      e.preventDefault();
      this.openEdgeMenu(hit, e.point);
    }
  }

  vertexAt(p) {
    const i = this.target.coords.findIndex((c) => {
      const q = this.map.project(c);
      return Math.hypot(q.x - p.x, q.y - p.y) < HIT_PX;
    });
    return i < 0 ? null : i;
  }

  edgeAt(p) {
    const verts = this.target.coords.map((c) => this.map.project(c));
    let best = null;
    for (const [i, j] of this.segments()) {
      const c = closestOnSegment(p, verts[i], verts[j]);
      if (c.d <= HIT_PX && c.t > 0 && c.t < 1 && (!best || c.d < best.d)) best = { ...c, i };
    }
    return best;
  }

  addVertexAt(hit) {
    this.pushHistory();
    const ll = this.map.unproject([hit.q.x, hit.q.y]);
    this.insertVertex(hit.i + 1, [ll.lng, ll.lat]);
    this.selected = hit.i + 1;
    this.commit([]);
    this.render();
  }

  deleteVertex(i) {
    const t = this.target;
    if (i === null || t.coords.length <= t.min) return;
    this.pushHistory();
    this.removeVertex(i);
    this.selected = null;
    this.commit([]);
    this.render();
  }

  onDblClick(e) {
    if (!this.target) return;
    e.preventDefault();
    if (this.vertexAt(e.point) !== null) return;
    const hit = this.edgeAt(e.point);
    if (hit) {
      this.closeMenu();
      this.addVertexAt(hit);
    }
  }

  onKey(e) {
    if (!this.target || e.target.closest?.("input, select, textarea")) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === "z") {
      e.preventDefault();
      const prev = this.history.pop();
      if (!prev) return;
      const others = this.restore(prev);
      this.selected = null;
      this.closeMenu();
      this.commit(others);
      this.render();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if ((e.key === "Delete" || e.key === "Backspace") && this.selected !== null) {
      e.preventDefault();
      this.closeMenu();
      this.deleteVertex(this.selected);
    } else if (k === "d" && this.selected !== null) {
      e.preventDefault();
      this.closeMenu();
      this.disconnect(this.selected);
    } else if (k === "s" && this.target.kind === "wall") {
      e.preventDefault();
      this.closeMenu();
      this.straighten();
    } else if (e.key === "Escape") {
      if (this.menu) this.closeMenu();
      else this.selected = null;
      this.render();
    }
  }

  snap(point, lngLat, origin) {
    // Encaixa num vértice próximo (muros, landuse, edifícios) ou, senão, numa aresta.
    // Os pontos ligados ao vértice arrastado (no sítio de onde saiu) não contam.
    const t = this.target;
    const targets = this.snapTargets(point, SNAP_PX).filter((s) => s.key !== t.key);
    const idx = this.drag?.idx;
    const own = { key: t.key, source: "self", coords: t.closed ? [...t.coords, t.coords[0]] : t.coords };
    let best = null;
    own.coords.forEach((c, i) => {
      if (idx === undefined || i % t.coords.length === idx) return;
      const q = this.map.project(c);
      const d = Math.hypot(q.x - point.x, q.y - point.y);
      if (d <= SNAP_PX && (!best || d < best.d)) best = { d, c, s: { ...own, vertex: i % t.coords.length } };
    });
    for (const s of targets) {
      const coords = t.others.get(s.key) ?? s.coords;
      coords.forEach((c, i) => {
        if (same(c, origin)) return;
        const q = this.map.project(c);
        const d = Math.hypot(q.x - point.x, q.y - point.y);
        if (d <= SNAP_PX && (!best || d < best.d)) best = { d, c, s: { ...s, coords, vertex: i } };
      });
    }
    if (best) return { lngLat: [best.c[0], best.c[1]], snap: best.s };
    let edge = null;
    for (const [a, b] of this.segments()) {
      if (a === idx || b === idx) continue; // as arestas do vértice arrastado
      const c = closestOnSegment(point, this.map.project(t.coords[a]), this.map.project(t.coords[b]));
      if (c.d <= EDGE_SNAP_PX && (!edge || c.d < edge.d)) edge = { ...c, s: { ...own, seg: a } };
    }
    for (const s of targets) {
      const coords = t.others.get(s.key) ?? s.coords;
      for (let i = 0; i < coords.length - 1; i++) {
        if (same(coords[i], origin) || same(coords[i + 1], origin)) continue; // aresta que se move com ele
        const c = closestOnSegment(point, this.map.project(coords[i]), this.map.project(coords[i + 1]));
        if (c.d <= EDGE_SNAP_PX && (!edge || c.d < edge.d)) edge = { ...c, s: { ...s, coords, seg: i } };
      }
    }
    if (edge) {
      const ll = this.map.unproject([edge.q.x, edge.q.y]);
      return { lngLat: [ll.lng, ll.lat], snap: edge.s };
    }
    return { lngLat: [lngLat.lng, lngLat.lat], snap: null };
  }

  commit(otherKeys) {
    const t = this.target;
    const others = {};
    for (const k of otherKeys) if (t.others.has(k)) others[k] = clone(t.others.get(k));
    this.onCommit(t, clone(t.coords), clone(t.ids), others);
  }

  // ------------------------------------------------------------- menu

  openVertexMenu(i, point) {
    this.selected = i;
    this.render();
    const t = this.target;
    this.openMenu(point, [
      { label: "Apagar vértice", hint: "Delete", enabled: t.coords.length > t.min, run: () => this.deleteVertex(i) },
      { label: "Desligar", hint: "D", enabled: this.isShared(i), run: () => this.disconnect(i),
        title: t.kind === "wall" ? "Este muro deixa de partilhar o ponto com os outros muros" : "Este landuse passa a ter o seu próprio nó (os outros elementos ficam com o deles)" },
    ]);
  }

  openEdgeMenu(hit, point) {
    const t = this.target;
    const items = [{ label: "Acrescentar vértice aqui", hint: "duplo clique", enabled: true, run: () => this.addVertexAt(hit) }];
    if (t.kind === "wall") {
      items.push({ label: "Alinhar muro", hint: "S", enabled: t.coords.length > 2, run: () => this.straighten(),
        title: "Muro reto do início ao fim: os pontos intermédios saem (os ligados a outros muros passam para a reta)" });
    }
    this.openMenu(point, items);
  }

  openMenu(point, items) {
    this.closeMenu();
    const el = document.createElement("div");
    el.className = "edit-menu";
    for (const it of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.disabled = !it.enabled;
      if (it.title) b.title = it.title;
      b.innerHTML = `<span>${it.label}</span><kbd>${it.hint}</kbd>`;
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this.closeMenu();
        it.run();
      });
      el.appendChild(b);
    }
    const box = this.map.getContainer();
    box.appendChild(el);
    const x = Math.min(point.x + 8, box.clientWidth - el.offsetWidth - 4);
    const y = Math.min(point.y + 8, box.clientHeight - el.offsetHeight - 4);
    el.style.left = `${Math.max(4, x)}px`;
    el.style.top = `${Math.max(4, y)}px`;
    this.menu = el;
  }

  closeMenu() {
    this.menu?.remove();
    this.menu = null;
  }
}
