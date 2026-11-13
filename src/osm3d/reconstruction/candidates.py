"""Edifícios em falta no OSM, detetados no LiDAR (pontos de classe 6 sem contorno OSM).

Com ortofoto (caminho principal, ver :mod:`osm3d.reconstruction.ortho_outline`):

1. **Desvio ortofoto ↔ LiDAR** da quadrícula (correlação de arestas). O OSM foi
   desenhado sobre imagem, por isso o ortofoto é o referencial dos candidatos.
2. **Sementes:** células de 0,5 m com pontos de classe 6 a > 1,8 m do terreno, menos
   os contornos OSM (folga de 1 m), com fecho e abertura ligeiros.
3. **Divisão:** cada semente é partida em superfícies de telhado contínuas; um degrau
   de altura separa edifícios contíguos (casas e anexos de uma quinta, p.ex.).
4. **Contorno:** de cada superfície, esquadrado na orientação das arestas do
   ortofoto e com cada lado ajustado à aresta do ortofoto / queda de altura.
5. **Filtros:** área, densidade de pontos, altura acima do terreno, centróide dentro
   da quadrícula e da área de estudo.

Sem ortofoto a cobrir a quadrícula, o contorno é o da mancha LiDAR (inclui beirais),
regularizado como antes.

Os candidatos seguem depois o mesmo ajuste de telhado dos edifícios OSM e entram na
revisão como ``new/<id>``. Só os aceites são exportados, como ways novas.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely
from affine import Affine
from pyproj import Transformer
from rasterio.features import rasterize, shapes
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

from osm3d.acquisition.municipal import MunicipalBuilding
from osm3d.config import MunicipalSourceConfig
from osm3d.domain.models import Building, QAFlag
from osm3d.geospatial.point_clouds import PointSet, read_points
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation
from osm3d.geospatial.tiling import TileKey
from osm3d.parts import layout_from_polygons, validate_layout
from osm3d.reconstruction.ortho_outline import (
    OrthoMosaic,
    Patch,
    buildings_from_seed,
    coverage,
    estimate_ortho_offset,
    group_lean_tos,
    height_grid,
    make_patch,
    vegetation_fraction,
)

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


# landuse onde há edifícios; os restantes (agrícola, mato, relva…) não os têm, em regra.
BUILT_LANDUSE = {
    "residential",
    "industrial",
    "commercial",
    "retail",
    "farmyard",
    "construction",
    "religious",
    "institutional",
    "education",
    "garages",
    "military",
}
# Meia largura da faixa de rodagem (m), conservadora: o eixo da via no OSM pode estar
# ~1 m ao lado. Vias pedonais e caminhos não recortam (passeios encostam a paredes).
ROAD_HALF_WIDTH_M = {
    "motorway": 5.0,
    "trunk": 4.5,
    "primary": 3.5,
    "secondary": 3.0,
    "tertiary": 2.5,
    "unclassified": 2.0,
    "residential": 2.0,
    "living_street": 1.75,
    "service": 1.25,
}
MAX_LANDUSE_CLIP_FRACTION = 0.15
CLIP_MAX_ROOF_COVER = 0.4  # acima disto o LiDAR vê telhado: não se recorta


@dataclass
class LandContext:
    """``landuse`` e estradas do OSM (referencial do OSM/ortofoto), já projetados."""

    built: BaseGeometry | None = None  # união dos landuse construídos
    other: BaseGeometry | None = None  # união dos restantes landuse
    roads: BaseGeometry | None = None  # faixas de rodagem (eixo + meia largura)


def load_land(path: Path, crs: str) -> LandContext:
    """``landuse`` e estradas do contexto OSM (vazio em caches sem estes dados)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def project(g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda c: np.column_stack(to_crs.transform(c[:, 0], c[:, 1])))

    built: list[BaseGeometry] = []
    other: list[BaseGeometry] = []
    roads: list[BaseGeometry] = []
    for el in data["elements"]:
        tags = el.get("tags", {})
        geom: BaseGeometry | None = None
        if "landuse" in tags:
            if el["type"] == "way" and len(el.get("geometry", [])) >= 4:
                coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
                if coords[0] == coords[-1]:
                    geom = Polygon(coords)
            elif el["type"] == "relation":
                geom = _polygon_from_members(el.get("members", []))
            if geom is not None:
                geom = shapely.make_valid(project(geom))
                (built if tags["landuse"] in BUILT_LANDUSE else other).append(geom)
        elif el["type"] == "way" and tags.get("highway") in ROAD_HALF_WIDTH_M:
            if tags.get("tunnel") not in (None, "no") or tags.get("area") == "yes":
                continue
            pts = el.get("geometry", [])
            if len(pts) < 2:
                continue
            half = ROAD_HALF_WIDTH_M[tags["highway"]]
            with contextlib.suppress(ValueError, IndexError):  # largura medida no OSM
                half = max(half, 0.4 * float(tags.get("width", "").split()[0]))
            line = project(LineString([(p["lon"], p["lat"]) for p in pts]))
            roads.append(line.buffer(half, cap_style="flat"))
    return LandContext(
        shapely.union_all(built) if built else None,
        shapely.union_all(other) if other else None,
        shapely.union_all(roads) if roads else None,
    )


