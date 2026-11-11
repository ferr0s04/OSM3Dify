"""Verificações de qualidade por edifício.

Exemplos de sinalizações (``QAFlag.code``):
- ``no_lidar_points``: footprint sem pontos de edifício (demolido/novo vs. voo LiDAR)
- ``large_registration_offset``: desvio de co-registo perto do limite
- ``roof_fit_poor``: RMSE acima do limite em todos os modelos
- ``height_outlier``: altura implausível face aos vizinhos
- ``parts_overlap`` / ``parts_outside_outline``: topologia de partes inválida
- ``tag_conflict``: tag existente no OSM diferente da proposta
"""

from __future__ import annotations

from osm3d.domain.models import Building, QAFlag


def check_building(building: Building) -> list[QAFlag]:
    flags: list[QAFlag] = []
    if not building.footprint.is_valid:
        flags.append(
            QAFlag(code="invalid_footprint", message="Footprint inválido", severity="error")
        )
    if not building.parts:
        flags.append(QAFlag(code="no_parts", message="Edifício sem partes reconstruídas"))
    if building.osm_parts:
        # Os renderizadores desenham as partes e escondem o contorno: tags 3D no contorno
        # seriam ignoradas ou contraditórias. Exige decisão humana.
        flags.append(
            QAFlag(
                code="osm_building_parts",
                message=f"Já tem {len(building.osm_parts)} building:part no OSM "
                f"({', '.join(f'way/{i}' for i in building.osm_parts[:5])})",
            )
        )
    for part in building.parts:
        if not part.geometry.within(building.footprint.buffer(0.5)):
            flags.append(
                QAFlag(code="parts_outside_outline", message=f"Parte {part.id} fora do contorno")
            )
        if part.min_height >= part.height.value:
            flags.append(
                QAFlag(
                    code="invalid_height_range",
                    message=f"Parte {part.id}: min_height >= height",
                    severity="error",
                )
            )
    return flags
