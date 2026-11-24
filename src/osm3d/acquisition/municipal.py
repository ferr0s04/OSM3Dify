"""Cartografia municipal de edifícios e muros: formatos, cache e o geoportal MapGuide.

Os outros tipos de fonte (ArcGIS REST, ficheiros de dados abertos) estão em
:mod:`osm3d.acquisition.municipal_providers`, com a mesma interface
(``extent``/``select_buildings``/``select_barriers``); os downloads e recortes deste
módulo servem para todos. A tradução dos tipos para ``building=*`` está em
:mod:`osm3d.acquisition.municipal_types`.

**MapGuide.**

Muitos geoportais municipais são visualizadores sobre MapGuide. Mesmo quando os
serviços OGC (WMS/WFS) não publicam camadas, a API HTTP do MapGuide (``mapagent.fcgi``)
devolve os polígonos em vetor (``SELECTFEATURES``), com a sessão anónima que a página
do visualizador cria. O geoportal, a camada e o filtro configuram-se em
``sources.municipal`` (``config.yaml``).

Camada esperada: cartografia topográfica vetorial homologada (1:5000/1:10000), com os
atributos ``gid``, ``geom`` e ``descr`` (tipo: "Vivenda Casa", "Garagem",
"Telheiro, Alpendre"…; ver :data:`BUILDING_TAGS`). Sem altura, pisos nem telhado.

**Só com autorização do município** (da responsabilidade de quem configura a fonte).
Não é um serviço publicado: a sessão é obtida da página do visualizador e o município
pode mudar o acesso sem aviso. Os pedidos
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
from typing import Any

import certifi
import numpy as np
import shapely
from shapely import wkt
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

from osm3d.acquisition.municipal_types import CATALOGO, catalogo_tag
from osm3d.config import BarrierLayer, MunicipalSourceConfig

log = logging.getLogger(__name__)

USER_AGENT = "osm3d-building-generator/0.1"
_SESSION_RE = re.compile(r"SESSION=([^&\"'\s]+)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

# Tipo municipal → tag ``building`` (classificação do catálogo); ver municipal_types.
BUILDING_TAGS = CATALOGO

# Erros de rede passageiros num pedido: repete-se com pausa crescente.
_TRANSIENT = (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError)
RETRIES = 3


@dataclass(frozen=True)
class MunicipalBuilding:
    gid: int
    geometry: Polygon  # EPSG:3763
    descr: str
    # Tag já traduzida pela fonte (``mapped``); sem ela, a do catálogo pela descrição.
    tag: str | None = None
    mapped: bool = False
    source: str = ""  # câmara (nome), com várias fontes

    @property
    def building_tag(self) -> str | None:
        """Tag ``building`` do OSM, ou ``None`` se o tipo não é para mapear/desconhecido."""
        return self.tag if self.mapped else catalogo_tag(self.descr)


class MunicipalError(RuntimeError):
    pass


class MunicipalCancelled(MunicipalError):
    pass


class MapGuideParsing:
    """Interface comum das fontes (ver municipal_providers) sobre ``select`` do MapGuide."""

    cfg: MunicipalSourceConfig

    def select(
        self,
        box: tuple[float, float, float, float],
        feature_class: str | None = None,
        filter: str | None = None,
    ) -> bytes:
        raise NotImplementedError

    def select_buildings(self, box: tuple[float, float, float, float]) -> list[MunicipalBuilding]:
        name = getattr(getattr(self, "cfg", None), "name", "")
        return [
            MunicipalBuilding(b.gid, b.geometry, b.descr, source=name)
            for b in parse_features(self.select(box))
        ]

    def select_barriers(
        self, box: tuple[float, float, float, float], layer: BarrierLayer, priority: int
    ) -> list[MunicipalBarrier]:
        return parse_barriers(self.select(box, layer.feature_class, layer.filter), layer, priority)


class MapGuideClient(MapGuideParsing):
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

    def select(
        self,
        box: tuple[float, float, float, float],
        feature_class: str | None = None,
        filter: str | None = None,
    ) -> bytes:
        """XML dos elementos da camada (por omissão, a dos edifícios) na caixa (EPSG:3763)."""
        x0, y0, x1, y1 = box
        ring = f"{x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}"
        flt = f"geom ENVELOPEINTERSECTS GeomFromText('POLYGON(({ring}))')"
        extra = self.cfg.filter if feature_class is None else (filter or "")
        if extra:
            flt = f"{extra} AND {flt}"
        return self._request(
            {
                "OPERATION": "SELECTFEATURES",
                "RESOURCEID": self.cfg.feature_source,
                "CLASSNAME": feature_class or self.cfg.feature_class,
                "FILTER": flt,
            }
        )

    def _aggregate(self, expression: str) -> str:
        xml = self._request(
            {
                "OPERATION": "SELECTAGGREGATES",
                "RESOURCEID": self.cfg.feature_source,
                "CLASSNAME": self.cfg.feature_class,
                **({"FILTER": self.cfg.filter} if self.cfg.filter else {}),
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
    client: Any = None,
    *,
    chunk_m: float | None = None,
    on_box: BoxProgress | None = None,
    cancel: threading.Event | None = None,
    full: bool = False,
    log: BoxLog | None = None,
) -> Path:
    """Descarrega os edifícios municipais da caixa (EPSG:3763) para GeoJSON (cache).

    O ficheiro só é escrito no fim: cancelar ou falhar não deixa uma cópia parcial.
    """
    if out.exists() and not force:
        return out
    client = client or source_client(cfg)
    boxes = request_boxes(bbox, chunk_m or cfg.chunk_m)
    found: dict[int, MunicipalBuilding] = {}
    asked = False
    for i, box in enumerate(boxes):
        if cancel is not None and cancel.is_set():
            raise MunicipalCancelled("Download da cartografia municipal cancelado")
        if log is not None and i in log.done:  # já feito num download interrompido
            items = [_building_from_item(d) for d in log.done[i]]
        else:
            if asked:
                time.sleep(cfg.delay_s)
            asked = True
            items = client.select_buildings(box)
            if log is not None:
                log.add(i, [_building_item(b) for b in items])
        for b in items:
            found[b.gid] = b
        if on_box:
            on_box(i + 1, len(boxes), len(found))
    if progress:
        progress(
            f"  cartografia municipal: {len(found)} polígonos de edifício ({len(boxes)} pedidos)"
        )
    _write(out, sorted(found.values(), key=lambda b: b.gid), cfg, bbox, full=full)
    return out


class BoxLog:
    """Pedidos já feitos de um download longo, guardados em disco à medida que chegam.

    Se o download for interrompido (servidor reiniciado, cancelado, erro de rede), o
    seguinte com os mesmos parâmetros (``header``) retoma: os pedidos feitos não se
    repetem. Uma linha por pedido; a última pode ter ficado a meio e é ignorada.
    """

    def __init__(self, path: Path, header: dict[str, Any]) -> None:
        self.path = path
        self.done: dict[int, list[dict[str, Any]]] = {}
        header = json.loads(json.dumps(header))  # como fica no ficheiro (listas, não tuplos)
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            if lines and json.loads(lines[0]) == header:
                for line in lines[1:]:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        break
                    self.done[int(rec["i"])] = rec["items"]
        except (OSError, ValueError, KeyError):
            self.done = {}
        if not self.done:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    def add(self, i: int, items: list[dict[str, Any]]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"i": i, "items": items}) + "\n")

    def finish(self) -> None:
        self.path.unlink(missing_ok=True)

    @staticmethod
    def status(path: Path) -> dict[str, int] | None:
        """Pedidos feitos e total de um download interrompido (``None``: não há)."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            total = int(json.loads(lines[0]).get("total", 0))
        except (OSError, ValueError, IndexError, AttributeError):
            return None
        return {"done": max(0, len(lines) - 1), "total": total}


