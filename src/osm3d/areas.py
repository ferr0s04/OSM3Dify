"""Áreas de estudo: registo local e área ativa.

Uma área é um conjunto de quadrículas de 1 km. O identificador é neutro e gerado
(``area-1``, ``area-2``, …) e dá a pasta ``data/processed/<id>``. O nome a mostrar
(``label``) é opcional e pode mudar, porque as quadrículas também podem mudar. Sem
nome escrito, mostra-se o das freguesias que as quadrículas cobrem (``places``).

A área do ``config.yaml`` é só a área inicial. As outras são criadas na aplicação
(separador "Dados") ou com ``osm3d area --new``, e ficam em ``data/areas.json`` com a
área ativa, que vale para a aplicação e para a CLI.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from osm3d.config import AreaConfig, Config

log = logging.getLogger(__name__)

STORE_VERSION = 3
_ID_RE = re.compile(r"^area-(\d+)$")


def next_id(taken: set[str]) -> str:
    """Próximo identificador livre: ``area-<n>``."""
    used = [int(m.group(1)) for t in taken if (m := _ID_RE.match(t))]
    n = max(used, default=0) + 1
    while f"area-{n}" in taken:
        n += 1
    return f"area-{n}"


class AreaStore:
    """``{"version", "active": id, "areas": {id: AreaConfig}}`` num ficheiro JSON."""

    def __init__(self, path: Path, default: AreaConfig, processed_root: Path | None = None) -> None:
        self.path = path
        self.default = default
        self.processed_root = processed_root
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {"version": STORE_VERSION, "active": None, "areas": {}}
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            data.setdefault("areas", {})
            data.setdefault("active", None)
            if data.get("version", 1) < STORE_VERSION:
                data = self._migrate(data)
                self._write(data)
            return data

    def _write(self, data: dict[str, Any]) -> None:
        data["version"] = STORE_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.part")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("version", 1) < 2:
            data = self._migrate_ids(data)
        if data.get("version", 1) < 3:
            # Os nomes guardados até aqui vinham da área inicial do config.yaml, não do
            # utilizador ("Vilar e Mosteiró" em qualquer área): passam a ser as freguesias.
            for raw in data["areas"].values():
                raw["label"] = None
        return data

    def _migrate_ids(self, data: dict[str, Any]) -> dict[str, Any]:
        """Registo antigo (ids derivados de nomes de lugares) → ids neutros ``area-<n>``.

        A pasta ``data/processed/<id antigo>`` muda de nome com a área. Um nome herdado da
        área inicial (mesmo nome, outras quadrículas) é descartado: já não descreve a área.
        """
        areas: dict[str, Any] = data["areas"]
        taken = {self.default.name, *(k for k in areas if _ID_RE.match(k))}
        renamed: dict[str, str] = {}
        for old in [k for k in areas if not _ID_RE.match(k) and k != self.default.name]:
            new = next_id(taken)
            if not self._move_folder(old, new):
                continue  # pasta em uso: fica com o id antigo
            taken.add(new)
            raw = areas.pop(old)
            raw["name"] = new
            if raw.get("label") == self.default.label and raw.get("tiles") != self.default.tiles:
                raw["label"] = None
            areas[new] = raw
            renamed[old] = new
            log.info("Área %s → %s", old, new)
        active = data["active"]
        if active in renamed:
            data["active"] = renamed[active]
        elif active is None and renamed:
            # A área em uso era uma das renomeadas (a inicial sobreposta): mantê-la ativa.
            data["active"] = max(
                renamed.values(), key=lambda n: str(areas[n].get("updated_at", ""))
            )
        return data

    def _move_folder(self, old: str, new: str) -> bool:
        if self.processed_root is None:
            return True
        src, dst = self.processed_root / old, self.processed_root / new
        if not src.exists():
            return True
        if dst.exists():
            log.warning("Não migrada: %s já existe", dst)
            return False
        try:
            src.rename(dst)
        except OSError as exc:
            log.warning("Não migrada (%s): %s", src, exc)
            return False
        return True

    # ------------------------------------------------------------------ leitura

    def areas(self) -> dict[str, AreaConfig]:
        """Todas as áreas: a do ``config.yaml`` primeiro, depois as registadas."""
        stored = {
            name: AreaConfig.model_validate(raw) for name, raw in self._read()["areas"].items()
        }
        return {self.default.name: stored.pop(self.default.name, self.default), **stored}

    def get(self, name: str) -> AreaConfig:
        areas = self.areas()
        if name not in areas:
            raise KeyError(name)
        return areas[name]

    def active(self) -> AreaConfig:
        # Sem área ativa registada: a inicial, mas na versão guardada (as quadrículas
        # alteradas na aplicação), como em ``areas()``, e não a do config.yaml.
        name = self._read()["active"] or self.default.name
        areas = self.areas()
        return areas.get(name, areas[self.default.name])

    def is_default(self, name: str) -> bool:
        return name == self.default.name

    def tiles_changed_at(self, name: str) -> float | None:
        """Instante (epoch) da última alteração das quadrículas da área, se registado."""
        raw = self._read()["areas"].get(name) or {}
        value = raw.get("tiles_changed_at")
        return datetime.fromisoformat(value).timestamp() if value else None

    # ------------------------------------------------------------------ escrita

    def create(
        self, tiles: list[str], label: str | None = None, *, activate: bool = False
    ) -> AreaConfig:
        """Área nova com o próximo identificador livre."""
        with self._lock:
            area = AreaConfig(name=next_id(set(self.areas())), label=label or None, tiles=tiles)
            return self.save(area, activate=activate)

    def save(self, area: AreaConfig, *, activate: bool = False) -> AreaConfig:
        """Grava uma área (a do ``config.yaml`` também pode ser ajustada aqui)."""
        with self._lock:
            data = self._read()
            previous = data["areas"].get(area.name) or {}
            before = self.areas().get(area.name)
            now = datetime.now(UTC).isoformat()  # precisão para comparar com o mtime
            raw = area.model_dump(mode="json")
            if not area.places and previous.get("places"):
                raw["places"] = previous["places"]
                raw["places_tiles"] = previous.get("places_tiles", [])
            raw["updated_at"] = now
            changed = before is None or before.tiles != area.tiles
            raw["tiles_changed_at"] = now if changed else previous.get("tiles_changed_at")
            data["areas"][area.name] = raw
            if activate:
                data["active"] = area.name
            self._write(data)
        return area

    def activate(self, name: str) -> AreaConfig:
        area = self.get(name)
        with self._lock:
            data = self._read()
            data["active"] = name
            self._write(data)
        return area

    def set_places(self, name: str, tiles: list[str], places: list[str]) -> None:
        """Guarda as freguesias calculadas para as quadrículas ``tiles`` da área."""
        with self._lock:
            data = self._read()
            raw = data["areas"].get(name)
            if raw is None:
                area = self.areas().get(name)
                if area is None:
                    return
                raw = area.model_dump(mode="json")
                data["areas"][name] = raw
            if sorted(raw.get("tiles", [])) != sorted(tiles):
                return  # as quadrículas mudaram entretanto: fica para o próximo cálculo
            raw["places"] = places
            raw["places_tiles"] = sorted(tiles)
            self._write(data)

    def delete(self, name: str) -> None:
        """Remove do registo (os dados processados ficam em ``data/processed/<id>``)."""
        if self.is_default(name):
            raise ValueError("A área inicial (config.yaml) não pode ser removida")
        with self._lock:
            data = self._read()
            if name not in data["areas"]:
                raise KeyError(name)
            if data["active"] == name:
                raise ValueError("Não se pode remover a área ativa: ativar outra primeiro")
            del data["areas"][name]
            self._write(data)


def area_store(cfg: Config) -> AreaStore:
    return AreaStore(cfg.paths.areas_path, cfg.area, cfg.paths.processed)


def with_area(cfg: Config, area: AreaConfig) -> Config:
    return cfg.model_copy(update={"area": area})


def with_active_area(cfg: Config) -> Config:
    """Configuração com a área ativa do registo em vez da do ``config.yaml``."""
    return with_area(cfg, area_store(cfg).active())
