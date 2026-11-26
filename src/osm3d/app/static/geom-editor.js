// Edição de geometria no mapa (muros propostos, landuse e edifícios), ao estilo do iD:
//  - o elemento em edição tem o halo vermelho do iD; vértices brancos (cinzentos se
//    ligados a outros elementos) e, a meio de cada aresta, um triângulo no sentido da way;
//  - arrastar um vértice move-o; encaixa em vértices e arestas próximos (muros, landuse,
//    edifícios). Um vértice ligado a outros elementos (o mesmo ponto: muros com muros;
//    landuse e edifícios entre si) é cinzento e move-os a todos;
//  - largar um vértice em cima de outro elemento liga-os (numa aresta, cria lá o vértice);
//    em cima de outro vértice do mesmo elemento, os dois juntam-se (o arrastado sai);
//    uma way do OSM largada num nó de outra way do OSM passa a usar esse nó;
//  - arrastar o triângulo a meio de uma aresta, ou duplo clique numa aresta, cria um vértice;
//  - clique (ou botão direito) num vértice ou numa aresta abre o menu de opções: apagar o
//    vértice (Delete), desligar (D), esquadrar (Q: o contorno inteiro, ou o canto do
//    vértice selecionado; a aresta pelo menu), alinhar o muro (S);
//  - Ctrl+Z desfaz; Esc fecha o menu ou tira a seleção.
// Cada alteração terminada é entregue a onCommit(target, coords, ids, others), em que
// others são os outros elementos que mudaram com ela ({ chave: { coords, ids, source } }).
import { squareCorner, squareEdge, squareWay, SQUARE_PART_DEG, SQUARE_THRESHOLD_DEG } from "./orthogonalize.js";

const SNAP_PX = 10; // encaixe num vértice
const EDGE_SNAP_PX = 7; // encaixe numa aresta
const MID_MIN_PX = 26; // arestas mais curtas (no ecrã) não mostram o ponto a meio
const HIT_PX = 7; // clique/duplo clique numa aresta
const SAME_DEG = 1e-8; // ~1 mm: o mesmo ponto (= o mesmo nó na exportação)
const DISCONNECT_M = 0.05; // desligar: o vértice afasta-se 5 cm ao longo do próprio muro
const MOVED_M = 1e-4; // esquadrar: abaixo disto (0,1 mm) o vértice não mexeu
const EMPTY = { type: "FeatureCollection", features: [] };
// Cores do iD: halo do elemento selecionado e vértices.
const HALO = "#f6634f";
const LAYERS = ["edit-fill", "edit-halo", "edit-line", "edit-mid", "edit-vertex-halo", "edit-vertex"];
const HIT_LAYERS = ["edit-fill", "edit-line", "edit-mid", "edit-vertex"];
const SQUARE_ERRORS = {
  not_squarish: `Não dá para esquadrar: há cantos a mais de ${SQUARE_THRESHOLD_DEG}° de um ângulo reto (ou de um lado reto).`,
  not_squarish_part: `Não dá para esquadrar: o ângulo está a mais de ${SQUARE_PART_DEG}° de 90°.`,
  square_enough: "Já está esquadrado.",
  too_few: "Vértices a menos para esquadrar.",
};

const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));
const same = (a, b) => Math.abs(a[0] - b[0]) <= SAME_DEG && Math.abs(a[1] - b[1]) <= SAME_DEG;
// O mesmo ponto (~1 cm): ligar copia as coordenadas exatas do outro elemento.
export const posKey = (c) => `${c[0].toFixed(7)},${c[1].toFixed(7)}`;
const closedRing = (o) => (o.closed && o.coords.length ? [...o.coords, o.coords[0]] : o.coords);

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

function bearing(a, b) {
  // Rumo de a para b (graus a partir do norte, sentido horário).
  const k = Math.cos((a[1] * Math.PI) / 180);
  return (Math.atan2((b[0] - a[0]) * k, b[1] - a[1]) * 180) / Math.PI;
}

function arrowImage() {
  // Triângulo do iD a meio das arestas (aponta para este; roda com o rumo da aresta).
  const size = 24;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.beginPath();
  g.moveTo(18, 12);
  g.lineTo(7, 5.5);
  g.lineTo(7, 18.5);
  g.closePath();
  g.fillStyle = "rgba(238,238,238,0.85)";
  g.strokeStyle = "rgba(68,68,68,0.8)";
  g.lineWidth = 2;
  g.lineJoin = "round";
  g.fill();
  g.stroke();
  return g.getImageData(0, 0, size, size);
}

