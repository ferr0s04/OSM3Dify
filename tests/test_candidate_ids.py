"""Uma nova corrida dos candidatos mantém as chaves (as revisões estão associadas a elas)."""

from __future__ import annotations

from shapely.geometry import box

from osm3d.domain.models import Building
from osm3d.reconstruction.candidates import candidate_id, keep_candidate_ids


def _cand(x0: float, y0: float, w: float = 10.0, h: float = 8.0, gid: int = 1) -> Building:
    fp = box(x0, y0, x0 + w, y0 + h)
    c = fp.centroid
    return Building(
        osm_type="new",
        osm_id=candidate_id(c.x, c.y),
        osm_version=0,
        footprint=fp,
        diagnostics={"municipal_gid": float(gid)},
    )


def test_redrawn_outline_keeps_its_key() -> None:
    # Centróide a meio metro: um redesenho de 2 cm passa-o para o metro seguinte (outra chave).
    before = _cand(-45000.49, 183000.0)
    after = _cand(-45000.51, 183000.0)
    assert before.key != after.key
    (kept,) = keep_candidate_ids([after], [before])
    assert kept.key == before.key
    assert kept.footprint.equals(after.footprint)  # o contorno é o novo


def test_other_buildings_do_not_inherit_keys() -> None:
    old = _cand(-45000.0, 183000.0, gid=1)
    # Vizinho encostado (pouca sobreposição) e edifício novo noutro sítio: chaves próprias.
    neighbour = _cand(-44991.0, 183000.0, gid=2)
    elsewhere = _cand(-44900.0, 183000.0, gid=3)
    out = keep_candidate_ids([neighbour, elsewhere], [old])
    assert [b.key for b in out] == [neighbour.key, elsewhere.key]


def test_same_gid_redrawn_more_keeps_key_but_only_once() -> None:
    old = _cand(-45000.0, 183000.0, gid=7)
    # Mesmo gid, contorno bem mais pequeno (IoU ~0,6): o mesmo edifício.
    smaller = _cand(-45000.0, 183000.0, w=6.0, gid=7)
    # Um segundo candidato sobre o mesmo sítio não fica com a mesma chave.
    twin = _cand(-44999.5, 183000.0, w=6.0, gid=7)
    out = keep_candidate_ids([smaller, twin], [old])
    assert out[0].key == old.key
    assert out[1].key == twin.key


def test_unchanged_keys_stay() -> None:
    a, b = _cand(-45000.0, 183000.0), _cand(-44980.0, 183000.0, gid=2)
    assert [x.key for x in keep_candidate_ids([a, b], [a, b])] == [a.key, b.key]
