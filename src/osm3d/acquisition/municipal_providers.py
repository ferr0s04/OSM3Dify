"""Fontes de cartografia municipal: uma interface comum para os vários tipos de geoportal.

Cada câmara (``MunicipalSourceConfig``) tem um ``kind``; :func:`provider_for` devolve o
cliente certo, todos com a mesma interface (EPSG:3763):

- ``extent()``: caixa dos edifícios do concelho;
- ``select_buildings(box)``: edifícios que tocam a caixa (:class:`MunicipalBuilding`);
- ``select_barriers(box, layer, priority)``: muros de uma camada (:class:`MunicipalBarrier`).

Tipos:

- ``mapguide``: :class:`osm3d.acquisition.municipal.MapGuideClient`;
- ``arcgis``: :class:`ArcGISClient` (serviço ArcGIS REST, ``…/MapServer/<n>/query``,
  paginado);
- ``file``: :class:`FileSource` (GeoPackage de dados abertos, p.ex. do dados.gov.pt:
  descarregado uma vez para ``cache_dir`` e lido localmente; tem também
  ``all_buildings()``/``all_barriers()`` para a cópia do concelho de uma só leitura).

Uma câmara nova com um destes geoportais é só configuração (``config/local.yaml``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import certifi
import shapely
from shapely.geometry import LinearRing, LineString, Polygon

from osm3d.acquisition.municipal import (
    _TRANSIENT,
    RETRIES,
    USER_AGENT,
    MapGuideClient,
    MunicipalBarrier,
    MunicipalBuilding,
    MunicipalCancelled,
    MunicipalError,
)
from osm3d.acquisition.municipal_types import (
    carttop_label,
    carttop_tag,
    catalogo_tag,
    strip_code,
)
from osm3d.config import BarrierLayer, MunicipalSourceConfig

log = logging.getLogger(__name__)

BBox = tuple[float, float, float, float]
# Campos do tipo (catálogo) procurados por esta ordem, quando ``type_field`` não é dado.
TYPE_FIELDS = ("descr", "descricao", "layer", "tipo")


class Provider(Protocol):
    cfg: MunicipalSourceConfig

    def extent(self) -> BBox: ...

    def select_buildings(self, box: BBox) -> list[MunicipalBuilding]: ...

    def select_barriers(
        self, box: BBox, layer: BarrierLayer, priority: int
    ) -> list[MunicipalBarrier]: ...


def provider_for(cfg: MunicipalSourceConfig) -> Provider:
    if cfg.kind == "arcgis":
        return ArcGISClient(cfg)
    if cfg.kind == "file":
        return FileSource(cfg)
    return MapGuideClient(cfg)


def int_gid(value: object) -> int:
    """Identificador inteiro estável (os CartTop usam UUID): até 2^48, exato em float."""
    text = str(value)
    if text.lstrip("-").isdigit():
        return int(text)
    return int(hashlib.sha1(text.encode()).hexdigest()[:12], 16)


def _type_value(cfg: MunicipalSourceConfig, attrs: dict[str, Any]) -> str:
    fields = (cfg.type_field,) if cfg.type_field else TYPE_FIELDS
    for f in fields:
        for k, v in attrs.items():
            if k.lower() == f.lower() and v not in (None, ""):
                return str(v)
    return ""


def building_from(
    cfg: MunicipalSourceConfig, gid: object, geom: Polygon, attrs: dict[str, Any]
) -> MunicipalBuilding:
    """Edifício com a tag ``building`` pela classificação da câmara (``cfg.types``)."""
    if cfg.types == "carttop":
        tag, _known = carttop_tag(attrs)
        descr = carttop_label(attrs)
    else:
        raw = _type_value(cfg, attrs)
        tag, descr = catalogo_tag(raw), strip_code(raw)
    return MunicipalBuilding(int_gid(gid), geom, descr, tag=tag, mapped=True, source=cfg.name)


def _str_attrs(attrs: dict[str, Any]) -> dict[str, str | None]:
    return {k: (None if v is None else str(v)) for k, v in attrs.items()}


# --------------------------------------------------------------------- ArcGIS


def esri_polygons(rings: list[list[list[float]]]) -> list[Polygon]:
    """Anéis do JSON do ArcGIS → polígonos (exteriores no sentido horário, buracos ao
    contrário, cada buraco no exterior que o contém)."""
    outers: list[LinearRing] = []
    holes: list[LinearRing] = []
    for coords in rings:
        if len(coords) < 4:
            continue
        ring = LinearRing([(c[0], c[1]) for c in coords])
        (holes if ring.is_ccw else outers).append(ring)
    if not outers:  # orientação inesperada: todos exteriores
        outers, holes = holes, []
    groups: list[tuple[LinearRing, list[LinearRing]]] = [(o, []) for o in outers]
    for h in holes:
        pt = Polygon(h).representative_point()
        for o, inner in groups:
            if Polygon(o).contains(pt):
                inner.append(h)
                break
    out: list[Polygon] = []
    for o, inner in groups:
        p = shapely.make_valid(Polygon(o, inner))
        out += [g for g in getattr(p, "geoms", [p]) if isinstance(g, Polygon) and g.area > 0]
    return out


class ArcGISClient:
    """Pedidos ``query`` a uma camada ArcGIS REST (MapServer/FeatureServer), paginados."""

    def __init__(
        self,
        cfg: MunicipalSourceConfig,
        timeout_s: float = 90.0,
        opener: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cfg = cfg
        self.timeout_s = timeout_s
        tls = ssl.create_default_context(cafile=certifi.where())
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls))
        self._opener.addheaders = [("User-Agent", USER_AGENT)]
        self._open = opener or self._http
        self._info: dict[str, dict[str, Any]] = {}

    def _http(self, url: str) -> bytes:
        with self._opener.open(url, timeout=self.timeout_s) as resp:
            return bytes(resp.read())

    def _json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        full = f"{url}?{urllib.parse.urlencode({**params, 'f': 'json'})}"
        for attempt in range(RETRIES):
            try:
                data = json.loads(self._open(full))
                break
            except _TRANSIENT:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(2.0 * (attempt + 1))
        if not isinstance(data, dict):
            raise MunicipalError(f"Resposta inesperada do ArcGIS: {url}")
        if "error" in data:
            err = data["error"]
            raise MunicipalError(f"ArcGIS: {err.get('message')} ({err.get('code')}) em {url}")
        return data

    def layer_info(self, url: str) -> dict[str, Any]:
        if url not in self._info:
            self._info[url] = self._json(url, {})
        return self._info[url]

    def oid_field(self, url: str) -> str:
        """Campo do identificador (o MapServer nem sempre declara ``objectIdField``)."""
        info = self.layer_info(url)
        if info.get("objectIdField"):
            return str(info["objectIdField"])
        for f in info.get("fields") or []:
            if f.get("type") == "esriFieldTypeOID":
                return str(f["name"])
        return "objectid"

    def query(self, url: str, box: BBox | None, where: str) -> list[dict[str, Any]]:
        """Elementos da camada (geometria em EPSG:3763), todas as páginas."""
        info = self.layer_info(url)
        page = int(info.get("maxRecordCount") or 1000)
        paged = bool((info.get("advancedQueryCapabilities") or {}).get("supportsPagination"))
        params: dict[str, Any] = {
            "where": where or "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 3763,
        }
        if box is not None:
            params |= {
                "geometry": ",".join(f"{v:.3f}" for v in box),
                "geometryType": "esriGeometryEnvelope",
                "inSR": 3763,
                "spatialRel": "esriSpatialRelIntersects",
            }
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            extra = {"resultOffset": offset, "resultRecordCount": page} if paged else {}
            data = self._json(f"{url}/query", params | extra)
            feats = data.get("features", [])
            out += feats
            if not (data.get("exceededTransferLimit") and paged and feats):
                if data.get("exceededTransferLimit") and not paged:
                    raise MunicipalError(
                        f"A camada devolve no máximo {page} elementos por pedido e não "
                        "pagina: reduzir chunk_m"
                    )
                return out
            offset += len(feats)
            time.sleep(self.cfg.delay_s)

    def extent(self) -> BBox:
        data = self._json(
            f"{self.cfg.layer_url}/query",
            {"where": self.cfg.filter or "1=1", "returnExtentOnly": "true", "outSR": 3763},
        )
        e = data.get("extent") or {}
        try:
            return (float(e["xmin"]), float(e["ymin"]), float(e["xmax"]), float(e["ymax"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise MunicipalError("Extensão da camada ilegível no ArcGIS") from exc

    def select_buildings(self, box: BBox) -> list[MunicipalBuilding]:
        url = self.cfg.layer_url
        oid_field = self.oid_field(url).lower()
        out: list[MunicipalBuilding] = []
        for f in self.query(url, box, self.cfg.filter):
            attrs = f.get("attributes") or {}
            polys = esri_polygons((f.get("geometry") or {}).get("rings") or [])
            if not polys:
                continue
            oid = next((v for k, v in attrs.items() if k.lower() == oid_field), None)
            if oid is None:  # sem identificador: o da posição (estável entre pedidos)
                c = polys[0].centroid
                oid = f"{c.x:.1f},{c.y:.1f}"
            for i, poly in enumerate(sorted(polys, key=lambda p: -p.area)):
                gid = oid if i == 0 else f"{oid}:{i}"  # vários exteriores: um edifício cada
                out.append(building_from(self.cfg, gid, poly, attrs))
        return out

    def select_barriers(
        self, box: BBox, layer: BarrierLayer, priority: int
    ) -> list[MunicipalBarrier]:
        url = layer.feature_class
        oid_field = self.oid_field(url).lower()
        out: list[MunicipalBarrier] = []
        for f in self.query(url, box, layer.filter):
            attrs = _str_attrs(f.get("attributes") or {})
            tags = layer.tags_for(attrs)
            if tags is None:
                continue
            oid = next((v for k, v in attrs.items() if k.lower() == oid_field), "?")
            for i, path in enumerate((f.get("geometry") or {}).get("paths") or []):
                line = LineString([(c[0], c[1]) for c in path]) if len(path) >= 2 else None
                if line is not None and line.length > 0.2:
                    out.append(
                        MunicipalBarrier(f"{priority}:{oid}:{i}", layer.label, line, tags, priority)
                    )
        return out


# ------------------------------------------------------------------- ficheiro

# on_bytes(descarregados, total ou None)
BytesProgress = Callable[[int, int | None], None]


class FileSource:
    """GeoPackage (ou outro formato do GDAL) de dados abertos, lido localmente.

    Se ``file_url`` é um endereço, descarrega-se uma vez para ``cache_dir`` (retomável:
    um download interrompido continua do ponto em que parou).
    """

    def __init__(self, cfg: MunicipalSourceConfig) -> None:
        self.cfg = cfg

    @property
    def remote(self) -> bool:
        return self.cfg.file_url.startswith(("http://", "https://"))

    @property
    def path(self) -> Path:
        if not self.remote:
            return Path(self.cfg.file_url)
        name = Path(urllib.parse.urlparse(self.cfg.file_url).path).name or "dados.gpkg"
        return self.cfg.cache_dir / f"{self.cfg.key}-{name}"

    def ensure(
        self,
        on_bytes: BytesProgress | None = None,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        """O ficheiro local (descarregado se ainda não está)."""
        path = self.path
        if path.exists():
            return path
        if not self.remote:
            raise MunicipalError(f"Ficheiro da cartografia municipal em falta: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        done = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(self.cfg.file_url, headers={"User-Agent": USER_AGENT})
        if done:
            req.add_header("Range", f"bytes={done}-")
        if progress:
            progress(f"cartografia municipal: a descarregar {self.cfg.file_url}")
        tls = ssl.create_default_context(cafile=certifi.where())
        try:
            with urllib.request.urlopen(req, timeout=120, context=tls) as resp:
                if done and resp.status != 206:  # o servidor não retoma: do início
                    done = 0
                    part.unlink(missing_ok=True)
                size = resp.headers.get("Content-Length")
                total = done + int(size) if size else None
                with part.open("ab") as f:
                    while chunk := resp.read(1 << 20):
                        if cancel is not None and cancel.is_set():
                            raise MunicipalCancelled("Download da cartografia municipal cancelado")
                        f.write(chunk)
                        done += len(chunk)
                        if on_bytes:
                            on_bytes(done, total)
        except urllib.error.URLError as exc:
            raise MunicipalError(
                f"Não foi possível descarregar {self.cfg.file_url}: {exc}"
            ) from exc
        shutil.move(str(part), str(path))
        return path

    def _read(self, layer: str, box: BBox | None) -> Any:
        import pyogrio

        df = pyogrio.read_dataframe(self.ensure(), layer=layer, bbox=box)
        if df.crs is not None and df.crs.to_epsg() != 3763:
            df = df.to_crs(3763)
        return df

    def extent(self) -> BBox:
        import pyogrio

        info = pyogrio.read_info(self.ensure(), layer=self.cfg.layer)
        bounds = info.get("total_bounds")
        if bounds is None or (info.get("crs") or "").upper() not in ("EPSG:3763", ""):
            bounds = self._read(self.cfg.layer, None).total_bounds
        return tuple(float(v) for v in bounds)  # type: ignore[return-value]

    def _buildings(self, box: BBox | None) -> list[MunicipalBuilding]:
        df = self._read(self.cfg.layer, box)
        out: list[MunicipalBuilding] = []
        cols = [c for c in df.columns if c != "geometry"]
        id_col = next((c for c in ("identificador", "gid", "id", "objectid") if c in cols), None)
        for i, (geom, values) in enumerate(
            zip(df.geometry, df[cols].itertuples(index=False), strict=True)
        ):
            if geom is None or geom.is_empty:
                continue
            geom = shapely.make_valid(shapely.force_2d(geom))
            polys = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon) and g.area]
            if not polys:  # edifícios em ponto (pequenos): sem contorno
                continue
            attrs = {c: (None if _is_null(v) else v) for c, v in zip(cols, values, strict=True)}
            gid = attrs.get(id_col) if id_col else i
            out.append(building_from(self.cfg, gid, max(polys, key=lambda p: p.area), attrs))
        return out

    def select_buildings(self, box: BBox) -> list[MunicipalBuilding]:
        return self._buildings(box)

    def all_buildings(self) -> list[MunicipalBuilding]:
        return self._buildings(None)

    def _barriers(
        self, box: BBox | None, layer: BarrierLayer, priority: int
    ) -> list[MunicipalBarrier]:
        df = self._read(layer.feature_class, box)
        cols = [c for c in df.columns if c != "geometry"]
        out: list[MunicipalBarrier] = []
        for i, (geom, values) in enumerate(
            zip(df.geometry, df[cols].itertuples(index=False), strict=True)
        ):
            if geom is None or geom.is_empty:
                continue
            attrs = {
                c: (None if _is_null(v) else str(v)) for c, v in zip(cols, values, strict=True)
            }
            tags = layer.tags_for(attrs)
            if tags is None:
                continue
            gid = attrs.get("identificador") or attrs.get("gid") or i
            lines = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, LineString)]
            for j, line in enumerate(lines):
                line = shapely.force_2d(line)
                if line.length > 0.2:
                    key = f"{priority}:{int_gid(gid)}:{j}"
                    out.append(MunicipalBarrier(key, layer.label, line, tags, priority))
        return out

    def select_barriers(
        self, box: BBox, layer: BarrierLayer, priority: int
    ) -> list[MunicipalBarrier]:
        return self._barriers(box, layer, priority)

    def all_barriers(self, layer: BarrierLayer, priority: int) -> list[MunicipalBarrier]:
        return self._barriers(None, layer, priority)


def _is_null(v: object) -> bool:
    try:
        return v is None or v != v  # NaN
    except Exception:
        return False
