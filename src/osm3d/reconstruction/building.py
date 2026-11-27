"""Reconstrução por edifício (Fases 2–3: uma parte por edifício).

Para cada quadrícula com LAZ e MDT:

1. lê os pontos de edifício (classe 6) da zona, por blocos;
2. estima o deslocamento global footprint ↔ LiDAR da quadrícula;
3. atribui pontos a footprints;
4. cota do terreno (percentil baixo do MDT num anel à volta do footprint);
5. ajuste do modelo de telhado → forma, parâmetros S3DB e altura total;
6. sinalizações de QA.
"""

from __future__ import annotations

import atexit
import logging
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import numpy.typing as npt
import shapely
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from osm3d.config import Config
from osm3d.domain.models import (
    Building,
    BuildingPart,
    Estimate,
    GroundChoice,
    QAFlag,
    Roof,
    RoofModel,
    RoofShape,
)
from osm3d.geospatial.point_clouds import PointSet, assign_points, read_points
from osm3d.geospatial.raster import DtmMosaic, sample_ground_elevation, wall_foot_profile
from osm3d.geospatial.registration import estimate_tile_offset
from osm3d.geospatial.tiling import TileKey, tile_of
from osm3d.parallel import ordered_map, process_pool, worker_count
from osm3d.parts import suggest_layout, validate_layout
from osm3d.reconstruction.ground import STEP_THRESHOLD_M, choose_ground, parse_levels
from osm3d.reconstruction.parts import fit_layout
from osm3d.reconstruction.roof_fitting import (
    FitParams,
    FitResult,
    RoofFit,
    fit_main_volume,
    fit_roof,
    fit_surface,
    group_beats_individuals,
)
from osm3d.reconstruction.roof_models import model_surface_z

log = logging.getLogger(__name__)

# Densidade mínima de pontos de edifício (pts/m² de footprint) abaixo da qual se
# suspeita de edifício demolido, posterior ao voo ou mal classificado.
MIN_POINT_DENSITY = 4.0
# Área / área do retângulo mínimo orientado: abaixo disto a planta não é retangular
# e o modelo de telhado de uma só parte é pouco fiável (precisa de building:part).
MIN_RECTANGULARITY = 0.85


@dataclass
class TileReport:
    tile: str
    n_buildings: int
    n_points: int
    offset: tuple[float, float]
    iou_before: float
    iou_after: float


def _flag(code: str, message: str, severity: str = "warning") -> QAFlag:
    return QAFlag(code=code, message=message, severity=severity)  # type: ignore[arg-type]


def _shift(geom, offset: tuple[float, float]):  # type: ignore[no-untyped-def]
    dx, dy = offset
    return shapely.transform(geom, lambda c: c + np.array([dx, dy]))


def fit_params(cfg: Config) -> FitParams:
    return FitParams(
        candidates=tuple(cfg.roofs.candidate_shapes),
        max_rmse_m=cfg.roofs.max_rmse_m,
        flat_max_slope_deg=cfg.roofs.flat_max_slope_deg,
        min_points=cfg.heights.min_points_per_building,
        height_percentile=cfg.heights.roof_percentile,
    )


def _roof_flags(fit: RoofFit, cfg: Config) -> list[QAFlag]:
    flags: list[QAFlag] = []
    if fit.roof.shape.value is RoofShape.UNKNOWN:
        flags.append(_flag("roof_shape_unknown", "Nenhum modelo de telhado se ajusta bem"))
    elif fit.roof.shape.confidence < cfg.roofs.min_shape_confidence:
        flags.append(
            _flag("roof_shape_low_confidence", f"Confiança {fit.roof.shape.confidence:.2f}")
        )
    return flags


def _height_flags(height: float) -> list[QAFlag]:
    if 2.0 <= height <= 60.0:
        return []
    return [_flag("height_outlier", f"Altura {height:.1f} m implausível")]


_ROOF_FLAG_CODES = {"roof_shape_unknown", "roof_shape_low_confidence", "height_outlier"}


