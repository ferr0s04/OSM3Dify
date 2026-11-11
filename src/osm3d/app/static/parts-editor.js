// Editor 2D de partes (building:part), ao estilo do editor iD do OSM.
//
// Coordenadas locais em metros (x = este, y = norte), no referencial do OSM.
// Topologia: as partes são anéis de índices num array de vértices partilhados; mover
// um vértice move-o em todas as partes que o usam.
//
// Vértices:
//  - "outline": nó do contorno do edifício → fixo (o contorno edita-se no JOSM);
//  - "edge":    sobre um lado do contorno → desliza ao longo desse lado;
//  - "free":    interior → livre.
//
// Interações: arrastar vértices; clicar no "+" a meio de um lado para criar um
// vértice; Delete para remover o vértice selecionado; Q esquadra a parte selecionada;
// S (ou botão) divide a parte selecionada com dois cliques; Ctrl+Z desfaz; roda do
// rato para zoom; arrastar o fundo para mover a vista.

const SNAP = 0.01;
const PART_COLORS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#db2777", "#0891b2", "#ca8a04"];
const SQUARE_THRESHOLD_DEG = 13;
const NS = "http://www.w3.org/2000/svg";

const clone = (o) => JSON.parse(JSON.stringify(o));
const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

function onSegment(p, a, b, tol = SNAP) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  if (!l2) return null;
  const t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2;
  if (t <= 1e-6 || t >= 1 - 1e-6) return null;
  return Math.hypot(a[0] + t * dx - p[0], a[1] + t * dy - p[1]) <= tol ? t : null;
}

function closestOnSegment(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy || 1;
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2));
  return { point: [a[0] + t * dx, a[1] + t * dy], t };
}

function intersect(p1, d1, p2, d2) {
  const den = d1[0] * d2[1] - d1[1] * d2[0];
  if (Math.abs(den) < 0.05) return null; // quase paralelas
  const t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den;
  return [p1[0] + t * d1[0], p1[1] + t * d1[1]];
}

export class PartsEditor {
  constructor(container, { onChange, onSelectPart, onSplit, onStatus }) {
    this.container = container;
    this.onChange = onChange;
    this.onSelectPart = onSelectPart;
    this.onSplit = onSplit;
    this.onStatus = onStatus;
    this.canvas = document.createElement("canvas");
    this.svg = document.createElementNS(NS, "svg");
    this.svg.setAttribute("tabindex", "0");
    container.append(this.canvas, this.svg);
    this.layout = null;
    this.outline = [];
    this.points = [];
    this.selectedVertex = null;
    this.selectedPart = null;
    this.mode = "edit";
    this.splitStart = null;
    this.history = [];
    this.view = null;
    this.drag = null;
    this.bind();
    new ResizeObserver(() => this.render()).observe(container);
  }

  // --------------------------------------------------------------- dados

  load({ outline, points, layout }, keepView = false) {
    this.outline = outline;
    if (points) this.points = points;
    this.layout = layout ? clone(layout) : null;
    this.selectedVertex = null;
    if (this.selectedPart !== null && (!this.layout || this.selectedPart >= this.layout.parts.length)) {
      this.selectedPart = null;
    }
    this.setMode("edit");
    if (!keepView || !this.view) this.fit();
    this.render();
  }

  setLayout(layout) {
    this.layout = clone(layout);
    this.selectedVertex = null;
    if (this.selectedPart !== null && this.selectedPart >= layout.parts.length) this.selectedPart = null;
    this.render();
  }

  getLayout() {
    return this.layout ? { vertices: this.layout.vertices, parts: this.layout.parts } : null;
  }

  kind(i) {
    const p = this.layout.vertices[i];
    const o = this.outline;
    for (let k = 0; k < o.length; k++) if (dist(p, o[k]) <= SNAP) return { kind: "outline", index: k };
    for (let k = 0; k < o.length; k++) {
      if (onSegment(p, o[k], o[(k + 1) % o.length]) !== null) return { kind: "edge", index: k };
    }
    return { kind: "free" };
  }

  pushHistory() {
    this.history.push(clone(this.layout));
    if (this.history.length > 100) this.history.shift();
  }

