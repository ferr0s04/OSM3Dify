// Mapa (MapLibre): contornos coloridos por forma/estado/concordância; em modo 3D,
// telhados com a forma e as cores inferidas (camada three.js dentro do MapLibre).
import * as THREE from "three";
import { GeometryEditor, posKey } from "./geom-editor.js";
import {
  CHANGE_COLORS,
  CHANGE_LABELS,
  MATCH_COLORS,
  ORIGIN_COLORS,
  ORIGIN_LABELS,
  SHAPE_COLORS,
  SHAPE_LABELS,
  STATUS_COLORS,
} from "./colors.js";

// Contorno dos edifícios que já têm informação 3D no OSM (destacados em todos os modos).
const OSM3D_LINE = "#7c3aed";
function legendOpen(value) {
  // Legenda aberta ou fechada: lembrada neste browser (sem armazenamento, fica aberta).
  try {
    if (value !== undefined) localStorage.setItem("osm3d.legendOpen", value ? "1" : "0");
    return localStorage.getItem("osm3d.legendOpen") !== "0";
  } catch {
    return value ?? true;
  }
}

// Contorno dos edifícios da cartografia municipal em falta no OSM.
const CANDIDATE_LINE = "#ea580c";
// Camadas clicáveis dos muros propostos (muros e muros de suporte; vedações a tracejado).
const WALL_LAYERS = ["walls-line", "walls-fence"];

const EARTH_CIRCUMFERENCE = 2 * Math.PI * 6378137;

// Contexto OSM (landuse, vias, muros, edifícios), por baixo dos edifícios em revisão.
const BUILT_LANDUSE = ["residential", "industrial", "commercial", "retail", "farmyard", "construction", "religious", "institutional", "education", "garages", "military"];
const GREEN_LANDUSE = ["farmland", "meadow", "grass", "orchard", "vineyard", "allotments", "greenhouse_horticulture", "plant_nursery", "village_green", "recreation_ground"];
export const OSM_CTX_COLORS = { built: "#d97706", green: "#4d7c0f", forest: "#14532d", other: "#7c3aed", wall: "#44403c", fence: "#9a3412", hedge: "#15803d", highway: "#f8fafc", building: "#dc2626" };
const LANDUSE_COLOR = ["case",
  ["in", ["get", "value"], ["literal", BUILT_LANDUSE]], OSM_CTX_COLORS.built,
  ["in", ["get", "value"], ["literal", GREEN_LANDUSE]], OSM_CTX_COLORS.green,
  ["==", ["get", "value"], "forest"], OSM_CTX_COLORS.forest,
  OSM_CTX_COLORS.other];
const BARRIER_COLOR = ["match", ["get", "value"], "fence", OSM_CTX_COLORS.fence, "hedge", OSM_CTX_COLORS.hedge, OSM_CTX_COLORS.wall];
const CTX_LAYERS = {
  landuse: ["ctx-landuse-fill", "ctx-landuse-line"],
  highway: ["ctx-highway-casing", "ctx-highway", "ctx-highway-other"],
  barrier: ["ctx-barrier", "ctx-fence"],
  building: ["ctx-building-fill", "ctx-building"],
};
// Linha do elemento em edição (o halo é o do iD): a cor da camada a que pertence.
const EDIT_BUILDING_COLOR = "#e06e5f"; // a dos edifícios no iD
function landuseColor(value) {
  if (BUILT_LANDUSE.includes(value)) return OSM_CTX_COLORS.built;
  if (GREEN_LANDUSE.includes(value)) return "#a3e635"; // mais claro que o da camada: lê-se sobre o halo
  if (value === "forest") return "#4ade80";
  return "#c4b5fd";
}

// Camada personalizada do MapLibre que desenha, com three.js, os telhados e paredes de
// todos os edifícios tal como os renderizadores os vão mostrar a partir das tags.
// Geometria em metros Web Mercator relativos a uma origem; z em metros reais.
class RoofsLayer {
  constructor(scene) {
    this.id = "roofs-3d";
    this.type = "custom";
    this.renderingMode = "3d";
    this.data = scene;
    this.groups = new Map();
    this.materials = new Map();
    this.visibleKeys = null;
    this.selectedKey = null;
  }

