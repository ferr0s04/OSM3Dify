"""Edifícios em falta no OSM, detetados no LiDAR (pontos de classe 6 sem contorno OSM).

1. **Máscara:** raster 0,5 m dos pontos de edifício (classe 6), menos os contornos
   OSM de toda a quadrícula com uma folga de 1,5 m (beirais e desvio de co-registo).
   A folga vale também para edifícios da freguesia vizinha.
2. **Manchas:** fecho morfológico (falhas entre linhas de varrimento) e abertura
   (faixas finas junto a edifícios OSM). Ficam as componentes com ≥ ``MIN_AREA_M2``.
3. **Contorno:** polígono da mancha, simplificado e **esquadrado** (lados a < 15° de
   90° da orientação dominante ficam ortogonais, como o "Q" do iD). O LiDAR vê o
   telhado, por isso o contorno inclui os beirais.
4. **Filtros:**
   - altura mínima acima do terreno (muros, carros e sebes mal classificados);
   - densidade de pontos;
   - forma não demasiado irregular;
   - centróide dentro da área de estudo.

Os candidatos seguem depois o mesmo ajuste de telhado dos edifícios OSM e entram na
revisão como ``new/<id>``. Só os aceites são exportados, como ways novas.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from rasterio.features import rasterize, shapes
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import LineString, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

from osm3d.domain.models import Building, QAFlag
from osm3d.geospatial.point_clouds import read_points
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation
from osm3d.geospatial.tiling import TileKey

log = logging.getLogger(__name__)

RES_M = 0.5
OSM_CLEARANCE_M = 1.5
MIN_AREA_M2 = 20.0
MIN_HEIGHT_M = 2.2
MIN_POINT_DENSITY = 4.0  # pts/m² de classe 6 no contorno
MIN_SOLIDITY = 0.6  # área / área do invólucro convexo (rejeita formas esgarçadas)
TILE_MARGIN_M = 30.0
SQUARE_THRESHOLD_DEG = 15.0


# ----------------------------------------------------------------- contexto OSM


def _polygon_from_members(members: list[dict]) -> BaseGeometry | None:  # type: ignore[type-arg]
    outer = [
        LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
        for m in members
        if m.get("type") == "way" and m.get("geometry") and m.get("role", "outer") != "inner"
    ]
    inner = [
        LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
        for m in members
        if m.get("type") == "way" and m.get("geometry") and m.get("role") == "inner"
    ]
    polys = list(polygonize(unary_union(outer))) if outer else []
    if not polys:
        return None
    geom = unary_union(polys)
    if inner:
        geom = geom.difference(unary_union(list(polygonize(unary_union(inner)))))
    return geom


def load_context(
    path: Path, crs: str, relation_id: int | None
) -> tuple[list[BaseGeometry], BaseGeometry | None]:
    """(contornos OSM de toda a área das quadrículas, limite da área de estudo)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def project(g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda c: np.column_stack(to_crs.transform(c[:, 0], c[:, 1])))

    buildings: list[BaseGeometry] = []
    boundary: BaseGeometry | None = None
    for el in data["elements"]:
        if (
            el["type"] == "way"
            and "building" in el.get("tags", {})
            and len(el.get("geometry", [])) >= 4
        ):
            coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if coords[0] == coords[-1]:
                buildings.append(project(Polygon(coords)))
        elif el["type"] == "relation":
            geom = _polygon_from_members(el.get("members", []))
            if geom is None:
                continue
            if relation_id is not None and el["id"] == relation_id:
                boundary = project(geom)
            elif "building" in el.get("tags", {}):
                buildings.append(project(geom))
    return [shapely.make_valid(b) for b in buildings], boundary


# --------------------------------------------------------------- regularização


def candidate_id(x: float, y: float) -> int:
    """Id estável (independente da ordem de deteção): posição do centróide, a 1 m."""
    return (int(round(x)) + 500_000) * 1_000_000 + int(round(y))


