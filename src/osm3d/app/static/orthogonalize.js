// Esquadrar contornos, como o "Q" do editor iD do OSM (actionOrthogonalize).
//
// Coordenadas num plano em metros ([x, y]); anéis fechados sem repetir o primeiro ponto.
// Cada função devolve { coords, removed } (removed: índices dos vértices a tirar) ou
// { error }, com error = "not_squarish" (há cantos longe de 90°), "square_enough" (já
// está esquadrado) ou "too_few" (vértices a menos).

export const SQUARE_THRESHOLD_DEG = 13; // o do iD: cantos a menos disto de 90° ou de 180°
export const SQUARE_PART_DEG = 30; // canto ou aresta escolhidos à mão: mais tolerante
const EPSILON = 1e-4;
const LOWER = Math.cos(((90 - SQUARE_THRESHOLD_DEG) * Math.PI) / 180);
const UPPER = Math.cos((SQUARE_THRESHOLD_DEG * Math.PI) / 180);

const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1]];
const scale = (a, k) => [a[0] * k, a[1] * k];
const len = (a) => Math.hypot(a[0], a[1]);
const norm = (a) => {
  const l = len(a);
  return l ? [a[0] / l, a[1] / l] : [0, 0];
};
const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
const normDot = (a, b, o) => dot(norm(sub(a, o)), norm(sub(b, o)));
const angleDeg = (a, b, o) => (Math.acos(Math.max(-1, Math.min(1, normDot(a, b, o)))) * 180) / Math.PI;

function filterDot(dotp, allowStraight) {
  const v = Math.abs(dotp);
  if (v < EPSILON) return 0; // já ortogonal
  if (allowStraight && Math.abs(v - 1) < EPSILON) return 0; // reto: serve
  if (v < LOWER || v > UPPER) return dotp; // perto de 90° ou de 180°: ajusta-se
  return null; // nem uma coisa nem outra
}

function score(coords, closed) {
  let s = 0;
  const n = coords.length;
  for (let i = closed ? 0 : 1; i < (closed ? n : n - 1); i++) {
    const d = filterDot(normDot(coords[(i - 1 + n) % n], coords[(i + 1) % n], coords[i]), false);
    if (d === null) continue;
    s += 2 * Math.min(Math.abs(d - 1), Math.min(Math.abs(d), Math.abs(d + 1)));
  }
  return s;
}

function canSquare(coords, closed) {
  // null: não é esquadrável; 0: já está; 1: há o que fazer.
  const n = coords.length;
  let any = 0;
  for (let i = closed ? 0 : 1; i < (closed ? n : n - 1); i++) {
    const d = filterDot(normDot(coords[(i - 1 + n) % n], coords[(i + 1) % n], coords[i]), true);
    if (d === null) return null;
    if (Math.abs(d) > 0) any = 1;
  }
  return any;
}

function motion(points, i, closed) {
  const n = points.length;
  if (!closed && (i === 0 || i === n - 1)) return [0, 0];
  const o = points[i].coord;
  let p = sub(points[(i - 1 + n) % n].coord, o);
  let q = sub(points[(i + 1) % n].coord, o);
  const k = 2 * Math.min(len(p), len(q));
  p = norm(p);
  q = norm(q);
  const d = dot(p, q);
  if (Math.abs(d) < LOWER) return scale(norm(add(p, q)), 0.1 * d * k);
  return [0, 0];
}

function projectOnPolyline(c, line) {
  let best = null;
  for (let i = 0; i < line.length - 1; i++) {
    const a = line[i], b = line[i + 1];
    const ab = sub(b, a);
    const l2 = dot(ab, ab) || 1;
    const t = Math.max(0, Math.min(1, dot(sub(c, a), ab) / l2));
    const q = add(a, scale(ab, t));
    const d = len(sub(q, c));
    if (!best || d < best.d) best = { d, q };
  }
  return best?.q ?? c;
}

