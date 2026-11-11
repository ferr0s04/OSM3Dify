"""Avaliação contra as tags S3DB já mapeadas no OSM.

- ``roof:shape``: exatidão e matriz de confusão (só edifícios com a tag no OSM).
- Altura: não há ``height`` no OSM da área; usa-se ``building:levels`` como
  referência fraca, comparando a altura das paredes (``height − roof:height``)
  com o número de pisos (altura implícita por piso).
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from osm3d.domain.models import Building, RoofShape

# Valores OSM equivalentes às formas suportadas.
_SHAPE_ALIASES = {"lean_to": "skillion"}
_SUPPORTED = {s.value for s in RoofShape} - {"unknown"}
SHAPE_LABELS = ["flat", "skillion", "gabled", "hipped", "pyramidal", "outra", "unknown"]


def normalize_osm_shape(value: str | None) -> str | None:
    if value is None:
        return None
    v = _SHAPE_ALIASES.get(value.strip().lower(), value.strip().lower())
    return v if v in _SUPPORTED else "outra"


@dataclass
class ShapeEvaluation:
    n_reference: int = 0
    n_predicted: int = 0  # previsões exportáveis (forma conhecida e confiança ≥ limiar)
    n_correct: int = 0
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def accuracy(self) -> float | None:
        return self.n_correct / self.n_predicted if self.n_predicted else None

    @property
    def coverage(self) -> float | None:
        return self.n_predicted / self.n_reference if self.n_reference else None


@dataclass
class HeightEvaluation:
    n: int = 0
    storey_heights: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        if not self.storey_heights:
            return {}
        a = np.array(self.storey_heights)
        return {
            "mediana": float(np.median(a)),
            "p10": float(np.percentile(a, 10)),
            "p90": float(np.percentile(a, 90)),
            "entre 2.4 e 4.0 m": float(((a >= 2.4) & (a <= 4.0)).mean()),
        }


@dataclass
class EvaluationReport:
    n_buildings: int
    n_reconstructed: int
    flagged_ratio: float
    shapes: ShapeEvaluation
    shapes_rectangular: ShapeEvaluation
    heights: HeightEvaluation
    rows: list[dict[str, object]]


def evaluate(buildings: list[Building], min_shape_confidence: float) -> EvaluationReport:
    shapes, shapes_rect, heights = ShapeEvaluation(), ShapeEvaluation(), HeightEvaluation()
    rows: list[dict[str, object]] = []
    reconstructed = [b for b in buildings if b.parts]
    for b in reconstructed:
        part = b.parts[0]
        roof = part.roof
        pred = roof.shape.value.value
        if (
            roof.shape.value is not RoofShape.UNKNOWN
            and roof.shape.confidence < min_shape_confidence
        ):
            pred = "unknown"
        ref = normalize_osm_shape(b.existing_tags.get("roof:shape"))
        rect = b.diagnostics.get("rectangularity", 0.0) >= 0.85
        if ref is not None:
            for ev in (shapes, shapes_rect) if rect else (shapes,):
                ev.n_reference += 1
                ev.confusion[(ref, pred)] += 1
                if pred != "unknown":
                    ev.n_predicted += 1
                    ev.n_correct += int(pred == ref)

        wall = part.height.value - (roof.height.value if roof.height else 0.0)
        levels_raw = b.existing_tags.get("building:levels")
        storey = None
        try:
            levels = float(levels_raw) if levels_raw else None
        except ValueError:
            levels = None
        if levels and levels > 0 and wall > 0:
            storey = wall / levels
            heights.n += 1
            heights.storey_heights.append(storey)

        rows.append(
            {
                "osm_key": b.key,
                "osm_roof_shape": b.existing_tags.get("roof:shape"),
                "pred_roof_shape": pred,
                "shape_conf": round(roof.shape.confidence, 3),
                "match": (pred == ref) if ref and pred != "unknown" else None,
                "height": round(part.height.value, 2),
                "roof_height": round(roof.height.value, 2) if roof.height else None,
                "osm_levels": levels_raw,
                "storey_height": round(storey, 2) if storey else None,
                "rectangularity": round(b.diagnostics.get("rectangularity", 0.0), 3),
                "n_points": int(b.diagnostics.get("n_points", 0)),
                "rmse_m": round(roof.rmse_m, 3) if roof.rmse_m is not None else None,
                "qa_flags": ",".join(f.code for f in b.qa_flags),
            }
        )
    flagged = sum(b.needs_review for b in buildings) / len(buildings) if buildings else 0.0
    return EvaluationReport(
        len(buildings), len(reconstructed), flagged, shapes, shapes_rect, heights, rows
    )


def write_csv(report: EvaluationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if report.rows:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(report.rows[0]))
            writer.writeheader()
            writer.writerows(report.rows)
    return path


def format_report(report: EvaluationReport) -> str:
    lines = [
        f"Edifícios: {report.n_buildings} | reconstruídos: {report.n_reconstructed} | "
        f"a rever: {report.flagged_ratio:.0%}",
    ]
    for title, ev in (
        ("roof:shape (todos com referência OSM)", report.shapes),
        ("roof:shape (só plantas retangulares)", report.shapes_rectangular),
    ):
        acc = f"{ev.accuracy:.0%}" if ev.accuracy is not None else "-"
        cov = f"{ev.coverage:.0%}" if ev.coverage is not None else "-"
        lines += [
            "",
            f"{title}: n={ev.n_reference}, com previsão={ev.n_predicted} ({cov}), "
            f"corretas={ev.n_correct} → exatidão {acc}",
            f"  {'OSM \\ previsto':<14}" + "".join(f"{lbl[:9]:>10}" for lbl in SHAPE_LABELS),
        ]
        refs = sorted({r for r, _ in ev.confusion}, key=SHAPE_LABELS.index)
        for r in refs:
            lines.append(
                f"  {r:<14}" + "".join(f"{ev.confusion[(r, p)]:>10}" for p in SHAPE_LABELS)
            )
    s = report.heights.summary()
    lines += ["", f"Altura vs building:levels (n={report.heights.n}): altura por piso implícita"]
    if s:
        lines.append(
            f"  mediana {s['mediana']:.2f} m, P10 {s['p10']:.2f}, P90 {s['p90']:.2f}, "
            f"entre 2.4 e 4.0 m: {s['entre 2.4 e 4.0 m']:.0%}"
        )
    return "\n".join(lines)
