"""Topologia dos edifícios novos face ao OSM: sem sobreposições nem cruzamentos.

Os validadores do OSM (iD, JOSM, Osmose) assinalam edifícios sobrepostos a outros
edifícios e edifícios atravessados por vias. Os contornos novos não veem o OSM, por
isso um passeio, o limite de um ``landuse`` ou um edifício já mapeado passam facilmente
meio metro para dentro deles.

Só se tratam **sobreposições reais**. Um afastamento entre o edifício e o limite de um
``landuse`` (quintal, terraço, logradouro até ao muro) pode ser verdadeiro e fica como
está.

Cada edifício novo tem um modo (escolhido na revisão; ``Building.topology_mode``):

- ``osm`` (por omissão nos contornos **municipais**, vetorizados com mais rigor do que o
  OSM, que na zona pode estar todo desviado com a imagem de satélite): o edifício fica
  onde está e, na exportação, ajusta-se o elemento do OSM:

  - um ``landuse`` passa a contornar o edifício, que fica todo do lado onde já estava
    quase todo (ver :mod:`osm3d.export.osm_edits`);
  - um edifício do OSM perde a lasca que sobrepõe o novo.

  Vias não se mexem (são a rede viária): o cruzamento fica sinalizado.
- ``trim`` (por omissão nos contornos do **LiDAR/ortofoto**): recorta-se a lasca do
  edifício novo (do outro lado da via, fora do ``landuse``, sobre o edifício do OSM).
- ``keep``: nada muda; as sobreposições ficam só sinalizadas.

Entre edifícios novos sobrepostos, a sobreposição sai de um deles: do detetado só no
LiDAR antes do municipal; entre iguais, do menor. Ficam encostados, sem folga, e na
exportação partilham os nós.

Os ajustes deixam uma folga (``clearance_m``) entre o edifício e o elemento do OSM. Só
se ajustam **lascas**: nada mais largo do que ``max_trim_m`` e até ``max_trim_fraction``
da área. O resto fica sinalizado para revisão.

O id do candidato não muda: as revisões feitas continuam associadas.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from osm3d.config import TopologyConfig
from osm3d.domain.models import Building, PartsLayout, QAFlag, TopologyMode
from osm3d.geometry import remove_spikes

OVERLAP_TOL_M2 = 0.01  # abaixo disto, a sobreposição é ruído numérico
MIN_AREA_LEFT_M2 = 4.0

# Códigos das sinalizações desta verificação (são refeitas em cada passagem).
TRIMMED = "topology_trimmed"
ADJUSTS_OSM = "adjusts_osm"
OVERLAPS_OSM = "overlaps_osm_building"
OVERLAPS_NEW = "overlaps_new_building"
CROSSES_HIGHWAY = "crosses_highway"
STRADDLES_LANDUSE = "straddles_landuse"
PARTS_NOT_TRIMMED = "topology_parts"
CODES = {
    TRIMMED,
    ADJUSTS_OSM,
    OVERLAPS_OSM,
    OVERLAPS_NEW,
    CROSSES_HIGHWAY,
    STRADDLES_LANDUSE,
    PARTS_NOT_TRIMMED,
}
# O que os validadores do OSM assinalam (exportados na mesma, mas a rever no JOSM).
CONFLICT_CODES = {OVERLAPS_OSM, OVERLAPS_NEW, CROSSES_HIGHWAY, PARTS_NOT_TRIMMED}

# Vias que não são desenhadas como linhas no chão (ou não são vias).
_NOT_GROUND_HIGHWAY = {"proposed", "corridor", "elevator", "platform", "bus_stop"}


@dataclass(frozen=True)
class TopologyRules:
    clearance_m: float = 0.1
    max_trim_m: float = 1.0
    max_trim_fraction: float = 0.15
    landuse: bool = True

    @classmethod
    def from_config(cls, cfg: TopologyConfig) -> TopologyRules:
        return cls(cfg.clearance_m, cfg.max_trim_m, cfg.max_trim_fraction, cfg.landuse)


def default_mode(b: Building) -> TopologyMode:
    """Municipal: o contorno manda (ajusta-se o OSM). LiDAR/ortofoto: recorta-se."""
    return "osm" if "municipal_gid" in b.diagnostics else "trim"


def mode_of(b: Building) -> TopologyMode:
    return b.topology_mode or default_mode(b)


@dataclass(frozen=True)
class Feature:
    geom: BaseGeometry
    label: str  # p.ex. "highway=footway (way/123)"
    osm_type: str = "way"
    osm_id: int = 0

    @property
    def editable(self) -> bool:
        """Só ways fechadas simples se ajustam (não multipolígonos)."""
        return self.osm_type == "way" and isinstance(self.geom, Polygon) and self.osm_id > 0


@dataclass
class _Layer:
    features: list[Feature] = field(default_factory=list)
    _tree: shapely.STRtree | None = None

    def near(self, geom: BaseGeometry, distance: float) -> list[Feature]:
        if not self.features:
            return []
        if self._tree is None:
            self._tree = shapely.STRtree([f.geom for f in self.features])
        idx = self._tree.query(geom, predicate="dwithin", distance=distance)
        return [self.features[i] for i in sorted(idx)]


@dataclass
class Surroundings:
    """O que já está no OSM à volta dos edifícios novos (no CRS de processamento)."""

    buildings: _Layer = field(default_factory=_Layer)
    highways: _Layer = field(default_factory=_Layer)
    landuse: _Layer = field(default_factory=_Layer)
    timestamp: str = "?"


def blocks_buildings(tags: dict[str, str]) -> bool:
    """A via, desenhada no chão, não pode atravessar um edifício (como no iD)."""
    if tags.get("highway") in _NOT_GROUND_HIGHWAY or tags.get("area") == "yes":
        return False
    for key in ("tunnel", "bridge", "covered", "indoor"):
        if tags.get(key, "no") != "no":
            return False
    if tags.get("layer", "0") not in ("0", ""):
        return False
    return tags.get("location") not in ("underground", "indoor", "rooftop")


def _members_polygon(members: list[dict]) -> BaseGeometry | None:  # type: ignore[type-arg]
    from osm3d.reconstruction.candidates import _polygon_from_members

    return _polygon_from_members(members)


def load_surroundings(context: Path, crs: str, fetched_osm: Path | None = None) -> Surroundings:
    """Edifícios, vias e ``landuse`` do contexto OSM da fase candidates (JSON do Overpass).

    ``fetched_osm`` (o ``.osm`` da fase fetch) junta os edifícios da área que o contexto
    não tenha (servidor Overpass desatualizado).
    """
    data = json.loads(context.read_text(encoding="utf-8"))
    to_crs = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    def project(g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda c: np.column_stack(to_crs.transform(c[:, 0], c[:, 1])))

    env = Surroundings(timestamp=str(data.get("osm3s", {}).get("timestamp_osm_base", "?")))
    seen: set[str] = set()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        kind, oid = el["type"], int(el["id"])
        key = f"{kind}/{oid}"
        coords = [(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
        closed = kind == "way" and len(coords) >= 4 and coords[0] == coords[-1]
        area: BaseGeometry | None = None
        if closed:
            area = Polygon(coords)
        elif kind == "relation" and tags.get("type") == "multipolygon":
            area = _members_polygon(el.get("members", []))
        if "building" in tags and tags["building"] != "no" and area is not None:
            geom = shapely.make_valid(project(area))
            label = f"{key} (building={tags['building']})"
            env.buildings.features.append(Feature(geom, label, kind, oid))
            seen.add(key)
        elif "landuse" in tags and area is not None:
            geom = shapely.make_valid(project(area))
            label = f"landuse={tags['landuse']} ({key})"
            env.landuse.features.append(Feature(geom, label, kind, oid))
        elif "highway" in tags and kind == "way" and len(coords) >= 2 and blocks_buildings(tags):
            label = f"highway={tags['highway']} ({key})"
            env.highways.features.append(Feature(project(LineString(coords)), label, kind, oid))
    if fetched_osm is not None and fetched_osm.exists():
        from osm3d.geospatial.footprints import load_footprints

        for b in load_footprints(fetched_osm, crs, 0.0):
            if b.key not in seen:
                label = f"{b.key} (building={b.existing_tags.get('building', 'yes')})"
                env.buildings.features.append(Feature(b.footprint, label, b.osm_type, b.osm_id))
    return env


# ------------------------------------------------------------------ recortes


def _polygons(g: BaseGeometry) -> list[Polygon]:
    return [p for p in getattr(g, "geoms", [g]) if isinstance(p, Polygon) and not p.is_empty]


def is_sliver(g: BaseGeometry, max_width_m: float) -> bool:
    """Nada em ``g`` é mais largo do que ``max_width_m``."""
    return bool(g.is_empty or g.buffer(-max_width_m / 2).is_empty)


def obstacle(fp: Polygon, rules: TopologyRules) -> Polygon:
    """O edifício com a folga: o que o elemento do OSM ajustado deixa de tocar."""
    return fp.buffer(rules.clearance_m, join_style="mitre")


@dataclass
class _Trim:
    footprint: Polygon
    removed_m2: float


def _trim(
    fp: Polygon, rest: BaseGeometry, rules: TopologyRules, *, split: bool = False
) -> _Trim | None:
    """O maior polígono de ``rest`` (o que fica de ``fp``), se só saiu uma lasca.

    Com ``split``, ``rest`` tem de ter vários pedaços (a via atravessa o edifício de
    lado a lado; uma via que acaba lá dentro faria um entalhe).
    """
    pieces = _polygons(rest)
    if not pieces or (split and len(pieces) < 2):
        return None
    kept = max(pieces, key=lambda p: p.area)
    if kept.interiors:
        return None  # o recorte abriria um buraco no edifício
    removed = fp.difference(kept)
    if removed.area > rules.max_trim_fraction * fp.area or kept.area < MIN_AREA_LEFT_M2:
        return None
    if not is_sliver(removed, rules.max_trim_m):
        return None  # sai um pedaço largo: não é uma lasca
    out = orient(remove_spikes(kept), 1.0)
    if not out.is_valid or out.is_empty:
        return None
    return _Trim(out, float(removed.area))


def adjusted_feature(
    geom: BaseGeometry,
    obstacles: list[tuple[Polygon, bool]],
    rules: TopologyRules,
    max_width_m: float | None = None,
) -> Polygon | None:
    """Polígono de um elemento do OSM ajustado: inclui (``True``) ou exclui os obstáculos.

    ``None`` se o resultado não é um polígono simples ou se muda mais do que lascas.
    """
    out: BaseGeometry = geom
    for obs, include in sorted(obstacles, key=lambda o: not o[1]):  # inclusões primeiro
        out = out.union(obs) if include else out.difference(obs)
    if not isinstance(out, Polygon) or not out.is_valid or out.is_empty:
        return None
    if len(out.interiors) != len(getattr(geom, "interiors", [])):
        return None
    width = max_width_m or rules.max_trim_m + 2 * rules.clearance_m
    if not is_sliver(geom.symmetric_difference(out), width):
        return None
    return out


@dataclass(frozen=True)
class Adjustment:
    """Ajuste de um elemento do OSM à volta de um edifício novo (na exportação)."""

    building_key: str
    feature: Feature
    obstacle: Polygon  # edifício + folga, no CRS de processamento
    include: bool  # o landuse passa a contornar o edifício por fora (fica dentro dele)
    description: str
    # Largura máxima da mudança (muros: o limite pode estar a 2 m); None = a das regras.
    max_width_m: float | None = None

    @property
    def group(self) -> tuple[str, str]:
        """Ajustes que são tudo ou nada: os de landuse de um edifício, juntos."""
        if self.feature.label.startswith("landuse="):
            return self.building_key, "landuse"
        return self.building_key, f"{self.feature.osm_type}/{self.feature.osm_id}"


@dataclass
class _State:
    b: Building
    fp: Polygon
    mode: TopologyMode
    trims: list[str] = field(default_factory=list)
    flags: list[QAFlag] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)

    def trimmed(self, t: _Trim, what: str) -> None:
        if t.removed_m2 <= OVERLAP_TOL_M2:
            return  # só ruído numérico: o contorno fica como estava
        self.fp = t.footprint
        self.trims.append(f"{what} ({t.removed_m2:.1f} m²)")

    @property
    def is_municipal(self) -> bool:
        return "municipal_gid" in self.b.diagnostics


def _fit_to_highways(s: _State, env: Surroundings, rules: TopologyRules) -> None:
    for f in env.highways.near(s.fp, 0.0):
        if f.geom.intersection(s.fp.buffer(-0.01)).length <= 0.01:
            continue  # não entra no edifício: só encosta (num vértice ou ao longo do lado)
        if s.mode == "trim":
            rest = s.fp.difference(f.geom.buffer(rules.clearance_m))
            t = _trim(s.fp, rest, rules, split=True)
            if t is not None:
                s.trimmed(t, f.label)
                continue
        s.flags.append(
            QAFlag(
                code=CROSSES_HIGHWAY,
                message=f"Atravessado pela via {f.label}. As vias não são ajustadas "
                "automaticamente: corrigir a via (ou o contorno) no JOSM; os validadores "
                "do OSM assinalam-no.",
                severity="warning",
            )
        )


def _fit_to_landuse(s: _State, env: Surroundings, rules: TopologyRules) -> None:
    c = rules.clearance_m
    planned: list[Adjustment] = []
    straddles: list[QAFlag] = []
    for f in env.landuse.near(s.fp, 0.0):
        inside_m2 = s.fp.intersection(f.geom).area
        outside_m2 = s.fp.area - inside_m2
        if min(inside_m2, outside_m2) <= OVERLAP_TOL_M2:
            continue  # todo dentro ou todo fora (um afastamento pode ser verdadeiro)
        include = inside_m2 >= outside_m2  # fica do lado onde já está quase todo
        minority = outside_m2 if include else inside_m2
        what = "dentro de" if include else "fora de"
        small = minority <= rules.max_trim_fraction * s.fp.area
        if s.mode == "trim" and small:
            local = f.geom.intersection(shapely.box(*s.fp.buffer(c + 1.0).bounds))
            rest = (
                s.fp.intersection(local.buffer(-c)) if include else s.fp.difference(local.buffer(c))
            )
            t = _trim(s.fp, rest, rules)
            if t is not None:
                s.trimmed(t, f"{what} {f.label}")
                continue
        if s.mode == "osm" and small and f.editable:
            obs = obstacle(s.fp, rules)
            if adjusted_feature(f.geom, [(obs, include)], rules) is not None:
                verb = "passa a contornar o edifício" if include else "deixa de entrar no edifício"
                planned.append(
                    Adjustment(
                        s.b.key,
                        f,
                        obs,
                        include,
                        f"{f.label} {verb} ({minority:.1f} m² do edifício estavam "
                        f"{'fora' if include else 'dentro'})",
                    )
                )
                continue
        why = "" if s.mode == "keep" else " Demasiado para ajustar automaticamente."
        straddles.append(
            QAFlag(
                code=STRADDLES_LANDUSE,
                message=f"Parte dentro e parte fora de {f.label} ({inside_m2:.1f} de "
                f"{s.fp.area:.0f} m² dentro).{why} Rever o limite do landuse no JOSM.",
                severity="info",
            )
        )
    if straddles and any(a.include for a in planned):
        # Tudo ou nada: alargar um landuse para dentro de outro que não se ajusta
        # sobrepunha os dois.
        straddles += [
            QAFlag(
                code=STRADDLES_LANDUSE,
                message=f"Não ajustado: {a.description} (o landuse vizinho não se ajusta).",
                severity="info",
            )
            for a in planned
        ]
        planned = []
    s.adjustments += planned
    s.flags += straddles


def _fit_to_osm_buildings(s: _State, env: Surroundings, rules: TopologyRules) -> None:
    for f in env.buildings.near(s.fp, 0.0):
        overlap = s.fp.intersection(f.geom).area
        if overlap <= OVERLAP_TOL_M2:
            continue  # encostado (ou afastado) não é problema
        if s.mode == "trim":
            t = _trim(s.fp, s.fp.difference(f.geom.buffer(rules.clearance_m)), rules)
            if t is not None:
                s.trimmed(t, f"edifício {f.label}")
                continue
        if s.mode == "osm" and f.editable and overlap <= rules.max_trim_fraction * f.geom.area:
            obs = obstacle(s.fp, rules)
            if adjusted_feature(f.geom, [(obs, False)], rules) is not None:
                s.adjustments.append(
                    Adjustment(
                        s.b.key,
                        f,
                        obs,
                        False,
                        f"edifício {f.label} perde os {overlap:.1f} m² que sobrepõem o novo",
                    )
                )
                continue
        s.flags.append(
            QAFlag(
                code=OVERLAPS_OSM,
                message=f"Sobrepõe o edifício OSM {f.label} em {overlap:.1f} m²"
                + ("" if s.mode == "keep" else " (demasiado para ajustar automaticamente)")
                + ": duplicado ou contornos a corrigir no JOSM.",
                severity="warning",
            )
        )


def _fit_between_new(states: list[_State], rules: TopologyRules) -> None:
    tree = shapely.STRtree([s.fp for s in states])
    pairs = tree.query([s.fp for s in states], predicate="intersects")
    # Ordem fixa (i, j): o resultado não depende da forma da árvore, e recalcular só um
    # grupo de edifícios sobrepostos (na mesma ordem relativa) dá o mesmo que o todo.
    pairs = pairs[:, np.lexsort((pairs[1], pairs[0]))]
    for i, j in zip(*pairs.tolist(), strict=True):
        if i >= j:
            continue
        a, b = states[i], states[j]
        overlap = a.fp.intersection(b.fp).area  # já com os recortes anteriores
        if overlap <= OVERLAP_TOL_M2:
            continue
        # Primeiro tenta-se recortar o menos fiável: LiDAR antes de municipal, depois o menor.
        order = sorted((a, b), key=lambda s: (s.is_municipal, s.fp.area))
        for victim, other in (order, order[::-1]):
            if victim.mode == "keep":
                continue
            t = _trim(victim.fp, victim.fp.difference(other.fp), rules)
            if t is not None:
                victim.trimmed(t, f"candidato {other.b.key}")
                break
        else:
            for s, o in ((a, b), (b, a)):
                s.flags.append(
                    QAFlag(
                        code=OVERLAPS_NEW,
                        message=f"Sobrepõe o edifício novo {o.b.key} em {overlap:.1f} m²: "
                        "exportar só um deles ou corrigir.",
                        severity="warning",
                    )
                )


def _clip_layout(layout: PartsLayout, fp: Polygon) -> PartsLayout | None:
    from osm3d.parts import layout_from_polygons, validate_layout

    polys = []
    for i in range(len(layout.parts)):
        pieces = _polygons(layout.polygon(i).intersection(fp))
        if not pieces:
            return None
        polys.append(orient(max(pieces, key=lambda p: p.area), 1.0))
    clipped = layout_from_polygons(polys, source=layout.source)
    if len(clipped.parts) != len(layout.parts) or validate_layout(clipped, fp):
        return None
    return clipped


def _finish(s: _State) -> Building:
    b = s.b
    # Sem recortes novos (p.ex. na exportação, depois de já recortado), fica a nota.
    keep = CODES - {TRIMMED} if not s.trims else CODES
    flags = [f for f in b.qa_flags if f.code not in keep]
    update: dict[str, object] = {}
    if s.trims:
        layout = b.layout
        if layout is not None and len(layout.parts) > 1:
            layout = _clip_layout(layout, s.fp)
        if layout is None and b.layout is not None:
            # Divisão em partes que não se consegue ajustar: fica o contorno original.
            s.flags.append(
                QAFlag(
                    code=PARTS_NOT_TRIMMED,
                    message="O contorno devia ser recortado ("
                    + "; ".join(s.trims)
                    + "), mas a divisão em partes não se ajusta: recortar à mão no JOSM.",
                    severity="warning",
                )
            )
        else:
            suggested = b.suggested_layout
            if suggested is not None and len(suggested.parts) > 1:
                suggested = _clip_layout(suggested, s.fp)
            update = {"footprint": s.fp, "layout": layout, "suggested_layout": suggested}
            flags.append(
                QAFlag(
                    code=TRIMMED,
                    message="Contorno recortado para não sobrepor o OSM: " + "; ".join(s.trims),
                    severity="info",
                )
            )
    if s.adjustments:
        flags.append(
            QAFlag(
                code=ADJUSTS_OSM,
                message="O edifício fica onde está; na exportação ajusta-se o OSM: "
                + "; ".join(a.description for a in s.adjustments),
                severity="info",
            )
        )
    return b.model_copy(update={**update, "qa_flags": [*flags, *s.flags]})


@dataclass
class TopologyResult:
    checked: int = 0
    trimmed: int = 0
    adjusting: int = 0  # edifícios com ajustes ao OSM planeados
    conflicts: int = 0
    adjustments: list[Adjustment] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.checked} edifícios novos verificados: {self.trimmed} recortados, "
            f"{self.adjusting} com ajustes ao OSM, {self.conflicts} com conflitos por resolver"
        )


def resolve_topology(
    buildings: Iterable[Building], env: Surroundings, rules: TopologyRules | None = None
) -> tuple[list[Building], TopologyResult]:
    """Ajusta (conforme o modo de cada edifício novo) e sinaliza as sobreposições.

    Os edifícios que já existem no OSM passam sem alterações; os ajustes ao OSM vêm no
    resultado, para a exportação. Idempotente: numa segunda passagem já não há o que
    recortar (as sinalizações são refeitas).
    """
    rules = rules or TopologyRules()
    out = list(buildings)
    idx = [i for i, b in enumerate(out) if b.osm_type == "new" and not b.footprint.is_empty]
    states: dict[int, _State] = {}
    for i in idx:
        polys = _polygons(out[i].footprint)
        if len(polys) != 1:
            continue
        s = _State(out[i], orient(polys[0], 1.0), mode_of(out[i]))
        if s.mode != "osm":  # vias e edifícios primeiro; os ajustes ao OSM, no fim
            _fit_to_highways(s, env, rules)
        states[i] = s
    if len(states) > 1:
        _fit_between_new(list(states.values()), rules)
    for s in states.values():
        if s.mode == "osm":
            _fit_to_highways(s, env, rules)
        if rules.landuse:
            _fit_to_landuse(s, env, rules)
        _fit_to_osm_buildings(s, env, rules)
    result = TopologyResult(checked=len(states))
    for i, s in states.items():
        out[i] = _finish(s)
        result.trimmed += bool(s.trims) and out[i].footprint is not s.b.footprint
        result.adjusting += bool(s.adjustments)
        result.conflicts += any(f.code in CONFLICT_CODES for f in out[i].qa_flags)
        result.adjustments += s.adjustments
    return out, result
