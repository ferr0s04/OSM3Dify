// Cliente da API da aplicação de revisão.

async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { msg = (await res.json()).detail ?? msg; } catch { /* sem corpo JSON */ }
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const path = (key) => `/api/buildings/${key}`; // key = "way/123"

export const api = {
  summary: () => request("GET", "/api/summary"),
  buildings: () => request("GET", "/api/buildings"),
  geojson: () => request("GET", "/api/buildings.geojson"),
  detail: (key) => request("GET", path(key)),
  mesh: (key) => request("GET", `${path(key)}/mesh`),
  points: (key) => request("GET", `${path(key)}/points`),
  preview: (key, overrides) => request("POST", `${path(key)}/preview`, overrides),
  saveReview: (key, status, note, overrides, parts = null) =>
    request("PUT", `${path(key)}/review`, { status, note, overrides, parts }),
  resetReview: (key) => request("DELETE", `${path(key)}/review`),
  // Contorno editado no mapa: [[id do nó do OSM ou null, lon, lat], ...] → detalhe.
  buildingGeometry: (key) => request("GET", `${path(key)}/geometry`),
  saveBuildingGeometry: (key, nodes) => request("PUT", `${path(key)}/geometry`, { nodes }),
  resetBuildingGeometry: (key) => request("DELETE", `${path(key)}/geometry`),
  export: () => request("POST", "/api/export"),
  osmContext: () => request("GET", "/api/osm-context"),
  walls: () => request("GET", "/api/walls"),
  fetchWalls: () => request("POST", "/api/walls/fetch"),
  wall: (key) => request("GET", `/api/${key.replace("wall/", "walls/")}`),
  saveWallReview: (key, status, note) => request("PUT", `/api/${key.replace("wall/", "walls/")}/review`, { status, note }),
  resetWallReview: (key) => request("DELETE", `/api/${key.replace("wall/", "walls/")}/review`),
  saveWallsGeometry: (walls) => request("PUT", "/api/walls/geometry", { walls }),
  saveWallGeometry: (key, coords) => request("PUT", `/api/${key.replace("wall/", "walls/")}/geometry`, { coords }),
  resetWallGeometry: (key) => request("DELETE", `/api/${key.replace("wall/", "walls/")}/geometry`),
  landuse: (id) => request("GET", `/api/landuse/${id}`),
  saveLanduse: (id, nodes) => request("PUT", `/api/landuse/${id}/geometry`, { nodes }),
  resetLanduse: (id) => request("DELETE", `/api/landuse/${id}/geometry`),
  refreshOsmContext: () => request("POST", "/api/osm-context/refresh"),
  settings: () => request("GET", "/api/settings"),
  saveSettings: (settings) => request("PUT", "/api/settings", settings),
  archive: (status, archived = true) => request("POST", "/api/reviews/archive", { status, archived }),
  scene: () => request("GET", "/api/scene"),
  sceneOne: (key) => request("GET", `${path(key)}/scene`),
  parts: (key) => request("GET", `${path(key)}/parts`),
  partsStart: (key) => request("POST", `${path(key)}/parts/start`),
  partsPreview: (key, layout, overrides) => request("POST", `${path(key)}/parts/preview`, { layout, overrides }),
  partsSplit: (key, layout, part, a, b) => request("POST", `${path(key)}/parts/split`, { layout, part, a, b }),
  partsMerge: (key, layout, part) => request("POST", `${path(key)}/parts/merge`, { layout, part }),
  // Dados raw (quadrículas DGT).
  dataStatus: () => request("GET", "/api/data/status"),
  dataTiles: (bbox) => request("GET", `/api/data/tiles?bbox=${bbox.map((v) => v.toFixed(5)).join(",")}`),
  dataTilesByName: (names) => request("GET", `/api/data/tiles?names=${names.join(",")}`),
  downloads: () => request("GET", "/api/data/downloads"),
  startDownloads: (tiles, products) => request("POST", "/api/data/downloads", { tiles, products }),
  cancelDownloads: () => request("DELETE", "/api/data/downloads"),
  deleteRaw: (tiles, products, includeCache = true) =>
    request("POST", "/api/data/delete", { tiles, products, include_cache: includeCache }),
  // Áreas de estudo e processamento.
  areas: () => request("GET", "/api/areas"),
  createArea: (area) => request("POST", "/api/areas", area),
  updateArea: (name, area) => request("PUT", `/api/areas/${encodeURIComponent(name)}`, area),
  activateArea: (name) => request("POST", `/api/areas/${encodeURIComponent(name)}/activate`),
  deleteArea: (name) => request("DELETE", `/api/areas/${encodeURIComponent(name)}`),
  osmRelation: (id) => request("GET", `/api/osm/relation/${id}`),
  municipalDownload: () => request("GET", "/api/municipal/download"),
  startMunicipalDownload: (source = null) =>
    request("POST", `/api/municipal/download${source ? `?source=${encodeURIComponent(source)}` : ""}`),
  cancelMunicipalDownload: () => request("DELETE", "/api/municipal/download"),
  processing: () => request("GET", "/api/processing"),
  startProcessing: (opts) => request("POST", "/api/processing", opts),
  cancelProcessing: () => request("DELETE", "/api/processing"),
};
