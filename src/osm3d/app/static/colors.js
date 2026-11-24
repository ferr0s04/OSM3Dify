// Paletas partilhadas entre o mapa, a lista e a vista 3D.

export const SHAPE_COLORS = {
  flat: "#7c8aa0",
  skillion: "#8b5cf6",
  gabled: "#e4572e",
  hipped: "#f59e0b",
  side_hipped: "#d946ef",
  pyramidal: "#0ea5a4",
  round: "#16a34a",
  unknown: "#c4c9d1",
};

export const STATUS_COLORS = {
  pending: "#9aa3b0",
  accepted: "#16a34a",
  rejected: "#dc2626",
};

export const MATCH_COLORS = {
  true: "#16a34a",
  false: "#dc2626",
  none: "#c4c9d1",
};

export const SHAPE_LABELS = {
  flat: "plano",
  skillion: "uma água",
  gabled: "duas águas",
  hipped: "quatro águas",
  side_hipped: "três águas",
  pyramidal: "pirâmide",
  round: "abóbada (meia-lua)",
  unknown: "desconhecido",
};

// Relação com o OSM (ver src/osm3d/export/osm_changes.py).
export const CHANGE_COLORS = {
  novo: "#16a34a",
  complementa: "#2563eb",
  diverge: "#ea580c",
  igual: "#9aa3b0",
  "sem proposta": "#d4d7dd",
};

export const CHANGE_LABELS = {
  novo: "novo (sem 3D no OSM)",
  complementa: "complementa o 3D do OSM",
  diverge: "diverge do OSM",
  igual: "igual ao OSM",
  "sem proposta": "sem proposta",
};

// Origem do edifício: do OSM, ou da cartografia municipal (em falta no OSM).
export const ORIGIN_COLORS = {
  municipal: "#0891b2",
  osm: "#c4c9d1",
};

export const ORIGIN_LABELS = {
  municipal: "novo: cartografia municipal",
  osm: "do OSM",
};

// Classes ASPRS
export const CLASS_COLORS = {
  2: [150, 120, 90],
  3: [120, 190, 90],
  4: [70, 160, 70],
  5: [30, 110, 50],
  6: [235, 90, 60],
};
