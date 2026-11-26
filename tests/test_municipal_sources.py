"""Várias câmaras: classificação dos tipos, ArcGIS REST, ficheiros de dados abertos e a
junção por área."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from osm3d.acquisition.municipal import fetch_full_municipality, load_municipal_buildings
from osm3d.acquisition.municipal_providers import ArcGISClient, FileSource, esri_polygons
from osm3d.acquisition.municipal_types import carttop_label, carttop_tag, catalogo_tag
from osm3d.config import (
    AreaConfig,
    BarrierLayer,
    BarrierRule,
    Config,
    MunicipalSourceConfig,
    PathsConfig,
    SourcesConfig,
)

# ------------------------------------------------------------------- tipos


@pytest.mark.parametrize(
    ("descr", "tag"),
    [
        ("Vivenda Casa", "house"),  # Vila do Conde
        ("Arrecadação_Anexo", "shed"),
        ("Telheiro, Alpendre", "roof"),
        ("06020102-VIVENDA CASA", "house"),  # Porto: código à frente, maiúsculas
        ("06020103-PRÉDIO", "yes"),
        ("06010803-LAR 3ª IDADE CENTRO DIA", "yes"),
        ("06010307-ESCOLA EB23 - SECUNDÁRIA", "school"),
        ("06020112-EDIFÍCIO CORPO BALANÇADO", None),  # parte de outro edifício
        ("Escadas", None),
        ("Tipo que não existe", None),
    ],
)
def test_catalogo_types(descr: str, tag: str | None) -> None:
    assert catalogo_tag(descr) == tag


def test_carttop_types() -> None:
    assert carttop_tag({"valor_utilizacao_atual": "Residencial | Comércio"}) == (
        "residential",
        True,
    )
    # Gralha da fonte ("Asssociado") e anexos.
    assert carttop_tag({"valor_utilizacao_atual": "Asssociado à residencia"}) == ("shed", True)
    assert carttop_tag({"valor_forma_edificio": "Estufa"}) == ("greenhouse", True)
    assert (
        carttop_tag({"valor_condicao_const": "Ruína", "valor_forma_edificio": "Telheiro"})[0]
        is None
    )
    assert carttop_tag({"valor_condicao_const": "Em construção"})[0] == "construction"
    assert carttop_tag({"valor_tipo_equipamento_coletivo": "Hospital"})[0] == "hospital"
    assert carttop_tag({})[0] == "yes"
    assert (
        carttop_label({"valor_utilizacao_atual": "Asssociado à residencia"})
        == "Associado à residencia"
    )


# -------------------------------------------------------------------- ArcGIS


def test_esri_rings_with_hole() -> None:
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]  # horário: exterior
    hole = [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]  # anti-horário: buraco
    (poly,) = esri_polygons([outer, hole])
    assert poly.area == pytest.approx(96) and len(poly.interiors) == 1


class _FakeArcGIS:
    """Camada com 3 edifícios e paginação de 2 em 2 (como o MapServer do Porto)."""

    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []

    def __call__(self, url: str) -> bytes:
        base, _, qs = url.partition("?")
        params = dict(urllib.parse.parse_qsl(qs))
        if not base.endswith("/query"):
            # Como o MapServer do Porto: sem objectIdField, o OID só na lista de campos.
            oid = {"name": "objectid_1", "type": "esriFieldTypeOID"}
            info = {"maxRecordCount": 2, "fields": [oid],
                    "advancedQueryCapabilities": {"supportsPagination": True}}  # fmt: skip
            return json.dumps(info).encode()
        self.queries.append(params)
        if params.get("returnExtentOnly"):
            return json.dumps({"extent": {"xmin": 0, "ymin": 0, "xmax": 30, "ymax": 10}}).encode()
        feats = [
            {"attributes": {"objectid_1": i, "layer": layer},
             "geometry": {"rings": [[[x, 0], [x, 8], [x + 8, 8], [x + 8, 0], [x, 0]]]}}
            for i, (x, layer) in enumerate(
                [(0, "06020102-VIVENDA CASA"), (10, "06020103-PRÉDIO"), (20, "ESCADAS")], start=1
            )
        ]  # fmt: skip
        off, n = int(params.get("resultOffset", 0)), int(params.get("resultRecordCount", 99))
        page = feats[off : off + n]
        return json.dumps(
            {"features": page, "exceededTransferLimit": off + n < len(feats)}
        ).encode()


def test_arcgis_paginates_and_maps_types() -> None:
    cfg = MunicipalSourceConfig(
        kind="arcgis", name="Câmara Municipal do Teste", layer_url="https://x/MapServer/106",
        delay_s=0.2,
    )  # fmt: skip
    fake = _FakeArcGIS()
    client = ArcGISClient(cfg, opener=fake)
    found = client.select_buildings((0, 0, 30, 10))
    assert [(b.gid, b.building_tag, b.descr) for b in found] == [
        (1, "house", "VIVENDA CASA"),
        (2, "yes", "PRÉDIO"),
        (3, None, "ESCADAS"),
    ]
    assert len(fake.queries) == 2  # 2 páginas
    assert fake.queries[0]["outSR"] == "3763" and fake.queries[0]["inSR"] == "3763"
    assert client.extent() == (0, 0, 30, 10)


# ------------------------------------------------------------------ ficheiro


def _write_gpkg(path: Path, origin: tuple[float, float]) -> None:
    """GeoPackage CartTop mínimo: 2 edifícios, 1 em ponto, 1 muro e 1 sebe."""
    x, y = origin
    edificios = gpd.GeoDataFrame(
        {
            "identificador": ["a1b2-1", "a1b2-2", "a1b2-3"],
            "valor_utilizacao_atual": ["Residencial", "Asssociado à residencia", "Residencial"],
            "valor_forma_edificio": [None, None, None],
            "valor_condicao_const": [None, None, None],
        },
        geometry=[box(x, y, x + 12, y + 9), box(x + 20, y, x + 24, y + 4), Point(x + 40, y + 5)],
        crs=3763,
    )
    edificios.to_file(path, layer="public_edificio", driver="GPKG")
    tipos = ["Muro de alvenaria ou betão", "Sebe"]
    linhas = gpd.GeoDataFrame(
        {"identificador": ["m1", "m2"], "valor_construcao_linear": tipos},
        geometry=[LineString([(x, y - d), (x + 30, y - d)]) for d in (3, 6)],
        crs=3763,
    )
    linhas.to_file(path, layer="public_constru_linear", driver="GPKG")


def _file_source(tmp_path: Path, gpkg: Path, name: str, **kw: Any) -> MunicipalSourceConfig:
    walls = BarrierLayer(
        label="NdD2",
        feature_class="public_constru_linear",
        rules=[BarrierRule(match={"valor_construcao_linear": "Muro de alvenaria ou betão"},
                           tags={"barrier": "wall"})],
    )  # fmt: skip
    return MunicipalSourceConfig(
        kind="file", name=name, file_url=str(gpkg), layer="public_edificio", types="carttop",
        cache_dir=tmp_path / "raw", barriers=[walls], **kw,
    )  # fmt: skip


def test_file_source_full_copy(tmp_path: Path) -> None:
    gpkg = tmp_path / "ndd2.gpkg"
    _write_gpkg(gpkg, (-44900, 179100))
    cfg = _file_source(tmp_path, gpkg, "Câmara Municipal da Póvoa de Teste")
    src = FileSource(cfg)
    found = src.all_buildings()
    assert [b.building_tag for b in found] == ["residential", "shed"]  # o ponto fica de fora
    assert all(b.source == cfg.name for b in found)
    (near,) = src.select_buildings((-44905, 179095, -44885, 179112))
    assert near.descr == "Residencial"
    fetch_full_municipality(cfg)
    tags = sorted(str(b.building_tag) for b in load_municipal_buildings(cfg.full_path))
    assert tags == ["residential", "shed"]
    walls = json.loads(cfg.full_barriers_path.read_text(encoding="utf-8"))
    assert [f["properties"]["barrier"] for f in walls["features"]] == ["wall"]  # a sebe não


# ------------------------------------------------------------- várias câmaras


def test_area_uses_the_municipalities_that_cover_it(tmp_path: Path) -> None:
    from osm3d.municipal_area import area_barriers, area_buildings, sources_for_area

    # Duas câmaras com a fronteira na quadrícula 155480 (x de -45000 a -44000) e uma longe.
    a, b, far = tmp_path / "a.gpkg", tmp_path / "b.gpkg", tmp_path / "c.gpkg"
    _write_gpkg(a, (-44900, 179100))
    _write_gpkg(b, (-44900, 179100))  # o mesmo edifício na outra cartografia (fronteira)
    _write_gpkg(far, (-10000, 100000))
    sources = [
        _file_source(tmp_path, a, "Câmara A", bbox=(-46000, 178000, -44500, 181000)),
        _file_source(tmp_path, b, "Câmara B", bbox=(-44950, 178000, -43000, 181000)),
        _file_source(tmp_path, far, "Câmara Longe", bbox=(-11000, 99000, -9000, 101000)),
    ]
    cfg = Config(
        area=AreaConfig(name="fronteira", tiles=["155480"]),
        paths=PathsConfig(processed=tmp_path / "processed", output=tmp_path / "out"),
        sources=SourcesConfig(municipalities=sources),
    )
    assert [s.name for s in sources_for_area(cfg)] == ["Câmara A", "Câmara B"]
    buildings, used = area_buildings(cfg, ["155480"])
    assert [s.name for s in used] == ["Câmara A", "Câmara B"]
    # Os edifícios repetidos na fronteira ficam só os da primeira câmara.
    assert sorted((b.source, b.building_tag) for b in buildings) == [
        ("Câmara A", "residential"),
        ("Câmara A", "shed"),
    ]
    # Muros das duas, com a câmara na chave.
    walls = json.loads(area_barriers(cfg).read_text(encoding="utf-8"))["features"]
    assert sorted(f["properties"]["key"].split("/")[0] for f in walls) == ["camara-a", "camara-b"]


def test_legacy_single_municipality_config_still_works() -> None:
    legacy = MunicipalSourceConfig(
        name="Câmara Municipal de Vila do Conde", viewer_url="https://v", mapagent_url="https://m",
        feature_source="Library://x", feature_class="a:b",
    )  # fmt: skip
    porto = MunicipalSourceConfig(
        kind="arcgis", name="Câmara Municipal do Porto", layer_url="https://p/1"
    )
    s = SourcesConfig(municipal=legacy, municipalities=[porto, legacy])
    assert [m.key for m in s.municipal_sources] == [
        "camara-municipal-de-vila-do-conde",
        "camara-municipal-do-porto",
    ]
    assert s.municipal_source("camara-municipal-do-porto").kind == "arcgis"


def test_area_walls_are_cut_from_the_full_copy_without_asking(tmp_path: Path) -> None:
    """Com a cópia do concelho (edifícios e muros) descarregada, os muros da área saem dela
    sem esperar pela fase candidates nem pelo "obter do geoportal"."""
    from osm3d.pipeline import barriers_path, local_area_barriers

    gpkg = tmp_path / "ndd2.gpkg"
    _write_gpkg(gpkg, (-44900, 179100))
    src = _file_source(tmp_path, gpkg, "Câmara A", bbox=(-46000, 178000, -44000, 181000))
    cfg = Config(
        area=AreaConfig(name="a", tiles=["155480"]),
        paths=PathsConfig(processed=tmp_path / "processed", output=tmp_path / "out"),
        sources=SourcesConfig(municipalities=[src]),
    )
    assert local_area_barriers(cfg) is None  # sem cópia: só o "obter do geoportal"
    fetch_full_municipality(src)
    gpkg.unlink()  # nada de ir à fonte: o recorte vem só da cópia
    path = local_area_barriers(cfg)
    assert path == barriers_path(cfg) and path.exists()
    walls = json.loads(path.read_text(encoding="utf-8"))["features"]
    assert [f["properties"]["barrier"] for f in walls] == ["wall"]
    made = path.stat().st_mtime
    assert local_area_barriers(cfg) == path and path.stat().st_mtime == made  # em cache