// Contorno inteiro. keep(i): vértice que não pode sair (ligado a outros elementos); os
// outros vértices em troços retos saem, os que ficam passam para cima do lado.
export function squareWay(coords, closed = true, keep = () => false) {
  if (coords.length < 3) return { error: "too_few" };
  const check = canSquare(coords, closed);
  if (check === null) return { error: "not_squarish" };
  if (check === 0) return { error: "square_enough" };
  const n = coords.length;
  const straights = [];
  const simplified = [];
  for (let i = 0; i < n; i++) {
    let d = 0;
    if (closed || (i > 0 && i < n - 1)) d = Math.abs(normDot(coords[(i - 1 + n) % n], coords[(i + 1) % n], coords[i]));
    (d > UPPER ? straights : simplified).push({ i, coord: [...coords[i]] });
  }
  if (simplified.length < 3) return { error: "too_few" };
  const clonePts = (pts) => pts.map((p) => ({ i: p.i, coord: [...p.coord] }));
  let best = clonePts(simplified);
  let bestScore = Infinity;
  for (let k = 0; k < 1000; k++) {
    const moves = simplified.map((_, j) => motion(simplified, j, closed));
    simplified.forEach((p, j) => (p.coord = add(p.coord, moves[j])));
    const s = score(simplified.map((p) => p.coord), closed);
    if (s < bestScore) {
      best = clonePts(simplified);
      bestScore = s;
    }
    if (bestScore < EPSILON) break;
  }
  const out = coords.map((c) => [...c]);
  for (const p of best) out[p.i] = p.coord;
  const line = best.map((p) => p.coord);
  if (closed) line.push(line[0]);
  const removed = [];
  for (const p of straights) {
    if (keep(p.i)) out[p.i] = projectOnPolyline(p.coord, line);
    else removed.push(p.i);
  }
  return { coords: out, removed };
}

// Um canto (o vértice i): passa para o ponto mais perto onde o ângulo é reto (o círculo
// com diâmetro entre os vizinhos, como no teorema de Tales). Os vizinhos não mexem.
export function squareCorner(coords, i, closed = true, maxDeg = SQUARE_PART_DEG) {
  const n = coords.length;
  if (n < 3 || (!closed && (i === 0 || i === n - 1))) return { error: "too_few" };
  const a = coords[(i - 1 + n) % n], o = coords[i], b = coords[(i + 1) % n];
  const off = Math.abs(angleDeg(a, b, o) - 90);
  if (off < 0.01) return { error: "square_enough" };
  if (off > maxDeg) return { error: "not_squarish" };
  const m = scale(add(a, b), 0.5);
  const r = len(sub(b, a)) / 2;
  const out = coords.map((c) => [...c]);
  out[i] = add(m, scale(norm(sub(o, m)), r));
  return { coords: out, removed: [] };
}

function intersect(p, u, q, v) {
  // Interseção das retas p + s·u e q + t·v (null se quase paralelas).
  const den = u[0] * v[1] - u[1] * v[0];
  if (Math.abs(den) < 1e-6) return null;
  const s = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / den;
  return add(p, scale(u, s));
}

// Uma aresta (i → i+1): fica perpendicular aos lados vizinhos. Os seus dois vértices
// deslizam ao longo desses lados (os outros cantos não mudam).
export function squareEdge(coords, i, closed = true, maxDeg = SQUARE_PART_DEG) {
  const n = coords.length;
  const j = (i + 1) % n;
  const prev = (i - 1 + n) % n, next = (j + 1) % n;
  if (n < 4 || (!closed && (i === 0 || j === n - 1))) return { error: "too_few" };
  const p = coords[prev], a = coords[i], b = coords[j], q = coords[next];
  const offA = Math.abs(angleDeg(p, b, a) - 90), offB = Math.abs(angleDeg(a, q, b) - 90);
  if (offA < 0.01 && offB < 0.01) return { error: "square_enough" };
  if (offA > maxDeg || offB > maxDeg) return { error: "not_squarish" };
  const u1 = norm(sub(a, p)), u2 = norm(sub(b, q));
  const u = norm(add(u1, u2)); // direção média dos lados vizinhos (a chegar à aresta)
  const d = [-u[1], u[0]]; // a aresta fica perpendicular a ela, pelo seu ponto médio
  const m = scale(add(a, b), 0.5);
  const a2 = intersect(p, u1, m, d), b2 = intersect(q, u2, m, d);
  if (!a2 || !b2) return { error: "not_squarish" };
  const out = coords.map((c) => [...c]);
  out[i] = a2;
  out[j] = b2;
  return { coords: out, removed: [] };
}
