"""Contornos de edifícios editados no mapa da revisão e o reajuste do 3D a eles.

O contorno de um edifício edita-se como os landuse (ver :mod:`osm3d.geometry_edits`):

- **edifício do OSM** (way): é a way do contexto OSM, com os nós movidos, acrescentados
  ou tirados; um nó partilhado com um vizinho (edifício encostado, landuse) move-se
  também nele. Na exportação vai como as edições manuais de landuse;
- **edifício novo** (cartografia municipal): o anel editado substitui o contorno
  municipal (``GeometryEdits.buildings``).

Com outro contorno, a reconstrução deixa de servir: terreno, telhado, altura, cor e
sinalizações são refeitos para esse edifício com o LiDAR e o MDT, com o mesmo
deslocamento de co-registo. O resultado fica guardado (``ReviewStore.footprint_fits``)
com uma assinatura do contorno e do edifício de origem; a revisão e a exportação usam-no
sem voltar a ler o LiDAR enquanto nenhum dos dois muda.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from osm3d.config import Config
from osm3d.domain.models import Building, QAFlag
from osm3d.geometry_edits import ContextIndex, GeometryEdits, is_edited, landuse_ring
from osm3d.geospatial.point_clouds import PointSet
from osm3d.geospatial.tiling import TileKey, tile_of

FOOTPRINT_EDITED = "footprint_edited"
REFIT_FAILED = "footprint_refit_failed"

# Sinalizações e diagnósticos da reconstrução e do QA: refeitos com o contorno novo.
# As outras (p.ex. as da deteção dos candidatos municipais) mantêm-se.
_REBUILT_FLAGS = {
    "complex_footprint",
    "no_ground",
    "low_point_density",
    "secondary_volume",
    "terrain_step",
    "roof_shape_unknown",
    "roof_shape_low_confidence",
    "height_outlier",
    "shared_roof",
    "attached_heights",  # igualado aos encostados: o contorno novo ajusta-se sozinho
    "invalid_footprint",
    "no_parts",
    "osm_building_parts",
    "parts_outside_outline",
    "invalid_height_range",
    FOOTPRINT_EDITED,
    REFIT_FAILED,
}
_REBUILT_DIAGNOSTICS = {
    "n_points",
    "point_density",
    "rectangularity",
    "area_m2",
    "main_volume_fraction",
    "rmse_m",
    "inlier_ratio",
    "terrain_step_m",
    "roof_group_size",
    "attached_group_size",
    "attached_dh_m",
    "attached_droof_m",
}

PointsAround = Callable[[tuple[float, float, float, float]], PointSet]


def _ring_polygon(ring: Iterable[tuple[float, float]], to_crs: Transformer) -> Polygon:
    lons, lats = zip(*ring, strict=True)
    xs, ys = to_crs.transform(list(lons), list(lats))
    return Polygon(list(zip(xs, ys, strict=True)))


def edited_footprints(
    buildings: Iterable[Building],
    ctx: ContextIndex | None,
    edits: GeometryEdits,
    to_crs: Transformer,
) -> dict[str, Polygon]:
    """Contorno editado (CRS de processamento) de cada edifício que o tem."""
    out: dict[str, Polygon] = {}
    for b in buildings:
        if b.osm_type == "new":
            new_ring = edits.buildings.get(b.key)
            if new_ring:
                out[b.key] = _ring_polygon(new_ring, to_crs)
        elif (
            b.osm_type == "way"
            and ctx is not None
            and ctx.is_area(b.osm_id)
            and is_edited(ctx, edits, b.osm_id)
        ):
            nodes = landuse_ring(ctx, edits, b.osm_id)
            out[b.key] = _ring_polygon([(x, y) for _, x, y in nodes], to_crs)
    return out


def fit_signature(original: Building, footprint: Polygon) -> str:
    """Muda se o contorno editado ou o edifício de origem (nova corrida do pipeline) mudam."""
    coords = np.round(np.asarray(footprint.exterior.coords), 2).tolist()
    raw = json.dumps({"fp": coords, "b": original.model_dump_json()})
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def tiles_covering(
    tiles: dict[str, Path], bounds: tuple[float, float, float, float], margin: float
) -> list[Path]:
    minx, miny, maxx, maxy = bounds
    lo, hi = tile_of(minx - margin, miny - margin), tile_of(maxx + margin, maxy + margin)
    names = [
        TileKey(ix, iy).dgt_name for ix in range(lo.ix, hi.ix + 1) for iy in range(lo.iy, hi.iy + 1)
    ]
    return [tiles[n] for n in names if n in tiles]


class FootprintRefitter:
    """Refaz a reconstrução de um edifício com outro contorno.

    ``points_around(bounds)``: pontos LiDAR (referencial do LiDAR) numa caixa;
    ``dtm_tiles()`` e ``ortho_paths()``: ficheiros atuais do MDT e das ortofotos.
    """

    def __init__(
        self,
        cfg: Config,
        points_around: PointsAround,
        dtm_tiles: Callable[[], dict[str, Path]],
        ortho_paths: Callable[[], list[Path]],
    ) -> None:
        self.cfg = cfg
        self.points_around = points_around
        self.dtm_tiles = dtm_tiles
        self.ortho_paths = ortho_paths

    def __call__(self, b: Building, footprint: Polygon) -> Building:
        from osm3d.appearance.area import colour_parts
        from osm3d.appearance.roof_colours import BUILDING_CLASS
        from osm3d.geospatial.point_clouds import assign_points
        from osm3d.geospatial.raster import DtmMosaic
        from osm3d.reconstruction.building import reconstruct_building
        from osm3d.validation.quality import check_building

        cfg = self.cfg
        footprint = orient(footprint, 1.0)
        kept = [f for f in b.qa_flags if f.code not in _REBUILT_FLAGS]
        probe = b.model_copy(
            update={
                "footprint": footprint,
                "parts": [],
                "layout": None,
                "suggested_layout": None,
                "qa_flags": kept,
                "diagnostics": {
                    k: v for k, v in b.diagnostics.items() if k not in _REBUILT_DIAGNOSTICS
                },
            }
        )
        offset = b.registration_offset_m
        dx, dy = offset
        minx, miny, maxx, maxy = footprint.bounds
        m = 2.0
        points = self.points_around((minx + dx - m, miny + dy - m, maxx + dx + m, maxy + dy + m))
        dtm_paths = tiles_covering(
            self.dtm_tiles(), footprint.bounds, cfg.heights.ground_ring_buffer_m + 5.0
        )
        if not dtm_paths:
            return _failed(probe, "Sem MDT descarregado para reajustar o edifício ao contorno")
        pts = points.with_classes(cfg.heights.building_classes) if len(points) else points
        if len(pts):
            pts = pts.subset(assign_points(pts, [footprint], offset)[0])
        with ExitStack() as stack:
            dtm = stack.enter_context(DtmMosaic(dtm_paths))
            built, _ = reconstruct_building(probe, pts, dtm, offset, cfg)
            if built.parts:
                orthos = self.ortho_paths()
                ortho = None
                if orthos:
                    from osm3d.geospatial.orthophoto import OrthoMosaic

                    ortho = stack.enter_context(OrthoMosaic(orthos))
                roof_pts = pts.with_classes([BUILDING_CLASS]) if len(pts) else pts
                built = colour_parts(built, roof_pts, ortho)
        known = {f.code for f in built.qa_flags}
        extra = [f for f in check_building(built) if f.code not in known]
        note = QAFlag(
            code=FOOTPRINT_EDITED,
            message="Contorno editado na revisão: telhado, altura e cor reajustados ao LiDAR",
            severity="info",
        )
        return built.model_copy(update={"qa_flags": [*built.qa_flags, *extra, note]})


def _failed(b: Building, why: str) -> Building:
    flag = QAFlag(code=REFIT_FAILED, message=why, severity="error")
    return b.model_copy(update={"qa_flags": [*b.qa_flags, flag]})


def apply_footprint_edits(
    buildings: list[Building],
    footprints: dict[str, Polygon],
    fits: dict[str, tuple[str, str]],
    refit: Callable[[Building, Polygon], Building],
    save: Callable[[str, str, str], None] | None = None,
) -> list[Building]:
    """Edifícios com o contorno editado substituídos pela reconstrução reajustada.

    ``fits``: reajustes guardados (chave → (assinatura, JSON)); os que faltam ou estão
    desatualizados são refeitos com ``refit`` e entregues a ``save(chave, assinatura,
    JSON)``.
    """
    out: list[Building] = []
    for b in buildings:
        poly = footprints.get(b.key)
        if poly is None:
            out.append(b)
            continue
        sig = fit_signature(b, poly)
        hit = fits.get(b.key)
        if hit is not None and hit[0] == sig:
            out.append(Building.model_validate_json(hit[1]))
            continue
        built = refit(b, poly)
        if save is not None:
            save(b.key, sig, built.model_dump_json())
        out.append(built)
    return out
