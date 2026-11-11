from __future__ import annotations

import pytest

from osm3d.acquisition.dgt import (
    DgtAsset,
    DgtError,
    DgtItem,
    filename_for,
    latest_versions,
    parse_items,
    parse_login_form,
    tile_from_id,
)
from osm3d.env import parse_dotenv
from osm3d.geospatial.tiling import TileKey, tiles_in_bbox

LOGIN_HTML = """
<html><body>
<form id="kc-form-login" onsubmit="return true;"
      action="/realms/dgterritorio/login-actions/authenticate?session_code=abc&amp;tab_id=xyz"
      method="post">
  <input tabindex="1" id="username" name="username" value="" type="text"/>
  <input tabindex="2" id="password" name="password" type="password"/>
  <input type="hidden" id="id-hidden-input" name="credentialId" value=""/>
  <input type="hidden" name="extra" value="42"/>
</form>
</body></html>
"""

SEARCH_PAYLOAD = {
    "type": "FeatureCollection",
    "context": {"matched": 3, "returned": 3},
    "features": [
        {
            "id": "LO-155480-06-2024",
            "collection": "LAZ",
            "links": [{"rel": "self", "href": "https://x/collections/LAZ/items/LO-155480-06-2024"}],
            "assets": {"data": {"href": "https://x/download/a", "type": "application/vnd.laszip"}},
        },
        {
            "id": "MDT-50cm-155480-06-2024",
            "collection": "MDT-50cm",
            "properties": {"version": 0},
            "links": [],
            "assets": {
                "d": {"href": "https://x/download/b", "type": "image/tiff; application=geotiff"}
            },
        },
        {
            "id": "MDT-50cm-155480-06-2024",
            "collection": "MDT-50cm",
            "properties": {"version": 2},
            "links": [],
            "assets": {
                "d": {"href": "https://x/download/c", "type": "image/tiff; application=geotiff"}
            },
        },
    ],
}


def test_parse_login_form() -> None:
    action, hidden = parse_login_form(LOGIN_HTML)
    assert action == (
        "https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/login-actions/"
        "authenticate?session_code=abc&tab_id=xyz"
    )
    assert hidden == {"credentialId": "", "extra": "42"}


def test_parse_login_form_missing() -> None:
    with pytest.raises(DgtError):
        parse_login_form("<html><form action='/other'></form></html>")


def test_parse_items_and_latest_versions() -> None:
    items = parse_items(SEARCH_PAYLOAD)
    assert [i.tile for i in items] == ["155480"] * 3
    latest = latest_versions(items)
    assert len(latest) == 2
    mdt = next(i for i in latest if i.collection == "MDT-50cm")
    assert mdt.version == 2
    assert mdt.assets[0].href.endswith("/c")


def test_filenames() -> None:
    laz = DgtItem("LO-155480-06-2024", "LAZ", "155480", 0)
    assert filename_for(laz, DgtAsset("h", "application/vnd.laszip")) == "LO-155480-06-2024.laz"
    mdt = DgtItem("MDT-50cm-155480-06-2024", "MDT-50cm", "155480", 2)
    assert (
        filename_for(mdt, DgtAsset("h", "image/tiff; application=geotiff"))
        == "MDT-50cm-155480-06-2024_v02.tif"
    )


def test_tile_from_id() -> None:
    assert tile_from_id("MDS-2m-115197-07-2024") == "115197"
    assert tile_from_id("sem-quadricula") is None


def test_tiles_in_area_bbox() -> None:
    keys = tiles_in_bbox((-8.69911, 41.27348, -8.63541, 41.29553))
    names = {k.dgt_name for k in keys}
    assert len(keys) == 18  # igual à grelha oficial
    assert {"155480", "156480", "152479", "157481"} <= names


def test_inset_bbox_is_inside_tile() -> None:
    key = TileKey.from_dgt_name("155480")
    w, s, e, n = key.bounds_wgs84()
    wi, si, ei, ni = key.bounds_wgs84(inset_m=50)
    assert w < wi < ei < e and s < si < ni < n


def test_parse_dotenv() -> None:
    text = '# comentário\nDGT_USER=a@b.pt\nexport DGT_PASSWORD="p=ss word"\n\nINVALID\n'
    assert parse_dotenv(text) == {"DGT_USER": "a@b.pt", "DGT_PASSWORD": "p=ss word"}
