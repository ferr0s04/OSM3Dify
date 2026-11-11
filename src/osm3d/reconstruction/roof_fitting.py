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
from shapely.geometry.base import BaseGeometry

from osm3d.domain.models import Estimate, Roof, RoofShape, Source
from osm3d.geospatial.point_clouds import PointSet
from osm3d.reconstruction.roof_models import N_PARAMS, LocalFrame, roof_surface

Array = npt.NDArray[np.float64]

# Ruído mínimo assumido (m): evita ln(0) com dados perfeitos e nivela diferenças
# abaixo da precisão do LiDAR.
_NOISE_FLOOR_M = 0.03
# Escala da diferença de pontuação para a confiança: Δ=10 → 0.63, Δ=30 → 0.95.
_CONFIDENCE_SCALE = 10.0
# Retângulos com lados a menos de 10 % de diferença: hipped ≈ pyramidal.
_SQUARE_TOLERANCE = 0.10


@dataclass(frozen=True)
class FitParams:
    candidates: tuple[RoofShape, ...] = (
        RoofShape.FLAT,
        RoofShape.SKILLION,
        RoofShape.GABLED,
        RoofShape.HIPPED,
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
    flip: bool = False  # skillion: água virada para -v em vez de +v

    def basis(self, x: Array, y: Array) -> Array | None:
        """f(u, v) do modelo, ou ``None`` para telhado plano."""
        if self.shape is RoofShape.FLAT:
            return None
        u, v = self.frame.to_local(x, y)
        if self.flip:
            v = -v
        a, b = self.frame.half_length, self.frame.half_width
        return roof_surface(self.shape, u, v, a, b, z0=0.0, h=1.0)

    def slope_deg(self, h: float) -> float:
        a, b = self.frame.half_length, self.frame.half_width
        run = {
            RoofShape.SKILLION: 2 * b,
            RoofShape.GABLED: b,
            RoofShape.HIPPED: b,
            RoofShape.PYRAMIDAL: min(a, b),
        }.get(self.shape)
        return 0.0 if run is None else math.degrees(math.atan2(h, run))

    @property
    def ridge_azimuth_deg(self) -> float | None:
        if self.shape in (RoofShape.GABLED, RoofShape.HIPPED):
            return self.frame.azimuth_deg % 180.0
        return None

    @property
    def facing_azimuth_deg(self) -> float | None:
        if self.shape is not RoofShape.SKILLION:
            return None
        return (self.frame.v_azimuth_deg + (180.0 if self.flip else 0.0)) % 360.0


@dataclass(frozen=True)
class FitResult:
    hypothesis: Hypothesis
    z0: float
    h: float
    rmse_m: float  # sobre os inliers
    inlier_ratio: float
    score: float  # menor é melhor

    @property
    def shape(self) -> RoofShape:
        return self.hypothesis.shape

    @property
    def top_z(self) -> float:
        return self.z0 + max(self.h, 0.0)


@dataclass(frozen=True)
class RoofFit:
    roof: Roof
    height: Estimate[float]  # chão → topo do telhado
    best: FitResult | None
    results: list[FitResult] = field(default_factory=list)  # ordenados por pontuação


def hypotheses_for(geometry: BaseGeometry, candidates: tuple[RoofShape, ...]) -> list[Hypothesis]:
    frame = LocalFrame.from_geometry(geometry)
    across = frame.rotated()
    near_square = (frame.half_length - frame.half_width) <= _SQUARE_TOLERANCE * frame.half_length
    hyps: list[Hypothesis] = []
    for shape in candidates:
        match shape:
            case RoofShape.FLAT | RoofShape.PYRAMIDAL:
                hyps.append(Hypothesis(shape, frame))
            case RoofShape.GABLED:
                hyps += [Hypothesis(shape, frame), Hypothesis(shape, across)]
            case RoofShape.HIPPED:
                # Em plantas quase quadradas, hipped degenera em pyramidal.
                if not (near_square and RoofShape.PYRAMIDAL in candidates):
                    hyps.append(Hypothesis(shape, frame))
            case RoofShape.SKILLION:
                hyps += [
                    Hypothesis(shape, f, flip) for f in (frame, across) for flip in (False, True)
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
    score = n * math.log(rho / n + _NOISE_FLOOR_M**2) + N_PARAMS[hyp.shape] * math.log(n)
    rmse = float(np.sqrt(np.mean(r[inliers] ** 2))) if inliers.any() else float("inf")
    return FitResult(hyp, z0, h, rmse, float(inliers.mean()), score)


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
        for hyp in hypotheses_for(geometry, params.candidates)
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
    )
    height = Estimate[float](
        value=best.top_z - ground_z, confidence=height_conf, source=Source.LIDAR
    )
    return RoofFit(roof=roof, height=height, best=best, results=results)