def fit_to_land(
    footprint: Polygon,
    land: LandContext,
    roof_cover: Callable[[BaseGeometry], float | None] | None = None,
) -> tuple[Polygon | None, list[QAFlag]]:
    """Encaixa o contorno no ``landuse`` e nas estradas do OSM.

    - não sobre a faixa de rodagem (recorta-se);
    - um pequeno excesso (< 15 % da área) de um ``landuse`` construído para um não
      construído (agrícola, mato…) é recortado pelo limite;
    - todo fora de ``landuse`` construído: só um aviso (há armazéns agrícolas).

    Com ``roof_cover`` (fração da área com pontos de edifício no LiDAR), só se recorta
    o que o LiDAR não vê como telhado: estradas e landuse do OSM são desenhados à
    mão e o eixo da via pode passar a 1–2 m do sítio; o LiDAR manda.
    """
    flags: list[QAFlag] = []
    fp: BaseGeometry = footprint

    def clip(excess: BaseGeometry) -> None:
        nonlocal fp
        if excess.is_empty:
            return
        cover = roof_cover(excess) if roof_cover is not None else None
        if cover is None or cover < CLIP_MAX_ROOF_COVER:
            fp = fp.difference(excess)

    if land.roads is not None and fp.intersects(land.roads):
        clip(fp.intersection(land.roads))
    if land.built is not None and land.other is not None and fp.intersects(land.built):
        excess = fp.difference(land.built).intersection(land.other)
        if 0 < excess.area <= MAX_LANDUSE_CLIP_FRACTION * fp.area:
            clip(excess)
    polys = [g for g in getattr(fp, "geoms", [fp]) if isinstance(g, Polygon) and not g.is_empty]
    if not polys:
        return None, flags
    out = max(polys, key=lambda p: p.area)
    # Restos finos do recorte (< 1 m).
    opened = out.buffer(-0.5, join_style="mitre").buffer(0.5, join_style="mitre")
    if isinstance(opened, Polygon) and opened.area >= 0.9 * out.area:
        out = opened
    if land.built is not None and not out.intersects(land.built):
        flags.append(
            QAFlag(
                code="outside_built_landuse",
                message="Fora de landuse residencial/industrial/agrícola-construído no OSM: "
                "confirmar que é edifício (ou que falta o landuse).",
                severity="info",
            )
        )
    return orient(out, 1.0), flags


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
    ortho_paths: list[Path] | None = None,
    progress: Callable[[str], None] | None = None,
    land: LandContext | None = None,
    municipal: list[MunicipalBuilding] | None = None,
    municipal_cfg: MunicipalSourceConfig | None = None,
) -> list[Building]:
    """Edifícios em falta no OSM, quadrícula a quadrícula.

    Com ortofoto a cobrir a quadrícula, o contorno vem do ortofoto e as manchas LiDAR
    são divididas em edifícios contíguos (:mod:`osm3d.reconstruction.ortho_outline`).
    Sem ortofoto, fica o contorno derivado só do LiDAR. Com ``land`` (landuse e
    estradas do OSM), os contornos do ortofoto são encaixados neles. Com
    ``municipal`` (cartografia municipal), ver :func:`merge_municipal`.
    """
    available = sorted(set(lidar_tiles) & set(dtm_tiles))
    osm_index = shapely.STRtree(osm_buildings) if osm_buildings else None
    municipal_cfg = municipal_cfg or MunicipalSourceConfig()
    muni_index = shapely.STRtree([m.geometry for m in municipal or []])
    found: dict[int, Building] = {}
    ortho = OrthoMosaic(ortho_paths) if ortho_paths else None
    try:
        with DtmMosaic(dtm_tiles[t] for t in available) as dtm:
            for tile in available:
                x0, y0, x1, y1 = TileKey.from_dgt_name(tile).bounds
                m = TILE_MARGIN_M
                bounds = (x0 - m, y0 - m, x1 + m, y1 + m)
                pts = read_points((lidar_tiles[t] for t in available), bounds, [6])
                if not len(pts):
                    continue
                near = (
                    [osm_buildings[i] for i in osm_index.query(shapely.box(*bounds))]
                    if osm_index
                    else []
                )
                ctx = _TileContext(tile, (x0, y0, x1, y1), bounds, pts, dtm, near, boundary, land)
                offset = (0.0, 0.0)
                if ortho is not None and ortho.covers((x0, y0, x1, y1)):
                    new, offset = _detect_tile_ortho(ctx, ortho)
                    how = "ortofoto"
                else:
                    new = _detect_tile_lidar(ctx)
                    how = "só LiDAR (sem ortofoto)"
                note = ""
                if municipal is not None:
                    near_m = [municipal[i] for i in muni_index.query(shapely.box(*bounds))]
                    new, stats = merge_municipal(ctx, new, near_m, offset, municipal_cfg)
                    note = (
                        f"; cartografia municipal: {stats.from_municipal} contornos municipais"
                        f" confirmados pelo LiDAR, {stats.without_lidar} sem LiDAR (demolidos"
                        f" ou sob árvores), {stats.replaced} candidatos LiDAR substituídos,"
                        f" {stats.not_in_municipal} só no LiDAR"
                    )
                for b in new:
                    found[b.osm_id] = b
                if progress:
                    progress(f"  quadrícula {tile}: {len(new)} candidatos ({how}{note})")
    finally:
        if ortho is not None:
            ortho.close()
    log.info("%d edifícios em falta detetados", len(found))
    return list(found.values())


