"""Persistência do modelo intermédio em GeoPackage.

Camadas:

- ``buildings``: um registo por edifício (footprint OSM), com colunas legíveis no
  QGIS e ``model_json`` com o modelo completo (para leitura sem perdas);
- ``building_parts``: um registo por parte, com os atributos S3DB propostos
  (``height`` serve para extrusão na vista 3D do QGIS) e a tag ``roof:shape``
  existente no OSM, para comparação.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import geopandas as gpd
import pyogrio

from osm3d.domain.models import Building
from osm3d.export.s3db_tags import part_to_tags


def _building_row(b: Building) -> dict[str, object]:
    t = b.existing_tags
    d = b.diagnostics
    return {
        "osm_key": b.key,
        "osm_type": b.osm_type,
        "osm_id": b.osm_id,
        "osm_version": b.osm_version,
        "review_status": b.review_status,
        "needs_review": b.needs_review,
        "qa_flags": ",".join(f.code for f in b.qa_flags),
        "n_parts": len(b.parts),
        "ground_m": b.ground_elevation_m,
        "offset_dx": b.registration_offset_m[0],
        "offset_dy": b.registration_offset_m[1],
        "n_points": d.get("n_points"),
        "point_density": d.get("point_density"),
        "rectangularity": d.get("rectangularity"),
        "osm_building": t.get("building"),
        "osm_levels": t.get("building:levels"),
        "osm_height": t.get("height"),
        "osm_roof_shape": t.get("roof:shape"),
        "model_json": b.model_dump_json(),
        "geometry": b.footprint,
    }


def _part_rows(b: Building) -> list[dict[str, object]]:
    rows = []
    for p in b.parts:
        r = p.roof
        rows.append(
            {
                "osm_key": b.key,
                "part_id": p.id,
                "height": round(p.height.value, 2),
                "height_conf": round(p.height.confidence, 3),
                "min_height": p.min_height,
                "roof_shape": r.shape.value.value,
                "roof_shape_conf": round(r.shape.confidence, 3),
                "roof_height": round(r.height.value, 2) if r.height else None,
                "ridge_azimuth": r.ridge_azimuth_deg,
                "facing_azimuth": r.facing_azimuth_deg,
                "rmse_m": r.rmse_m,
                "s3db_tags": ";".join(f"{k}={v}" for k, v in part_to_tags(p).items()),
                "osm_roof_shape": b.existing_tags.get("roof:shape"),
                "geometry": p.geometry,
            }
        )
    return rows


def write_buildings(buildings: Iterable[Building], path: Path, crs: str) -> Path:
    buildings = list(buildings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    bdf = gpd.GeoDataFrame([_building_row(b) for b in buildings], geometry="geometry", crs=crs)
    bdf.to_file(path, layer="buildings", driver="GPKG", engine="pyogrio")
    part_rows = [r for b in buildings for r in _part_rows(b)]
    if part_rows:
        pdf = gpd.GeoDataFrame(part_rows, geometry="geometry", crs=crs)
        pdf.to_file(path, layer="building_parts", driver="GPKG", engine="pyogrio")
    return path


def read_buildings(path: Path) -> list[Building]:
    df = pyogrio.read_dataframe(
        path, layer="buildings", columns=["model_json"], read_geometry=False
    )
    return [Building.model_validate_json(s) for s in df["model_json"]]
