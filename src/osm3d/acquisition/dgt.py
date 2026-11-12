"""Cliente do Centro de Dados da DGT (cdd.dgterritorio.gov.pt): pesquisa STAC e download.

A DGT não documenta esta API publicamente. O fluxo foi reconstruído a partir do
comportamento descrito por clientes da comunidade (GMT.jl ``dgt_lidar``,
``qgispt/dgtcd_downer``); este código é independente desses projetos.

- Login: formulário Keycloak (realm ``dgterritorio``); a sessão vive em cookies.
- Pesquisa: ``POST {base}/search`` com ``{"bbox": [W, S, E, N], "collections": [...]}``.
- Download: ``GET`` do ``href`` de cada asset, com a mesma sessão.

Uso responsável: pedidos sequenciais com pausa, retoma de ficheiros já descarregados.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urljoin

import requests

if TYPE_CHECKING:
    from osm3d.geospatial.tiling import TileKey

log = logging.getLogger(__name__)

MAIN_SITE = "https://cdd.dgterritorio.gov.pt"
AUTH_HOST = "https://auth.cdd.dgterritorio.gov.pt"
AUTH_URL = f"{AUTH_HOST}/realms/dgterritorio/protocol/openid-connect/auth"
AUTH_PARAMS = {
    "client_id": "aai-oidc-dgt",
    "response_type": "code",
    "redirect_uri": f"{MAIN_SITE}/auth/callback",
    "scope": "openid profile email",
}
DEFAULT_STAC_URL = f"{MAIN_SITE}/dgt-be/v1/"
USER_AGENT = "osm3d/0.1 (OSM 3D Building Generator)"
SESSION_MAX_AGE_S = 25 * 60

_TILE_RE = re.compile(r"-(\d{6})-")
_EXTENSIONS = {
    "application/vnd.laszip": ".laz",
    "image/tiff": ".tif",
    "image/jp2": ".jp2",
}


class DgtError(RuntimeError):
    pass


class DgtCancelled(Exception):
    """Download interrompido a pedido do utilizador (não é repetido como uma falha)."""


# --------------------------------------------------------------------------- login


class _LoginFormParser(HTMLParser):
    """Extrai a ação e os campos escondidos do formulário de login Keycloak."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.hidden: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            is_login = a.get("id") == "kc-form-login"
            looks_like_login = self.action is None and "login-actions" in a.get("action", "")
            if is_login or looks_like_login:
                self.action = a.get("action")
                self._in_form = True
        elif tag == "input" and self._in_form and a.get("type") == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def parse_login_form(html: str) -> tuple[str, dict[str, str]]:
    parser = _LoginFormParser()
    parser.feed(html)
    if not parser.action:
        raise DgtError("Formulário de login não encontrado (a página do portal mudou?)")
    action = parser.action.replace("&amp;", "&")
    return urljoin(AUTH_HOST, action), parser.hidden


# ------------------------------------------------------------------------- modelos


@dataclass(frozen=True)
class DgtAsset:
    href: str
    media_type: str


@dataclass(frozen=True)
class DgtItem:
    id: str
    collection: str
    tile: str | None
    version: int
    assets: tuple[DgtAsset, ...] = field(default_factory=tuple)


def tile_from_id(item_id: str) -> str | None:
    m = _TILE_RE.search(item_id)
    return m.group(1) if m else None


def _item_id(feature: dict[str, Any]) -> str:
    for link in feature.get("links", []):
        if link.get("rel") == "self" and link.get("href"):
            return str(link["href"]).rstrip("/").rsplit("/", 1)[-1]
    return str(feature.get("id", ""))


def parse_items(payload: dict[str, Any]) -> list[DgtItem]:
    items: list[DgtItem] = []
    for feat in payload.get("features", []):
        item_id = _item_id(feat)
        version = int((feat.get("properties") or {}).get("version") or 0)
        assets = tuple(
            DgtAsset(str(a["href"]), str(a.get("type", "")))
            for a in (feat.get("assets") or {}).values()
            if a.get("href")
        )
        items.append(
            DgtItem(
                item_id, str(feat.get("collection", "")), tile_from_id(item_id), version, assets
            )
        )
    return items


def latest_versions(items: Iterable[DgtItem]) -> list[DgtItem]:
    """Mantém só a versão mais recente de cada (coleção, quadrícula)."""
    best: dict[tuple[str, str], DgtItem] = {}
    for it in items:
        key = (it.collection, it.tile or it.id)
        if key not in best or it.version > best[key].version:
            best[key] = it
    return sorted(best.values(), key=lambda i: (i.collection, i.tile or i.id))


def filename_for(item: DgtItem, asset: DgtAsset) -> str:
    base_type = asset.media_type.split(";")[0].strip().lower()
    ext = _EXTENSIONS.get(base_type) or Path(asset.href.split("?")[0]).suffix or ".bin"
    suffix = f"_v{item.version:02d}" if item.version else ""
    return f"{item.id}{suffix}{ext}"


# -------------------------------------------------------------------------- cliente


