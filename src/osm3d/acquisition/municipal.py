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
são pequenos (caixas de 250 m por área, 1 km para o concelho) e espaçados
(``delay_s``).

Duas formas de obter os polígonos:

- por área (:func:`fetch_municipal_buildings` na caixa da área), na fase candidates;
- todo o concelho de uma vez (:func:`fetch_full_municipality`: ~110 mil polígonos,
  ~260 pedidos, ~5 min), para ``MunicipalSourceConfig.full_path``. Com esta cópia,
  as áreas recortam-na localmente (:func:`crop_municipal`) e não voltam a pedir.

O servidor envia uma cadeia de certificados que o Python no Windows não valida com o
repositório do sistema: usa-se o ``certifi`` (o mesmo do ``requests``).
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import certifi
import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

from osm3d.config import MunicipalSourceConfig

log = logging.getLogger(__name__)

USER_AGENT = "osm3d-building-generator/0.1"
_SESSION_RE = re.compile(r"SESSION=([^&\"'\s]+)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

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

# Erros de rede passageiros num pedido: repete-se com pausa crescente.
_TRANSIENT = (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError)
RETRIES = 3


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


class MunicipalCancelled(MunicipalError):
    pass


class MapGuideClient:
    """Pedidos ao ``mapagent`` com a sessão anónima do visualizador."""

    def __init__(self, cfg: MunicipalSourceConfig, timeout_s: float = 60.0) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        jar = http.cookiejar.CookieJar()
        tls = ssl.create_default_context(cafile=certifi.where())
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=tls)
        )
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

    def _request(self, params: dict[str, str]) -> bytes:
        """Pedido com a sessão (renovada se expirou) e repetições em falhas de rede."""
        renew = False
        for attempt in range(RETRIES + 1):
            query = {"VERSION": "1.0.0", "FORMAT": "text/xml", **params}
            query["SESSION"] = self.session(renew=renew)
            try:
                return self._open(f"{self.cfg.mapagent_url}?{urllib.parse.urlencode(query)}")
            except urllib.error.HTTPError as exc:  # sessão expirada: 401
                if exc.code in (401, 559) and not renew:
                    log.info("Sessão MapGuide expirada: a pedir outra")
                    renew = True
                    continue
                if exc.code < 500 or attempt == RETRIES:
                    raise
            except _TRANSIENT:
                if attempt == RETRIES:
                    raise
            log.info("Pedido ao geoportal falhou: nova tentativa (%d)", attempt + 1)
            time.sleep(2.0 * (attempt + 1))
        raise AssertionError("inalcançável")

    def select(self, box: tuple[float, float, float, float]) -> bytes:
        """XML dos polígonos de edifício que intersetam a caixa (EPSG:3763)."""
        x0, y0, x1, y1 = box
        ring = f"{x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}"
        flt = f"{self.cfg.filter} AND geom ENVELOPEINTERSECTS GeomFromText('POLYGON(({ring}))')"
        return self._request(
            {
                "OPERATION": "SELECTFEATURES",
                "RESOURCEID": self.cfg.feature_source,
                "CLASSNAME": self.cfg.feature_class,
                "FILTER": flt,
            }
        )

    def _aggregate(self, expression: str) -> str:
        xml = self._request(
            {
                "OPERATION": "SELECTAGGREGATES",
                "RESOURCEID": self.cfg.feature_source,
                "CLASSNAME": self.cfg.feature_class,
                "FILTER": self.cfg.filter,
                "COMPUTED_ALIASES": "x",
                "COMPUTED_PROPERTIES": expression,
            }
        )
        value = ET.fromstring(xml).findtext(".//Property/Value")
        if not value:
            raise MunicipalError(f"O geoportal não devolveu {expression}")
        return value

    def extent(self) -> tuple[float, float, float, float]:
        """Caixa (EPSG:3763) de todos os polígonos de edifício do concelho."""
        # O MapGuide devolve o polígono sem fechar o anel (WKT inválido): lêem-se só os
        # números.
        values = [float(v) for v in _NUMBER_RE.findall(self._aggregate("EXTENT(geom)"))]
        if len(values) < 4 or len(values) % 2:
            raise MunicipalError("Extensão do concelho ilegível no geoportal")
        xs, ys = values[0::2], values[1::2]
        return min(xs), min(ys), max(xs), max(ys)


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


def request_boxes(
    bbox: tuple[float, float, float, float], step: float
) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = bbox
    return [
        (float(xa), float(ya), float(min(xa + step, x1)), float(min(ya + step, y1)))
        for xa in np.arange(x0, x1, step)
        for ya in np.arange(y0, y1, step)
    ]