  onAdd(map, gl) {
    this.map = map;
    const origin = maplibregl.MercatorCoordinate.fromLngLat(this.data.origin, 0);
    const s = 1 / EARTH_CIRCUMFERENCE;
    this.zScale = origin.meterInMercatorCoordinateUnits() / s; // metros reais → metros Mercator
    this.modelMatrix = new THREE.Matrix4()
      .makeTranslation(origin.x, origin.y, origin.z)
      .scale(new THREE.Vector3(s, -s, s));
    this.camera = new THREE.Camera();
    this.scene = new THREE.Scene();
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.4));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-0.5, 0.8, 1.2); // de noroeste, alto (relevo nas águas)
    this.scene.add(sun);
    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl, antialias: true });
    this.renderer.autoClear = false;
    for (const b of this.data.buildings) this.setBuilding(b);
  }

  material(hex, kind) {
    const key = `${kind}:${hex}`;
    if (!this.materials.has(key)) {
      this.materials.set(key, new THREE.MeshLambertMaterial({
        color: new THREE.Color(hex), flatShading: kind === "roof", side: THREE.DoubleSide,
        emissive: 0x000000,
      }));
    }
    return this.materials.get(key);
  }

  mesh(m, material) {
    if (!m.f.length) return null;
    const pos = new Float32Array(m.v.length);
    for (let i = 0; i < m.v.length; i += 3) {
      pos[i] = m.v[i];
      pos[i + 1] = m.v[i + 1];
      pos[i + 2] = m.v[i + 2] * this.zScale;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setIndex(m.f);
    g.computeVertexNormals();
    return new THREE.Mesh(g, material);
  }

  setBuilding(b) {
    const old = this.groups.get(b.key);
    if (old) {
      this.root.remove(old);
      old.traverse((o) => o.geometry?.dispose());
    }
    const group = new THREE.Group();
    group.userData.key = b.key;
    for (const p of b.parts) {
      const roof = this.mesh(p.roof, this.material(p.roof_colour, "roof"));
      const walls = this.mesh(p.walls, this.material(p.walls_colour, "walls"));
      if (roof) group.add(roof);
      if (walls) group.add(walls);
    }
    group.visible = !this.visibleKeys || this.visibleKeys.has(b.key);
    this.groups.set(b.key, group);
    this.root.add(group);
    this.highlight(this.selectedKey);
    this.map?.triggerRepaint();
  }

  setVisible(keys) {
    this.visibleKeys = keys;
    for (const [key, g] of this.groups) g.visible = !keys || keys.has(key);
    this.map?.triggerRepaint();
  }

  highlight(key) {
    // Edifício selecionado: brilho ligeiro (materiais próprios para não afetar os outros).
    if (this.selectedKey && this.selectedKey !== key) {
      this.groups.get(this.selectedKey)?.traverse((o) => {
        if (o.isMesh && o.userData.base) { o.material = o.userData.base; delete o.userData.base; }
      });
    }
    this.selectedKey = key;
    this.groups.get(key)?.traverse((o) => {
      if (o.isMesh && !o.userData.base) {
        o.userData.base = o.material;
        o.material = o.material.clone();
        o.material.emissive = new THREE.Color(0x1d4ed8);
        o.material.emissiveIntensity = 0.45;
      }
    });
    this.map?.triggerRepaint();
  }

  render(gl, args) {
    // MapLibre 4: args é a matriz; versões mais recentes passam um objeto.
    const raw = args?.defaultProjectionData?.mainMatrix ?? args?.modelViewProjectionMatrix ?? args;
    this.camera.projectionMatrix = new THREE.Matrix4().fromArray(raw).multiply(this.modelMatrix);
    this.renderer.resetState();
    this.renderer.render(this.scene, this.camera);
  }
}

export const BASEMAP = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 19,
      attribution: "© contribuidores OpenStreetMap",
    },
    // Ortofotos descarregadas (separador Dados), servidas pela própria aplicação.
    ortho: {
      type: "raster",
      tiles: [`${location.origin}/api/ortho/{z}/{x}/{y}.png`],
      tileSize: 256,
      minzoom: 10,
      maxzoom: 19,
      attribution: "Ortofotos © DGT",
    },
  },
  layers: [
    { id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.6, "raster-opacity": 0.85 } },
    { id: "ortho", type: "raster", source: "ortho", layout: { visibility: "none" } },
  ],
};

// Fundo do mapa: "osm" (mapa) ou "ortho" (ortofotos locais; o mapa fica por baixo, onde
// não houver ortofoto). A escolha fica guardada no browser.
const BASEMAP_KEY = "osm3d.basemap";

export function savedBasemap() {
  try {
    return localStorage.getItem(BASEMAP_KEY) === "ortho" ? "ortho" : "osm";
  } catch {
    return "osm";
  }
}

export function applyBasemap(map, kind) {
  try { localStorage.setItem(BASEMAP_KEY, kind); } catch { /* sem armazenamento */ }
  if (!map.getLayer("ortho")) return;
  const ortho = kind === "ortho";
  map.setLayoutProperty("ortho", "visibility", ortho ? "visible" : "none");
  map.setPaintProperty("osm", "raster-opacity", ortho ? 0.55 : 0.85);
}

// Opções do seletor de fundo; sem ortofotos descarregadas, a opção fica desativada.
export async function basemapOptions(select) {
  let info = { available: false, files: [] };
  try {
    info = await (await fetch("/api/ortho/info")).json();
  } catch { /* sem ortofotos */ }
  const years = [...new Set(info.files.map((f) => f.year).filter(Boolean))].sort().join(", ");
  select.innerHTML =
    `<option value="osm">Fundo: mapa OSM</option>` +
    `<option value="ortho" ${info.available ? "" : "disabled"}>Fundo: ortofoto${years ? ` ${years}` : " (sem ficheiros)"}</option>`;
  select.title = info.available
    ? `${info.files.length} ortofoto(s) descarregada(s); fora delas vê-se o mapa`
    : "Sem ortofotos descarregadas: descarregar no separador Dados (produto ortofotos)";
  const kind = info.available ? savedBasemap() : "osm";
  select.value = kind;
  return kind;
}

function matchExpr(prop, palette, fallback) {
  const expr = ["match", ["to-string", ["get", prop]]];
  for (const [k, v] of Object.entries(palette)) expr.push(k, v);
  expr.push(fallback);
  return expr;
}

const COLOR_MODES = {
  // Cores que vão ser aplicadas no OSM (roof:colour final; sem tag → cor por omissão).
  real: { expr: ["coalesce", ["get", "roof_colour"], "#c8c8c8"], legend: {}, labels: {} },
  change: { expr: matchExpr("change", CHANGE_COLORS, "#ccc"), legend: CHANGE_COLORS, labels: CHANGE_LABELS },
  shape: { expr: matchExpr("shape", SHAPE_COLORS, SHAPE_COLORS.unknown), legend: SHAPE_COLORS, labels: SHAPE_LABELS },
  status: {
    expr: matchExpr("status", STATUS_COLORS, "#999"),
    legend: STATUS_COLORS,
    labels: { pending: "por rever", accepted: "aceite", rejected: "rejeitado" },
  },
  match: {
    expr: ["case", ["==", ["get", "match"], true], MATCH_COLORS.true, ["==", ["get", "match"], false], MATCH_COLORS.false, MATCH_COLORS.none],
    legend: MATCH_COLORS,
    labels: { true: "= OSM", false: "≠ OSM", none: "sem referência" },
  },
  // Origem: OSM, ou candidato com contorno municipal / do LiDAR (ortofoto).
  origin: {
    expr: matchExpr("candidate_source", { municipal: ORIGIN_COLORS.municipal }, ORIGIN_COLORS.osm),
    legend: ORIGIN_COLORS,
    labels: ORIGIN_LABELS,
  },
};