  undo() {
    if (!this.history.length) return;
    this.layout = this.history.pop();
    this.selectedVertex = null;
    this.render();
    this.onChange(this.getLayout(), true);
  }

  // ------------------------------------------------------------- edição

  insertVertex(a, b, point) {
    // Novo vértice no lado a–b de todas as partes que o têm (mantém a topologia).
    this.pushHistory();
    const v = this.layout.vertices;
    v.push(point);
    const id = v.length - 1;
    for (const ring of this.layout.parts) {
      for (let i = 0; i < ring.length; i++) {
        const j = (i + 1) % ring.length;
        if ((ring[i] === a && ring[j] === b) || (ring[i] === b && ring[j] === a)) {
          ring.splice(i + 1, 0, id);
          break;
        }
      }
    }
    this.selectedVertex = id;
    this.render();
    this.onChange(this.getLayout(), true);
  }

  deleteVertex(id) {
    if (id === null) return;
    if (this.kind(id).kind === "outline") return this.onStatus("Os nós do contorno não se removem aqui (edita-se no JOSM).");
    if (this.layout.parts.some((r) => r.includes(id) && r.length <= 3)) {
      return this.onStatus("Não é possível: uma parte ficaria com menos de 3 vértices.");
    }
    this.pushHistory();
    this.layout.parts = this.layout.parts.map((r) => r.filter((k) => k !== id));
    // Remove o vértice e renumera.
    this.layout.vertices.splice(id, 1);
    this.layout.parts = this.layout.parts.map((r) => r.map((k) => (k > id ? k - 1 : k)));
    this.selectedVertex = null;
    this.render();
    this.onChange(this.getLayout(), true);
  }

  moveVertex(id, p) {
    const k = this.kind(id);
    if (k.kind === "outline") return;
    let target = p;
    if (this.drag?.edge !== undefined) {
      // Vértice no contorno: desliza só ao longo do seu lado.
      const o = this.outline;
      const a = o[this.drag.edge], b = o[(this.drag.edge + 1) % o.length];
      const { t } = closestOnSegment(p, a, b);
      const tc = Math.max(0.01, Math.min(0.99, t));
      target = [a[0] + tc * (b[0] - a[0]), a[1] + tc * (b[1] - a[1])];
    }
    this.layout.vertices[id] = [Math.round(target[0] * 1000) / 1000, Math.round(target[1] * 1000) / 1000];
    this.render();
  }

