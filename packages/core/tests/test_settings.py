from pathlib import Path

import pytest

from predictiondb_core.settings import Settings


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("PDB_DATABASE_URL", "PDB_BUCKET_BRONZE"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.buckets.all() == ["wx-bronze", "wx-silver", "ml-artifacts", "backups"]


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDB_S3_ENDPOINT_URL", "https://s3.example.org")
    monkeypatch.setenv("PDB_S3_SECRET_ACCESS_KEY", "geheim")
    monkeypatch.setenv("PDB_BUCKET_BRONZE", "anderer-bucket")
    s = Settings()
    assert s.s3_endpoint_url == "https://s3.example.org"
    assert s.s3_secret_access_key.get_secret_value() == "geheim"
    assert "geheim" not in repr(s)
    assert s.buckets.bronze == "anderer-bucket"