@dataclass
class _TileContext:
    tile: str
    core: tuple[float, float, float, float]  # a quadrícula (dono do candidato)
    bounds: tuple[float, float, float, float]  # com margem
    pts: PointSet  # classe 6, no referencial do LiDAR
    dtm: DtmMosaic
    osm: list[BaseGeometry]  # contornos OSM perto (referencial do OSM/ortofoto)
    boundary: BaseGeometry | None
    land: LandContext | None = None

    def owns(self, c: Point) -> bool:
        """Cada candidato é tratado na quadrícula do seu centróide (sem duplicados)."""
        x0, y0, x1, y1 = self.core
        inside = x0 <= c.x < x1 and y0 <= c.y < y1
        return inside and (self.boundary is None or self.boundary.contains(c))


def _blobs(occ: np.ndarray, transform: Affine, res: float, min_area: float) -> list[Polygon]:
    """Manchas (componentes ligadas) de ``occ`` como polígonos, por recorte."""
    labels, k = ndimage.label(occ)
    if not k:
        return []
    sizes = ndimage.sum(occ, labels, range(1, k + 1)) * res**2
    out = []
    for lab, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None or sizes[lab - 1] < min_area:
            continue
        sub = (labels[sl] == lab).astype(np.uint8)
        t = transform * Affine.translation(sl[1].start, sl[0].start)
        polys = [shape(g) for g, v in shapes(sub, mask=sub == 1, transform=t) if v == 1]
        if polys:
            out.append(max(polys, key=lambda p: p.area))
    return out


# ------------------------------------------------- contorno do ortofoto (novo)

# Folga às paredes OSM: só o desvio residual (o LiDAR já é deslocado para o OSM).
ORTHO_OSM_CLEARANCE_M = 1.0
SEED_MIN_HEIGHT_M = 1.8
MIN_WIDTH_M = 2.5  # mais estreito: muros, sebes, pérgolas
MAX_VEGETATION_FRACTION = 0.35  # mais: sebe/árvore classificada como edifício
MAX_OBLIQUE_FRACTION = 0.5  # fração do perímetro fora de esquadria
MIN_PART_AREA_M2 = 8.0  # parte (alpendre) mínima
SMALL_IRREGULAR_M2 = 60.0  # abaixo disto, irregular = não é edifício