def _building_item(b: MunicipalBuilding) -> dict[str, Any]:
    d: dict[str, Any] = {"gid": b.gid, "descr": b.descr, "geom": mapping(b.geometry)}
    if b.mapped:
        d |= {"building": b.tag, "mapped": True}
    if b.source:
        d["source"] = b.source
    return d


def _building_from_item(d: dict[str, Any]) -> MunicipalBuilding:
    return MunicipalBuilding(
        int(d["gid"]),
        shape(d["geom"]),
        str(d.get("descr") or ""),
        tag=d.get("building"),
        mapped=bool(d.get("mapped")),
        source=str(d.get("source") or ""),
    )


def source_client(cfg: MunicipalSourceConfig) -> Any:
    """O cliente da fonte da câmara (MapGuide, ArcGIS, ficheiro)."""
    from osm3d.acquisition.municipal_providers import provider_for

    return provider_for(cfg)


def _barrier_item(b: MunicipalBarrier) -> dict[str, Any]:
    return {"key": b.key, "layer": b.layer, "tags": b.tags, "geom": mapping(b.geometry)}


def progress_path(out: Path) -> Path:
    """Ficheiro de progresso (para retomar) de um download do concelho."""
    return out.with_name(out.name + ".progress.jsonl")


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
    meta = {
        "fetched_at": fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "source": cfg.name,
        "bbox": list(bbox),
        "n": len(buildings),
        # full: cópia de todo o concelho; from_full: recorte local dessa cópia.
        "full": full,
        "from_full": fetched_at is not None,
    }
    write_municipal(out, buildings, meta)


