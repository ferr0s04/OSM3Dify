"""Ajuste de modelos paramétricos de telhado a pontos LiDAR (Fase 3).

Para cada hipótese (forma × eixo × sentido):

1. o modelo é linear em (z0, h): ``z = z0 + h·f(u, v)``;
2. ajuste robusto: IRLS com pesos de Huber, seguido de mínimos quadrados só com
   inliers (|resíduo| ≤ ``truncation_m``), que exclui vegetação, chaminés e claraboias;
3. pontuação tipo BIC com perda truncada:
   ``n·ln(Σ min(r², t²)/n + ε) + k·ln(n)``. Cada outlier custa o mesmo em todos os
   modelos, por isso não decide a escolha.

A confiança vem da diferença de pontuação entre a melhor hipótese e a melhor
hipótese de *outra* forma. RMSE alto ou poucos inliers → ``unknown``.

Os pontos têm de vir já selecionados para a parte (footprint + co-registo) e
filtrados por classe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import Estimate, Roof, RoofModel, RoofShape, Source
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_models import N_PARAMS, LocalFrame, roof_surface
from osm3d.reconstruction.skeleton import (
    SKELETON_MAX_RECTANGULARITY,
    SKELETON_SHAPES,
    has_skeleton,
    rectangularity,
    skeleton_basis,
)

Array = npt.NDArray[np.float64]

# Ruído mínimo assumido (m): evita ln(0) com dados perfeitos e nivela diferenças
# abaixo da precisão do LiDAR.
_NOISE_FLOOR_M = 0.03
# Escala da diferença de pontuação para a confiança: Δ=10 → 0.63, Δ=30 → 0.95.
_CONFIDENCE_SCALE = 10.0
# Grelha do comprimento relativo da cumeeira dos telhados hipped (meia cumeeira / a).
# Abaixo de 0,10 o telhado é, na prática, uma pirâmide (hipótese própria).
HIPPED_RIDGE_RATIOS = tuple(round(r, 2) for r in np.arange(0.10, 0.951, 0.05))
# side_hipped: cumeeira / comprimento. Abaixo de 0,3 a ponta em hip ocuparia mais de
# 70 % do comprimento, o que é implausível; perto de 1 aproxima-se de gabled.
SIDE_HIPPED_RIDGE_RATIOS = tuple(round(r, 2) for r in np.arange(0.30, 0.951, 0.05))
# gabled: posição da cumeeira em v (fração da meia-largura); 0 = a meio.
GABLED_RIDGE_OFFSETS = (-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3)


@dataclass(frozen=True)
class FitParams:
    candidates: tuple[RoofShape, ...] = (
        RoofShape.FLAT,
        RoofShape.SKILLION,
        RoofShape.GABLED,
        RoofShape.HIPPED,
        RoofShape.SIDE_HIPPED,
        RoofShape.PYRAMIDAL,
    )
    max_rmse_m: float = 0.35
    flat_max_slope_deg: float = 5.0
    truncation_m: float = 0.5
    min_points: int = 30
    min_inlier_ratio: float = 0.6
    height_percentile: float = 95.0  # para o fallback quando a forma é desconhecida


@dataclass(frozen=True)
class Hypothesis:
    shape: RoofShape
    frame: LocalFrame
    flip: bool = False  # skillion: água virada para −v; side_hipped: hip em −u
    ridge_ratio: float | None = None  # hipped/side_hipped: cumeeira / comprimento
    ridge_offset: float | None = None  # gabled: posição da cumeeira (fração de b)
    # gabled/hipped por straight skeleton sobre esta planta (não retangular).
    skeleton: BaseGeometry | None = None

    def basis(self, x: Array, y: Array) -> Array | None:
        """f(u, v) do modelo, ou ``None`` para telhado plano."""
        if self.shape is RoofShape.FLAT:
            return None
        if self.skeleton is not None:
            return skeleton_basis(self.skeleton, self.shape, x, y)
        u, v = self.frame.to_local(x, y)
        a, b = self.frame.half_length, self.frame.half_width
        return roof_surface(
            self.shape,
            u,
            v,
            a,
            b,
            z0=0.0,
            h=1.0,
            ridge_ratio=self.ridge_ratio,
            flip=self.flip,
            ridge_offset=self.ridge_offset,
        )

    def slope_deg(self, h: float) -> float:
        a, b = self.frame.half_length, self.frame.half_width
        run = {
            RoofShape.SKILLION: 2 * b,
            RoofShape.GABLED: b,
            RoofShape.HIPPED: b,
            RoofShape.SIDE_HIPPED: b,
            RoofShape.PYRAMIDAL: min(a, b),
        }.get(self.shape)
        return 0.0 if run is None else math.degrees(math.atan2(h, run))

    @property
    def ridge_azimuth_deg(self) -> float | None:
        if self.shape in (RoofShape.GABLED, RoofShape.HIPPED, RoofShape.SIDE_HIPPED):
            return self.frame.azimuth_deg % 180.0
        return None

    @property
    def facing_azimuth_deg(self) -> float | None:
        """``roof:direction``: para onde olha a água (skillion) ou a ponta em hip."""
        if self.shape is RoofShape.SKILLION:
            return (self.frame.v_azimuth_deg + (180.0 if self.flip else 0.0)) % 360.0
        if self.shape is RoofShape.SIDE_HIPPED:
            return (self.frame.azimuth_deg + (180.0 if self.flip else 0.0)) % 360.0
        return None


@dataclass(frozen=True)
class FitResult:
    hypothesis: Hypothesis
    z0: float
    h: float
    rmse_m: float  # sobre os inliers
    inlier_ratio: float
    score: float  # menor é melhor
    rss: float = 0.0  # Σ min(r², t²) — perda truncada
    n: int = 0

    @property
    def shape(self) -> RoofShape:
        return self.hypothesis.shape

    @property
    def n_params(self) -> int:
        return N_PARAMS[self.shape]

    @property
    def top_z(self) -> float:
        return self.z0 + max(self.h, 0.0)


@dataclass(frozen=True)
class RoofFit:
    roof: Roof
    height: Estimate[float]  # chão → topo do telhado
    best: FitResult | None
    results: list[FitResult] = field(default_factory=list)  # ordenados por pontuação


def hypotheses_for(
    geometry: BaseGeometry,
    candidates: tuple[RoofShape, ...],
    quick: bool = False,
    free_ridge: bool = False,
) -> list[Hypothesis]:
    """Hipóteses a testar.

    ``quick``: grelhas de cumeeira mais grossas (triagem).
    ``free_ridge``: cumeeira do gabled em posição livre. Só se usa em zonas que não
    coincidem com as paredes (volume principal). Na planta inteira deixaria um gabled
    assimétrico "esconder" volumes secundários (testado em Vila do Conde: 7 hipped
    passaram a gabled).
    """
    frame = LocalFrame.from_geometry(geometry)
    hipped_ratios = HIPPED_RIDGE_RATIOS[::3] if quick else HIPPED_RIDGE_RATIOS
    side_ratios = SIDE_HIPPED_RIDGE_RATIOS[::3] if quick else SIDE_HIPPED_RIDGE_RATIOS
    if not free_ridge:
        gabled_offsets: tuple[float, ...] = (0.0,)
    else:
        gabled_offsets = GABLED_RIDGE_OFFSETS[::3] if quick else GABLED_RIDGE_OFFSETS
    across = frame.rotated()
    hyps: list[Hypothesis] = []
    for shape in candidates:
        match shape:
            case RoofShape.FLAT | RoofShape.PYRAMIDAL:
                hyps.append(Hypothesis(shape, frame))
            case RoofShape.GABLED:
                # Cumeeira em posição livre: telhados reais assimétricos e zonas
                # (volume principal) que não coincidem com as paredes.
                hyps += [
                    Hypothesis(shape, f, ridge_offset=c)
                    for f in (frame, across)
                    for c in gabled_offsets
                ]
            case RoofShape.HIPPED:
                # Comprimento da cumeeira livre: as águas das pontas podem ter inclinação
                # diferente das laterais (frequente em Portugal).
                hyps += [Hypothesis(shape, frame, ridge_ratio=r) for r in hipped_ratios]
            case RoofShape.SIDE_HIPPED:
                # Três águas (hip numa só ponta). A cumeeira pode seguir qualquer dos
                # lados: metade de um hipped dividido ao meio fica com ela no lado curto.
                hyps += [
                    Hypothesis(shape, f, flip, ridge_ratio=r)
                    for f in (frame, across)
                    for flip in (False, True)
                    for r in side_ratios
                ]
            case RoofShape.SKILLION:
                hyps += [
                    Hypothesis(shape, f, flip) for f in (frame, across) for flip in (False, True)
                ]
    # Plantas em L, T ou dobradas: telhado que segue o eixo do edifício, como os
    # renderizadores o desenham (ver reconstruction/skeleton.py). Só na planta inteira.
    if not free_ridge and rectangularity(geometry) < SKELETON_MAX_RECTANGULARITY:
        hyps += [
            Hypothesis(shape, frame, skeleton=geometry)
            for shape in SKELETON_SHAPES
            if shape in candidates and has_skeleton(geometry, shape)
        ]
    return hyps


def _robust_linear_fit(
    f: Array | None, z: Array, truncation: float, iterations: int = 8
) -> tuple[float, float, Array]:
    """Ajusta z ≈ z0 + h·f. Devolve (z0, h, resíduos)."""
    cols = [np.ones_like(z)] if f is None else [np.ones_like(z), f]
    design = np.column_stack(cols)
    w = np.ones_like(z)
    coef = np.zeros(design.shape[1])
    for i in range(iterations):
        sw = np.sqrt(w)
        coef, *_ = np.linalg.lstsq(design * sw[:, None], z * sw, rcond=None)
        r = z - design @ coef
        if i < iterations - 2:
            # Huber
            sigma = max(1.4826 * float(np.median(np.abs(r - np.median(r)))), _NOISE_FLOOR_M)
            c = 1.345 * sigma
            a = np.abs(r)
            w = np.where(a <= c, 1.0, c / np.maximum(a, 1e-12))
        else:
            # Últimas iterações: só inliers.
            w = (np.abs(r) <= truncation).astype(float)
            if w.sum() < design.shape[1]:
                w = np.ones_like(z)
    r = z - design @ coef
    z0 = float(coef[0])
    h = float(coef[1]) if f is not None else 0.0
    return z0, h, r


def _evaluate(hyp: Hypothesis, xyz: Array, params: FitParams) -> FitResult | None:
    f = hyp.basis(xyz[:, 0], xyz[:, 1])
    z0, h, r = _robust_linear_fit(f, xyz[:, 2], params.truncation_m)
    if hyp.shape is not RoofShape.FLAT and (h <= 0 or hyp.slope_deg(h) < params.flat_max_slope_deg):
        return None  # declive nulo/invertido: é o modelo plano que deve ganhar
    n = len(r)
    inliers = np.abs(r) <= params.truncation_m
    rho = float(np.minimum(r**2, params.truncation_m**2).sum())
    score = penalized_score(rho, n, N_PARAMS[hyp.shape])
    rmse = float(np.sqrt(np.mean(r[inliers] ** 2))) if inliers.any() else float("inf")
    return FitResult(hyp, z0, h, rmse, float(inliers.mean()), score, rho, n)


def penalized_score(rss: float, n: int, n_params: int) -> float:
    """Pontuação tipo BIC com perda truncada (menor é melhor)."""
    return n * math.log(rss / n + _NOISE_FLOOR_M**2) + n_params * math.log(n)


def group_beats_individuals(group: FitResult, members: list[FitResult]) -> bool:
    """Um só telhado para o grupo explica os pontos melhor do que um por edifício?

    Compara, sobre os mesmos pontos, um modelo único com a soma dos modelos
    individuais (cada um com os seus parâmetros), pelo mesmo critério penalizado.
    """
    n = sum(m.n for m in members)
    if n == 0 or group.n != n:
        return False
    individual = penalized_score(sum(m.rss for m in members), n, sum(m.n_params for m in members))
    return penalized_score(group.rss, n, group.n_params) < individual


def _unknown(
    points: Array, ground_z: float, params: FitParams, results: list[FitResult]
) -> RoofFit:
    top = float(np.percentile(points[:, 2], params.height_percentile))
    conf = 0.5 * min(1.0, len(points) / 200.0)
    return RoofFit(
        roof=Roof(
            shape=Estimate[RoofShape](value=RoofShape.UNKNOWN, confidence=0.0, source=Source.LIDAR)
        ),
        height=Estimate[float](value=top - ground_z, confidence=conf, source=Source.LIDAR),
        best=results[0] if results else None,
        results=results,
    )


def fit_roof(
    geometry: BaseGeometry,
    points: PointSet,
    ground_z: float,
    params: FitParams | None = None,
    *,
    quick: bool = False,
    free_ridge: bool = False,
) -> RoofFit:
    """Ajusta o telhado de uma parte e devolve forma, parâmetros S3DB e altura total."""
    params = params or FitParams()
    xyz = points.xyz
    if len(xyz) == 0:
        raise ValueError("Sem pontos para ajustar o telhado")
    if len(xyz) < params.min_points:
        return _unknown(xyz, ground_z, params, [])

    results = [
        res
        for hyp in hypotheses_for(geometry, params.candidates, quick, free_ridge)
        if (res := _evaluate(hyp, xyz, params)) is not None
    ]
    results.sort(key=lambda r: r.score)
    if not results:
        return _unknown(xyz, ground_z, params, results)

    best = results[0]
    if best.rmse_m > params.max_rmse_m or best.inlier_ratio < params.min_inlier_ratio:
        return _unknown(xyz, ground_z, params, results)

    rival = next((r for r in results if r.shape is not best.shape), None)
    delta = math.inf if rival is None else rival.score - best.score
    shape_conf = 1.0 - math.exp(-delta / _CONFIDENCE_SCALE)
    fit_quality = max(0.0, 1.0 - best.rmse_m / (2 * params.max_rmse_m))
    height_conf = min(1.0, len(xyz) / 200.0) * fit_quality

    hyp = best.hypothesis
    roof = Roof(
        shape=Estimate[RoofShape](value=best.shape, confidence=shape_conf, source=Source.LIDAR),
        height=(
            None
            if best.shape is RoofShape.FLAT
            else Estimate[float](value=best.h, confidence=shape_conf, source=Source.LIDAR)
        ),
        ridge_azimuth_deg=hyp.ridge_azimuth_deg,
        facing_azimuth_deg=hyp.facing_azimuth_deg,
        rmse_m=best.rmse_m,
        model=RoofModel(
            cx=hyp.frame.cx,
            cy=hyp.frame.cy,
            azimuth_deg=hyp.frame.azimuth_deg,
            half_length=hyp.frame.half_length,
            half_width=hyp.frame.half_width,
            flip=hyp.flip,
            ridge_ratio=hyp.ridge_ratio,
            ridge_offset=hyp.ridge_offset or None,
            skeleton=hyp.skeleton is not None,
            eave_z=best.z0,
            roof_height=0.0 if best.shape is RoofShape.FLAT else best.h,
        ),
    )
    height = Estimate[float](
        value=best.top_z - ground_z, confidence=height_conf, source=Source.LIDAR
    )
    return RoofFit(roof=roof, height=height, best=best, results=results)


@dataclass(frozen=True)
class MainVolumeFit:
    fit: RoofFit
    region: BaseGeometry  # zona do volume principal (no referencial do LiDAR)
    fraction: float  # área da zona / área do footprint
    point_mask: npt.NDArray[np.bool_]


MAIN_VOLUME_CUTS = (0.15, 0.25, 0.35, 0.45)
MAIN_VOLUME_MIN_FRACTION = 0.55
MAIN_VOLUME_MIN_INLIERS = 0.8
MAIN_VOLUME_AREA_WEIGHT = 0.25
# A triagem das zonas usa uma amostra de pontos (o ajuste final usa todos).
MAIN_VOLUME_TRIAL_POINTS = 1000


def fit_main_volume(
    geometry: BaseGeometry,
    points: PointSet,
    ground_z: float,
    params: FitParams | None = None,
) -> MainVolumeFit | None:
    """Telhado do volume principal quando o footprint inclui um volume secundário.

    Casos típicos (todos dentro do mesmo contorno OSM):

    - anexo, alpendre ou garagem mais baixos ao longo de um lado;
    - volume numa ponta (torre, corpo mais alto);
    - os dois ao mesmo tempo (igrejas: nave + capelas laterais + torre).

    Nenhum modelo explica a planta inteira e até o referencial fica errado. Recorta-se
    o retângulo mínimo numa ponta e/ou num lado (15–45 % cada), com pelo menos 55 % da
    área a ficar. Entre as zonas em que um modelo
    explica ≥ 80 % dos pontos, escolhe-se a que maximiza ``inliers + 0,25 × fração``.
    Assim prefere-se o recorte que remove exatamente o volume secundário: um recorte
    curto demais deixa sobras (menos inliers), um longo demais perde área.
    Devolve ``None`` se não houver nenhuma zona aceitável.
    """
    params = params or FitParams()
    frame = LocalFrame.from_geometry(geometry)
    a, b = frame.half_length, frame.half_width
    u, v = frame.to_local(points.xyz[:, 0], points.xyz[:, 1])
    area = float(geometry.area)
    best: tuple[float, float, BaseGeometry, npt.NDArray[np.bool_]] | None = None
    rng = np.random.default_rng(0)
    sample = np.zeros(len(points), dtype=bool)
    sample[rng.permutation(len(points))[: MAIN_VOLUME_TRIAL_POINTS * 2]] = True

    # Um recorte opcional numa ponta (eixo u) combinado com um opcional num lado
    # (eixo v): cobre o anexo ao longo de um lado, o volume numa ponta e os dois ao
    # mesmo tempo (p.ex. igreja: nave + capelas laterais + torre).
    def options(half: float) -> list[tuple[float, float]]:
        return [(0.0, 0.0)] + [
            (sign, 2 * half * cut) for sign in (1.0, -1.0) for cut in MAIN_VOLUME_CUTS
        ]

    for su, du in options(a):
        for sv, dv in options(b):
            if du == 0 and dv == 0:
                continue
            lo_u, hi_u = (-a, a - du) if su > 0 else (-a + du, a)
            lo_v, hi_v = (-b, b - dv) if sv > 0 else (-b + dv, b)
            corners = np.array([[lo_u, lo_v], [hi_u, lo_v], [hi_u, hi_v], [lo_u, hi_v]])
            wx, wy = frame.to_world(corners[:, 0], corners[:, 1])
            region = Polygon(np.column_stack([wx, wy])).intersection(geometry)
            fraction = float(region.area) / area if area else 0.0
            if fraction < MAIN_VOLUME_MIN_FRACTION:
                continue
            if best is not None and 1.0 + MAIN_VOLUME_AREA_WEIGHT * fraction <= best[0]:
                continue  # nem com 100 % de inliers superaria a melhor zona
            mask = (u >= lo_u) & (u <= hi_u) & (v >= lo_v) & (v <= hi_v)
            if mask.sum() < params.min_points:
                continue
            trial_mask = mask & sample if mask.sum() > MAIN_VOLUME_TRIAL_POINTS else mask
            trial = fit_roof(
                region, points.subset(trial_mask), ground_z, params, quick=True, free_ridge=True
            )
            if (
                trial.best is None
                or trial.roof.shape.value is RoofShape.UNKNOWN
                or trial.best.inlier_ratio < MAIN_VOLUME_MIN_INLIERS
            ):
                continue
            objective = trial.best.inlier_ratio + MAIN_VOLUME_AREA_WEIGHT * fraction
            if best is None or objective > best[0]:
                best = (objective, fraction, region, mask)
    if best is None:
        return None
    _, fraction, region, mask = best
    final = fit_roof(region, points.subset(mask), ground_z, params, free_ridge=True)
    if final.roof.shape.value is RoofShape.UNKNOWN:
        return None
    return MainVolumeFit(final, region, fraction, mask)
