// Mapa (MapLibre): contornos coloridos por forma/estado/concordância; em modo 3D,
// telhados com a forma e as cores inferidas (camada three.js dentro do MapLibre).
import * as THREE from "three";
import {
  CHANGE_COLORS,
  CHANGE_LABELS,
  MATCH_COLORS,
  SHAPE_COLORS,
  SHAPE_LABELS,
  STATUS_COLORS,
} from "./colors.js";

// Contorno dos edifícios que já têm informação 3D no OSM (destacados em todos os modos).
const OSM3D_LINE = "#7c3aed";

const EARTH_CIRCUMFERENCE = 2 * Math.PI * 6378137;

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
  },
  layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.6, "raster-opacity": 0.85 } }],
};

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
};

export class BuildingMap {
  constructor(container, legendEl, onSelect) {
    this.legendEl = legendEl;
    this.onSelect = onSelect;
    this.selectedId = null;
    this.selectedKey = null;
    this.mode = "real";
    this.visibleKeys = null;
    this.roofs = null;
    this.map = new maplibregl.Map({ container, style: BASEMAP, center: [-8.66, 41.284], zoom: 15, maxPitch: 70 });
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    this.ready = new Promise((resolve) => this.map.on("load", resolve));
  }

  async setData(geojson) {
    await this.ready;
    this.keyToId = new Map(geojson.features.map((f) => [f.properties.key, f.id]));
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
      paint: { "fill-color": color, "fill-opacity": 0.7 },
    });

    this.map.addLayer({
      id: "b-line", type: "line", source: "buildings",
      paint: {
        "line-color": ["case", ["boolean", ["feature-state", "selected"], false], "#1d4ed8", ["get", "osm3d"], OSM3D_LINE, ["get", "needs_review"], "#b45309", "#374151"],
        "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 3.5, ["get", "osm3d"], 2.4, ["get", "needs_review"], 1.4, 0.6],
      },
    });
    this.applyFilter();
    this.map.on("click", "b-fill", (e) => e.features[0] && this.onSelect(e.features[0].properties.key));
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

  setVisibleKeys(keys) {
    // Filtros da barra: mostram/escondem edifícios no mapa (2D e 3D).
    this.visibleKeys = keys;
    this.applyFilter();
    this.roofs?.setVisible(keys);
  }

  applyFilter() {
    const filter = this.visibleKeys ? ["in", ["get", "key"], ["literal", [...this.visibleKeys]]] : null;
    for (const id of ["b-fill", "b-line"]) {
      if (this.map.getLayer(id)) this.map.setFilter(id, filter);
    }
  }

  setColorMode(mode) {
    this.mode = mode;
    this.map.setPaintProperty("b-fill", "fill-color", COLOR_MODES[mode].expr);
    this.renderLegend();
  }

  async set3D(on, loadScene) {
    // 3D: telhados com forma e cores inferidas; o polígono 2D fica invisível mas clicável.
    this.map.setPaintProperty("b-fill", "fill-opacity", on ? 0 : 0.7);
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
    this.legendEl.innerHTML = Object.entries(legend)
      .map(([k, c]) => `<div><span class="sw" style="background:${c}"></span>${labels[k] ?? k}</div>`)
      .join("") +
      `<div><span class="sw" style="background:none;border:2.5px solid ${OSM3D_LINE}"></span>já com 3D no OSM</div>`;
  }
}