def reconstruct_building(
    building: Building,
    points: PointSet,
    dtm: DtmMosaic,
    offset: tuple[float, float],
    cfg: Config,
) -> tuple[Building, RoofFit | None]:
    footprint = building.footprint
    area = float(footprint.area)
    rect_area = float(footprint.minimum_rotated_rectangle.area) or area
    diagnostics = {
        **building.diagnostics,  # p.ex. desvio do ortofoto dos candidatos
        "n_points": float(len(points)),
        "point_density": len(points) / area if area else 0.0,
        "rectangularity": area / rect_area,
        "area_m2": area,
    }
    # As sinalizações de entrada (p.ex. da deteção dos candidatos: em falta no OSM, não
    # consta da cartografia municipal) mantêm-se; a reconstrução só acrescenta.
    flags: list[QAFlag] = list(building.qa_flags)
    if diagnostics["rectangularity"] < MIN_RECTANGULARITY:
        flags.append(
            _flag("complex_footprint", "Planta não retangular: candidata a partes", "info")
        )

    ground = sample_ground_elevation(
        dtm, footprint, cfg.heights.ground_ring_buffer_m, cfg.heights.ground_percentile
    )
    update: dict[str, object] = {
        "registration_offset_m": offset,
        "ground_elevation_m": ground,
        "diagnostics": diagnostics,
    }
    if ground is None:
        flags.append(_flag("no_ground", "Sem MDT à volta do edifício", "error"))
        return building.model_copy(update=update | {"qa_flags": flags, "parts": []}), None
    if len(points) == 0 or diagnostics["point_density"] < MIN_POINT_DENSITY:
        flags.append(
            _flag(
                "low_point_density",
                f"{diagnostics['point_density']:.1f} pts/m² de edifício: demolido, "
                "posterior ao voo ou mal classificado?",
            )
        )
        if len(points) < cfg.heights.min_points_per_building:
            return building.model_copy(update=update | {"qa_flags": flags, "parts": []}), None

    params = fit_params(cfg)
    fit = fit_roof(_shift(footprint, offset), points, ground, params)
    main = None
    if fit.roof.shape.value is RoofShape.UNKNOWN:
        # Nenhum modelo explica a planta inteira: há um volume secundário (anexo,
        # alpendre) mais baixo? Ajusta-se o telhado do volume principal.
        main = fit_main_volume(_shift(footprint, offset), points, ground, params)
        if main is not None:
            fit = main.fit
            diagnostics["main_volume_fraction"] = main.fraction
            flags.append(
                _flag(
                    "secondary_volume",
                    f"Telhado {fit.roof.shape.value.value} no volume principal "
                    f"({main.fraction:.0%} da planta); o resto é mais baixo: candidato a "
                    "building:part",
                )
            )
    if fit.best is not None:
        diagnostics["rmse_m"] = fit.best.rmse_m
        diagnostics["inlier_ratio"] = fit.best.inlier_ratio

    # Encosta/patamar: o chão baixo (estrada abaixo de um muro de suporte, p.ex.) pode
    # não ser a base do edifício. Decide-se com o perfil do terreno no pé das paredes.
    eave = fit.roof.model.eave_z if fit.roof.model is not None else None
    ground_info = choose_ground(
        ground,
        wall_foot_profile(dtm, footprint),
        eave,
        parse_levels(building.existing_tags.get("building:levels")),
    )
    height = fit.height
    chosen = ground_info.elevation()
    if chosen != ground:
        height = height.model_copy(update={"value": height.value + ground - chosen})
    diagnostics["terrain_step_m"] = ground_info.step_m
    if ground_info.step_m > STEP_THRESHOLD_M:
        alt: GroundChoice = "high" if ground_info.choice == "low" else "low"
        alt_height = height.value + chosen - ground_info.elevation(alt)
        flags.append(
            _flag(
                "terrain_step",
                f"Desnível de {ground_info.step_m:.1f} m no pé das paredes; "
                f"{ground_info.reason}. Alternativa: {alt_height:.1f} m",
                "info" if ground_info.basis in ("levels", "eave") else "warning",
            )
        )
    flags += _roof_flags(fit, cfg) + _height_flags(height.value)

    part = BuildingPart(id=f"{building.key}#1", geometry=footprint, height=height, roof=fit.roof)
    update |= {"ground_elevation_m": chosen, "ground": ground_info}
    if main is not None and building.osm_type == "way" and not building.osm_parts:
        # Sugestão de partes: volume principal + volumes secundários, cada um ajustado.
        region = _shift(main.region, (-offset[0], -offset[1]))
        layout = suggest_layout(footprint, region)
        # Só se sugerem divisões topologicamente válidas (senão, a revisão faz à mão).
        if layout is not None and not validate_layout(layout, footprint):
            probe = building.model_copy(update=update | {"parts": [part]})
            update["suggested_layout"] = fit_layout(probe, layout, points, params)
    if building.osm_type == "new" and building.layout is not None:
        # Candidato com alpendres (divisão da deteção): cada parte com o seu telhado.
        probe = building.model_copy(update=update | {"parts": [part]})
        fitted = fit_layout(probe, building.layout, points, params)
        if len(fitted.fitted) == len(fitted.parts) > 1:
            return building.model_copy(
                update=update | {"qa_flags": flags, "parts": fitted.fitted, "layout": fitted}
            ), fit
    return building.model_copy(update=update | {"qa_flags": flags, "parts": [part]}), fit