const WALL_COLORS = { pending: "#db2777", accepted: "#16a34a", rejected: "#9ca3af" };

export class BuildingMap {
  constructor(container, legendEl, onSelect, onSelectWall) {
    this.legendEl = legendEl;
    this.onSelect = onSelect;
    this.onSelectWall = onSelectWall;
    this.wallFilter = new Set(["pending", "accepted"]);
    this.selectedWall = null;
    this.selectedId = null;
    this.selectedKey = null;
    this.mode = "real";
    this.visibleKeys = null;
    this.roofs = null;
    this.map = new maplibregl.Map({ container, style: BASEMAP, center: [-8.0, 39.6], zoom: 6, maxPitch: 70 }); // enquadrado nos edifícios ao carregar
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    this.ready = new Promise((resolve) => this.map.on("load", resolve));
    // Edição de geometria (muros e landuse), ao estilo do iD; o elemento em edição sai
    // das camadas normais enquanto é editado.
    this.onSelectLanduse = null;
    this.onSelectOsmBuilding = null;
    this.onDeselect = null; // clique no fundo do mapa (ou Esc sem nada no editor)
    this.onEditCommit = null;
    this.onStatus = null;
    this.editingWall = null;
    this.editingCtx = null; // way do OSM em edição (landuse ou edifício): "way/<id>"
    this.editingBuilding = null; // edifício da cartografia municipal em edição
    this.index = { walls: new Map(), ctx: new Map(), buildings: new Map() };
    this.data = { walls: null, ctx: null, buildings: null }; // as coleções mostradas (para applyLocalEdit)
    this.editor = new GeometryEditor(this.map, {
      snapTargets: (point, px) => this.snapTargets(point, px),
      linkCandidates: (t) => this.linkCandidates(t),
      onCommit: (target, coords, ids, others) => {
        this.applyWallFilter(); // muros que passaram a estar ligados também se editam
        this.onEditCommit?.(target, coords, ids, others);
      },
      onStatus: (msg) => this.onStatus?.(msg),
      onEscape: () => this.onDeselect?.(),
    });
    this.ready.then(() => this.editor.install());
    // Shift + arrastar fazia zoom a uma caixa: Shift + clique é a seleção múltipla.
    this.map.boxZoom.disable();
    this.multiKeys = [];
  }

  // ------------------------------------------------------------- edição

  setEditing({ wall = null, ctx = null, building = null }) {
    // O elemento em edição sai das camadas normais: desenha-o o editor.
    this.editingWall = wall;
    this.editingCtx = ctx;
    this.editingBuilding = building;
    this.applyCtxFilters();
    this.applyWallFilter();
    this.applyFilter();
  }

  editWall(d) {
    this.setEditing({ wall: d.key });
    this.editor.start({ key: d.key, closed: false, coords: d.coords, min: 2 });
    this.applyWallFilter(); // os muros ligados também saem das camadas (desenha-os o editor)
  }

  editLanduse(d) {
    // Way fechada do OSM: landuse ou, fora da revisão, edifício.
    this.setEditing({ ctx: d.key });
    const building = !!d.is_building;
    this.editor.start({
      key: d.key,
      kind: "area",
      closed: true,
      coords: d.nodes.map((n) => [n[1], n[2]]),
      ids: d.nodes.map((n) => n[0]),
      shared: new Set(Object.keys(d.shared ?? {}).map(Number)),
      min: 3,
      color: building ? EDIT_BUILDING_COLOR : landuseColor(d.tags?.landuse),
      fill: building,
      label: building ? "edifício" : "landuse",
    });
  }

  editBuilding(key, g) {
    // Contorno de um edifício em revisão: do OSM (nós, como os landuse) ou municipal.
    const osm = g.kind === "osm";
    this.setEditing(osm ? { ctx: key } : { building: key });
    this.editor.start({
      key,
      kind: "area",
      closed: true,
      coords: g.nodes.map((n) => [n[1], n[2]]),
      ids: osm ? g.nodes.map((n) => n[0]) : null,
      shared: new Set(Object.keys(g.shared ?? {}).map(Number)),
      min: 3,
      color: EDIT_BUILDING_COLOR,
      fill: true,
      label: "edifício",
    });
  }

  applyLocalEdit(changes) {
    // Edição terminada no mapa, mostrada já (sem esperar pelo servidor): a geometria nova de
    // cada elemento nas camadas e nos índices de encaixe/ligação. changes: [{ key, coords,
    // ids }] (anéis sem repetir o primeiro ponto; ids: nós do OSM, null num vértice novo).
    // Um nó do OSM movido move-se também nas outras ways que o usam (como no servidor).
    // A versão do servidor (recortes, nós novos) chega depois, ao recarregar as camadas.
    const dirty = new Set();
    const moved = new Map(); // nó do OSM → posição nova
    const geometry = (closed, coords) => (closed
      ? { type: "Polygon", coordinates: [[...coords, coords[0]].map((c) => [...c])] }
      : { type: "LineString", coordinates: coords.map((c) => [...c]) });
    const byKey = (fc, prop, key) => fc?.features.find((f) => f.properties[prop] === key && (fc !== this.data.walls || f.properties.kind === "wall"));
    for (const { key, coords, ids } of changes) {
      if (!coords?.length) continue;
      ids?.forEach((id, i) => id != null && moved.set(id, coords[i]));
      if (key.startsWith("wall/")) {
        const f = byKey(this.data.walls, "key", key);
        if (!f) continue;
        f.geometry = geometry(false, coords);
        this.index.walls.set(key, f.geometry);
        dirty.add("walls");
        continue;
      }
      const b = byKey(this.data.buildings, "key", key);
      if (b && coords.length >= 3) {
        b.geometry = geometry(true, coords);
        this.index.buildings.set(key, b.geometry);
        dirty.add("buildings");
      }
      const c = byKey(this.data.ctx, "id", key);
      if (c && coords.length >= 3 && c.geometry?.type === "Polygon") {
        c.geometry = geometry(true, coords);
        c.properties.nodes = ids ? [...ids, ids[0]] : null;
        this.index.ctx.set(key, c.geometry);
        if (c.properties.nodes) this.index.ctxNodes.set(key, c.properties.nodes);
        else this.index.ctxNodes.delete(key);
        dirty.add("ctx");
      }
    }
    if (moved.size && this.data.ctx) {
      const keys = new Set(changes.map((c) => c.key));
      for (const f of this.data.ctx.features) {
        const nodes = f.properties.nodes;
        if (keys.has(f.properties.id) || !nodes?.some((n) => moved.has(n))) continue;
        const line = f.geometry.type === "Polygon" ? f.geometry.coordinates[0] : f.geometry.type === "LineString" ? f.geometry.coordinates : null;
        if (!line || line.length !== nodes.length) continue;
        const next = line.map((c, i) => (moved.has(nodes[i]) ? [...moved.get(nodes[i])] : c));
        f.geometry = f.geometry.type === "Polygon" ? { type: "Polygon", coordinates: [next, ...f.geometry.coordinates.slice(1)] } : { type: "LineString", coordinates: next };
        this.index.ctx.set(f.properties.id, f.geometry);
        dirty.add("ctx");
      }
    }
    const sources = { walls: "walls", buildings: "buildings", ctx: "osm-ctx" };
    for (const d of dirty) this.map.getSource(sources[d])?.setData(this.data[d]);
  }