def _detect_tile_ortho(
    ctx: _TileContext, ortho: OrthoMosaic
) -> tuple[list[Building], tuple[float, float]]:
    """Candidatos da quadrícula e o desvio ortofoto → LiDAR usado."""
    grid = height_grid(ctx.pts, ctx.dtm, ctx.bounds)
    offset = estimate_ortho_offset(grid, ortho, ctx.core)
    if offset is None:
        offset = (0.0, 0.0)
    log.info("quadrícula %s: desvio ortofoto → LiDAR (%+.2f, %+.2f) m", ctx.tile, *offset)
    occ = grid.occ & (np.nan_to_num(grid.h) > SEED_MIN_HEIGHT_M)
    occ = ndimage.binary_closing(occ, iterations=1)
    transform = from_origin(grid.x0, grid.y1, grid.res, grid.res)
    if ctx.osm:
        # OSM está no referencial do ortofoto; a grelha, no do LiDAR.
        dx, dy = offset
        covered = rasterize(
            [
                (
                    shapely.transform(g, lambda c: c + np.array([dx, dy])).buffer(
                        ORTHO_OSM_CLEARANCE_M
                    ),
                    1,
                )
                for g in ctx.osm
            ],
            out_shape=occ.shape,
            transform=transform,
        ).astype(bool)
        occ &= ~covered
    occ = ndimage.binary_opening(occ, iterations=1)
    shift_back = np.array([-offset[0], -offset[1]])
    out: list[Building] = []
    for blob in _blobs(occ, transform, grid.res, MIN_AREA_M2):
        seed = shapely.transform(blob, lambda c: c + shift_back)
        if not ctx.owns(seed.centroid):
            continue
        patch = make_patch(seed, ortho, grid, offset)
        if patch is None:
            continue
        pieces = buildings_from_seed(seed, patch)
        for group in group_lean_tos(patch, pieces):
            if len(group) == 1:
                cand = _candidate(group[0], ctx, offset, len(pieces), patch)
                out += [cand] if cand is not None else []
            else:
                out += _grouped_candidates(group, ctx, offset, len(pieces), patch)
    return out, offset


def _grouped_candidates(
    group: list[Polygon],
    ctx: _TileContext,
    offset: tuple[float, float],
    n_split: int,
    patch: Patch,
) -> list[Building]:
    """Volume principal + alpendre(s): um edifício com ``building:part``.

    Se o contorno conjunto não for um polígono simples ou a divisão não for válida,
    cada peça fica um edifício contíguo, como as outras.
    """

    def separately() -> list[Building]:
        cands = [_candidate(p, ctx, offset, n_split, patch) for p in group]
        return [c for c in cands if c is not None]

    union = shapely.union_all([p.buffer(0.01, join_style="mitre") for p in group]).buffer(
        -0.01, join_style="mitre"
    )
    if not isinstance(union, Polygon) or union.interiors:
        return separately()
    cand = _candidate(orient(union, 1.0), ctx, offset, n_split, patch)
    if cand is None:
        return separately()
    parts = [g for p in group if (g := _largest_polygon(p.intersection(cand.footprint)))]
    parts = [p for p in parts if p.area >= MIN_PART_AREA_M2]
    if len(parts) < 2:
        return [cand]
    layout = layout_from_polygons(parts, source="suggested")
    if validate_layout(layout, cand.footprint):
        return separately()
    flag = QAFlag(
        code="lean_to_parts",
        message=f"Edifício com {len(parts) - 1} alpendre(s) encostado(s): dividido em "
        f"{len(parts)} building:part (volume principal + alpendres de uma água).",
        severity="info",
    )
    return [cand.model_copy(update={"layout": layout, "qa_flags": [*cand.qa_flags, flag]})]


def _largest_polygon(g: BaseGeometry) -> Polygon | None:
    polys = [p for p in getattr(g, "geoms", [g]) if isinstance(p, Polygon) and not p.is_empty]
    return max(polys, key=lambda p: p.area) if polys else None