// Esquadrar vários contornos de uma vez (seleção múltipla, como no iD), um a seguir ao
// outro: um vértice partilhado por dois deles (casas geminadas) move-se nos dois.
// rings: [{ key, coords, ids, keep(i) }] (anéis sem repetir o primeiro ponto; keep: o
// vértice está ligado a outro elemento e não pode sair). Devolve { rings: [{ key, coords,
// ids, changed }], errors: [{ key, error }] }.
export function squareRings(rings) {
  const all = rings.flatMap((r) => r.coords);
  if (!all.length) return { rings: [], errors: [] };
  const lat0 = all.reduce((s, c) => s + c[1], 0) / all.length;
  const kx = 111320 * Math.cos((lat0 * Math.PI) / 180), ky = 110540;
  const [x0, y0] = all[0];
  const fwd = (c) => [(c[0] - x0) * kx, (c[1] - y0) * ky];
  const inv = (p) => [x0 + p[0] / kx, y0 + p[1] / ky];
  const work = rings.map((r) => ({ key: r.key, coords: clone(r.coords), ids: r.ids ? clone(r.ids) : null, changed: false }));
  const errors = [];
  work.forEach((r, ri) => {
    const coincident = (i) => {
      const k = posKey(r.coords[i]);
      return work.some((o, oj) => oj !== ri && o.coords.some((c) => posKey(c) === k));
    };
    const pts = r.coords.map(fwd);
    const res = squareWay(pts, true, (i) => !!rings[ri].keep?.(i) || coincident(i));
    if (res.error) return errors.push({ key: r.key, error: res.error });
    res.coords.forEach((p, i) => {
      if (Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]) <= MOVED_M) return;
      const c = inv(p);
      const before = posKey(r.coords[i]);
      for (const o of work) {
        if (o === r) continue;
        o.coords.forEach((oc, k) => {
          if (posKey(oc) !== before) return;
          o.coords[k] = [...c];
          o.changed = true;
        });
      }
      r.coords[i] = c;
    });
    for (const i of [...res.removed].sort((a, b) => b - a)) {
      if (r.coords.length <= 3) break;
      r.coords.splice(i, 1);
      r.ids?.splice(i, 1);
    }
    r.changed = true;
  });
  return { rings: work, errors };
}

export const SQUARE_MESSAGES = SQUARE_ERRORS;

