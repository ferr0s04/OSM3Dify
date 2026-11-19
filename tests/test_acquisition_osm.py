from __future__ import annotations

from typing import Any

import pytest
import requests

from osm3d.acquisition.osm import (
    FALLBACK_OVERPASS_URLS,
    USER_AGENT,
    OverpassError,
    build_query,
    overpass_post,
    query_timeout_s,
)

BBOX = (-8.69911, 41.27348, -8.63541, 41.29553)
MAIN = "https://overpass-api.de/api/interpreter"


def test_bbox_query_uses_south_west_north_east_order() -> None:
    query = build_query(BBOX)
    assert 'way["building"](41.27348,-8.69911,41.29553,-8.63541);' in query
    assert "area(" not in query
    assert "out meta;" in query


def test_relation_query_filters_by_area() -> None:
    query = build_query(BBOX, osm_relation_id=1234567)
    assert "area(3601234567)->.a;" in query
    assert 'way["building"](area.a)(41.27348,-8.69911,41.29553,-8.63541);' in query


def test_declared_timeout_follows_area_size() -> None:
    one_km = (-8.6579, 41.2975, -8.6471, 41.3056)  # ~1 km²
    assert 60 < query_timeout_s(one_km) < 70
    assert query_timeout_s(BBOX) < 150  # ~13 km²
    assert query_timeout_s((-9.5, 37.0, -6.2, 42.1)) == 300  # Portugal: limite
    assert "[timeout:65]" in build_query(BBOX, timeout_s=65)


class FakePost:
    """Substitui ``Session.post``: respostas por servidor, em sequência."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, session: requests.Session, url: str, **kw: Any) -> requests.Response:
        self.calls.append((url, dict(session.headers)))
        item = self.script[url].pop(0)
        if isinstance(item, Exception):
            raise item
        resp = requests.Response()
        resp.status_code, resp._content = item[0], item[1]
        resp.headers.update(item[2] if len(item) > 2 else {})
        return resp


def run(script: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    fake = FakePost(script)
    monkeypatch.setattr(requests.Session, "post", lambda s, url, **k: fake(s, url, **k))
    sleeps: list[float] = []
    lines: list[str] = []
    try:
        out = overpass_post(
            "q",
            MAIN,
            lambda r: r.content,
            read_timeout_s=10,
            progress=lines.append,
            sleep=sleeps.append,
            **kw,
        )
    except OverpassError as exc:
        out = exc
    return out, fake, sleeps, lines


def test_busy_server_falls_through_to_next(monkeypatch: pytest.MonkeyPatch) -> None:
    script = {MAIN: [(502, b"at capacity")], FALLBACK_OVERPASS_URLS[0]: [(200, b"ok")]}
    out, fake, sleeps, lines = run(script, monkeypatch)
    assert out == b"ok" and sleeps == []  # sem espera: havia outro servidor
    assert fake.calls[0][1]["User-Agent"] == USER_AGENT
    assert "ocupado (502)" in lines[0]


def test_all_busy_backs_off_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    busy = (502, b"queue full")
    script = {
        MAIN: [busy, busy, (200, b"ok")],
        FALLBACK_OVERPASS_URLS[0]: [busy, busy],
        FALLBACK_OVERPASS_URLS[1]: [(429, b"", {"Retry-After": "30"}), busy],
    }
    out, _, sleeps, _ = run(script, monkeypatch)
    assert out == b"ok" and len(sleeps) == 2
    assert sleeps[0] == 30  # Retry-After manda (maior do que o backoff de ~5 s)
    assert 8 <= sleeps[1] <= 12  # 5·2¹ ± 20 %


def test_hung_server_leaves_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    busy = (502, b"")
    script = {
        MAIN: [busy, busy],
        FALLBACK_OVERPASS_URLS[0]: [requests.ReadTimeout()],  # só uma vez
        FALLBACK_OVERPASS_URLS[1]: [busy, (200, b"ok")],
    }
    out, fake, _, lines = run(script, monkeypatch)
    assert out == b"ok"
    assert [u for u, _ in fake.calls].count(FALLBACK_OVERPASS_URLS[0]) == 1
    assert any("deixa de ser usado" in line for line in lines)


def test_bad_query_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    out, fake, sleeps, _ = run({MAIN: [(400, b"parse error")]}, monkeypatch)
    assert isinstance(out, OverpassError) and "Consulta inválida" in str(out)
    assert len(fake.calls) == 1 and sleeps == []


def test_gives_up_with_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    busy = (502, b"")
    script = {u: [busy] * 2 for u in [MAIN, *FALLBACK_OVERPASS_URLS]}
    out, fake, sleeps, _ = run(script, monkeypatch, rounds=2)
    assert isinstance(out, OverpassError) and "sem capacidade" in str(out)
    assert len(fake.calls) == 6 and len(sleeps) == 1
