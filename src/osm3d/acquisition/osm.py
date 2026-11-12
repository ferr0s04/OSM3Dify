"""Download de edifícios OSM via Overpass (``out meta``).

Usa-se Overpass diretamente (e não OSMnx) porque é preciso guardar ``id``,
``version``, nós e relações (multipolígonos com pátios) para poder reescrever
no OSM sem conflitos. Para áreas grandes (concelho): extrato PBF da Geofabrik +
pyosmium (extra ``osm``).
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

OVERPASS_QUERY = """
[out:xml][timeout:{timeout}];
{area_stmt}(
  way["building"]{flt};
  relation["building"]["type"="multipolygon"]{flt};
  way["building:part"]{flt};
);
(._;>;);
out meta;
"""

# Overpass: id de área derivado de uma relação = 3_600_000_000 + id da relação.
_AREA_OFFSET = 3_600_000_000

# Instâncias públicas alternativas, usadas se a principal falhar (502/504 são frequentes).
FALLBACK_OVERPASS_URLS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Os servidores públicos exigem um User-Agent próprio: sem ele, overpass-api.de responde
# 406 e overpass.private.coffee 429 (verificado em 2026-11-12). Os browsers também são
# recusados (406): este é um cliente automatizado e identifica-se como tal.
USER_AGENT = "osm3d/0.1 (OSM 3D Building Generator)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}

# Servidor sem capacidade ("All query workers are busy and the request queue is full"
# chega como 502) ou limite de pedidos: tentar outro servidor e, depois, esperar.
_BUSY_STATUS = {429, 502, 503, 504}
MAX_BACKOFF_S = 120.0


class OverpassError(RuntimeError):
    pass


def query_timeout_s(bbox: tuple[float, float, float, float]) -> int:
    """``[timeout:]`` a declarar: proporcional à área (60 s + 5 s/km², máx. 300 s).

    Um valor alto em áreas pequenas só prende o cliente quando o servidor fica pendurado.
    """
    w, s, e, n = bbox
    km2 = (e - w) * 111.32 * math.cos(math.radians((s + n) / 2)) * (n - s) * 110.57
    return int(min(300, 60 + 5 * max(km2, 0)))


def build_query(
    bbox: tuple[float, float, float, float],
    osm_relation_id: int | None = None,
    timeout_s: int | None = None,
) -> str:
    """Query Overpass para uma bbox WGS84 (min_lon, min_lat, max_lon, max_lat).

    Com ``osm_relation_id``, filtra também pelo polígono do limite administrativo.
    """
    w, s, e, n = bbox
    flt = f"({s},{w},{n},{e})"
    area_stmt = ""
    if osm_relation_id is not None:
        area_stmt = f"area({_AREA_OFFSET + osm_relation_id})->.a;\n"
        flt = f"(area.a){flt}"
    timeout = timeout_s or query_timeout_s(bbox)
    return OVERPASS_QUERY.format(area_stmt=area_stmt, flt=flt, timeout=timeout)


def _host(url: str) -> str:
    return urlparse(url).hostname or url


def _retry_after(resp: requests.Response) -> float | None:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def overpass_post(
    query: str,
    overpass_url: str,
    validate: Callable[[requests.Response], Any],
    *,
    read_timeout_s: float,
    rounds: int = 5,
    base_pause_s: float = 5.0,
    progress: Progress | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """POST ao Overpass com rotação de servidores e backoff exponencial.

    Em cada ronda tenta todos os servidores. "Ocupado" (429/502/503/504) passa logo ao
    seguinte; se todos estiverem ocupados, espera ``base·2^ronda`` s (com jitter, ou o
    ``Retry-After``) antes da ronda seguinte. Um servidor que deixa o pedido pendurado
    (timeout) sai da rotação. Erros da própria consulta (400) não são repetidos.
    ``validate(resp)`` devolve o resultado ou levanta :class:`OverpassError`.
    """
    urls = [overpass_url, *[u for u in FALLBACK_OVERPASS_URLS if u != overpass_url]]
    errors: list[str] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    def note(msg: str) -> None:
        errors.append(msg)
        log.warning("Overpass: %s", msg)
        if progress:
            progress(f"  Overpass: {msg}")

    for rnd in range(rounds):
        wait: float | None = None
        for url in list(urls):
            host = _host(url)
            try:
                resp = session.post(url, data={"data": query}, timeout=(15, read_timeout_s))
            except requests.Timeout:
                note(f"{host} não respondeu em {read_timeout_s:.0f} s: deixa de ser usado")
                if len(urls) > 1:
                    urls.remove(url)
                continue
            except requests.RequestException as exc:
                note(f"{host}: {type(exc).__name__}")
                continue
            if resp.status_code in _BUSY_STATUS:
                note(f"{host} ocupado ({resp.status_code}): a tentar outro servidor")
                if (ra := _retry_after(resp)) is not None:
                    wait = max(wait or 0, ra)
                continue
            if resp.status_code == 400:
                raise OverpassError(f"Consulta inválida ({host}): {resp.text[:300]}")
            if resp.status_code != 200:
                note(f"{host}: HTTP {resp.status_code}")
                continue
            try:
                return validate(resp)
            except (OverpassError, ValueError) as exc:
                note(f"{host}: {exc}")
        if rnd + 1 < rounds:
            backoff = base_pause_s * 2**rnd * random.uniform(0.8, 1.2)
            pause = min(MAX_BACKOFF_S, max(backoff, wait or 0))
            if progress:
                progress(
                    f"  Overpass: todos ocupados, nova tentativa em {pause:.0f} s "
                    f"(ronda {rnd + 2} de {rounds})"
                )
            sleep(pause)
    raise OverpassError(
        "Servidores Overpass sem capacidade ou sem resposta; tentar mais tarde.\n"
        + "\n".join(errors[-9:])
    )


def _check_payload(content: bytes) -> None:
    head = content[:4096]
    if not head.lstrip().startswith(b"<?xml"):
        raise OverpassError("Resposta não é XML OSM")
    # Erros de execução do Overpass chegam com HTTP 200 e um <remark>.
    if b"<remark>" in content[-4096:] and b"error" in content[-4096:].lower():
        raise OverpassError("Overpass devolveu um erro de execução (<remark>)")


def fetch_buildings(
    bbox: tuple[float, float, float, float],
    overpass_url: str,
    out: Path,
    osm_relation_id: int | None = None,
    *,
    force: bool = False,
    progress: Progress | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Descarrega os edifícios da área para um ficheiro ``.osm`` (em cache).

    Se ``out`` já existir, não volta a descarregar, salvo com ``force=True``.
    """
    if out.exists() and not force:
        log.info("Em cache: %s", out)
        return out

    timeout = query_timeout_s(bbox)
    query = build_query(bbox, osm_relation_id, timeout)

    def validate(resp: requests.Response) -> bytes:
        _check_payload(resp.content)
        return resp.content

    # Margem para a espera na fila do servidor, além da execução da consulta.
    content = overpass_post(
        query, overpass_url, validate, read_timeout_s=timeout + 60, progress=progress, sleep=sleep
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(out)
    return out


def _overpass_json(
    query: str,
    overpass_url: str,
    *,
    timeout_s: float,
    progress: Progress | None = None,
) -> dict:  # type: ignore[type-arg]
    def validate(resp: requests.Response) -> Any:
        data = resp.json()
        if "elements" not in data:
            raise OverpassError(data.get("remark", "resposta sem elementos"))
        return data

    result: dict = overpass_post(  # type: ignore[type-arg]
        query, overpass_url, validate, read_timeout_s=timeout_s + 60, progress=progress
    )
    return result


NOMINATIM_LOOKUP_URL = "https://nominatim.openstreetmap.org/lookup"


class OsmLookupError(RuntimeError):
    pass


def relation_boundary(relation_id: int, *, timeout_s: float = 20.0) -> tuple[str | None, Any]:
    """Nome e polígono WGS84 (GeoJSON) de uma relação OSM, p.ex. um limite administrativo.

    Usa o Nominatim (um pedido, leve, dentro da política de uso): serve só para escolher
    as quadrículas de uma área a partir de um limite.
    """
    try:
        resp = requests.get(
            NOMINATIM_LOOKUP_URL,
            params={"osm_ids": f"R{relation_id}", "format": "jsonv2", "polygon_geojson": "1"},
            headers=HEADERS,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        found = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise OsmLookupError(f"Nominatim: {exc}") from exc
    if not found or "geojson" not in found[0]:
        raise OsmLookupError(f"Relação {relation_id} não encontrada no OSM")
    return found[0].get("name") or None, found[0]["geojson"]


# Versão do conteúdo do contexto (2: + landuse e estradas). Caches antigas refazem-se.
CONTEXT_VERSION = 2


def context_is_current(path: Path) -> bool:
    """O contexto em cache tem o conteúdo da versão atual?"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("osm3d_context") == CONTEXT_VERSION)


def context_timestamp(path: Path) -> str:
    """Data dos dados OSM do contexto (``timestamp_osm_base`` do Overpass)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "?"
    return str(data.get("osm3s", {}).get("timestamp_osm_base", "?"))


def fetch_context(
    bbox: tuple[float, float, float, float],
    overpass_url: str,
    out: Path,
    osm_relation_id: int | None = None,
    *,
    force: bool = False,
    progress: Progress | None = None,
) -> Path:
    """Edifícios, ``landuse`` e estradas OSM de toda a ``bbox`` + limite da área, em JSON.

    Serve a deteção de edifícios em falta: as quadrículas LiDAR passam os limites
    administrativos, e um edifício mapeado na freguesia vizinha não está "em falta".
    ``landuse`` e estradas (desenhados sobre imagem) ajudam a encaixar os contornos.
    """
    if out.exists() and not force:
        return out
    w, s, e, n = bbox
    timeout = query_timeout_s(bbox)
    boundary = f"rel({osm_relation_id});out geom;" if osm_relation_id else ""
    b = f"({s},{w},{n},{e})"
    query = (
        f"[out:json][timeout:{timeout}];"
        f'(way["building"]{b};relation["building"]{b};'
        f'way["landuse"]{b};relation["landuse"]["type"="multipolygon"]{b};'
        f'way["highway"]{b};);'
        f"out geom;{boundary}"
    )
    data = _overpass_json(query, overpass_url, timeout_s=timeout, progress=progress)
    data["osm3d_context"] = CONTEXT_VERSION
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(out)
    return out
