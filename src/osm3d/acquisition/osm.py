"""Download de edifícios OSM via Overpass (``out meta``).

Usa-se Overpass diretamente (e não OSMnx) porque é preciso guardar ``id``,
``version``, nós e relações (multipolígonos com pátios) para poder reescrever
no OSM sem conflitos. Para áreas grandes (concelho): extrato PBF da Geofabrik +
pyosmium (extra ``osm``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

OVERPASS_QUERY = """
[out:xml][timeout:300];
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

USER_AGENT = "osm3d/0.1 (OSM 3D Building Generator)"


class OverpassError(RuntimeError):
    pass


def build_query(bbox: tuple[float, float, float, float], osm_relation_id: int | None = None) -> str:
    """Query Overpass para uma bbox WGS84 (min_lon, min_lat, max_lon, max_lat).

    Com ``osm_relation_id``, filtra também pelo polígono do limite administrativo.
    """
    w, s, e, n = bbox
    flt = f"({s},{w},{n},{e})"
    area_stmt = ""
    if osm_relation_id is not None:
        area_stmt = f"area({_AREA_OFFSET + osm_relation_id})->.a;\n"
        flt = f"(area.a){flt}"
    return OVERPASS_QUERY.format(area_stmt=area_stmt, flt=flt)


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
    attempts: int = 3,
    pause_s: float = 10.0,
) -> Path:
    """Descarrega os edifícios da área para um ficheiro ``.osm`` (em cache).

    Se ``out`` já existir, não volta a descarregar, salvo com ``force=True``.
    """
    if out.exists() and not force:
        log.info("Em cache: %s", out)
        return out

    query = build_query(bbox, osm_relation_id)
    urls = [overpass_url, *[u for u in FALLBACK_OVERPASS_URLS if u != overpass_url]]
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        for url in urls:
            try:
                resp = requests.post(
                    url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=330
                )
                resp.raise_for_status()
                _check_payload(resp.content)
            except (requests.RequestException, OverpassError) as exc:
                errors.append(f"{url} (tentativa {attempt}): {exc}")
                log.warning("Overpass falhou: %s", errors[-1])
                time.sleep(pause_s)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(out.suffix + ".part")
            tmp.write_bytes(resp.content)
            tmp.replace(out)
            return out
    raise OverpassError("Todas as tentativas falharam:\n" + "\n".join(errors))