def write_municipal(out: Path, buildings: list[MunicipalBuilding], meta: dict[str, Any]) -> None:
    """GeoJSON (EPSG:3763) dos edifícios e os metadados do download ao lado."""
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3763"}},
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(b.geometry),
                "properties": {k: v for k, v in _building_item(b).items() if k != "geom"},
            }
            for b in buildings
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    fetch_meta(out).write_text(json.dumps(meta), encoding="utf-8")
    tmp.replace(out)


def fetch_full_municipality(
    cfg: MunicipalSourceConfig,
    client: Any = None,
    *,
    on_box: BoxProgress | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Descarrega todo o concelho: edifícios (``cfg.full_path``) e, com camadas de muros
    configuradas, muros e vedações (``cfg.full_barriers_path``).

    A extensão vem do próprio geoportal. Pedidos de ``full_chunk_m`` (1 km), espaçados
    como os outros; as cópias anteriores só são substituídas no fim. Cada pedido feito
    fica em disco: um download interrompido retoma onde parou.
    """
    client = client or source_client(cfg)
    if hasattr(client, "all_buildings"):  # ficheiro: descarrega-se e lê-se de uma vez
        return _full_from_file(cfg, client, on_box=on_box, cancel=cancel, progress=progress)
    extent = client.extent()
    step = cfg.full_chunk_m
    n_boxes = len(request_boxes(extent, step))
    n_layers = len(cfg.barriers)
    total = n_boxes * (1 + n_layers)
    if progress:
        x0, y0, x1, y1 = extent
        progress(
            f"cartografia municipal: concelho com {(x1 - x0) / 1000:.1f} × "
            f"{(y1 - y0) / 1000:.1f} km, {total} pedidos"
        )
    header = {
        "bbox": list(extent),
        "step": step,
        "total": n_boxes,
        "source": cfg.feature_source or cfg.layer_url,
    }
    blog = BoxLog(
        progress_path(cfg.full_path),
        header | {"layer": cfg.feature_class, "filter": cfg.filter},
    )
    found = {"buildings": 0}

    def buildings_box(done: int, _total: int, n: int) -> None:
        found["buildings"] = n
        if on_box:
            on_box(done, total, n)

    path = fetch_municipal_buildings(
        extent,
        cfg,
        cfg.full_path,
        force=True,
        progress=progress,
        client=client,
        chunk_m=step,
        on_box=buildings_box,
        cancel=cancel,
        full=True,
        log=blog,
    )
    logs = [blog]
    if cfg.barriers:
        layers = [[layer.feature_class, layer.filter] for layer in cfg.barriers]
        wlog = BoxLog(
            progress_path(cfg.full_barriers_path),
            header | {"total": n_boxes * n_layers, "layers": layers},
        )
        logs.append(wlog)

        def barriers_box(done: int, _total: int, n: int) -> None:
            if on_box:
                on_box(n_boxes + done, total, found["buildings"] + n)

        fetch_municipal_barriers(
            extent,
            cfg,
            cfg.full_barriers_path,
            force=True,
            progress=progress,
            client=client,
            chunk_m=step,
            on_box=barriers_box,
            cancel=cancel,
            full=True,
            log=wlog,
        )
    for done_log in logs:  # só no fim de tudo: uma interrupção nos muros não refaz os edifícios
        done_log.finish()
    return path


def _full_from_file(
    cfg: MunicipalSourceConfig,
    client: Any,
    *,
    on_box: BoxProgress | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Cópia do concelho a partir de um ficheiro de dados abertos (edifícios e muros)."""

    def on_bytes(done: int, total: int | None) -> None:  # progresso em MB
        if on_box:
            mb = 1 << 20
            on_box(done // mb, (total // mb) if total else done // mb + 1, 0)

    client.ensure(on_bytes=on_bytes, cancel=cancel, progress=progress)
    if progress:
        progress("cartografia municipal: a ler os edifícios do ficheiro")
    buildings = client.all_buildings()
    extent = client.extent()
    _write(cfg.full_path, sorted(buildings, key=lambda b: b.gid), cfg, extent, full=True)
    if cfg.barriers:
        found: dict[str, MunicipalBarrier] = {}
        for priority, layer in enumerate(cfg.barriers):
            for b in client.all_barriers(layer, priority):
                found[b.key] = b
        write_barriers(cfg.full_barriers_path, found, cfg, extent, full=True)
    if progress:
        progress(f"cartografia municipal: {len(buildings)} polígonos de edifício")
    return cfg.full_path


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
            out.append(_building_from_item({**f["properties"], "geom": f["geometry"]}))
    return out


# ------------------------------------------------------------- muros e vedações


@dataclass(frozen=True)
class MunicipalBarrier:
    """Muro/vedação de uma camada municipal (uma linha simples, EPSG:3763)."""

    key: str  # "<n.º da camada>:<gid>:<parte>", estável entre descargas
    layer: str  # etiqueta da camada
    geometry: LineString
    tags: dict[str, str]
    priority: int = 0  # índice da camada em ``barriers`` (0 = a que manda)


def parse_barriers(xml: bytes, layer: BarrierLayer, priority: int) -> list[MunicipalBarrier]:
    """``FeatureSet`` de linhas → muros com as tags das regras (as outras linhas saem)."""
    out: list[MunicipalBarrier] = []
    for feat in ET.fromstring(xml).iter("Feature"):
        props = {p.findtext("Name") or "": p.findtext("Value") for p in feat.iter("Property")}
        tags = layer.tags_for(props)
        if tags is None or not props.get("geom"):
            continue
        gid = props.get("gid") or props.get("id") or props.get("objectid") or "?"
        geom = wkt.loads(str(props["geom"]))
        lines = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, LineString)]
        for i, line in enumerate(lines):
            if line.length > 0.2:
                key = f"{priority}:{gid}:{i}"
                out.append(MunicipalBarrier(key, layer.label, line, tags, priority))
    return out


