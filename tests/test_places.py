from __future__ import annotations

from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely.geometry import box

from osm3d.acquisition.places import area_places, places_label, rank_parishes, short_name
from osm3d.areas import AreaStore
from osm3d.config import AreaConfig
from osm3d.geospatial.tiling import TileKey

CRS = "EPSG:3763"
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)


def test_names_and_label() -> None:
    assert short_name("União das Freguesias de Vilar e Mosteiró") == "Vilar e Mosteiró"
    assert short_name("Azurara") == "Azurara"
    assert places_label(["Azurara"]) == "Azurara"
    assert places_label(["Azurara", "Árvore"]) == "Azurara e Árvore"
    assert places_label(["Azurara", "Árvore", "Vila do Conde"]) == "Azurara, Árvore e Vila do Conde"
    assert places_label([]) is None


def _relation(rid: int, name: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    geom = [dict(zip(("lon", "lat"), TO_WGS.transform(x, y), strict=True)) for x, y in ring]
    return {
        "type": "relation",
        "id": rid,
        "tags": {"name": name, "admin_level": "8", "boundary": "administrative"},
        "members": [{"type": "way", "role": "outer", "geometry": geom}],
    }


def test_parishes_ranked_by_covered_area() -> None:
    tile = TileKey.from_dgt_name("154483")
    x0, y0, x1, y1 = tile.bounds
    xm = x0 + 0.6 * (x1 - x0)
    data = {
        "elements": [
            _relation(1, "Árvore", xm, y0, x1 + 500, y1),  # 40 %
            _relation(2, "Azurara", x0 - 500, y0, xm, y1),  # 60 %
            _relation(3, "Longe", x1 + 1000, y0, x1 + 2000, y1),  # não cobre
        ]
    }
    names = area_places(["154483"], CRS, "", query=lambda q: data)
    assert names == ["Azurara", "Árvore"]
    assert rank_parishes(data, box(x0, y0, x1, y1), CRS, limit=1) == ["Azurara"]


def test_store_keeps_places_for_the_tiles(tmp_path: Path) -> None:
    store = AreaStore(tmp_path / "areas.json", AreaConfig(name="area-1", tiles=["154483"]))
    store.set_places("area-1", ["154483"], ["Azurara", "Árvore"])
    a = store.get("area-1")
    assert a.display_label == "Azurara e Árvore"
    store.save(a.model_copy(update={"tiles": ["154483", "154484"], "places": []}))
    b = store.get("area-1")
    assert not b.places_current and b.display_label is None  # quadrículas mudaram
    store.save(b.model_copy(update={"label": "Minha área"}))
    assert store.get("area-1").display_label == "Minha área"  # nome escrito manda
