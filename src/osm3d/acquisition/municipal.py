"""Cartografia municipal de edifícios (geoportal MapGuide de Vila do Conde).

O geoportal (https://sig.cm-viladoconde.pt/geoportal) é um visualizador sobre
Autodesk MapGuide. Os serviços OGC (WMS/WFS) estão publicados sem camadas, mas a
API HTTP do MapGuide (``mapagent.fcgi``) devolve os polígonos em vetor
(``SELECTFEATURES``), com a sessão anónima que a página do visualizador cria.

Camada: cartografia topográfica vetorial 1:5000 homologada (~2017), classe
``muni_5000_h_planimetria_pg``, polígonos com ``objeto = 'const_poligonos'``.
Atributos úteis: ``gid``, ``descr`` (tipo: "Vivenda Casa", "Garagem",
"Telheiro, Alpendre"…), ``xiscod``. Sem altura, pisos nem telhado.

**Uso autorizado pelo município.** Não é um serviço publicado: a sessão é obtida
da página do visualizador e o município pode mudar o acesso sem aviso. Os pedidos
são pequenos (caixas de 250 m) e espaçados (``delay_s``).
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

from osm3d.config import MunicipalSourceConfig

log = logging.getLogger(__name__)

USER_AGENT = "osm3d-building-generator/0.1"
_SESSION_RE = re.compile(r"SESSION=([^&\"'\s]+)")

# Tipo municipal (``descr``) → tag ``building`` do OSM. ``None``: não é edifício a
# mapear (escadas, piscinas, reservatórios, ruínas sem cobertura).
BUILDING_TAGS: dict[str, str | None] = {
    "Vivenda Casa": "house",
    "Prédio": "yes",
    "Arrecadação_Anexo": "shed",
    "Garagem": "garage",
    "Telheiro, Alpendre": "roof",
    "Barraca": "shed",
    "Construções Em Geral": "yes",
    "Fábrica": "industrial",
    "Armazém": "warehouse",
    "Posto De Transformação": "service",
    "Edificio Em Ruínas": None,
    "Escadas": None,
    "Piscinas": None,
    "Depósito Água Elevado": None,
    "Depósito Água De Superfície": None,
    "Depósito de Gás(Gasómetro)": None,
}


@dataclass(frozen=True)
class MunicipalBuilding:
    gid: int
    geometry: Polygon  # EPSG:3763
    descr: str

    @property
    def building_tag(self) -> str | None:
        """Tag ``building`` do OSM, ou ``None`` se o tipo não é para mapear/desconhecido."""
        return BUILDING_TAGS.get(self.descr)


class MunicipalError(RuntimeError):
    pass


class MapGuideClient:
    """Pedidos ``SELECTFEATURES`` com a sessão anónima do visualizador."""

    def __init__(self, cfg: MunicipalSourceConfig, timeout_s: float = 60.0) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self._opener.addheaders = [("User-Agent", USER_AGENT)]
        self._session: str | None = None

    def _open(self, url: str) -> bytes:
        with self._opener.open(url, timeout=self.timeout_s) as resp:
            return bytes(resp.read())

    def session(self, renew: bool = False) -> str:
        if self._session is None or renew:
            html = self._open(self.cfg.viewer_url).decode("utf-8", "replace")
            m = _SESSION_RE.search(html)
            if m is None:
                raise MunicipalError(
                    f"Sem sessão MapGuide na página {self.cfg.viewer_url} (o geoportal mudou?)"
                )
            self._session = m.group(1)
        return self._session

    def select(self, box: tuple[float, float, float, float]) -> bytes:
        """XML dos polígonos de edifício que intersetam a caixa (EPSG:3763)."""
        x0, y0, x1, y1 = box
        ring = f"{x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}"
        flt = f"{self.cfg.filter} AND geom ENVELOPEINTERSECTS GeomFromText('POLYGON(({ring}))')"
        for attempt in range(2):
            query = {
                "OPERATION": "SELECTFEATURES",
                "VERSION": "1.0.0",
                "SESSION": self.session(renew=attempt > 0),
                "FORMAT": "text/xml",
                "RESOURCEID": self.cfg.feature_source,
                "CLASSNAME": self.cfg.feature_class,
                "FILTER": flt,
            }
            try:
                return self._open(f"{self.cfg.mapagent_url}?{urllib.parse.urlencode(query)}")
            except urllib.error.HTTPError as exc:  # sessão expirada: 401
                if attempt or exc.code not in (401, 559):
                    raise
                log.info("Sessão MapGuide expirada: a pedir outra")
        raise AssertionError("inalcançável")


def parse_features(xml: bytes) -> list[MunicipalBuilding]:
    """``FeatureSet`` do MapGuide → edifícios (o maior polígono de cada multipolígono)."""
    root = ET.fromstring(xml)
    out: list[MunicipalBuilding] = []
    for feat in root.iter("Feature"):
        props = {p.findtext("Name"): p.findtext("Value") for p in feat.iter("Property")}
        if not props.get("geom") or not props.get("gid"):
            continue
        geom = shapely.make_valid(wkt.loads(str(props["geom"])))
        polys = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon) and g.area]
        if not polys:
            continue
        out.append(
            MunicipalBuilding(
                int(str(props["gid"])), max(polys, key=lambda p: p.area), props.get("descr") or ""
            )
        )
    return out


def fetch_municipal_buildings(
    bbox: tuple[float, float, float, float],
    cfg: MunicipalSourceConfig,
    out: Path,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    client: MapGuideClient | None = None,
) -> Path:
    """Descarrega os edifícios municipais da caixa (EPSG:3763) para GeoJSON (cache)."""
    if out.exists() and not force:
        return out
    client = client or MapGuideClient(cfg)
    x0, y0, x1, y1 = bbox
    step = cfg.chunk_m
    boxes = [
        (float(xa), float(ya), float(min(xa + step, x1)), float(min(ya + step, y1)))
        for xa in np.arange(x0, x1, step)
        for ya in np.arange(y0, y1, step)
    ]
    found: dict[int, MunicipalBuilding] = {}
    for i, box in enumerate(boxes):
        if i:
            time.sleep(cfg.delay_s)
        for b in parse_features(client.select(box)):
            found[b.gid] = b
    if progress:
        progress(
            f"  cartografia municipal: {len(found)} polígonos de edifício ({len(boxes)} pedidos)"
        )
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3763"}},
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(b.geometry),
                "properties": {"gid": b.gid, "descr": b.descr},
            }
            for b in sorted(found.values(), key=lambda b: b.gid)
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    meta = {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": cfg.name,
        "bbox": list(bbox),
        "n": len(found),
    }
    out.with_name(out.name + ".fetch.json").write_text(json.dumps(meta), encoding="utf-8")
    return out


def load_municipal_buildings(path: Path) -> list[MunicipalBuilding]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for f in data["features"]:
        geom: BaseGeometry = shape(f["geometry"])
        if isinstance(geom, Polygon):
            p = f["properties"]
            out.append(MunicipalBuilding(int(p["gid"]), geom, str(p.get("descr") or "")))
    return out