  stopEditing() {
    if (!this.editor.target && !this.editingWall && !this.editingCtx && !this.editingBuilding) return;
    this.editor.stop();
    this.setEditing({});
  }

  snapTargets(point, px) {
    // Geometrias originais (não as das tiles, cortadas) dos elementos à volta do cursor:
    // [{ key, source, coords }] (um anel de polígono é uma linha).
    const layers = ["walls-line", "walls-fence", "ctx-landuse-line", "ctx-building", "ctx-barrier", "ctx-fence", "b-line"]
      .filter((id) => this.map.getLayer(id) && this.map.getLayoutProperty(id, "visibility") !== "none");
    if (!layers.length) return [];
    const box = [[point.x - px, point.y - px], [point.x + px, point.y + px]];
    const out = [];
    const seen = new Set();
    for (const f of this.map.queryRenderedFeatures(box, { layers })) {
      const p = f.properties;
      const [kind, id] = f.source === "walls" ? ["walls", p.key] : f.source === "osm-ctx" ? ["ctx", p.id] : ["buildings", p.key];
      if (!id || seen.has(`${kind}:${id}`) || (kind === "walls" && p.kind !== "wall")) continue;
      seen.add(`${kind}:${id}`);
      const g = this.index[kind].get(id);
      if (!g) continue;
      // Ways do OSM: o id do nó de cada vértice (anel exterior/linha, pela mesma ordem).
      const nodes = kind === "ctx" ? this.index.ctxNodes?.get(id) : null;
      const add = (coords, k = 0) => out.push({ key: id, source: kind, coords, ring: k, nodes: k === 0 && nodes?.length === coords.length ? nodes : null });
      if (g.type === "LineString") add(g.coordinates);
      else if (g.type === "Polygon") g.coordinates.forEach(add);
      else if (g.type === "MultiPolygon") for (const poly of g.coordinates) poly.forEach(add);
    }
    return out;
  }

  buildingAt(p) {
    // Edifício sob o ponto: o municipal (em revisão) ou o do OSM; também o que está em
    // edição (desenhado pelo editor). { key, osm } ou null.
    const vis = (id) => this.map.getLayer(id) && this.map.getLayoutProperty(id, "visibility") !== "none";
    if (this.editor.target?.fill && this.map.queryRenderedFeatures(p, { layers: ["edit-fill"] }).length) {
      const key = this.editor.target.key;
      return { key, osm: key.startsWith("way/") };
    }
    const b = vis("b-fill") ? this.map.queryRenderedFeatures(p, { layers: ["b-fill"] })[0] : null;
    if (b) return { key: b.properties.key, osm: false };
    const m = vis("multi-fill") ? this.map.queryRenderedFeatures(p, { layers: ["multi-fill"] })[0] : null;
    if (m) return { key: m.properties.key, osm: m.properties.key.startsWith("way/") };
    const o = vis("ctx-building-fill")
      ? this.map.queryRenderedFeatures(p, { layers: ["ctx-building-fill"] }).find((f) => String(f.properties.id).startsWith("way/"))
      : null;
    return o ? { key: o.properties.id, osm: true } : null;
  }

  setMulti(rings) {
    // Seleção múltipla (como no iD): os edifícios selecionados com o halo; rings:
    // [{ key, coords }] (anéis sem repetir o primeiro ponto).
    this.multiKeys = rings.map((r) => r.key);
    const data = {
      type: "FeatureCollection",
      features: rings.filter((r) => r.coords.length >= 3).map((r) => ({
        type: "Feature",
        properties: { key: r.key },
        geometry: { type: "Polygon", coordinates: [[...r.coords, r.coords[0]]] },
      })),
    };
    const src = this.map.getSource("multi");
    if (src) src.setData(data);
    else {
      this.map.addSource("multi", { type: "geojson", data });
      this.map.addLayer({ id: "multi-fill", type: "fill", source: "multi", paint: { "fill-color": EDIT_BUILDING_COLOR, "fill-opacity": 0.18 } });
      this.map.addLayer({ id: "multi-halo", type: "line", source: "multi", layout: { "line-join": "round" },
        paint: { "line-color": "#f6634f", "line-opacity": 0.7, "line-width": ["interpolate", ["linear"], ["zoom"], 15, 8, 18, 14] } });
      this.map.addLayer({ id: "multi-line", type: "line", source: "multi", paint: { "line-color": EDIT_BUILDING_COLOR, "line-width": 2 } });
    }
    this.editor.raise();
  }