def fetch_municipal_barriers(
    bbox: tuple[float, float, float, float],
    cfg: MunicipalSourceConfig,
    out: Path,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    client: MapGuideClient | None = None,
    chunk_m: float | None = None,
    on_box: BoxProgress | None = None,
    cancel: threading.Event | None = None,
    full: bool = False,
    log: BoxLog | None = None,
) -> Path:
    """Muros e vedações das camadas ``cfg.barriers`` na caixa (EPSG:3763), em GeoJSON.

    ``log``: pedidos já feitos (download do concelho retomável, ver :class:`BoxLog`).
    """
    if out.exists() and not force:
        return out
    if not cfg.barriers:
        raise MunicipalError("Sem camadas de muros configuradas (sources.municipal.barriers)")
    client = client or source_client(cfg)
    boxes = request_boxes(bbox, chunk_m or cfg.chunk_m * 2)
    found: dict[str, MunicipalBarrier] = {}
    asked = False
    for priority, layer in enumerate(cfg.barriers):
        for i, box in enumerate(boxes):
            if cancel is not None and cancel.is_set():
                raise MunicipalCancelled("Download da cartografia municipal cancelado")
            k = priority * len(boxes) + i
            if log is not None and k in log.done:
                items = [
                    MunicipalBarrier(d["key"], d["layer"], shape(d["geom"]), d["tags"], priority)
                    for d in log.done[k]
                ]
            else:
                if asked:
                    time.sleep(cfg.delay_s)
                asked = True
                items = client.select_barriers(box, layer, priority)
                if log is not None:
                    log.add(k, [_barrier_item(b) for b in items])
            for b in items:
                found[b.key] = b
            if on_box:
                on_box(k + 1, len(boxes) * len(cfg.barriers), len(found))
        if progress:
            n = sum(b.priority == priority for b in found.values())
            progress(f"  muros municipais ({layer.label or layer.feature_class}): {n} linhas")
    return write_barriers(out, found, cfg, bbox, full=full)


