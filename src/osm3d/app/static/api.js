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
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
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
  export: () => request("POST", "/api/export"),
  archive: (status, archived = true) => request("POST", "/api/reviews/archive", { status, archived }),
  scene: () => request("GET", "/api/scene"),
  sceneOne: (key) => request("GET", `${path(key)}/scene`),
  parts: (key) => request("GET", `${path(key)}/parts`),
  partsStart: (key) => request("POST", `${path(key)}/parts/start`),
  partsPreview: (key, layout, overrides) => request("POST", `${path(key)}/parts/preview`, { layout, overrides }),
  partsSplit: (key, layout, part, a, b) => request("POST", `${path(key)}/parts/split`, { layout, part, a, b }),
  partsMerge: (key, layout, part) => request("POST", `${path(key)}/parts/merge`, { layout, part }),
};