def oblique_fraction(poly: Polygon, tol_deg: float = 12.0) -> float:
    """Fração do perímetro com lados fora de esquadria (> ``tol_deg`` de 0°/90°)."""
    c = np.asarray(poly.exterior.coords)
    v = np.diff(c, axis=0)
    length = np.hypot(v[:, 0], v[:, 1])
    if not length.sum():
        return 0.0
    th = np.arctan2(v[:, 1], v[:, 0])
    dom = float(np.angle(np.sum(length * np.exp(4j * th)))) / 4  # orientação módulo 90°
    off = np.abs((th - dom + np.pi / 4) % (np.pi / 2) - np.pi / 4)
    return float(length[off > math.radians(tol_deg)].sum() / length.sum())


def _width(poly: Polygon) -> float:
    """Largura: lado menor do retângulo mínimo orientado."""
    c = np.asarray(poly.minimum_rotated_rectangle.exterior.coords)
    sides = np.hypot(*np.diff(c[:3], axis=0).T)
    return float(sides.min())


def _candidate(
    footprint: Polygon,
    ctx: _TileContext,
    offset: tuple[float, float],
    n_split: int,
    patch: Patch | None = None,
) -> Building | None:
    """Encaixe no landuse/estradas, filtros (área, pontos, altura) e o ``Building``."""
    land_flags: list[QAFlag] = []
    if ctx.land is not None:
        cover = (lambda g: coverage(patch, g)) if patch is not None else None
        fitted, land_flags = fit_to_land(footprint, ctx.land, cover)
        if fitted is None:
            return None
        footprint = fitted
    if footprint.area < MIN_AREA_M2 or _width(footprint) < MIN_WIDTH_M:
        return None
    if patch is not None and vegetation_fraction(patch, footprint) > MAX_VEGETATION_FRACTION:
        return None
    oblique = oblique_fraction(footprint)
    if oblique > MAX_OBLIQUE_FRACTION and footprint.area < SMALL_IRREGULAR_M2:
        return None  # pequeno e sem paredes em esquadria: pátio, muro, terraço
    if oblique > MAX_OBLIQUE_FRACTION:
        land_flags.append(
            QAFlag(
                code="irregular_outline",
                message=f"Contorno irregular ({oblique:.0%} do perímetro fora de esquadria): "
                "confirmar com o ortofoto.",
                severity="warning",
            )
        )
    if any(footprint.intersection(g).area > 0.3 * footprint.area for g in ctx.osm):
        return None
    dx, dy = offset
    in_lidar = shapely.transform(footprint, lambda c: c + np.array([dx, dy]))
    xyz = ctx.pts.xyz
    bx0, by0, bx1, by1 = in_lidar.bounds
    near = np.flatnonzero(
        (xyz[:, 0] >= bx0) & (xyz[:, 0] <= bx1) & (xyz[:, 1] >= by0) & (xyz[:, 1] <= by1)
    )
    inside = near[shapely.contains_xy(in_lidar, xyz[near, 0], xyz[near, 1])]
    if len(inside) / footprint.area < MIN_POINT_DENSITY:
        return None
    ground = sample_ground_elevation(ctx.dtm, in_lidar, 2.0, 5)
    if ground is None:
        return None
    top = float(np.percentile(xyz[inside, 2], 90))
    if top - ground < MIN_HEIGHT_M:
        return None
    c = footprint.centroid
    split = (
        f" Mancha LiDAR dividida em {n_split} edifícios contíguos (telhados a alturas diferentes)."
        if n_split > 1
        else ""
    )
    return Building(
        osm_type="new",
        osm_id=candidate_id(c.x, c.y),
        osm_version=0,
        footprint=footprint,
        existing_tags={"building": "yes"},
        qa_flags=[
            QAFlag(
                code="missing_in_osm",
                message=f"Edifício no LiDAR sem contorno no OSM ({footprint.area:.0f} m², "
                f"~{top - ground:.1f} m de altura). Contorno ajustado ao ortofoto.{split}",
                severity="info",
            ),
            *land_flags,
        ],
        diagnostics={"ortho_offset_dx": dx, "ortho_offset_dy": dy, "split_count": n_split},
    )