export class GeometryEditor {
  // snapTargets(point, px): elementos à volta ({ key, source, coords, ring });
  // linkCandidates(target): elementos a que o elemento em edição se pode ligar
  // ([{ key, source, closed, coords, ids }], anéis sem repetir o primeiro ponto);
  // onCommit: alteração terminada; onStatus(mensagem): avisos (p.ex. não dá para esquadrar).
  constructor(map, { snapTargets, linkCandidates, onCommit, onStatus, onEscape }) {
    this.map = map;
    this.snapTargets = snapTargets;
    this.linkCandidates = linkCandidates;
    this.candidates = new Map();
    this.onCommit = onCommit;
    this.onStatus = onStatus ?? (() => {});
    this.onEscape = onEscape ?? (() => {}); // Esc sem menu nem vértice: tirar a seleção
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
    if (!m.hasImage("edit-mid-arrow")) m.addImage("edit-mid-arrow", arrowImage(), { pixelRatio: 2 });
    m.addLayer({ id: "edit-fill", type: "fill", source: "edit", filter: ["==", ["get", "kind"], "fill"],
      paint: { "fill-color": ["get", "color"], "fill-opacity": 0.18 } });
    m.addLayer({ id: "edit-halo", type: "line", source: "edit", filter: ["all", ["==", ["get", "kind"], "line"], ["!", ["get", "other"]]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": HALO, "line-opacity": 0.7, "line-width": ["interpolate", ["linear"], ["zoom"], 15, 8, 18, 14] } });
    m.addLayer({ id: "edit-line", type: "line", source: "edit", filter: ["==", ["get", "kind"], "line"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "other"], 1.5, 2] } });
    m.addLayer({ id: "edit-mid", type: "symbol", source: "edit", filter: ["==", ["get", "kind"], "mid"],
      layout: {
        "icon-image": "edit-mid-arrow",
        "icon-rotate": ["-", ["get", "bearing"], 90],
        "icon-rotation-alignment": "map",
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
      } });
    m.addLayer({ id: "edit-vertex-halo", type: "circle", source: "edit", filter: ["all", ["==", ["get", "kind"], "vertex"], ["get", "selected"]],
      paint: { "circle-radius": 9, "circle-color": HALO, "circle-opacity": 0.7 } });
    m.addLayer({ id: "edit-vertex", type: "circle", source: "edit", filter: ["==", ["get", "kind"], "vertex"],
      paint: {
        "circle-radius": ["case", ["get", "selected"], 5.5, 4.5],
        // Vértice ligado a outros elementos (muros, landuse, edifícios): cinzento, como no
        // iD; mexê-lo mexe-lhes também.
        "circle-color": ["case", ["get", "snapped"], "#fde047", ["get", "shared"], "#bbbbbb", "#ffffff"],
        "circle-stroke-color": ["case", ["get", "selected"], "#222222", "#666666"],
        "circle-stroke-width": ["case", ["get", "selected"], 2, 1.5],
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

  // target: { key, closed, coords, ids?, shared?, min?, kind?, color?, fill?, label? }
  //  - kind: "wall" (muro proposto) ou "area" (landuse/edifício); por omissão pela chave;
  //  - ids: nós do OSM de cada vértice (ways do OSM; null num vértice novo);
  //  - fill: preencher o interior (edifícios); label: "edifício", "landuse"… nos menus.
  start(target) {
    this.closeMenu();
    const kind = target.kind ?? (target.key.startsWith("wall/") ? "wall" : "area");
    const t = {
      key: target.key,
      kind,
      closed: !!target.closed,
      coords: clone(target.coords),
      ids: target.ids ? clone(target.ids) : null,
      shared: target.shared ?? new Set(),
      min: target.min ?? (target.closed ? 3 : 2),
      color: target.color ?? (kind === "wall" ? "#f1f5f9" : "#fde68a"),
      fill: !!target.fill,
      label: target.label ?? (kind === "wall" ? "muro" : "área"),
      // Por vértice, [{ key, i, node }] dos vértices de outros elementos no mesmo ponto
      // (node: o mesmo nó do OSM, que o servidor move em todas as ways).
      links: [],
      others: new Map(), // elementos ligados: chave → { coords, ids, closed, source }
    };
    this.target = t;
    this.findLinks();
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
    // Vértices de outros elementos no mesmo ponto que um vértice do elemento em edição:
    // muros com muros; landuse e edifícios (do OSM e municipais) entre si.
    const t = this.target;
    t.links = t.coords.map(() => []);
    t.others = new Map();
    this.candidates = new Map(
      (this.linkCandidates?.(t) ?? []).filter((c) => c.key !== t.key && c.coords?.length).map((c) => [c.key, c]),
    );
    const at = new Map();
    t.coords.forEach((c, j) => {
      const k = posKey(c);
      if (!at.has(k)) at.set(k, []);
      at.get(k).push(j);
    });
    for (const cand of this.candidates.values()) {
      cand.coords.forEach((c, i) => {
        for (const j of at.get(posKey(c)) ?? []) {
          const node = t.ids?.[j] != null && cand.ids?.[i] != null && t.ids[j] === cand.ids[i];
          t.links[j].push({ key: cand.key, i, node });
          if (!t.others.has(cand.key)) t.others.set(cand.key, this.otherOf(cand));
        }
      });
    }
  }

  otherOf(cand) {
    return { coords: clone(cand.coords), ids: cand.ids ? clone(cand.ids) : null, closed: !!cand.closed, source: cand.source };
  }

  refreshLinks() {
    // Os outros elementos mudaram (dados do mapa recarregados): refaz as ligações.
    if (!this.target || this.drag) return;
    this.findLinks();
    this.render();
  }

  setGeometry(coords, ids = null, shared = null) {
    // Geometria confirmada pelo servidor (sem perder a seleção nem o histórico).
    if (!this.target || this.drag) return;
    const resized = coords.length !== this.target.coords.length;
    this.target.coords = clone(coords);
    if (ids) this.target.ids = clone(ids);
    if (shared) this.target.shared = shared;
    if (this.target.kind === "wall" || resized) this.findLinks();
    if (this.selected !== null && this.selected >= coords.length) this.selected = null;
    this.render();
  }

  raise() {
    // Por cima das outras camadas (as que forem acrescentadas depois também).
    for (const id of LAYERS) if (this.map.getLayer(id)) this.map.moveLayer(id);
  }

  hitTest(point) {
    // O clique foi no elemento em edição (vértice, triângulo, aresta ou, num edifício, o
    // interior)? Então não seleciona outra coisa.
    if (!this.target) return false;
    const box = [[point.x - HIT_PX, point.y - HIT_PX], [point.x + HIT_PX, point.y + HIT_PX]];
    if (this.map.queryRenderedFeatures(box, { layers: HIT_LAYERS.filter((l) => l !== "edit-fill") }).length) return true;
    return this.map.queryRenderedFeatures(point, { layers: ["edit-fill"] }).length > 0;
  }

  isShared(i) {
    // Ligado a outro elemento (cinzento, como no iD): o mesmo ponto ou o mesmo nó do OSM.
    const t = this.target;
    if (t.links[i]?.length > 0) return true;
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
    const { coords, closed, others, color, fill } = this.target;
    const line = closed ? [...coords, coords[0]] : coords;
    const feats = [];
    if (fill && closed && coords.length >= 3) {
      feats.push({ type: "Feature", geometry: { type: "Polygon", coordinates: [line] }, properties: { kind: "fill", color } });
    }
    feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: line }, properties: { kind: "line", other: false, color } });
    for (const o of others.values()) {
      feats.push({ type: "Feature", geometry: { type: "LineString", coordinates: closedRing(o) }, properties: { kind: "line", other: true, color: "#93c5fd" } });
    }
    for (const [i, j] of this.segments()) {
      const a = this.map.project(coords[i]), b = this.map.project(coords[j]);
      if (Math.hypot(a.x - b.x, a.y - b.y) < MID_MIN_PX) continue;
      const mid = [(coords[i][0] + coords[j][0]) / 2, (coords[i][1] + coords[j][1]) / 2];
      feats.push({ type: "Feature", geometry: { type: "Point", coordinates: mid }, properties: { kind: "mid", idx: i, bearing: bearing(coords[i], coords[j]) } });
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
    // Os elementos ligados que mudaram desde então voltam também (são gravados).
    return [...before.keys()].filter((k) => JSON.stringify(before.get(k)) !== JSON.stringify(t.others.get(k)));
  }

  // ------------------------------------------------------------- edição

  insertVertex(i, c) {
    // Novo vértice na posição i do elemento em edição.
    const t = this.target;
    t.coords.splice(i, 0, c);
    if (t.ids) t.ids.splice(i, 0, null);
    t.links.splice(i, 0, []);
  }

  removeVertex(i) {
    const t = this.target;
    t.coords.splice(i, 1);
    if (t.ids) t.ids.splice(i, 1);
    this.unlink(t.links.splice(i, 1)[0] ?? []);
  }

  unlink(gone) {
    // Os elementos que só estavam ligados por estes vértices deixam de acompanhar a edição.
    const t = this.target;
    for (const { key } of gone) if (!t.links.some((ls) => ls.some((l) => l.key === key))) t.others.delete(key);
  }

  moveVertex(i, c) {
    const t = this.target;
    t.coords[i] = c;
    for (const { key, i: j } of t.links[i] ?? []) {
      const o = t.others.get(key);
      if (o) o.coords[j] = [...c];
    }
  }

  mergeOnDrop(i, snap) {
    // Largado num vértice: como no iD, os dois passam a ser um só e o arrastado sai.
    // Devolve { idx, self } (self: juntou-se a um vértice do próprio elemento) ou null.
    const t = this.target;
    if (!snap || snap.vertex === undefined) return null;
    let j = null;
    if (snap.source === "self") j = snap.vertex;
    else if (t.ids && snap.nodes?.[snap.vertex] > 0) {
      // Nó de outra way do OSM: a way em edição passa a usá-lo (ou junta-se ao seu, se já o tem).
      const node = snap.nodes[snap.vertex];
      const k = t.ids.indexOf(node);
      if (k < 0) {
        t.ids[i] = node;
        t.shared.add(node);
        return { idx: i, self: false };
      }
      j = k;
    }
    if (j === null || j === i) return null;
    const n = t.coords.length;
    // Pontas de um muro aberto: juntá-las fecha-o (fica o ponto repetido, como no iD).
    if (!t.closed && n > 2 && ((i === 0 && j === n - 1) || (i === n - 1 && j === 0))) return null;
    if (n <= t.min) return null;
    t.coords[j] = [...t.coords[i]];
    // Os elementos ligados ao arrastado passam a estar ligados ao que fica.
    const keep = t.links[j];
    for (const l of t.links[i]) if (!keep.some((x) => x.key === l.key && x.i === l.i)) keep.push(l);
    t.links[i] = [];
    this.removeVertex(i);
    this.selected = j > i ? j - 1 : j;
    return { idx: this.selected, self: true };
  }

  connectTo(i, snap) {
    // Largado em cima de outro elemento a que se pode ligar (muros com muros; landuse e
    // edifícios entre si): liga-se a ele num vértice ou, numa aresta, cria-o lá (como no
    // iD). Devolve as chaves dos elementos que mudaram (para gravar).
    const t = this.target;
    if (!snap || snap.source === "self" || snap.key === t.key || (snap.ring ?? 0) !== 0) return [];
    const cand = this.candidates.get(snap.key);
    if (!cand) return [];
    if (t.links[i].some((l) => l.key === snap.key)) return [];
    if (!t.others.has(snap.key)) t.others.set(snap.key, this.otherOf(cand));
    const o = t.others.get(snap.key);
    const n = o.coords.length;
    let j;
    let changed = false;
    if (snap.vertex !== undefined) j = o.closed ? snap.vertex % n : snap.vertex;
    else {
      j = snap.seg + 1;
      o.coords.splice(j, 0, [...t.coords[i]]);
      if (o.ids) o.ids.splice(j, 0, null);
      // Índices do outro elemento depois do vértice novo andam uma posição.
      for (const ls of t.links) for (const l of ls) if (l.key === snap.key && l.i >= j) l.i += 1;
      changed = true;
    }
    const node = t.ids?.[i] != null && o.ids?.[j] != null && t.ids[i] === o.ids[j];
    t.links[i].push({ key: snap.key, i: j, node });
    return changed ? [snap.key] : [];
  }

  disconnect(i) {
    // Desliga o vértice i dos outros elementos (como o "Desligar" do iD): passa a ter o
    // seu próprio ponto (nó novo, numa way do OSM), 5 cm para dentro do elemento; os
    // outros ficam onde estão.
    const t = this.target;
    if (!this.isShared(i)) return;
    this.pushHistory();
    const n = t.coords.length;
    const neighbour = i > 0 ? t.coords[i - 1] : t.closed ? t.coords[n - 1] : t.coords[Math.min(1, n - 1)];
    const gone = t.links[i];
    t.links[i] = [];
    this.unlink(gone);
    if (t.ids) t.ids[i] = null;
    t.coords[i] = towards(t.coords[i], neighbour, DISCONNECT_M);
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

  plane() {
    // Plano local em metros à volta do elemento (para esquadrar).
    const t = this.target;
    const lat0 = t.coords.reduce((s, c) => s + c[1], 0) / t.coords.length;
    const kx = 111320 * Math.cos((lat0 * Math.PI) / 180), ky = 110540;
    const [x0, y0] = t.coords[0];
    return {
      fwd: (c) => [(c[0] - x0) * kx, (c[1] - y0) * ky],
      inv: (p) => [x0 + p[0] / kx, y0 + p[1] / ky],
    };
  }

  square(what = "way", index = null) {
    // Esquadrar como o "Q" do iD: o contorno inteiro, um canto (vértice) ou uma aresta.
    // Num contorno inteiro, os vértices em troços retos saem (os ligados a outros
    // elementos ficam, sobre o lado).
    const t = this.target;
    if (!t || !t.closed) return;
    const { fwd, inv } = this.plane();
    const pts = t.coords.map(fwd);
    const res = what === "corner" ? squareCorner(pts, index)
      : what === "edge" ? squareEdge(pts, index)
      : squareWay(pts, true, (i) => this.isShared(i));
    if (res.error) {
      const key = res.error === "not_squarish" && what !== "way" ? "not_squarish_part" : res.error;
      return this.onStatus(SQUARE_ERRORS[key]);
    }
    this.pushHistory();
    res.coords.forEach((p, i) => {
      if (Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]) > MOVED_M) t.coords[i] = inv(p);
    });
    const removed = [...res.removed].sort((a, b) => b - a);
    for (const i of removed) if (t.coords.length > Math.max(t.min, 3)) this.removeVertex(i);
    if (what !== "corner" || removed.length) this.selected = null;
    this.commit([]);
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
      // Os elementos ligados a este vértice mudaram com ele (os que partilham o mesmo nó
      // do OSM, move-os o servidor).
      const linked = (this.target.links[d.idx] ?? []).filter((l) => !l.node).map((l) => l.key);
      const merged = this.mergeOnDrop(d.idx, d.snap);
      const extra = merged?.self ? [] : this.connectTo(merged?.idx ?? d.idx, d.snap);
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
      return this.openEdgeMenu(hit, e.point);
    }
    // Dentro do edifício em edição: as ações do edifício (como o menu do iD).
    if (this.target.fill && this.target.closed && this.map.queryRenderedFeatures(e.point, { layers: ["edit-fill"] }).length) {
      e.preventDefault();
      const t = this.target;
      this.openMenu(e.point, [{ label: `Esquadrar ${t.label}`, hint: "Q", enabled: t.coords.length >= 3, run: () => this.square("way"),
        title: `Todos os cantos a menos de ${SQUARE_THRESHOLD_DEG}° de 90° passam a retos (como o Q do iD)` }]);
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
    // A vista "Planta" (partes) tem as suas teclas.
    if (e.target.closest?.("#planner")) return;
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
    } else if (k === "q" && this.target.closed) {
      // Como no iD: com um vértice selecionado, só esse canto; senão, o contorno todo.
      e.preventDefault();
      this.closeMenu();
      if (this.selected !== null) this.square("corner", this.selected);
      else this.square("way");
    } else if (k === "s" && this.target.kind === "wall") {
      e.preventDefault();
      this.closeMenu();
      this.straighten();
    } else if (e.key === "Escape") {
      // Como no iD: fecha o menu, depois tira o vértice e, por fim, o elemento.
      if (this.menu) this.closeMenu();
      else if (this.selected !== null) this.selected = null;
      else return this.onEscape();
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
    const coordsOf = (s) => (t.others.has(s.key) && (s.ring ?? 0) === 0 ? closedRing(t.others.get(s.key)) : s.coords);
    for (const s of targets) {
      const coords = coordsOf(s);
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
      const coords = coordsOf(s);
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
    for (const k of otherKeys) {
      const o = t.others.get(k);
      if (o) others[k] = { coords: clone(o.coords), ids: clone(o.ids), source: o.source };
    }
    this.onCommit(t, clone(t.coords), clone(t.ids), others);
  }

  // ------------------------------------------------------------- menu

  openVertexMenu(i, point) {
    this.selected = i;
    this.render();
    const t = this.target;
    const items = [
      { label: "Apagar vértice", hint: "Delete", enabled: t.coords.length > t.min, run: () => this.deleteVertex(i) },
      { label: "Desligar", hint: "D", enabled: this.isShared(i), run: () => this.disconnect(i),
        title: t.kind === "wall" ? "Este muro deixa de partilhar o ponto com os outros muros" : `Este ${t.label} passa a ter o seu próprio vértice (os outros elementos ficam com o deles)` },
    ];
    if (t.closed) {
      items.push({ label: "Esquadrar canto", hint: "Q", enabled: t.coords.length >= 3, run: () => this.square("corner", i),
        title: `O ângulo neste vértice passa a reto (só este vértice mexe; até ${SQUARE_PART_DEG}° de diferença)` });
    }
    this.openMenu(point, items);
  }

  openEdgeMenu(hit, point) {
    const t = this.target;
    const items = [{ label: "Acrescentar vértice aqui", hint: "duplo clique", enabled: true, run: () => this.addVertexAt(hit) }];
    if (t.closed) {
      items.push({ label: "Esquadrar aresta", hint: "", enabled: t.coords.length >= 4, run: () => this.square("edge", hit.i),
        title: "A aresta fica perpendicular aos lados vizinhos (os seus vértices deslizam ao longo deles)" });
      items.push({ label: `Esquadrar ${t.label}`, hint: "Q", enabled: t.coords.length >= 3, run: () => this.square("way"),
        title: `Todos os cantos a menos de ${SQUARE_THRESHOLD_DEG}° de 90° passam a retos (como o Q do iD)` });
    }
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
      b.innerHTML = `<span>${it.label}</span>${it.hint ? `<kbd>${it.hint}</kbd>` : ""}`;
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