def attached_clusters(footprints: list[BaseGeometry], gap_m: float = 0.3) -> list[list[int]]:
    """Grupos (≥ 2) de footprints encostados entre si, a menos de ``gap_m``."""
    parent = list(range(len(footprints)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tree = shapely.STRtree(footprints)
    src, dst = tree.query([f.buffer(gap_m) for f in footprints], predicate="intersects")
    for i, j in zip(src.tolist(), dst.tolist(), strict=True):
        if i != j:
            parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(footprints)):
        groups[find(i)].append(i)
    return [g for g in groups.values() if len(g) >= 2]


# Tarefa de um grupo encostado (passa para os processos do pool): planta conjunta e
# pontos (referencial do LiDAR), chão mais baixo, plantas e chão de cada edifício, os
# ajustes individuais, os parâmetros e a confiança mínima da forma.
_SharedTask = tuple[
    Polygon, PointSet, float, list[BaseGeometry], list[float], list[FitResult], FitParams, float
]
# Resultado: telhado do grupo (JSON) e, por edifício, o seu telhado e altura (JSON) ou None.
_SharedDone = tuple[str, list[tuple[str, str] | None]] | None


def _shared_roof_task(task: _SharedTask) -> _SharedDone:
    union, pts, ground, footprints, grounds, bests, params, min_conf = task
    group_fit = fit_roof(union, pts, ground, params)
    if (
        group_fit.best is None
        or group_fit.roof.shape.value is RoofShape.UNKNOWN
        or group_fit.roof.shape.confidence < min_conf
        or not group_beats_individuals(group_fit.best, bests)
    ):
        return None
    members: list[tuple[str, str] | None] = []
    for fp, g in zip(footprints, grounds, strict=True):
        # As tags de cada edifício descrevem o telhado sobre a planta dele: o da ponta de
        # um hipped em banda é side_hipped, os do meio gabled (ver member_roof).
        own = member_roof(group_fit.roof, union, fp, g, params)
        if own is None:
            members.append(None)  # nenhuma forma o reproduz: fica com o ajuste próprio
            continue
        height = own.height.model_copy(update={"confidence": group_fit.height.confidence})
        members.append((own.roof.model_dump_json(), height.model_dump_json()))
    return group_fit.roof.model_dump_json(), members


def apply_shared_roofs(
    results: list[Building],
    fits: list[RoofFit | None],
    point_sets: list[PointSet],
    offset: tuple[float, float],
    cfg: Config,
    max_group: int = 12,
    pool: Executor | None = None,
) -> int:
    """Telhados contínuos sobre edifícios encostados (casas geminadas/em banda).

    Para cada grupo de edifícios encostados com planta conjunta retangular, ajusta um
    único telhado ao conjunto. Se esse telhado explicar os pontos melhor do que os
    telhados individuais (mesmo critério penalizado), cada edifício recebe o telhado do
    conjunto visto sobre a sua planta (ver ``member_roof``). Os grupos são independentes:
    com ``pool``, ajustam-se em paralelo. Devolve o n.º de edifícios alterados.
    """
    params = fit_params(cfg)
    groups: list[list[int]] = []
    tasks: list[_SharedTask] = []
    for cluster in attached_clusters([b.footprint for b in results]):
        members = [
            i
            for i in cluster
            if (f := fits[i]) is not None
            and f.best is not None
            and results[i].ground_elevation_m is not None
            and results[i].layout is None  # já dividido em partes: fica como está
        ]
        if not 2 <= len(members) <= max_group:
            continue
        # Fecha as pequenas folgas entre paredes antes de medir a planta conjunta.
        union = shapely.union_all(
            [results[i].footprint.buffer(0.3, join_style="mitre") for i in members]
        ).buffer(-0.3, join_style="mitre")
        if not isinstance(union, Polygon) or union.is_empty:
            continue
        if union.area / union.minimum_rotated_rectangle.area < MIN_RECTANGULARITY:
            continue
        grounds = [results[i].ground_elevation_m for i in members]
        assert all(g is not None for g in grounds)
        groups.append(members)
        tasks.append(
            (
                _shift(union, offset),
                PointSet.concat(point_sets[i] for i in members),
                min(g for g in grounds if g is not None),
                [_shift(results[i].footprint, offset) for i in members],
                [g for g in grounds if g is not None],
                [fits[i].best for i in members],  # type: ignore[union-attr,misc]
                params,
                cfg.roofs.min_shape_confidence,
            )
        )
    changed = 0
    for members, done in zip(groups, ordered_map(pool, _shared_roof_task, tasks), strict=True):
        if done is None:
            continue
        group_keys = ",".join(results[i].key for i in members)
        for i, own in zip(members, done[1], strict=True):
            if own is None:
                continue
            b = results[i]
            roof = Roof.model_validate_json(own[0])
            height = Estimate[float].model_validate_json(own[1])
            part = BuildingPart(id=f"{b.key}#1", geometry=b.footprint, height=height, roof=roof)
            flags = [f for f in b.qa_flags if f.code not in _ROOF_FLAG_CODES]
            flags += _height_flags(height.value)
            flags.append(_flag("shared_roof", f"Telhado contínuo com: {group_keys}", "info"))
            diagnostics = dict(b.diagnostics, roof_group_size=float(len(members)))
            results[i] = b.model_copy(
                update={"parts": [part], "qa_flags": flags, "diagnostics": diagnostics}
            )
            changed += 1
    return changed


def member_roof(
    group_roof: Roof,
    group_footprint: BaseGeometry,
    footprint: BaseGeometry,
    ground_z: float,
    params: FitParams,
) -> RoofFit | None:
    """Telhado de um edifício de um grupo com telhado contínuo, como as tags dele o
    desenham: a forma S3DB (e o modelo) que reproduz a superfície do telhado do grupo sobre
    a planta do edifício. Plantas no referencial do LiDAR. None se nenhuma a reproduz.

    Um hipped sobre uma banda de casas, visto só sobre a casa da ponta, tem a água da ponta
    de um lado e é cortado na parede comum do outro: é side_hipped, não hipped (que um
    renderizador desenharia com águas nas duas pontas de cada casa).
    """
    model = group_roof.model
    if model is None:
        return None
    step = min(0.5, float(np.sqrt(footprint.area / 150.0)))  # >= ~150 amostras
    minx, miny, maxx, maxy = footprint.bounds
    gx, gy = np.meshgrid(
        np.arange(minx + step / 2, maxx, step), np.arange(miny + step / 2, maxy, step)
    )
    x, y = gx.ravel(), gy.ravel()
    inside = shapely.contains_xy(footprint, x, y)
    x, y = x[inside], y[inside]
    shape = group_roof.shape.value
    z = model_surface_z(model, shape, x, y, model.eave_z, group_footprint)
    samples = PointSet(np.column_stack([x, y, z]), np.full(len(x), 6, dtype=np.uint8))
    candidates = tuple(s for s in params.candidates if s is not RoofShape.ROUND or s is shape)
    fit = fit_surface(
        footprint,
        samples,
        ground_z,
        replace(params, candidates=candidates),
        group_roof.shape.confidence,
    )
    if fit is None:
        return None
    colour = group_roof.colour
    return replace(fit, roof=fit.roof.model_copy(update={"colour": colour}))


# --------------------------------------------------- geminados / em banda

ATTACHED_FLAG = "attached_heights"
# Incerteza da diferença de altura entre vizinhos em terreno plano (chão do MDT).
ATTACHED_BASE_NOISE_M = 0.1


def _trunc_rmse(r: npt.NDArray[np.float64], t: float) -> float:
    return float(np.sqrt(np.mean(np.minimum(r * r, t * t))))


def _shape_increase(
    pts: PointSet, own: RoofModel, other: RoofModel, shape: RoofShape, t: float
) -> float:
    """Quanto pior o telhado ``other`` (prolongado) explica estes pontos do que ``own``,
    descontado um desnível constante entre os dois (casas em banda numa encosta)."""
    x, y, z = pts.xyz[:, 0], pts.xyz[:, 1], pts.xyz[:, 2]
    r_own = z - model_surface_z(own, shape, x, y, own.eave_z)
    r_other = z - model_surface_z(other, shape, x, y, other.eave_z)
    return _trunc_rmse(r_other - np.median(r_other), t) - _trunc_rmse(r_own - np.median(r_own), t)


def _weighted_median(values: list[float], weights: list[float]) -> float:
    order = np.argsort(values)
    cum = np.cumsum(np.asarray(weights)[order])
    return float(np.asarray(values)[order][np.searchsorted(cum, cum[-1] / 2)])


def harmonize_attached(
    results: list[Building],
    point_sets: list[PointSet],
    cfg: Config,
) -> int:
    """Iguala height e roof:height de edifícios encostados com o mesmo telhado.

    Dois edifícios com parede comum são "o mesmo telhado" quando têm a mesma forma e o
    modelo de cada um, prolongado, explica os pontos do outro quase tão bem como o
    próprio (descontado um desnível constante: em banda numa encosta, cada casa sobe com
    o terreno e a altura relativa é a mesma). Juntam-se em grupos cujas alturas diferem
    menos do que as tolerâncias (todos com todos: não se encadeia uma rua inteira em
    escada) e cada grupo fica com a mediana pesada pelos pontos — o vizinho com o ajuste
    mais bem suportado decide, em vez de uma média que muda os dois. Diferenças que o
    LiDAR vê como reais (formas diferentes ou alturas além da tolerância) ficam.

    O telhado de cada um mantém a planta e a orientação; só os níveis mudam (o modelo 3D
    fica coerente com as tags). Devolve o n.º de edifícios alterados.
    """
    h_cfg = cfg.heights
    if not h_cfg.attached_harmonize or len(results) < 2:
        return 0
    t = fit_params(cfg).truncation_m
    ok = [
        i
        for i, b in enumerate(results)
        if len(b.parts) == 1
        and b.layout is None
        and b.ground_elevation_m is not None
        and (m := b.parts[0].roof.model) is not None
        and not m.skeleton  # o esqueleto não se prolonga para fora da planta
        and b.parts[0].roof.shape.value is not RoofShape.UNKNOWN
        and b.parts[0].roof.shape.confidence >= cfg.roofs.min_shape_confidence
        and len(point_sets[i]) >= cfg.heights.min_points_per_building
    ]
    if len(ok) < 2:
        return 0

    def levels(i: int) -> tuple[float, float]:
        part = results[i].parts[0]
        return part.height.value, part.roof.height.value if part.roof.height else 0.0

    def h_tolerance(i: int) -> float:
        # O telhado do LiDAR é preciso ao cm; o ruído da altura (relativa) está no chão.
        # Em terreno plano o chão de dois vizinhos coincide: diferenças de 0,2-0,3 m são
        # reais. Numa encosta (desnível no pé das paredes) o chão de cada um é incerto.
        step = results[i].diagnostics.get("terrain_step_m", 0.0)
        return min(h_cfg.attached_height_tolerance_m, ATTACHED_BASE_NOISE_M + 0.2 * step)

    gap = 0.3
    fps = [results[i].footprint for i in ok]
    src, dst = shapely.STRtree(fps).query([f.buffer(gap) for f in fps], predicate="intersects")
    edges: list[tuple[float, int, int]] = []
    for a, b in zip(src.tolist(), dst.tolist(), strict=True):
        if a >= b:
            continue
        i, j = ok[a], ok[b]
        pi, pj = results[i].parts[0], results[j].parts[0]
        shape = pi.roof.shape.value
        if pj.roof.shape.value is not shape:
            continue
        (hi, rhi), (hj, rhj) = levels(i), levels(j)
        dh, drh = abs(hi - hj), abs(rhi - rhj)
        if dh > min(h_tolerance(i), h_tolerance(j)) or drh > h_cfg.attached_roof_height_tolerance_m:
            continue
        shared = fps[a].buffer(gap).intersection(fps[b].boundary).length
        if shared < h_cfg.attached_min_shared_m:
            continue
        mi, mj = pi.roof.model, pj.roof.model
        assert mi is not None and mj is not None
        limit = h_cfg.attached_max_shape_increase_m
        if (
            _shape_increase(point_sets[i], mi, mj, shape, t) > limit
            or _shape_increase(point_sets[j], mj, mi, shape, t) > limit
        ):
            continue
        edges.append((max(dh, drh), i, j))
    if not edges:
        return 0

    # Grupos por ligação completa: junta-se pela diferença mais pequena primeiro e só se
    # o grupo junto continuar dentro das tolerâncias.
    group: dict[int, int] = {i: i for i in ok}
    members: dict[int, list[int]] = {i: [i] for i in ok}
    for _, i, j in sorted(edges):
        gi, gj = group[i], group[j]
        if gi == gj:
            continue
        merged = members[gi] + members[gj]
        hs, rhs = zip(*(levels(k) for k in merged), strict=True)
        if (
            max(hs) - min(hs) > min(h_tolerance(k) for k in merged)
            or max(rhs) - min(rhs) > h_cfg.attached_roof_height_tolerance_m
        ):
            continue
        members[gi] = merged
        del members[gj]
        for k in merged:
            group[k] = gi

    changed = 0
    for keys in members.values():
        if len(keys) < 2:
            continue
        weights = [
            results[k].diagnostics.get("n_points", 1.0)
            * results[k].diagnostics.get("inlier_ratio", 1.0)
            for k in keys
        ]
        hs, rhs = zip(*(levels(k) for k in keys), strict=True)
        h_target = _weighted_median(list(hs), weights)
        rh_target = min(_weighted_median(list(rhs), weights), h_target)
        names = sorted(results[k].key for k in keys)
        others = ", ".join(names[:6]) + (f" (+{len(names) - 6})" if len(names) > 6 else "")
        for k in keys:
            b = results[k]
            part = b.parts[0]
            h, rh = levels(k)
            if abs(h - h_target) < 1e-6 and abs(rh - rh_target) < 1e-6:
                continue
            roof = part.roof
            model = roof.model
            assert model is not None and b.ground_elevation_m is not None
            new_roof = roof.model_copy(
                update={
                    "height": (
                        roof.height.model_copy(update={"value": rh_target})
                        if roof.height is not None
                        else None
                    ),
                    "model": model.model_copy(
                        update={
                            "eave_z": b.ground_elevation_m + h_target - rh_target,
                            "roof_height": rh_target if roof.height is not None else 0.0,
                        }
                    ),
                }
            )
            new_part = part.model_copy(
                update={
                    "height": part.height.model_copy(update={"value": h_target}),
                    "roof": new_roof,
                }
            )
            what = f"height {h:.2f} → {h_target:.2f} m"
            if roof.height is not None:
                what += f", roof:height {rh:.2f} → {rh_target:.2f} m"
            flags = [f for f in b.qa_flags if f.code not in ("height_outlier", ATTACHED_FLAG)]
            flags += _height_flags(h_target)
            flags.append(
                _flag(
                    ATTACHED_FLAG,
                    f"Igualado aos encostados com o mesmo telhado ({others}): {what}",
                    "info",
                )
            )
            diagnostics = dict(
                b.diagnostics,
                attached_group_size=float(len(keys)),
                attached_dh_m=h_target - h,
                attached_droof_m=rh_target - rh,
            )
            results[k] = b.model_copy(
                update={"parts": [new_part], "qa_flags": flags, "diagnostics": diagnostics}
            )
            changed += 1
    return changed


# ------------------------------------------------------------- em paralelo

# Recursos de cada processo do pool (abertos uma vez, no arranque do processo).
_worker_dtm: DtmMosaic | None = None
_worker_cfg: Config | None = None

# Entre processos, os modelos pydantic vão em JSON (o mesmo do GeoPackage, sem perdas):
# os genéricos (Estimate[float]) não passam pelo pickle.
_Task = tuple[str, PointSet, tuple[float, float]]
_Done = tuple[str, tuple[str, str, FitResult | None] | None]


def _init_worker(dtm_paths: list[Path], cfg: Config) -> None:
    global _worker_dtm, _worker_cfg
    _worker_dtm, _worker_cfg = DtmMosaic(dtm_paths), cfg
    atexit.register(_worker_dtm.close)  # fechar o GDAL antes de o processo terminar


def _reconstruct_here(task: _Task, dtm: DtmMosaic, cfg: Config) -> _Done:
    b_json, pts, offset = task
    built, fit = reconstruct_building(Building.model_validate_json(b_json), pts, dtm, offset, cfg)
    # A lista de todas as hipóteses só serve o próprio ajuste: não se transporta.
    payload = (
        None
        if fit is None
        else (fit.roof.model_dump_json(), fit.height.model_dump_json(), fit.best)
    )
    return built.model_dump_json(), payload


def _reconstruct_task(task: _Task) -> _Done:
    assert _worker_dtm is not None and _worker_cfg is not None
    return _reconstruct_here(task, _worker_dtm, _worker_cfg)


def _unpack(done: _Done) -> tuple[Building, RoofFit | None]:
    b_json, payload = done
    if payload is None:
        return Building.model_validate_json(b_json), None
    roof, height, best = payload
    fit = RoofFit(Roof.model_validate_json(roof), Estimate[float].model_validate_json(height), best)
    return Building.model_validate_json(b_json), fit


def reconstruct_area(
    buildings: list[Building],
    lidar_tiles: dict[str, Path],
    dtm_tiles: dict[str, Path],
    cfg: Config,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[Building], list[TileReport]]:
    """Reconstrói os edifícios cobertos por LAZ + MDT. Os restantes não são devolvidos."""
    available = sorted(set(lidar_tiles) & set(dtm_tiles))
    if not available:
        raise RuntimeError("Sem quadrículas com LAZ e MDT descarregados (osm3d dgt ...)")
    coverage = shapely.union_all([box(*TileKey.from_dgt_name(t).bounds) for t in available])
    covered = [b for b in buildings if b.footprint.within(coverage)]
    log.info("%d de %d edifícios dentro da cobertura LiDAR", len(covered), len(buildings))

    by_tile: dict[str, list[Building]] = defaultdict(list)
    for b in covered:
        c = b.footprint.centroid
        by_tile[tile_of(c.x, c.y).dgt_name].append(b)

    margin = cfg.footprints.registration_max_shift_m + 1.0
    results: list[Building] = []
    reports: list[TileReport] = []
    dtm_paths = [dtm_tiles[t] for t in available]
    workers = worker_count(cfg.workers, len(covered))
    with (
        DtmMosaic(dtm_paths) as dtm,
        process_pool(workers, _init_worker, (dtm_paths, cfg)) as pool,
    ):
        if progress and pool is not None:
            progress(f"  ajuste dos telhados em {workers} processos")
        for tile in sorted(by_tile):
            group = by_tile[tile]
            minx, miny, maxx, maxy = shapely.union_all([b.footprint for b in group]).bounds
            bbox = (minx - margin, miny - margin, maxx + margin, maxy + margin)
            points = read_points(
                (lidar_tiles[t] for t in available),
                bbox,
                cfg.heights.building_classes,
                cache_dir=cfg.paths.lidar_cache,
            )
            offset, iou0, iou1 = estimate_tile_offset(
                [b.footprint for b in group],
                points,
                bbox,
                max_shift_m=cfg.footprints.registration_max_shift_m,
            )
            reports.append(TileReport(tile, len(group), len(points), offset, iou0, iou1))
            if progress:
                progress(
                    f"  quadrícula {tile}: {len(group)} edifícios, {len(points):,} pontos, "
                    f"desloc. ({offset[0]:+.2f}, {offset[1]:+.2f}) m, IoU {iou0:.3f}→{iou1:.3f}"
                )
            indices = assign_points(points, [b.footprint for b in group], offset)
            point_sets = [points.subset(idx) for idx in indices]
            tasks = [
                (b.model_dump_json(), pts, offset) for b, pts in zip(group, point_sets, strict=True)
            ]
            done = [
                _unpack(d)
                for d in ordered_map(
                    pool, _reconstruct_task, tasks, lambda t: _reconstruct_here(t, dtm, cfg)
                )
            ]
            tile_results = [built for built, _ in done]
            fits = [fit for _, fit in done]
            shared = apply_shared_roofs(tile_results, fits, point_sets, offset, cfg, pool=pool)
            if progress and shared:
                progress(f"  quadrícula {tile}: {shared} edifícios com telhado partilhado")
            same = harmonize_attached(tile_results, point_sets, cfg)
            if progress and same:
                progress(f"  quadrícula {tile}: {same} encostados com alturas igualadas")
            results.extend(tile_results)
    return results, reports
