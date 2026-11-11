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
  saveReview: (key, status, note, overrides) =>
    request("PUT", `${path(key)}/review`, { status, note, overrides }),
  resetReview: (key) => request("DELETE", `${path(key)}/review`),
  export: () => request("POST", "/api/export"),
  archive: (status, archived = true) => request("POST", "/api/reviews/archive", { status, archived }),
};