  vertexOwners() {
    // Ponto → elementos (landuse e edifícios) com um vértice nele: um vértice partilhado
    // não sai ao esquadrar.
    const out = new Map();
    for (const c of this.linkCandidates({ kind: "area" })) {
      for (const p of c.coords) {
        const k = posKey(p);
        if (!out.has(k)) out.set(k, new Set());
        out.get(k).add(c.key);
      }
    }
    return out;
  }

  linkCandidates(t) {
    // Elementos a que o elemento em edição se liga (vértices no mesmo ponto), como no iD:
    // muros com muros; landuse e edifícios (do OSM e municipais) entre si. Anéis sem
    // repetir o primeiro ponto; ids: os nós do OSM de cada vértice.
    if (t.kind === "wall") {
      return [...this.index.walls]
        .filter(([, g]) => g?.type === "LineString")
        .map(([key, g]) => ({ key, source: "walls", closed: false, coords: g.coordinates, ids: null }));
    }
    const out = [];
    for (const [key, g] of this.index.ctx) {
      if (!String(key).startsWith("way/") || g?.type !== "Polygon" || !this.index.ctxAreas?.has(key)) continue;
      const ring = g.coordinates[0].slice(0, -1);
      const nodes = this.index.ctxNodes?.get(key);
      const ids = nodes?.length === ring.length + 1 ? nodes.slice(0, -1) : ring.map(() => null);
      out.push({ key, source: "ctx", closed: true, coords: ring, ids });
    }
    for (const [key, g] of this.index.buildings) {
      if (g?.type === "Polygon") out.push({ key, source: "buildings", closed: true, coords: g.coordinates[0].slice(0, -1), ids: null });
    }
    return out;
  }

  applyCtxFilters() {
    // Way do OSM em edição: só a geometria do editor (a do contexto ainda é a antiga).
    if (!this.map.getLayer("ctx-landuse-line")) return;
    const notEditing = ["!=", ["get", "id"], this.editingCtx ?? ""];
    const landuse = ["==", ["get", "kind"], "landuse"];
    const building = ["==", ["get", "kind"], "building"];
    const polygon = ["==", ["geometry-type"], "Polygon"];
    this.map.setFilter("ctx-landuse-fill", ["all", landuse, polygon, notEditing]);
    this.map.setFilter("ctx-landuse-line", ["all", landuse, notEditing]);
    this.map.setFilter("ctx-building-fill", ["all", building, polygon, notEditing]);
    this.map.setFilter("ctx-building", ["all", building, notEditing]);
  }

