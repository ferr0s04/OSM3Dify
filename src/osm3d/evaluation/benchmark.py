"""Comparação das estimativas com um conjunto de referência.

Referência = edifícios da área de estudo já mapeados no OSM com tags S3DB
(``height``, ``roof:shape``, ...) + anotações manuais em ``data/reference/``.

Métricas: erro absoluto de altura (mediana, P90), matriz de confusão de
``roof:shape``, erro angular de orientação, % de edifícios sinalizados.
"""

from __future__ import annotations

from dataclasses import dataclass

from osm3d.domain.models import Building


@dataclass(frozen=True)
class BenchmarkReport:
    n_buildings: int
    height_mae_m: float | None
    height_p90_abs_error_m: float | None
    roof_shape_accuracy: float | None
    flagged_ratio: float


def evaluate(predicted: list[Building], reference: list[Building]) -> BenchmarkReport:
    raise NotImplementedError("Fase 0/2: avaliação")
