import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from predictiondb_core.garage_init import GarageAdminSettings, parse_capacity, run
from predictiondb_core.settings import Settings

KEY_ID = "GK" + "0" * 24
SECRET = SecretStr("f" * 64)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("100G", 100 * 10**9), ("1.5T", 1_500 * 10**9), ("500MB", 500 * 10**6), ("1024", 1024)],
)
def test_parse_capacity(value: str, expected: int) -> None:
    """@relation(SWR-CORE-005, scope=function)"""
    assert parse_capacity(value) == expected


class FakeGarage:
    """Minimaler Zustand der Admin-API v2 für die Endpunkte, die garage_init nutzt."""

    def __init__(self) -> None:
        self.layout_version = 0
        self.staged: list[dict[str, Any]] = []
        self.keys: set[str] = set()
        self.buckets: dict[str, str] = {}
        self.grants: set[tuple[str, str]] = set()
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        ep = request.url.path.removeprefix("/v2/")
        self.calls.append(ep)
        body = json.loads(request.content) if request.content else {}
        q = request.url.params
        match ep:
            case "GetClusterStatus":
                return httpx.Response(
                    200, json={"layoutVersion": self.layout_version, "nodes": [{"id": "ab" * 32}]}
                )
            case "UpdateClusterLayout":
                self.staged = body["roles"]
                return httpx.Response(200, json={})
            case "ApplyClusterLayout":
                assert body["version"] == self.layout_version + 1
                self.layout_version = body["version"]
                return httpx.Response(200, json={})
            case "GetKeyInfo":
                return httpx.Response(200 if q["id"] in self.keys else 404, json={})
            case "ImportKey":
                self.keys.add(body["accessKeyId"])
                return httpx.Response(200, json={})
            case "GetBucketInfo":
                alias = q["globalAlias"]
                if alias in self.buckets:
                    return httpx.Response(200, json={"id": self.buckets[alias]})
                return httpx.Response(404, json={})
            case "CreateBucket":
                bucket_id = f"id-{body['globalAlias']}"
                self.buckets[body["globalAlias"]] = bucket_id
                return httpx.Response(200, json={"id": bucket_id})
            case "AllowBucketKey":
                self.grants.add((body["bucketId"], body["accessKeyId"]))
                return httpx.Response(200, json={})
        return httpx.Response(404)


def _run(fake: FakeGarage) -> None:
    settings = Settings(s3_access_key_id=KEY_ID, s3_secret_access_key=SECRET)
    admin = GarageAdminSettings(capacity="10G")
    with httpx.Client(base_url="http://garage", transport=httpx.MockTransport(fake.handler)) as c:
        run(settings, admin, c)


def test_init_creates_layout_key_and_buckets() -> None:
    """@relation(SWR-CORE-005, scope=function)"""
    fake = FakeGarage()
    _run(fake)
    assert fake.layout_version == 1
    assert fake.staged[0]["capacity"] == 10 * 10**9
    assert fake.keys == {KEY_ID}
    assert set(fake.buckets) == {
        "wx-bronze",
        "wx-silver",
        "ml-artifacts",
        "backups",
        "assets-bronze",
    }
    assert len(fake.grants) == 5


def test_init_is_idempotent() -> None:
    """@relation(SWR-CORE-005, scope=function)"""
    fake = FakeGarage()
    _run(fake)
    fake.calls.clear()
    _run(fake)
    assert fake.layout_version == 1
    assert "ImportKey" not in fake.calls
    assert "CreateBucket" not in fake.calls
    assert "ApplyClusterLayout" not in fake.calls


def test_init_requires_credentials() -> None:
    """@relation(SWR-CORE-005, scope=function)"""
    with httpx.Client() as client, pytest.raises(ValueError, match="PDB_S3_ACCESS_KEY_ID"):
        run(Settings(s3_access_key_id=""), GarageAdminSettings(), client)
