// Mapa (MapLibre): contornos coloridos por forma/estado/concordância, extrusão 3D.
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

const BASEMAP = {
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
    this.mode = "real";
    this.map = new maplibregl.Map({ container, style: BASEMAP, center: [-8.66, 41.284], zoom: 15, maxPitch: 70 });
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    this.ready = new Promise((resolve) => this.map.on("load", resolve));
  }

  async setData(geojson) {
    await this.ready;
    this.keyToId = new Map(geojson.features.map((f) => [f.properties.key, f.id]));
    const src = this.map.getSource("buildings");
    if (src) { src.setData(geojson); return; }
    this.map.addSource("buildings", { type: "geojson", data: geojson });
    const color = COLOR_MODES[this.mode].expr;
    this.map.addLayer({
      id: "b-fill", type: "fill", source: "buildings",
      paint: { "fill-color": color, "fill-opacity": 0.7 },
    });
    this.map.addLayer({
      id: "b-extrusion", type: "fill-extrusion", source: "buildings",
      layout: { visibility: "none" },
      paint: {
        "fill-extrusion-color": color,
        "fill-extrusion-height": ["coalesce", ["get", "height"], 3],
        "fill-extrusion-opacity": 0.85,
      },
    });
    this.map.addLayer({
      id: "b-line", type: "line", source: "buildings",
      paint: {
        "line-color": ["case", ["boolean", ["feature-state", "selected"], false], "#1d4ed8", ["get", "osm3d"], OSM3D_LINE, ["get", "needs_review"], "#b45309", "#374151"],
        "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 3.5, ["get", "osm3d"], 2.4, ["get", "needs_review"], 1.4, 0.6],
      },
    });
    for (const layer of ["b-fill", "b-extrusion"]) {
      this.map.on("click", layer, (e) => e.features[0] && this.onSelect(e.features[0].properties.key));
      this.map.on("mouseenter", layer, () => (this.map.getCanvas().style.cursor = "pointer"));
      this.map.on("mouseleave", layer, () => (this.map.getCanvas().style.cursor = ""));
    }
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

  setColorMode(mode) {
    this.mode = mode;
    const expr = COLOR_MODES[mode].expr;
    this.map.setPaintProperty("b-fill", "fill-color", expr);
    this.map.setPaintProperty("b-extrusion", "fill-extrusion-color", expr);
    this.renderLegend();
  }

  set3D(on) {
    this.map.setLayoutProperty("b-extrusion", "visibility", on ? "visible" : "none");
    this.map.setLayoutProperty("b-fill", "visibility", on ? "none" : "visible");
    this.map.easeTo({ pitch: on ? 55 : 0, duration: 600 });
  }

  select(key, bbox) {
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