# ------------------------------------------------ só LiDAR (sem ortofoto)


def _detect_tile_lidar(ctx: _TileContext) -> list[Building]:
    bx0, by0, bx1, by1 = ctx.bounds
    pts = ctx.pts
    ncol, nrow = int((bx1 - bx0) / RES_M), int((by1 - by0) / RES_M)
    col = ((pts.xyz[:, 0] - bx0) / RES_M).astype(int).clip(0, ncol - 1)
    row = ((by1 - pts.xyz[:, 1]) / RES_M).astype(int).clip(0, nrow - 1)
    occ = np.zeros((nrow, ncol), bool)
    occ[row, col] = True
    occ = ndimage.binary_closing(occ, iterations=2)
    transform = from_origin(bx0, by1, RES_M, RES_M)
    if ctx.osm:
        covered = rasterize(
            [(g.buffer(OSM_CLEARANCE_M), 1) for g in ctx.osm],
            out_shape=occ.shape,
            transform=transform,
        ).astype(bool)
        occ &= ~covered
    occ = ndimage.binary_opening(occ, iterations=2)
    out = []
    for blob in _blobs(occ, transform, RES_M, MIN_AREA_M2):
        if not ctx.owns(blob.centroid):
            continue
        cand = _evaluate(blob, pts, ctx.dtm)
        if cand is not None:
            out.append(cand)
    return out


def _evaluate(blob: Polygon, pts: PointSet, dtm: DtmMosaic) -> Building | None:
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


# ------------------------------------------------------- cartografia municipal

MUNICIPAL_INSET_M = 0.5  # erosão antes de medir o LiDAR (desvio residual, beirais)
MUNICIPAL_CELL_M = 1.0
MUNICIPAL_MIN_COVER = 0.5  # fração da área com pontos de edifício
MUNICIPAL_MIN_HEIGHT_M = 1.8  # telheiros e anexos baixos
MUNICIPAL_REPLACES = 0.3  # candidato LiDAR coberto acima disto por contornos municipais: sai
NOT_IN_MUNICIPAL = 0.2  # abaixo disto, o candidato LiDAR não consta da cartografia


@dataclass
class MunicipalStats:
    from_municipal: int = 0
    without_lidar: int = 0
    replaced: int = 0
    not_in_municipal: int = 0


def lidar_support(footprint_lidar: Polygon, ctx: _TileContext) -> tuple[float, float | None]:
    """(fração da área com pontos de edifício, altura P90 acima do terreno).

    Mede-se no contorno erodido ``MUNICIPAL_INSET_M``, em células de 1 m.
    """
    inner = footprint_lidar.buffer(-MUNICIPAL_INSET_M, join_style="mitre")
    if inner.is_empty or inner.area < 2.0:
        inner = footprint_lidar
    xyz = ctx.pts.xyz
    bx0, by0, bx1, by1 = inner.bounds
    near = np.flatnonzero(
        (xyz[:, 0] >= bx0) & (xyz[:, 0] <= bx1) & (xyz[:, 1] >= by0) & (xyz[:, 1] <= by1)
    )
    inside = near[shapely.contains_xy(inner, xyz[near, 0], xyz[near, 1])]
    if not len(inside):
        return 0.0, None
    cells = np.unique(np.floor(xyz[inside, :2] / MUNICIPAL_CELL_M).astype(np.int64), axis=0)
    cover = min(1.0, len(cells) * MUNICIPAL_CELL_M**2 / inner.area)
    ground = sample_ground_elevation(ctx.dtm, footprint_lidar, 2.0, 5)
    if ground is None:
        return cover, None
    return cover, float(np.percentile(xyz[inside, 2], 90)) - ground


def _overlap(poly: BaseGeometry, others: list[BaseGeometry], tree: shapely.STRtree) -> float:
    """Fração da área de ``poly`` coberta por ``others`` (contornos não sobrepostos)."""
    if not others or not poly.area:
        return 0.0
    inter = sum(float(poly.intersection(others[i]).area) for i in tree.query(poly))
    return min(1.0, inter / float(poly.area))