def square_polygon(poly: Polygon, threshold_deg: float = SQUARE_THRESHOLD_DEG) -> Polygon:
    """Esquadra um polígono: lados quase ortogonais à orientação dominante ficam a 90°."""
    rect = poly.minimum_rotated_rectangle
    rc = np.asarray(rect.exterior.coords)[:4]
    sides = [rc[(i + 1) % 4] - rc[i] for i in range(4)]
    longest = max(sides, key=lambda d: float(np.hypot(*d)))
    dom = math.atan2(longest[1], longest[0])
    thr = math.radians(threshold_deg)
    pts = np.asarray(orient(poly, 1.0).exterior.coords)[:-1]
    for _ in range(3):
        n = len(pts)
        if n < 4:
            break
        dirs, mids, snapped = [], [], []
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            th = math.atan2(b[1] - a[1], b[0] - a[0])
            k = round((th - dom) / (math.pi / 2))
            th_s = dom + k * math.pi / 2
            ok = abs(th - th_s) <= thr
            dirs.append(
                np.array([math.cos(th_s), math.sin(th_s)])
                if ok
                else (b - a) / (np.hypot(*(b - a)) or 1)
            )
            mids.append((a + b) / 2)
            snapped.append(k % 2 if ok else None)
        # Vértices entre dois lados com a mesma direção esquadrada são redundantes.
        keep = [
            i
            for i in range(n)
            if not (snapped[i] is not None and snapped[(i - 1) % n] == snapped[i])
        ]
        if len(keep) < n and len(keep) >= 4:
            pts = pts[keep]
            continue
        new = []
        for i in range(n):
            p1, d1 = mids[(i - 1) % n], dirs[(i - 1) % n]
            p2, d2 = mids[i], dirs[i]
            den = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(den) < 0.05:
                new.append(pts[i])
            else:
                t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
                new.append(p1 + t * d1)
        pts = np.asarray(new)
    out = Polygon(pts)
    return out if out.is_valid and out.area > 0 else poly


def _drop_small_steps(poly: Polygon, max_area_change_m2: float) -> Polygon:
    """Remove vértices cuja remoção quase não muda a área (degraus do raster)."""
    pts = list(np.asarray(poly.exterior.coords)[:-1])
    changed = True
    while changed and len(pts) > 4:
        changed = False
        best_i, best_d = -1, max_area_change_m2
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            tri = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2
            if tri < best_d:
                best_i, best_d = i, tri
        if best_i >= 0:
            trial = Polygon(pts[:best_i] + pts[best_i + 1 :])
            if trial.is_valid:
                pts.pop(best_i)
                changed = True
    return Polygon(pts)


def regularize(poly: Polygon) -> Polygon:
    """Contorno limpo a partir da mancha raster.

    - Quase retangular (≥ 75 % do retângulo mínimo): o retângulo. Beirais irregulares,
      árvores encostadas e falhas de pontos estragam a mancha, mas não a forma.
    - Restantes (L, T…): simplificação a 1 m, remoção de degraus do raster e
      esquadria, com proteção contra mudanças grandes de área.
    """
    clean = Polygon(poly.exterior).buffer(0)
    if not isinstance(clean, Polygon) or clean.is_empty:
        return orient(poly.minimum_rotated_rectangle, 1.0)
    rect = clean.minimum_rotated_rectangle
    if clean.area / rect.area >= 0.75:
        return orient(rect, 1.0)
    simple = clean.simplify(1.0, preserve_topology=True)
    if not isinstance(simple, Polygon) or simple.is_empty:
        simple = clean
    simple = _drop_small_steps(simple, max_area_change_m2=0.02 * clean.area + 1.0)
    squared = square_polygon(simple, threshold_deg=20.0)
    squared = _drop_small_steps(squared, max_area_change_m2=0.5)
    if not squared.is_valid or abs(squared.area - clean.area) > 0.2 * clean.area:
        return orient(rect, 1.0) if clean.area / rect.area >= 0.6 else orient(simple, 1.0)
    return orient(squared, 1.0)


# ------------------------------------------------------------------- deteção


