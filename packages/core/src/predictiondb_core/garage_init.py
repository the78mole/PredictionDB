"""Idempotente Erstinitialisierung eines Garage-Single-Nodes über die Admin-API v2.

Legt das Cluster-Layout an (falls noch keines existiert), importiert den Zugangsschlüssel
der Dienste aus der Konfiguration und erstellt die Buckets mit Lese- und Schreibrecht.
Mehrfaches Ausführen ist unschädlich; ``compose.yaml`` startet es als einmaligen Dienst.

@relation(SWA-CORE-GARAGE, scope=file)
"""

import argparse
import sys
import time
from typing import Any

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from predictiondb_core.log import configure_logging, get_logger
from predictiondb_core.settings import Settings

log = get_logger(__name__)


class GarageAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDB_GARAGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    admin_url: str = "http://localhost:3903"
    admin_token: SecretStr = SecretStr("")
    zone: str = "dc1"
    capacity: str = "100G"
    key_name: str = "predictiondb"


_UNITS = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def parse_capacity(value: str) -> int:
    """Wandelt ``"100G"`` o. ä. in Bytes (Dezimalpräfixe wie bei ``garage layout assign``).

    @relation(SWR-CORE-005, scope=function)
    """
    v = value.strip().upper().removesuffix("B")
    unit = v[-1] if v and v[-1] in _UNITS else ""
    number = v[: -1 or None] if unit else v
    return int(float(number) * _UNITS[unit])


class GarageAdmin:
    """@relation(SWR-CORE-005, scope=class)"""

    def __init__(self, client: httpx.Client) -> None:
        self._c = client

    def _get(self, endpoint: str, **params: str) -> Any:
        r = self._c.get(f"/v2/{endpoint}", params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, body: dict[str, Any], **params: str) -> Any:
        r = self._c.post(f"/v2/{endpoint}", json=body, params=params)
        r.raise_for_status()
        return r.json() if r.content else None

    def wait_ready(self, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self._get("GetClusterStatus")
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if time.monotonic() > deadline:
                    raise TimeoutError("Garage-Admin-API nicht erreichbar") from exc
                time.sleep(1)

    def ensure_layout(self, zone: str, capacity: int) -> None:
        status = self._get("GetClusterStatus")
        if status["layoutVersion"] > 0:
            log.info("garage.layout.exists", version=status["layoutVersion"])
            return
        nodes = status["nodes"]
        if len(nodes) != 1:
            raise RuntimeError(f"Single Node erwartet, gefunden: {len(nodes)} Knoten")
        node_id = nodes[0]["id"]
        self._post(
            "UpdateClusterLayout",
            {"roles": [{"id": node_id, "zone": zone, "capacity": capacity, "tags": []}]},
        )
        self._post("ApplyClusterLayout", {"version": 1})
        log.info("garage.layout.applied", node=node_id[:16], zone=zone, capacity=capacity)

    def ensure_key(self, access_key_id: str, secret: str, name: str) -> None:
        r = self._c.get("/v2/GetKeyInfo", params={"id": access_key_id})
        if r.status_code == httpx.codes.OK:
            log.info("garage.key.exists", key=access_key_id)
            return
        if r.status_code not in (httpx.codes.BAD_REQUEST, httpx.codes.NOT_FOUND):
            r.raise_for_status()
        self._post(
            "ImportKey",
            {"accessKeyId": access_key_id, "secretAccessKey": secret, "name": name},
        )
        log.info("garage.key.imported", key=access_key_id)

    def ensure_bucket(self, alias: str, access_key_id: str) -> None:
        r = self._c.get("/v2/GetBucketInfo", params={"globalAlias": alias})
        if r.status_code == httpx.codes.OK:
            bucket_id = r.json()["id"]
        elif r.status_code == httpx.codes.NOT_FOUND:
            bucket_id = self._post("CreateBucket", {"globalAlias": alias})["id"]
            log.info("garage.bucket.created", bucket=alias)
        else:
            r.raise_for_status()
            raise AssertionError("unreachable")
        self._post(
            "AllowBucketKey",
            {
                "bucketId": bucket_id,
                "accessKeyId": access_key_id,
                "permissions": {"read": True, "write": True, "owner": True},
            },
        )


def run(settings: Settings, admin: GarageAdminSettings, client: httpx.Client) -> None:
    """@relation(SWR-CORE-005, scope=function)"""
    if not settings.s3_access_key_id or not settings.s3_secret_access_key.get_secret_value():
        raise ValueError("PDB_S3_ACCESS_KEY_ID und PDB_S3_SECRET_ACCESS_KEY müssen gesetzt sein")
    ga = GarageAdmin(client)
    ga.wait_ready()
    ga.ensure_layout(admin.zone, parse_capacity(admin.capacity))
    ga.ensure_key(
        settings.s3_access_key_id, settings.s3_secret_access_key.get_secret_value(), admin.key_name
    )
    for bucket in settings.buckets.all():
        ga.ensure_bucket(bucket, settings.s3_access_key_id)
    log.info("garage.init.done", buckets=settings.buckets.all())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    admin = GarageAdminSettings()
    headers = {"Authorization": f"Bearer {admin.admin_token.get_secret_value()}"}
    with httpx.Client(base_url=admin.admin_url, headers=headers, timeout=30) as client:
        try:
            run(settings, admin, client)
        except Exception:
            log.exception("garage.init.failed")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