# on_box(pedidos feitos, total de pedidos, polígonos encontrados)
BoxProgress = Callable[[int, int, int], None]


def fetch_municipal_buildings(
    bbox: tuple[float, float, float, float],
    cfg: MunicipalSourceConfig,
    out: Path,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    client: MapGuideClient | None = None,
    *,
    chunk_m: float | None = None,
    on_box: BoxProgress | None = None,
    cancel: threading.Event | None = None,
    full: bool = False,
) -> Path:
    """Descarrega os edifícios municipais da caixa (EPSG:3763) para GeoJSON (cache).

    O ficheiro só é escrito no fim: cancelar ou falhar não deixa uma cópia parcial.
    """
    if out.exists() and not force:
        return out
    client = client or MapGuideClient(cfg)
    boxes = request_boxes(bbox, chunk_m or cfg.chunk_m)
    found: dict[int, MunicipalBuilding] = {}
    for i, box in enumerate(boxes):
        if cancel is not None and cancel.is_set():
            raise MunicipalCancelled("Download da cartografia municipal cancelado")
        if i:
            time.sleep(cfg.delay_s)
        for b in parse_features(client.select(box)):
            found[b.gid] = b
        if on_box:
            on_box(i + 1, len(boxes), len(found))
    if progress:
        progress(
            f"  cartografia municipal: {len(found)} polígonos de edifício ({len(boxes)} pedidos)"
        )
    _write(out, sorted(found.values(), key=lambda b: b.gid), cfg, bbox, full=full)
    return out


def fetch_meta(path: Path) -> Path:
    """Metadados do download (data, caixa, n.º de polígonos), ao lado do GeoJSON."""
    return path.with_name(path.name + ".fetch.json")


def read_fetch_meta(path: Path) -> dict[str, object]:
    meta = fetch_meta(path)
    if not path.exists() or not meta.exists():
        return {}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write(
    out: Path,
    buildings: list[MunicipalBuilding],
    cfg: MunicipalSourceConfig,
    bbox: tuple[float, float, float, float],
    *,
    full: bool = False,
    fetched_at: str | None = None,
) -> None:
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3763"}},
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(b.geometry),
                "properties": {"gid": b.gid, "descr": b.descr},
            }
            for b in buildings
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    meta = {
        "fetched_at": fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "source": cfg.name,
        "bbox": list(bbox),
        "n": len(buildings),
        # full: cópia de todo o concelho; from_full: recorte local dessa cópia.
        "full": full,
        "from_full": fetched_at is not None,
    }
    fetch_meta(out).write_text(json.dumps(meta), encoding="utf-8")
    tmp.replace(out)


def fetch_full_municipality(
    cfg: MunicipalSourceConfig,
    client: MapGuideClient | None = None,
    *,
    on_box: BoxProgress | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Descarrega todos os edifícios do concelho para ``cfg.full_path``.

    A extensão vem do próprio geoportal. Pedidos de ``full_chunk_m`` (1 km), espaçados
    como os outros; a cópia anterior só é substituída no fim.
    """
    client = client or MapGuideClient(cfg)
    extent = client.extent()
    if progress:
        x0, y0, x1, y1 = extent
        n = len(request_boxes(extent, cfg.full_chunk_m))
        progress(
            f"cartografia municipal: concelho com {(x1 - x0) / 1000:.1f} × "
            f"{(y1 - y0) / 1000:.1f} km, {n} pedidos"
        )
    return fetch_municipal_buildings(
        extent,
        cfg,
        cfg.full_path,
        force=True,
        progress=progress,
        client=client,
        chunk_m=cfg.full_chunk_m,
        on_box=on_box,
        cancel=cancel,
        full=True,
    )


def crop_municipal(
    full: Path,
    bbox: tuple[float, float, float, float],
    out: Path,
    cfg: MunicipalSourceConfig,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Recorta da cópia do concelho os polígonos que intersetam a caixa (sem rede).

    Fica com a data do download do concelho: é essa a idade dos dados.
    """
    box = shapely.box(*bbox)
    buildings = [b for b in load_municipal_buildings(full) if b.geometry.intersects(box)]
    fetched_at = read_fetch_meta(full).get("fetched_at")
    _write(
        out,
        buildings,
        cfg,
        bbox,
        fetched_at=str(fetched_at or datetime.now(UTC).isoformat(timespec="seconds")),
    )
    if progress:
        progress(
            f"  cartografia municipal: {len(buildings)} polígonos de edifício "
            "(recortados da cópia do concelho, sem pedidos ao geoportal)"
        )
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
