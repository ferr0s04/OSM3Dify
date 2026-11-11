"""Download de edifícios OSM via Overpass (``out meta``).

Usa-se Overpass diretamente (e não OSMnx) porque é preciso guardar ``id``,
``version``, nós e relações (multipolígonos com pátios) para poder reescrever
no OSM sem conflitos. Para áreas grandes: extrato PBF da Geofabrik + pyosmium
(extra ``osm``).
"""

from __future__ import annotations

from pathlib import Path

OVERPASS_QUERY = """
[out:xml][timeout:120];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]["type"="multipolygon"]({s},{w},{n},{e});
  way["building:part"]({s},{w},{n},{e});
);
(._;>;);
out meta;
"""


def build_query(bbox: tuple[float, float, float, float]) -> str:
    """Query Overpass para uma bbox WGS84 (min_lon, min_lat, max_lon, max_lat)."""
    w, s, e, n = bbox
    return OVERPASS_QUERY.format(s=s, w=w, n=n, e=e)


def fetch_buildings(bbox: tuple[float, float, float, float], overpass_url: str, out: Path) -> Path:
    """Descarrega os edifícios da bbox para um ficheiro ``.osm`` em cache."""
    raise NotImplementedError("Fase 1: download Overpass")