  squarePart(index) {
    // Esquadrar (como o "Q" do iD): lados a menos de 13° de um múltiplo de 90° da
    // orientação dominante ficam ortogonais. Nós do contorno ficam fixos; vértices
    // sobre o contorno deslizam ao longo do seu lado.
    if (index === null) return this.onStatus("Seleciona uma parte para esquadrar.");
    const ring = this.layout.parts[index];
    const v = this.layout.vertices;
    const kinds = ring.map((i) => this.kind(i));
    this.pushHistory();
    for (let iter = 0; iter < 3; iter++) {
      const pts = ring.map((i) => v[i]);
      const n = pts.length;
      let sx = 0, sy = 0;
      const edges = pts.map((a, i) => {
        const b = pts[(i + 1) % n];
        const th = Math.atan2(b[1] - a[1], b[0] - a[0]);
        const len = dist(a, b);
        sx += len * Math.cos(4 * th);
        sy += len * Math.sin(4 * th);
        return { a, b, th, mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] };
      });
      const dom = Math.atan2(sy, sx) / 4;
      const thr = (SQUARE_THRESHOLD_DEG * Math.PI) / 180;
      const dirs = edges.map((e) => {
        const k = Math.round((e.th - dom) / (Math.PI / 2));
        const snapped = dom + (k * Math.PI) / 2;
        const th = Math.abs(e.th - snapped) <= thr ? snapped : e.th;
        return [Math.cos(th), Math.sin(th)];
      });
      ring.forEach((vid, i) => {
        const kind = kinds[i];
        if (kind.kind === "outline") return;
        const prev = (i - 1 + n) % n;
        let p = intersect(edges[prev].mid, dirs[prev], edges[i].mid, dirs[i]);
        if (!p) {
          // Vértice "reto" (180°): projeta-se na reta do lado seguinte.
          const d = dirs[i], m = edges[i].mid, q = v[vid];
          const t = (q[0] - m[0]) * d[0] + (q[1] - m[1]) * d[1];
          p = [m[0] + t * d[0], m[1] + t * d[1]];
        }
        if (kind.kind === "edge") {
          const o = this.outline;
          p = closestOnSegment(p, o[kind.index], o[(kind.index + 1) % o.length]).point;
        }
        v[vid] = [Math.round(p[0] * 1000) / 1000, Math.round(p[1] * 1000) / 1000];
      });
    }
    this.render();
    this.onChange(this.getLayout(), true);
  }

  setMode(mode) {
    this.mode = mode;
    this.splitStart = null;
    this.svg.style.cursor = mode === "split" ? "crosshair" : "default";
    if (mode === "split") this.onStatus("Dividir: clica em dois pontos na fronteira da parte selecionada (Esc cancela).");
  }

  startSplit() {
    if (!this.layout) return;
    if (this.selectedPart === null) {
      if (this.layout.parts.length === 1) this.selectPart(0);
      else return this.onStatus("Seleciona primeiro a parte a dividir.");
    }
    this.setMode("split");
    this.render();
  }

  selectPart(i) {
    this.selectedPart = i;
    this.onSelectPart(i);
    this.render();
  }

  snapToPartBoundary(p) {
    // Ponto mais próximo na fronteira da parte selecionada (com íman nos vértices).
    const ring = this.layout.parts[this.selectedPart];
    const v = this.layout.vertices;
    let best = null;
    ring.forEach((a, i) => {
      const b = ring[(i + 1) % ring.length];
      const c = closestOnSegment(p, v[a], v[b]);
      const d = dist(c.point, p);
      if (!best || d < best.d) best = { d, point: c.point };
    });
    for (const k of ring) if (dist(v[k], p) < this.unit * 10) return v[k];
    return best.point;
  }

  // ---------------------------------------------------------------- vista

  fit() {
    const xs = this.outline.map((p) => p[0]), ys = this.outline.map((p) => p[1]);
    if (!xs.length) return;
    const pad = 3;
    this.view = {
      minx: Math.min(...xs) - pad, maxx: Math.max(...xs) + pad,
      miny: Math.min(...ys) - pad, maxy: Math.max(...ys) + pad,
    };
  }

  viewBox() {
    const { clientWidth: W, clientHeight: H } = this.container;
    const v = this.view;
    let w = v.maxx - v.minx, h = v.maxy - v.miny;
    const aspect = W / H;
    let cx = (v.minx + v.maxx) / 2, cy = (v.miny + v.maxy) / 2;
    if (w / h > aspect) h = w / aspect; else w = h * aspect;
    return { x: cx - w / 2, y: -(cy + h / 2), w, h, W, H };
  }

  toWorld(evt) {
    const r = this.svg.getBoundingClientRect();
    const vb = this.viewBox();
    const x = vb.x + ((evt.clientX - r.left) / r.width) * vb.w;
    const y = vb.y + ((evt.clientY - r.top) / r.height) * vb.h;
    return [x, -y];
  }

  zoom(evt) {
    evt.preventDefault();
    const [mx, my] = this.toWorld(evt);
    const f = evt.deltaY > 0 ? 1.15 : 1 / 1.15;
    const v = this.view;
    this.view = {
      minx: mx + (v.minx - mx) * f, maxx: mx + (v.maxx - mx) * f,
      miny: my + (v.miny - my) * f, maxy: my + (v.maxy - my) * f,
    };
    this.render();
  }

  // --------------------------------------------------------------- eventos

  bind() {
    this.svg.addEventListener("wheel", (e) => this.zoom(e), { passive: false });
    this.svg.addEventListener("mousedown", (e) => this.mousedown(e));
    window.addEventListener("mousemove", (e) => this.mousemove(e));
    window.addEventListener("mouseup", (e) => this.mouseup(e));
    this.svg.addEventListener("keydown", (e) => this.keydown(e));
  }

  keydown(e) {
    if (!this.layout) return;
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); this.undo(); }
    else if (k === "delete" || k === "backspace") { e.preventDefault(); this.deleteVertex(this.selectedVertex); }
    else if (k === "q") { e.preventDefault(); this.squarePart(this.selectedPart); }
    else if (k === "s") { e.preventDefault(); this.startSplit(); }
    else if (k === "escape") { this.setMode("edit"); this.render(); }
    e.stopPropagation();
  }

  mousedown(e) {
    this.svg.focus();
    if (!this.layout) return this.startPan(e);
    const target = e.target;
    const p = this.toWorld(e);
    if (this.mode === "split") {
      const q = this.snapToPartBoundary(p);
      if (!this.splitStart) {
        this.splitStart = q;
        this.render();
      } else {
        const a = this.splitStart;
        this.setMode("edit");
        if (dist(a, q) > 0.2) this.onSplit(this.selectedPart, a, q);
        this.render();
      }
      return;
    }
    if (target.dataset.vertex !== undefined) {
      const id = Number(target.dataset.vertex);
      this.selectedVertex = id;
      const k = this.kind(id);
      if (k.kind !== "outline") {
        this.pushHistory();
        this.drag = { vertex: id, moved: false, edge: k.kind === "edge" ? k.index : undefined };
      }
      this.render();
      return;
    }
    if (target.dataset.mid !== undefined) {
      const [a, b] = target.dataset.mid.split(",").map(Number);
      const v = this.layout.vertices;
      this.insertVertex(a, b, [(v[a][0] + v[b][0]) / 2, (v[a][1] + v[b][1]) / 2]);
      return;
    }
    if (target.dataset.part !== undefined) {
      this.selectedVertex = null;
      this.selectPart(Number(target.dataset.part));
      return;
    }
    this.selectedVertex = null;
    this.startPan(e);
    this.render();
  }

  startPan(e) {
    this.drag = { pan: true, start: [e.clientX, e.clientY], view: { ...this.view } };
  }

  mousemove(e) {
    if (this.mode === "split" && this.splitStart) {
      this.splitCursor = this.snapToPartBoundary(this.toWorld(e));
      this.render();
    }
    if (!this.drag) return;
    if (this.drag.pan) {
      const vb = this.viewBox();
      const dx = ((e.clientX - this.drag.start[0]) / vb.W) * vb.w;
      const dy = ((e.clientY - this.drag.start[1]) / vb.H) * vb.h;
      const v = this.drag.view;
      this.view = { minx: v.minx - dx, maxx: v.maxx - dx, miny: v.miny + dy, maxy: v.maxy + dy };
      this.render();
      return;
    }
    this.drag.moved = true;
    this.moveVertex(this.drag.vertex, this.toWorld(e));
  }

  mouseup() {
    if (!this.drag) return;
    const d = this.drag;
    this.drag = null;
    if (d.vertex !== undefined) {
      if (d.moved) this.onChange(this.getLayout(), true);
      else this.history.pop(); // clique sem arrasto: nada a desfazer
    }
  }

  // ----------------------------------------------------------- desenho

  drawPoints(vb) {
    const { canvas } = this;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = vb.W * dpr;
    canvas.height = vb.H * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#1b1f27";
    ctx.fillRect(0, 0, vb.W, vb.H);
    if (!this.points.length) return;
    const zs = this.points.map((p) => p[2]);
    const zmin = Math.min(...zs), zmax = Math.max(...zs);
    const r = Math.max(1.2, (0.35 / vb.w) * vb.W);
    for (const [x, y, z] of this.points) {
      const t = zmax > zmin ? (z - zmin) / (zmax - zmin) : 0.5;
      // Rampa azul → verde → amarelo: o degrau entre volumes vê-se pela cor.
      const hue = 240 - 180 * t;
      ctx.fillStyle = `hsl(${hue}, 80%, ${40 + 20 * t}%)`;
      const px = ((x - vb.x) / vb.w) * vb.W;
      const py = ((-y - vb.y) / vb.h) * vb.H;
      ctx.fillRect(px - r / 2, py - r / 2, r, r);
    }
  }

  render() {
    if (!this.view || !this.container.clientWidth) return;
    const vb = this.viewBox();
    this.unit = vb.w / vb.W; // metros por píxel
    const u = this.unit;
    this.drawPoints(vb);
    const svg = this.svg;
    svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    svg.innerHTML = "";
    const el = (name, attrs) => {
      const e = document.createElementNS(NS, name);
      for (const [k, val] of Object.entries(attrs)) e.setAttribute(k, val);
      svg.appendChild(e);
      return e;
    };
    const pts = (coords) => coords.map(([x, y]) => `${x},${-y}`).join(" ");

    el("polygon", { points: pts(this.outline), fill: "none", stroke: "#facc15", "stroke-width": 3 * u });
    if (!this.layout) return;
    const v = this.layout.vertices;

    this.layout.parts.forEach((ring, i) => {
      const sel = i === this.selectedPart;
      el("polygon", {
        points: pts(ring.map((k) => v[k])),
        fill: PART_COLORS[i % PART_COLORS.length],
        "fill-opacity": sel ? 0.45 : 0.22,
        stroke: PART_COLORS[i % PART_COLORS.length],
        "stroke-width": (sel ? 3 : 1.5) * u,
        "data-part": i,
      });
      const c = ring.reduce((acc, k) => [acc[0] + v[k][0] / ring.length, acc[1] + v[k][1] / ring.length], [0, 0]);
      const label = el("text", { x: c[0], y: -c[1], "font-size": 14 * u, fill: "#fff", "text-anchor": "middle", "pointer-events": "none" });
      label.textContent = String(i + 1);
    });

    // "+" a meio de cada lado (uma vez por lado partilhado).
    const seen = new Set();
    for (const ring of this.layout.parts) {
      ring.forEach((a, i) => {
        const b = ring[(i + 1) % ring.length];
        const key = a < b ? `${a},${b}` : `${b},${a}`;
        if (seen.has(key) || dist(v[a], v[b]) < 30 * u) return;
        seen.add(key);
        el("circle", {
          cx: (v[a][0] + v[b][0]) / 2, cy: -(v[a][1] + v[b][1]) / 2, r: 4 * u,
          fill: "#0f172a", stroke: "#e5e7eb", "stroke-width": 1.2 * u, "data-mid": key, class: "mid",
        });
      });
    }

    v.forEach((p, i) => {
      const k = this.kind(i).kind;
      const sel = i === this.selectedVertex;
      const fill = sel ? "#ef4444" : k === "outline" ? "#111827" : k === "edge" ? "#f97316" : "#ffffff";
      const stroke = k === "outline" ? "#facc15" : "#2563eb";
      const s = (k === "outline" ? 5 : 6) * u;
      if (k === "edge") {
        el("polygon", {
          points: `${p[0]},${-p[1] - s} ${p[0] + s},${-p[1]} ${p[0]},${-p[1] + s} ${p[0] - s},${-p[1]}`,
          fill, stroke, "stroke-width": 1.5 * u, "data-vertex": i, class: "vertex",
        });
      } else if (k === "outline") {
        el("rect", { x: p[0] - s, y: -p[1] - s, width: 2 * s, height: 2 * s, fill, stroke, "stroke-width": 1.5 * u, "data-vertex": i });
      } else {
        el("circle", { cx: p[0], cy: -p[1], r: s, fill, stroke, "stroke-width": 1.5 * u, "data-vertex": i, class: "vertex" });
      }
    });

    if (this.mode === "split" && this.splitStart) {
      const a = this.splitStart, b = this.splitCursor ?? a;
      el("line", { x1: a[0], y1: -a[1], x2: b[0], y2: -b[1], stroke: "#ef4444", "stroke-width": 2 * u, "stroke-dasharray": `${6 * u} ${4 * u}`, "pointer-events": "none" });
      el("circle", { cx: a[0], cy: -a[1], r: 5 * u, fill: "#ef4444", "pointer-events": "none" });
    }
  }
}

export { PART_COLORS };