class DgtClient:
    def __init__(
        self,
        user: str,
        password: str,
        stac_url: str = DEFAULT_STAC_URL,
        *,
        delay_s: float = 2.0,
        timeout_s: float = 60.0,
    ) -> None:
        self._user = user
        self._password = password
        self.stac_url = stac_url if stac_url.endswith("/") else stac_url + "/"
        self.delay_s = delay_s
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._logged_at: float | None = None

    def __repr__(self) -> str:  # nunca expor credenciais
        return f"DgtClient(stac_url={self.stac_url!r})"

    def login(self) -> None:
        s = self.session
        s.cookies.clear()
        s.get(MAIN_SITE, timeout=self.timeout_s)
        page = s.get(AUTH_URL, params=AUTH_PARAMS, timeout=self.timeout_s)
        page.raise_for_status()
        action, fields = parse_login_form(page.text)
        fields |= {"username": self._user, "password": self._password}
        resp = s.post(
            action,
            data=fields,
            headers={"Origin": AUTH_HOST, "Referer": page.url},
            timeout=self.timeout_s,
        )
        if not resp.url.startswith(MAIN_SITE):
            raise DgtError("Login falhou: verificar DGT_USER/DGT_PASSWORD no .env")
        self._logged_at = time.monotonic()
        log.info("Sessão DGT iniciada")

    def _ensure_session(self) -> None:
        if self._logged_at is None or time.monotonic() - self._logged_at > SESSION_MAX_AGE_S:
            self.login()

    def search(
        self, bbox_wgs84: tuple[float, float, float, float], collections: list[str]
    ) -> list[DgtItem]:
        self._ensure_session()
        body = {"bbox": list(bbox_wgs84), "collections": collections, "limit": 1000}
        time.sleep(self.delay_s)
        resp = self.session.post(self.stac_url + "search", json=body, timeout=self.timeout_s)
        resp.raise_for_status()
        payload = resp.json()
        if "code" in payload and "features" not in payload:
            raise DgtError(
                f"Pesquisa recusada: {payload.get('code')} {payload.get('description', '')}"
            )
        ctx = payload.get("context") or {}
        if ctx.get("matched", 0) > ctx.get("returned", len(payload.get("features", []))):
            log.warning(
                "Resultados truncados (%s de %s): usar áreas menores",
                ctx.get("returned"),
                ctx.get("matched"),
            )
        return parse_items(payload)

    def download(
        self,
        item: DgtItem,
        asset: DgtAsset,
        dest_dir: Path,
        *,
        progress: Callable[[str], None] | None = None,
        on_bytes: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """Descarrega um asset (retoma: não repete ficheiros completos).

        ``on_bytes(feitos, total)`` reporta o progresso; ``cancel`` interrompe o download
        (apaga o ``.part`` e levanta :class:`DgtCancelled`).
        """
        dest = dest_dir / filename_for(item, asset)
        if dest.exists() and dest.stat().st_size > 0:
            log.info("Já existe: %s", dest)
            return dest
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in (1, 2, 3):
            self._ensure_session()
            time.sleep(self.delay_s)
            try:
                with self.session.get(
                    asset.href, stream=True, timeout=(30, 300), allow_redirects=True
                ) as resp:
                    resp.raise_for_status()
                    if resp.headers.get("Content-Type", "").startswith("text/html"):
                        # Sessão expirada → página de login em vez do ficheiro.
                        self._logged_at = None
                        raise DgtError("Sessão expirada")
                    total = int(resp.headers.get("Content-Length", 0))
                    done, last_report = 0, 0.0
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            if cancel is not None and cancel.is_set():
                                raise DgtCancelled(dest.name)
                            fh.write(chunk)
                            done += len(chunk)
                            if on_bytes:
                                on_bytes(done, total)
                            if progress and time.monotonic() - last_report > 5:
                                last_report = time.monotonic()
                                pct = f" ({done / total:.0%})" if total else ""
                                progress(f"  {dest.name}: {done / 1e6:.0f} MB{pct}")
                    tmp.replace(dest)
                    return dest
            except DgtCancelled:
                tmp.unlink(missing_ok=True)
                raise
            except (requests.RequestException, DgtError) as exc:
                log.warning("Download falhou (tentativa %d): %s", attempt, exc)
                time.sleep(5 * attempt)
        tmp.unlink(missing_ok=True)
        raise DgtError(f"Não foi possível descarregar {dest.name}")


class Searcher(Protocol):
    def search(
        self, bbox_wgs84: tuple[float, float, float, float], collections: list[str]
    ) -> list[DgtItem]: ...


def tile_items(
    client: Searcher, key: TileKey, collection: str, crs: str = "EPSG:3763"
) -> list[DgtItem]:
    """Versão mais recente dos itens de uma coleção para uma quadrícula.

    A bbox é encolhida 50 m para não apanhar as quadrículas vizinhas. Nos produtos da
    grelha LiDAR (LAZ, MDT, MDS), os itens de outras quadrículas que ainda assim apareçam
    são descartados. Os produtos noutra grelha (ortofotos: folhas de ~8 × 5 km, id sem
    quadrícula, p.ex. ``ORTOS-2025-cog-25cm-097-3``) ficam: a pesquisa já garante que
    intersetam a quadrícula.
    """
    found = [
        it
        for it in client.search(key.bounds_wgs84(crs, inset_m=50), [collection])
        if it.tile is None or it.tile == key.dgt_name
    ]
    return latest_versions(found)