def write_barriers(
    out: Path,
    found: dict[str, MunicipalBarrier],
    cfg: MunicipalSourceConfig,
    bbox: tuple[float, float, float, float],
    *,
    full: bool = False,
) -> Path:
    features = [
        {
            "type": "Feature",
            "geometry": mapping(b.geometry),
            "properties": {"key": b.key, "layer": b.layer, "priority": b.priority, **b.tags},
        }
        for b in sorted(found.values(), key=lambda b: b.key)
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "osm3d": {
            "source": cfg.name,
            "layers": barrier_layers(cfg),
            "bbox": list(bbox),
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "full": full,  # cópia de todo o concelho
            "from_full": False,
            "n": len(features),
        },
    }
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out)
    return out


def barrier_layers(cfg: MunicipalSourceConfig) -> list[str]:
    return [layer.label or layer.feature_class for layer in cfg.barriers]


def full_barriers_usable(cfg: MunicipalSourceConfig) -> bool:
    """Há uma cópia dos muros do concelho feita com as camadas configuradas agora?"""
    path = cfg.full_barriers_path
    if not cfg.barriers or not path.exists():
        return False
    meta = barriers_meta(path)
    return bool(meta.get("full")) and meta.get("layers") == barrier_layers(cfg)


def crop_barriers(
    full: Path,
    bbox: tuple[float, float, float, float],
    out: Path,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Muros da caixa, recortados da cópia do concelho (sem pedidos ao geoportal)."""
    data = json.loads(full.read_text(encoding="utf-8"))
    area = shapely.box(*bbox)
    features = [f for f in data.get("features", []) if shape(f["geometry"]).intersects(area)]
    meta = dict(data.get("osm3d", {}))
    meta.update(bbox=list(bbox), full=False, from_full=True, n=len(features))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_text(
        json.dumps({"type": "FeatureCollection", "features": features, "osm3d": meta}),
        encoding="utf-8",
    )
    tmp.replace(out)
    if progress:
        progress(
            f"  muros municipais: {len(features)} linhas (recortadas da cópia do concelho, "
            "sem pedidos ao geoportal)"
        )
    return out


def load_municipal_barriers(path: Path) -> list[MunicipalBarrier]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for f in data.get("features", []):
        p = dict(f["properties"])
        key, layer, priority = p.pop("key"), p.pop("layer", ""), int(p.pop("priority", 0))
        out.append(MunicipalBarrier(key, layer, shape(f["geometry"]), p, priority))
    return out


def barriers_meta(path: Path) -> dict[str, object]:
    try:
        meta = json.loads(path.read_text(encoding="utf-8")).get("osm3d", {})
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}