def municipal_candidate(
    m: MunicipalBuilding,
    ctx: _TileContext,
    offset: tuple[float, float],
    cfg: MunicipalSourceConfig,
) -> Building | None | bool:
    """Candidato com o contorno municipal, se o LiDAR confirma o edifício.

    ``None``: não é candidato (tipo excluído, pequeno, já no OSM, fora da quadrícula);
    ``False``: o LiDAR não o confirma. A cartografia é anterior ao LiDAR, por isso é
    um edifício demolido ou escondido (sob árvores).
    """
    tag = m.building_tag
    if tag is None or not ctx.owns(m.geometry.centroid):
        return None
    footprint = orient(Polygon(m.geometry.exterior), 1.0)
    if footprint.area < cfg.min_area_m2:
        return None
    if any(footprint.intersection(g).area > 0.3 * footprint.area for g in ctx.osm):
        return None
    dx, dy = offset
    in_lidar = shapely.transform(footprint, lambda c: c + np.array([dx, dy]))
    cover, height = lidar_support(in_lidar, ctx)
    if height is None or cover < MUNICIPAL_MIN_COVER or height < MUNICIPAL_MIN_HEIGHT_M:
        return False
    tags = {"building": tag}
    flags = [
        QAFlag(
            code="missing_in_osm",
            message=f"Edifício da cartografia municipal sem contorno no OSM: {m.descr} "
            f"({footprint.area:.0f} m², ~{height:.1f} m de altura). Contorno municipal "
            f"(gid {m.gid}), confirmado pelo LiDAR em {cover:.0%} da área.",
            severity="info",
        )
    ]
    if m.geometry.interiors:
        flags.append(
            QAFlag(
                code="municipal_courtyard",
                message="O contorno municipal tem pátio interior, que não foi incluído: "
                "rever (multipolígono).",
                severity="warning",
            )
        )
    c = footprint.centroid
    return Building(
        osm_type="new",
        osm_id=candidate_id(c.x, c.y),
        osm_version=0,
        footprint=footprint,
        existing_tags=tags,
        qa_flags=flags,
        diagnostics={
            "ortho_offset_dx": dx,
            "ortho_offset_dy": dy,
            "municipal_gid": float(m.gid),
            "municipal_lidar_cover": cover,
        },
    )


def merge_municipal(
    ctx: _TileContext,
    lidar_candidates: list[Building],
    municipal: list[MunicipalBuilding],
    offset: tuple[float, float],
    cfg: MunicipalSourceConfig,
) -> tuple[list[Building], MunicipalStats]:
    """Junta a cartografia municipal aos candidatos LiDAR da quadrícula.

    - Cada polígono municipal (tipos de edifício) sem contorno no OSM e **confirmado
      pelo LiDAR** passa a candidato com o contorno municipal, que separa casa,
      anexo, garagem e telheiro (o LiDAR não os distingue quando estão encostados).
    - Os candidatos LiDAR cobertos (> 30 %) por esses contornos saem.
    - Os restantes candidatos LiDAR ficam; os que não constam da cartografia (< 20 %,
      contando todos os tipos) são sinalizados: a cartografia é anterior ao LiDAR,
      logo construção recente, ou falso positivo.
    """
    stats = MunicipalStats()
    accepted: list[Building] = []
    for m in municipal:
        cand = municipal_candidate(m, ctx, offset, cfg)
        if cand is False:
            stats.without_lidar += 1
        elif isinstance(cand, Building):
            accepted.append(cand)
    stats.from_municipal = len(accepted)
    acc_geoms = [b.footprint for b in accepted]
    acc_tree = shapely.STRtree(acc_geoms)
    all_geoms = [m.geometry for m in municipal]
    all_tree = shapely.STRtree(all_geoms)
    kept: list[Building] = []
    for b in lidar_candidates:
        if _overlap(b.footprint, acc_geoms, acc_tree) > MUNICIPAL_REPLACES:
            stats.replaced += 1
            continue
        if _overlap(b.footprint, all_geoms, all_tree) < NOT_IN_MUNICIPAL:
            stats.not_in_municipal += 1
            flag = QAFlag(
                code="not_in_municipal",
                message="Não consta da cartografia municipal (anterior ao LiDAR): "
                "construção recente ou falso positivo. Confirmar no ortofoto.",
                severity="warning",
            )
            b = b.model_copy(update={"qa_flags": [*b.qa_flags, flag]})
        kept.append(b)
    return kept + accepted, stats
