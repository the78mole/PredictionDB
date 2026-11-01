"""StrictDoc-Projekt der Anforderungen (``reqs/``).

Aufruf aus dem Repo-Wurzelverzeichnis::

    uv run strictdoc export reqs --output-dir build/reqs   # HTML nach build/reqs/html
    uv run strictdoc server reqs                           # Bearbeiten im Browser

Die Quellpfade gelten relativ zum Repo (``source_root_path``), damit ``@relation``-Marker in
Code und Tests aufgelöst werden. Ein Marker auf eine unbekannte ID bricht den Export ab.
"""

from strictdoc.core.project_config import ProjectConfig


def create_config() -> ProjectConfig:
    return ProjectConfig(
        project_title="PredictionDB - Anforderungen",
        project_features=[
            "TABLE_SCREEN",
            "TRACEABILITY_SCREEN",
            "DEEP_TRACEABILITY_SCREEN",
            "SEARCH",
            "TRACEABILITY_MATRIX_SCREEN",
            "REQUIREMENT_TO_SOURCE_TRACEABILITY",
            "PROJECT_STATISTICS_SCREEN",
            "TREE_MAP_SCREEN",
        ],
        grammars={
            "@anforderung": "grammatik/anforderung.sgra",
            "@architektur": "grammatik/architektur.sgra",
        },
        source_root_path="..",
        include_source_paths=[
            "/packages/**/*.py",
            "/services/**/*.py",
            "/predict/**/*.py",
            "/tests/**/*.py",
        ],
    )
