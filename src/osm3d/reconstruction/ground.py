"""Escolha da cota do terreno para ``height`` em edifícios em encosta ou em patamar.

Em Simple 3D Buildings, ``height`` mede-se a partir do ponto mais baixo onde **o
próprio edifício** toca o terreno. Com terreno plano, isso é o percentil baixo do
MDT à volta das paredes. Em encosta há dois casos que o LiDAR aéreo, sem fachadas,
não distingue diretamente:

- **edifício encaixado na encosta:** a fachada de baixo desce até ao terreno baixo,
  e a referência é o chão baixo;
- **edifício num patamar com muro de suporte à frente:** o muro é outro elemento
  (``barrier=retaining_wall``), e a referência é o terreno do lote.

Quando o desnível no pé das paredes passa ``STEP_THRESHOLD_M``, decide-se com evidência:

1. o beiral tem de ficar pelo menos ``MIN_WALL_M`` acima do chão. Senão o "lote" é
   o lado de cima de um edifício encaixado, e usa-se o chão baixo;
2. com ``building:levels``, escolhe-se o chão que dá uma altura de parede por piso
   plausível (mais perto de ``TYPICAL_STOREY_M``);
3. sem evidência, chão baixo (convenção literal), com sinalização para revisão.

Calibração (Vila do Conde, 476 edifícios): 15 % têm desnível > 2 m. Nesses, com
``building:levels``, o chão baixo dá uma altura por piso plausível em 34 % dos casos
e a mediana em 64 %.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from osm3d.domain.models import GroundChoice, GroundInfo

STEP_THRESHOLD_M = 2.0
MIN_WALL_M = 2.2
TYPICAL_STOREY_M = 3.0
STOREY_RANGE_M = (2.2, 4.0)


def parse_levels(value: str | None) -> float | None:
    try:
        levels = float(value) if value else None
    except ValueError:
        return None
    return levels if levels and levels > 0 else None


def choose_ground(
    low_m: float,
    profile: npt.NDArray[np.float64],
    eave_z: float | None,
    levels: float | None,
) -> GroundInfo:
    """Decide a cota do terreno (ver docstring do módulo)."""
    if profile.size < 4:
        return GroundInfo(low_m=low_m, high_m=low_m, step_m=0.0, reason="sem perfil do terreno")
    high = float(np.median(profile))
    step = float(np.percentile(profile, 95) - np.percentile(profile, 5))
    info = GroundInfo(low_m=low_m, high_m=max(high, low_m), step_m=step)
    if step <= STEP_THRESHOLD_M:
        return info.model_copy(update={"reason": "terreno plano"})
    if eave_z is None:
        return info.model_copy(update={"basis": "none", "reason": "desnível; sem telhado ajustado"})
    if eave_z - info.high_m < MIN_WALL_M:
        return info.model_copy(
            update={
                "basis": "eave",
                "reason": "desnível; o lado de cima está quase à altura do beiral (encaixado)",
            }
        )
    if levels:
        lo, hi = STOREY_RANGE_M

        def score(choice: GroundChoice) -> tuple[bool, float]:
            per = (eave_z - info.elevation(choice)) / levels
            return (not lo <= per <= hi, abs(per - TYPICAL_STOREY_M))

        choice: GroundChoice = min(("low", "high"), key=score)
        return info.model_copy(
            update={
                "choice": choice,
                "basis": "levels",
                "reason": f"desnível; escolhido por building:levels={levels:g}",
            }
        )
    return info.model_copy(
        update={"basis": "none", "reason": "desnível; sem evidência (chão baixo por omissão)"}
    )
