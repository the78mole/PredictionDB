"""Zugriff auf den Objektspeicher (Garage oder jeder andere S3-kompatible Dienst).

@relation(SWA-CORE-S3, scope=file)
"""

from obstore.store import S3Store

from predictiondb_core.settings import Settings, get_settings


def s3_store(bucket: str, settings: Settings | None = None) -> S3Store:
    """Erzeugt einen ``obstore``-Store für einen Bucket.

    Endpoint, Region und Schlüssel kommen ausschließlich aus der Konfiguration, damit ein
    Wechsel des S3-Dienstes reine Konfiguration bleibt.

    @relation(SWR-CORE-003, scope=function)
    """
    s = settings or get_settings()
    return S3Store(
        bucket,
        endpoint=s.s3_endpoint_url,
        region=s.s3_region,
        access_key_id=s.s3_access_key_id,
        secret_access_key=s.s3_secret_access_key.get_secret_value(),
        virtual_hosted_style_request=False,
        client_options={"allow_http": s.s3_endpoint_url.startswith("http://")},
    )
