"""Nome de uma área a partir das freguesias que as suas quadrículas cobrem.

Freguesias = limites administrativos do OSM com ``admin_level=8`` (Portugal). O nome é
o das até 3 freguesias com mais área coberta, da mais para a menos representativa:
"Azurara, Árvore e Vila do Conde". As uniões de freguesias ficam só com os nomes
("União das Freguesias de Vilar e Mosteiró" → "Vilar e Mosteiró").
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry.base import BaseGeometry

MAX_PLACES = 3
MIN_SHARE = 0.02  # freguesias com menos do que isto da área coberta não contam

_PREFIX = re.compile(
    r"^(uni[aã]o das freguesias d[eao]s?|uni[aã]o de freguesias d[eao]s?|freguesia d[eao]s?)\s+",
    re.IGNORECASE,
)


def short_name(name: str) -> str:
    return _PREFIX.sub("", name.strip()).strip() or name.strip()


def places_label(names: Iterable[str]) -> str | None:
    """ "A", "A e B", "A, B e C"."""
    items = [n for n in names if n]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " e " + items[-1]


def parish_query(bbox: tuple[float, float, float, float], timeout_s: int = 60) -> str:
    w, s, e, n = bbox
    return (
        f"[out:json][timeout:{timeout_s}];"
        f'relation["boundary"="administrative"]["admin_level"="8"]({s},{w},{n},{e});'
        "out geom;"
    )


def rank_parishes(
    data: dict[str, Any], extent: BaseGeometry, crs: str, *, limit: int = MAX_PLACES
) -> list[str]:
    """Nomes curtos das freguesias que cobrem ``extent`` (CRS ``crs``), por área coberta."""
    from osm3d.reconstruction.candidates import _polygon_from_members

    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    shares: dict[str, float] = {}
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if el.get("type") != "relation" or not name:
            continue
        geom = _polygon_from_members(el.get("members", []))
        if geom is None:
            continue
        geom = shapely.transform(
            geom, lambda c: np.column_stack(to_crs.transform(c[:, 0], c[:, 1]))
        )
        covered = float(shapely.make_valid(geom).intersection(extent).area)
        if covered > MIN_SHARE * extent.area:
            key = short_name(name)
            shares[key] = shares.get(key, 0.0) + covered
    return [n for n, _ in sorted(shares.items(), key=lambda kv: -kv[1])[:limit]]


def area_places(
    tiles: list[str],
    crs: str,
    overpass_url: str,
    *,
    query: Callable[[str], dict[str, Any]] | None = None,
) -> list[str]:
    """Freguesias cobertas pelas quadrículas (pedido ao Overpass, ~1 s)."""
    from osm3d.acquisition.osm import _overpass_json
    from osm3d.geospatial.tiling import TileKey

    if not tiles:
        return []
    extent = shapely.union_all([shapely.box(*TileKey.from_dgt_name(t).bounds) for t in tiles])
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x0, y0, x1, y1 = extent.bounds
    lons, lats = to_wgs.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    q = parish_query((min(lons), min(lats), max(lons), max(lats)))
    data = query(q) if query else _overpass_json(q, overpass_url, timeout_s=60)
    return rank_parishes(data, extent, crs)