def detect_candidates(
    lidar_tiles: dict[str, Path],
    dtm_tiles: dict[str, Path],
    osm_buildings: list[BaseGeometry],
    boundary: BaseGeometry | None,
) -> list[Building]:
    available = sorted(set(lidar_tiles) & set(dtm_tiles))
    osm_index = shapely.STRtree(osm_buildings) if osm_buildings else None
    found: dict[int, Building] = {}
    with DtmMosaic(dtm_tiles[t] for t in available) as dtm:
        for tile in available:
            x0, y0, x1, y1 = TileKey.from_dgt_name(tile).bounds
            m = TILE_MARGIN_M
            bx0, by0, bx1, by1 = x0 - m, y0 - m, x1 + m, y1 + m
            pts = read_points((lidar_tiles[t] for t in available), (bx0, by0, bx1, by1), [6])
            if not len(pts):
                continue
            ncol, nrow = int((bx1 - bx0) / RES_M), int((by1 - by0) / RES_M)
            col = ((pts.xyz[:, 0] - bx0) / RES_M).astype(int).clip(0, ncol - 1)
            row = ((by1 - pts.xyz[:, 1]) / RES_M).astype(int).clip(0, nrow - 1)
            occ = np.zeros((nrow, ncol), bool)
            occ[row, col] = True
            occ = ndimage.binary_closing(occ, iterations=2)
            transform = from_origin(bx0, by1, RES_M, RES_M)
            near = (
                [osm_buildings[i] for i in osm_index.query(shapely.box(bx0, by0, bx1, by1))]
                if osm_index
                else []
            )
            if near:
                covered = rasterize(
                    [(g.buffer(OSM_CLEARANCE_M), 1) for g in near],
                    out_shape=occ.shape,
                    transform=transform,
                ).astype(bool)
                occ &= ~covered
            occ = ndimage.binary_opening(occ, iterations=2)
            labels, k = ndimage.label(occ)
            if not k:
                continue
            sizes = ndimage.sum(occ, labels, range(1, k + 1)) * RES_M**2
            for lab in np.flatnonzero(sizes >= MIN_AREA_M2) + 1:
                polys = [
                    shape(g)
                    for g, v in shapes(
                        (labels == lab).astype(np.uint8), mask=labels == lab, transform=transform
                    )
                    if v == 1
                ]
                if not polys:
                    continue
                blob = max(polys, key=lambda p: p.area)
                c = blob.centroid
                # Cada candidato é tratado na quadrícula do seu centróide (sem duplicados).
                if not (x0 <= c.x < x1 and y0 <= c.y < y1):
                    continue
                if boundary is not None and not boundary.contains(c):
                    continue
                cand = _evaluate(blob, pts, dtm)
                if cand is not None:
                    found[cand.osm_id] = cand
    log.info("%d edifícios em falta detetados", len(found))
    return list(found.values())


def _evaluate(blob: Polygon, pts, dtm: DtmMosaic) -> Building | None:  # type: ignore[no-untyped-def]
    footprint = regularize(Polygon(blob.exterior))
    if footprint.area < MIN_AREA_M2:
        return None
    if blob.area / blob.convex_hull.area < MIN_SOLIDITY:
        return None
    inside = shapely.contains_xy(footprint, pts.xyz[:, 0], pts.xyz[:, 1])
    density = int(inside.sum()) / footprint.area
    if density < MIN_POINT_DENSITY:
        return None
    ground = sample_ground_elevation(dtm, footprint, 2.0, 5)
    if ground is None:
        return None
    top = float(np.percentile(pts.xyz[inside, 2], 90))
    if top - ground < MIN_HEIGHT_M:
        return None
    c = footprint.centroid
    return Building(
        osm_type="new",
        osm_id=candidate_id(c.x, c.y),
        osm_version=0,
        footprint=footprint if not isinstance(footprint, MultiPolygon) else footprint.geoms[0],
        existing_tags={"building": "yes"},
        qa_flags=[
            QAFlag(
                code="missing_in_osm",
                message=f"Edifício no LiDAR sem contorno no OSM ({footprint.area:.0f} m², "
                f"~{top - ground:.1f} m de altura). Contorno derivado do LiDAR (inclui beirais).",
                severity="info",
            )
        ],
    )