  async setOsmContext(geojson) {
    // Landuse, vias, muros/vedações e edifícios do OSM, por baixo dos edifícios em revisão.
    await this.ready;
    this.index.ctx = new Map(geojson.features.map((f) => [f.properties.id, f.geometry]));
    this.index.ctxNodes = new Map(geojson.features.filter((f) => f.properties.nodes).map((f) => [f.properties.id, f.properties.nodes]));
    // Landuse e edifícios do OSM: ligam-se aos edifícios em edição (e entre si).
    this.index.ctxAreas = new Set(geojson.features.filter((f) => ["landuse", "building"].includes(f.properties.kind)).map((f) => f.properties.id));
    this.data.ctx = geojson;
    const src = this.map.getSource("osm-ctx");
    if (src) return src.setData(geojson);
    this.map.addSource("osm-ctx", { type: "geojson", data: geojson });
    const before = this.map.getLayer("b-fill") ? "b-fill" : undefined;
    const kind = (k) => ["==", ["get", "kind"], k];
    const add = (layer) => this.map.addLayer(layer, before);
    add({ id: "ctx-landuse-fill", type: "fill", source: "osm-ctx", filter: ["all", kind("landuse"), ["==", ["geometry-type"], "Polygon"]],
      paint: { "fill-color": LANDUSE_COLOR, "fill-opacity": 0.12 } });
    add({ id: "ctx-landuse-line", type: "line", source: "osm-ctx", filter: kind("landuse"),
      paint: { "line-color": LANDUSE_COLOR, "line-width": ["interpolate", ["linear"], ["zoom"], 15, 1, 19, 2.5], "line-opacity": 0.9 } });
    add({ id: "ctx-highway-casing", type: "line", source: "osm-ctx", filter: kind("highway"),
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#334155", "line-width": ["interpolate", ["linear"], ["zoom"], 15, 2.5, 19, 7] } });
    add({ id: "ctx-highway", type: "line", source: "osm-ctx", filter: ["all", kind("highway"), ["get", "crossing"]],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": OSM_CTX_COLORS.highway, "line-width": ["interpolate", ["linear"], ["zoom"], 15, 1.2, 19, 4.5] } });
    add({ id: "ctx-highway-other", type: "line", source: "osm-ctx", filter: ["all", kind("highway"), ["!", ["get", "crossing"]]],
      paint: { "line-color": "#94a3b8", "line-width": ["interpolate", ["linear"], ["zoom"], 15, 1.2, 19, 4.5], "line-dasharray": [2, 2] } });
    add({ id: "ctx-barrier", type: "line", source: "osm-ctx", filter: ["all", kind("barrier"), ["!=", ["get", "value"], "fence"]],
      paint: { "line-color": BARRIER_COLOR, "line-width": ["interpolate", ["linear"], ["zoom"], 15, 1.2, 19, 3.5] } });
    add({ id: "ctx-fence", type: "line", source: "osm-ctx", filter: ["all", kind("barrier"), ["==", ["get", "value"], "fence"]],
      paint: { "line-color": OSM_CTX_COLORS.fence, "line-width": ["interpolate", ["linear"], ["zoom"], 15, 1.2, 19, 3], "line-dasharray": [2, 1.5] } });
    // Edifícios do OSM: preenchimento leve (clicável: abre o edifício) e contorno tracejado.
    add({ id: "ctx-building-fill", type: "fill", source: "osm-ctx", filter: ["all", kind("building"), ["==", ["geometry-type"], "Polygon"]],
      paint: { "fill-color": OSM_CTX_COLORS.building, "fill-opacity": 0.06 } });
    add({ id: "ctx-building", type: "line", source: "osm-ctx", filter: kind("building"),
      paint: { "line-color": OSM_CTX_COLORS.building, "line-width": 1.2, "line-dasharray": [3, 1.5] } });
    this.map.on("mouseenter", "ctx-building-fill", () => (this.map.getCanvas().style.cursor = "pointer"));
    this.map.on("mouseleave", "ctx-building-fill", () => (this.map.getCanvas().style.cursor = ""));
    this.applyOsmLayers();
    this.applyCtxFilters();
    this.editor.raise();
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "ctx-popup" });
    for (const id of ["ctx-landuse-line", "ctx-highway", "ctx-highway-other", "ctx-barrier", "ctx-fence", "ctx-building"]) {
      this.map.on("mousemove", id, (e) => {
        const p = e.features[0]?.properties;
        if (!p) return;
        popup.setLngLat(e.lngLat).setHTML(`${p.kind}=${p.value}${p.name ? ` · ${p.name}` : ""}<br><span class="muted">${p.id}</span>`).addTo(this.map);
      });
      this.map.on("mouseleave", id, () => popup.remove());
    }
    this.renderLegend();
  }

  async setWalls(geojson) {
    // Muros propostos (cor pelo estado) e, do selecionado, os limites de landuse que passam
    // para ele (tracejado).
    await this.ready;
    this.index.walls = new Map(geojson.features.filter((f) => f.properties.kind === "wall").map((f) => [f.properties.key, f.geometry]));
    this.data.walls = geojson;
    const src = this.map.getSource("walls");
    if (src) {
      src.setData(geojson);
      return this.applyWallFilter();
    }
    this.map.addSource("walls", { type: "geojson", data: geojson });
    const color = ["match", ["get", "status"], "accepted", WALL_COLORS.accepted, "rejected", WALL_COLORS.rejected, WALL_COLORS.pending];
    this.map.addLayer({ id: "walls-edge", type: "line", source: "walls", filter: ["==", ["get", "key"], ""],
      paint: { "line-color": "#f97316", "line-width": 3, "line-dasharray": [1.5, 1.2] } });
    this.map.addLayer({ id: "walls-sel", type: "line", source: "walls", filter: ["==", ["get", "key"], ""],
      layout: { "line-cap": "round" }, paint: { "line-color": "#1d4ed8", "line-width": 8, "line-opacity": 0.45 } });
    // Vedações a tracejado numa camada à parte: o MapLibre 4 não aceita line-dasharray
    // por propriedade (a camada era rejeitada e nenhum muro aparecia).
    const width = ["interpolate", ["linear"], ["zoom"], 15, 1.5, 19, 4.5];
    this.map.addLayer({ id: "walls-line", type: "line", source: "walls", filter: ["==", ["get", "kind"], "wall"],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": color, "line-width": width } });
    this.map.addLayer({ id: "walls-fence", type: "line", source: "walls", filter: ["==", ["get", "kind"], "wall"],
      layout: { "line-join": "round" },
      paint: { "line-color": color, "line-width": width, "line-dasharray": [2, 1] } });
    for (const id of WALL_LAYERS) {
      this.map.on("mouseenter", id, () => (this.map.getCanvas().style.cursor = "pointer"));
      this.map.on("mouseleave", id, () => (this.map.getCanvas().style.cursor = ""));
    }
    this.applyWallFilter();
    this.editor.raise();
    this.renderLegend();
  }

  setWallFilter(statuses) {
    this.wallFilter = statuses;
    this.applyWallFilter();
  }

  applyWallFilter() {
    if (!this.map.getLayer("walls-line")) return;
    const showArchived = this.wallFilter.has("archived");
    const st = [...this.wallFilter].filter((s) => s !== "archived");
    const shown = ["all", ["==", ["get", "kind"], "wall"],
      ["in", ["get", "status"], ["literal", st]],
      showArchived ? true : ["!", ["get", "archived"]],
      // Em edição (o muro e os ligados a ele): desenha-os o editor.
      ["!", ["in", ["get", "key"], ["literal", [this.editingWall ?? "", ...this.editor.linkedKeys()]]]]];
    const fence = ["==", ["get", "barrier"], "fence"];
    this.map.setFilter("walls-line", ["all", shown, ["!", fence]]);
    this.map.setFilter("walls-fence", ["all", shown, fence]);
    const key = this.selectedWall ?? "";
    this.map.setFilter("walls-edge", ["all", ["==", ["get", "kind"], "edge"], ["==", ["get", "key"], key]]);
    const halo = this.editingWall === key ? "" : key; // em edição: o editor já o destaca
    this.map.setFilter("walls-sel", ["all", ["==", ["get", "kind"], "wall"], ["==", ["get", "key"], halo]]);
  }

  selectWall(key, bbox) {
    this.selectedWall = key;
    this.applyWallFilter();
    if (key && bbox) {
      const [w, s, e, n] = bbox;
      const center = [(w + e) / 2, (s + n) / 2];
      if (!this.map.getBounds().contains(center) || this.map.getZoom() < 17) {
        this.map.easeTo({ center, zoom: Math.max(this.map.getZoom(), 18), duration: 500 });
      }
    }
  }

  setOsmLayers(kinds) {
    this.osmKinds = kinds;
    this.applyOsmLayers();
    this.renderLegend();
  }

  applyOsmLayers() {
    const kinds = this.osmKinds ?? new Set(Object.keys(CTX_LAYERS));
    for (const [k, ids] of Object.entries(CTX_LAYERS)) {
      for (const id of ids) if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", kinds.has(k) ? "visible" : "none");
    }
  }

  async setData(geojson) {
    await this.ready;
    this.features = new Map(geojson.features.map((f) => [f.properties.key, f]));
    this.keyToId = new Map(geojson.features.map((f) => [f.properties.key, f.id]));
    this.index.buildings = new Map(geojson.features.map((f) => [f.properties.key, f.geometry]));
    this.data.buildings = geojson;
    const src = this.map.getSource("buildings");
    if (src) {
      src.setData(geojson);
      if (this.selectedKey) this.select(this.selectedKey);
      return;
    }
    this.map.addSource("buildings", { type: "geojson", data: geojson });
    const color = COLOR_MODES[this.mode].expr;
    this.map.addLayer({
      id: "b-fill", type: "fill", source: "buildings",
      paint: { "fill-color": color, "fill-opacity": this.fillOpacity() },
    });

    this.map.addLayer({
      id: "b-line", type: "line", source: "buildings",
      paint: {
        "line-color": ["case", ["boolean", ["feature-state", "selected"], false], "#1d4ed8", ["get", "candidate"], CANDIDATE_LINE, ["get", "osm3d"], OSM3D_LINE, ["get", "needs_review"], "#b45309", "#374151"],
        "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 3.5, ["get", "candidate"], 2.4, ["get", "osm3d"], 2.4, ["get", "needs_review"], 1.4, 0.6],
      },
    });
    this.applyFilter();
    this.map.on("click", (e) => {
      // Muros por cima dos edifícios: um clique perto de um muro seleciona o muro.
      // Shift + clique num edifício: junta-o (ou tira-o) da seleção, como no iD.
      const p = e.point;
      const shift = !!e.originalEvent?.shiftKey;
      if (shift) {
        const hit = this.buildingAt(p);
        if (hit) return hit.osm ? this.onSelectOsmBuilding?.(hit.key, true) : this.onSelect(hit.key, true);
        return;
      }
      if (this.editor.hitTest(p)) return; // vértice/aresta em edição
      const box = [[p.x - 5, p.y - 5], [p.x + 5, p.y + 5]];
      const wall = this.map.getLayer("walls-line") ? this.map.queryRenderedFeatures(box, { layers: WALL_LAYERS })[0] : null;
      if (wall) return this.onSelectWall?.(wall.properties.key);
      // Limite de um landuse (way do OSM): seleciona-o para editar os nós.
      const near = [[p.x - 3, p.y - 3], [p.x + 3, p.y + 3]];
      const landuse = this.map.getLayer("ctx-landuse-line") && this.map.getLayoutProperty("ctx-landuse-line", "visibility") !== "none"
        ? this.map.queryRenderedFeatures(near, { layers: ["ctx-landuse-line"] }).find((f) => String(f.properties.id).startsWith("way/"))
        : null;
      if (landuse) return this.onSelectLanduse?.(landuse.properties.id);
      const b = this.map.queryRenderedFeatures(p, { layers: ["b-fill"] })[0];
      if (b) return this.onSelect(b.properties.key);
      // Edifício do OSM (camada "edifícios do OSM"): abre-o para rever e editar.
      const osm = this.map.getLayer("ctx-building-fill") && this.map.getLayoutProperty("ctx-building-fill", "visibility") !== "none"
        ? this.map.queryRenderedFeatures(p, { layers: ["ctx-building-fill"] }).find((f) => String(f.properties.id).startsWith("way/"))
        : null;
      if (osm) return this.onSelectOsmBuilding?.(osm.properties.id);
      // Nada clicável (fundo, interior de um landuse): tira a seleção, como no iD.
      this.onDeselect?.();
    });
    // Botão direito dentro de um dos edifícios da seleção múltipla: as ações (esquadrar).
    this.map.on("contextmenu", (e) => {
      if (!this.multiKeys?.length || !this.map.getLayer("multi-fill")) return;
      if (this.map.queryRenderedFeatures(e.point, { layers: ["multi-fill"] }).length) {
        e.preventDefault();
        this.onMultiMenu?.(e.point);
      }
    });
    this.map.getCanvas().addEventListener("contextmenu", (ev) => this.multiKeys?.length && ev.preventDefault());
    this.map.on("mouseenter", "b-fill", () => (this.map.getCanvas().style.cursor = "pointer"));
    this.map.on("mouseleave", "b-fill", () => (this.map.getCanvas().style.cursor = ""));
    const b = new maplibregl.LngLatBounds();
    for (const f of geojson.features) for (const ring of f.geometry.coordinates.flat(f.geometry.type === "MultiPolygon" ? 1 : 0)) for (const c of ring) b.extend(c);
    if (!b.isEmpty()) this.map.fitBounds(b, { padding: 30, duration: 0 });
    this.renderLegend();
  }

  setPalette(palette, defaults) {
    // Legenda do modo "cores reais": paleta estimada + cor por omissão.
    const real = COLOR_MODES.real;
    real.legend = Object.fromEntries(palette.map((c) => [c.hex, c.hex]));
    real.labels = Object.fromEntries(palette.map((c) => [c.hex, c.label]));
    real.legend.default = defaults.roof;
    real.labels.default = "sem roof:colour (omissão)";
    if (this.mode === "real") this.renderLegend();
  }

  patchFeatures(features) {
    // Só os edifícios que mudaram (p.ex. numa revisão), sem voltar a carregar e indexar
    // o GeoJSON todo (o estado "selected" de cada um fica, é por id).
    const src = this.map.getSource("buildings");
    if (!src || !this.features || !features.length) return;
    for (const f of features) {
      this.features.set(f.properties.key, f);
      this.keyToId.set(f.properties.key, f.id);
      this.index.buildings.set(f.properties.key, f.geometry);
    }
    // As edições locais (applyLocalEdit) partem de data.buildings: fica igual ao mapa.
    if (this.data.buildings) this.data.buildings = { ...this.data.buildings, features: [...this.features.values()] };
    if (typeof src.updateData === "function") src.updateData({ remove: features.map((f) => f.id), add: features });
    else src.setData({ type: "FeatureCollection", features: [...this.features.values()] });
  }

  setFeatureProps(key, props) {
    // Muda propriedades de um edifício já no mapa (p.ex. o estado, antes da resposta).
    const f = this.features?.get(key);
    if (f) this.patchFeatures([{ ...f, properties: { ...f.properties, ...props } }]);
  }

  setVisibleKeys(keys) {
    // Filtros da barra: mostram/escondem edifícios no mapa (2D e 3D). O filtro do mapa
    // obriga a refazer as quadrículas: só quando o conjunto muda.
    const same = this.visibleKeys && keys && this.visibleKeys.size === keys.size && [...keys].every((k) => this.visibleKeys.has(k));
    if (same) return;
    this.visibleKeys = keys;
    this.applyFilter();
    this.roofs?.setVisible(keys);
  }

  applyFilter() {
    // Filtros da barra; o edifício em edição sai (desenha-o o editor).
    const parts = [];
    if (this.visibleKeys) parts.push(["in", ["get", "key"], ["literal", [...this.visibleKeys]]]);
    if (this.editingBuilding) parts.push(["!=", ["get", "key"], this.editingBuilding]);
    const filter = parts.length ? ["all", ...parts] : null;
    for (const id of ["b-fill", "b-line"]) {
      if (this.map.getLayer(id)) this.map.setFilter(id, filter);
    }
  }

  setColorMode(mode) {
    this.mode = mode;
    if (!this.map.getLayer("b-fill")) return; // criada depois, já com este modo
    this.map.setPaintProperty("b-fill", "fill-color", COLOR_MODES[mode].expr);
    this.renderLegend();
  }

  fillOpacity() {
    // Sobre a ortofoto, o preenchimento fica mais leve para se verem os telhados reais.
    return this.is3d ? 0 : this.basemap === "ortho" ? 0.3 : 0.7;
  }

  async setBasemap(kind) {
    await this.ready;
    this.basemap = kind;
    applyBasemap(this.map, kind);
    if (this.map.getLayer("b-fill")) this.map.setPaintProperty("b-fill", "fill-opacity", this.fillOpacity());
  }

  async set3D(on, loadScene) {
    // 3D: telhados com forma e cores inferidas; o polígono 2D fica invisível mas clicável.
    this.is3d = on;
    this.map.setPaintProperty("b-fill", "fill-opacity", this.fillOpacity());
    this.map.easeTo({ pitch: on ? 55 : 0, duration: 600 });
    if (on && !this.roofs) {
      const scene = await loadScene();
      this.roofs = new RoofsLayer(scene);
      this.roofs.visibleKeys = this.visibleKeys;
      this.map.addLayer(this.roofs, "b-line");
      if (this.selectedKey) this.roofs.highlight(this.selectedKey);
    }
    if (this.map.getLayer("roofs-3d")) {
      this.map.setLayoutProperty("roofs-3d", "visibility", on ? "visible" : "none");
    }
  }

  async reload3D(loadScene) {
    // Cena desatualizada (p.ex. outras cores propostas): volta a pedi-la se o 3D está ligado.
    if (this.map.getLayer("roofs-3d")) this.map.removeLayer("roofs-3d");
    this.roofs = null;
    if (this.is3d) await this.set3D(true, loadScene);
  }

  updateBuilding(sceneBuilding) {
    if (this.roofs && sceneBuilding) this.roofs.setBuilding(sceneBuilding);
  }

  select(key, bbox) {
    this.selectedKey = key;
    this.roofs?.highlight(key);
    const id = this.keyToId?.get(key);
    if (this.selectedId !== null) this.map.setFeatureState({ source: "buildings", id: this.selectedId }, { selected: false });
    this.selectedId = id ?? null;
    if (id !== undefined) this.map.setFeatureState({ source: "buildings", id }, { selected: true });
    if (bbox) {
      const [w, s, e, n] = bbox;
      const center = [(w + e) / 2, (s + n) / 2];
      if (!this.map.getBounds().contains(center) || this.map.getZoom() < 17) {
        this.map.easeTo({ center, zoom: Math.max(this.map.getZoom(), 18), duration: 500 });
      }
    }
  }

  renderLegend() {
    const { legend, labels } = COLOR_MODES[this.mode];
    const body = Object.entries(legend)
      .map(([k, c]) => `<div><span class="sw" style="background:${c}"></span>${labels[k] ?? k}</div>`)
      .join("") +
      `<div><span class="sw" style="background:none;border:2.5px solid ${CANDIDATE_LINE}"></span>em falta no OSM (cartografia municipal)</div>` +
      this.osmLegend();
    const open = legendOpen();
    this.legendEl.innerHTML = `<details class="legend-box" ${open ? "open" : ""}><summary>legenda</summary><div class="legend-body">${body}</div></details>`;
    this.legendEl.querySelector("details").addEventListener("toggle", (e) => legendOpen(e.target.open));
  }

  osmLegend() {
    if (!this.map.getSource("osm-ctx") && !this.map.getSource("walls")) return "";
    const kinds = this.osmKinds ?? new Set(Object.keys(CTX_LAYERS));
    const line = (c, label, dash = false) => `<div><span class="sw line" style="border-bottom:3px ${dash ? "dashed" : "solid"} ${c}"></span>${label}</div>`;
    const c = OSM_CTX_COLORS;
    const walls = this.map.getSource("walls")
      ? `<div class="legend-sep">muros propostos</div>` +
        line(WALL_COLORS.pending, "por rever") + line(WALL_COLORS.accepted, "aceite") + line(WALL_COLORS.rejected, "rejeitado") +
        line("#f97316", "limite de landuse que passa para o muro", true)
      : "";
    return walls + `<div class="legend-sep">OSM</div>` +
      (kinds.has("landuse") ? line(c.built, "landuse construído") + line(c.green, "landuse agrícola/verde") : "") +
      (kinds.has("highway") ? line("#334155", "via (não pode atravessar edifícios)") : "") +
      (kinds.has("barrier") ? line(c.wall, "muro") + line(c.fence, "vedação", true) : "") +
      (kinds.has("building") ? line(c.building, "edifício do OSM (clicar para rever e editar)", true) : "");
  }
}
