# MLflow: Bewertung und Integrationsvorschlag

**Status:** Vorschlag, nicht umgesetzt, auf der VM nichts installiert · **Stand:** 2026-11-05

Frage: Was ist MLflow, kann PredictionDB es nutzen, und wie passt es zu `ml-train`, zur
Datei-Registry und zu `meta.model_version`? Ziel ist, Backtests und trainierte Modelle bequem
vergleichen und auswählen zu können. Die Modelle stehen in [PROGNOSEMODELLE.md](PROGNOSEMODELLE.md),
der Backtest in [BACKTEST.md](BACKTEST.md), Registry und Tags in
[ARCHITEKTUR.md](ARCHITEKTUR.md) 7.7 und 7.11.

## Inhalt

1. [Ergebnis in Kürze](#1-ergebnis-in-kürze)
2. [Was MLflow ist](#2-was-mlflow-ist)
3. [Heutiger Stand in PredictionDB](#3-heutiger-stand-in-predictiondb)
4. [Das eigentliche Problem: Vergleichbarkeit der Backtests](#4-das-eigentliche-problem-vergleichbarkeit-der-backtests)
5. [Abbildung von Backtest und Fit auf MLflow](#5-abbildung-von-backtest-und-fit-auf-mlflow)
6. [Registry: ersetzen oder spiegeln?](#6-registry-ersetzen-oder-spiegeln)
7. [Betrieb auf der VM](#7-betrieb-auf-der-vm)
8. [Aufwand](#8-aufwand)
9. [Alternativen](#9-alternativen)
10. [Empfehlung und nächste Schritte](#10-empfehlung-und-nächste-schritte)
11. [Quellen](#11-quellen)

## 1. Ergebnis in Kürze

- **MLflow passt in den Stack.** Es ist frei (Apache 2.0), läuft als ein Container, speichert
  Läufe und Registry in Postgres (eigene Datenbank `mlflow` im vorhandenen TimescaleDB-Container)
  und Artefakte in Garage. Aktuelle Version 3.18.0 (PyPI, geprüft 2026-11-05).
- **Tracking ja.** Jeder `ml-train backtest` und jeder `ml-train fit` wird ein MLflow-Lauf mit
  Parametern, Kennzahlen je Ziel und Vorlauftag und kleinen Artefakten. In der Oberfläche lassen
  sich dann Läufe filtern, nebeneinanderstellen und nach Kennzahl sortieren.
- **Registry spiegeln, nicht ersetzen.** Die Datei-Registry in Garage bleibt die Quelle für
  `ml-predict`, `ml-train bundle`, den Stack `predict/` und später GHCR. MLflow bekommt die
  Versionen und Rollen als Kopie (Aliase `champion`, `baseline`, `previous`, Tag `role`), so wie
  heute `meta.model_version`. Freigaben laufen weiter über `ml-train registry promote` mit der
  Gruppenregel.
- **MLflow löst das Vergleichsproblem nicht allein.** Heute hat jeder Backtest seinen eigenen
  Testzeitraum und Snapshot, die Kennzahlen laufen über alle Zeilen. Das wäre in MLflow genauso
  irreführend. Deshalb gehört ein fester **Vergleichsfenster** in die Konfiguration, auf dem jeder
  Lauf zusätzlich bewertet wird (Kap. 4). Das hilft auch ohne MLflow.
- **Optional.** Ohne `MLFLOW_TRACKING_URI` verhält sich `ml-train` wie heute. Ist der Server weg,
  wird gewarnt und weitergerechnet, wie beim Registry-Spiegel (`try_sync`).
- **Ressourcen klein:** ein Container mit grob 0,3 bis 0,6 GB RAM (Schätzung, auf der VM zu
  messen), kaum CPU, wenige Schreibvorgänge je Lauf. Der IO-Engpass der VM wird nicht spürbar
  belastet, solange `predictions.parquet` nicht nach MLflow kopiert wird.
- **Aufwand:** etwa drei bis vier Arbeitstage in drei PRs (Kap. 8, Schätzung).

## 2. Was MLflow ist

MLflow ist ein freies Werkzeug für den Lebenszyklus von ML-Modellen, ursprünglich von Databricks,
heute ein Projekt der Linux Foundation. Für PredictionDB zählen zwei Teile:

| Teil | Was es tut | Nutzen hier |
|---|---|---|
| **Tracking** | speichert je Lauf Parameter, Kennzahlen, Tags und Artefakte; Läufe hängen an einem Experiment und können Unterläufe haben | Backtests und Fits über Wochen hinweg vergleichen, ohne Ordner auf der VM zu suchen |
| **Model Registry** | registrierte Modelle mit Versionen, Tags und Aliasen (z. B. `champion`); die früheren Stufen `Staging`/`Production` sind seit MLflow 2.9 durch Aliase ersetzt | Überblick, welche Version welche Rolle hat, mit Link auf den Lauf, aus dem sie stammt |

Die Weboberfläche zeigt Läufe als Tabelle mit frei wählbaren Spalten, Vergleichsansichten
(Parameterunterschiede, Kennzahlen als Balken, Parallelkoordinaten) und Artefakte zum Ansehen.
**Läufe starten kann man in der Oberfläche nicht**; das bleibt die CLI (bzw. später die
Steuer-UI, [STEUER-UI.md](STEUER-UI.md), die nicht gebaut wird). Die übrigen Teile von MLflow
(Serving, Projekte, LLM-Tracing, GenAI-Auswertung) werden nicht gebraucht.

## 3. Heutiger Stand in PredictionDB

| Baustein | Heute | Wo |
|---|---|---|
| Backtest | `ml-train backtest --out <ordner>` schreibt `predictions.parquet`, `metrics*.csv`, `rank_corr.csv`, `lauf.json` in einen Ordner auf der VM | [BACKTEST.md](BACKTEST.md) Kap. 8 |
| Fit | `ml-train fit` trainiert eine Variante auf allen Daten und legt sie in die Datei-Registry, optional mit `backtest.parquet` derselben Variante | `ml_train/cli.py`, `cmd_fit` |
| Registry | Verzeichnisbaum in `s3://ml-artifacts/models/<konfiguration>/`: `registry.json` (Rollen, Verlauf), je Version `model.json`, LightGBM-Texte, `manifest.json`, `backtest.parquet` | `ml_train/registry.py` |
| Freigabe | `registry evaluate`/`promote`: Challenger gegen Champion auf gemeinsamen Backtest-Zeilen, Gruppenregel Ziel × Vorlauftag × Jahreszeit | `registry.compare`, [BACKTEST.md](BACKTEST.md) Kap. 10 |
| Spiegel | `registry sync` schreibt `meta.model_version`, `meta.model_metric`, `meta.model_event`; Grafana liest | `ml_train/dbsync.py`, Migration 0012 |
| Fortschritt | `meta.ml_job` mit Schritt und Zählern je Aufruf | Migration 0020 |
| Veröffentlichung | geplant: ORAS-Artefakte in GHCR mit Tags `champion`, `challenger-…`, `honor-…`, `retired-…` | [ARCHITEKTUR.md](ARCHITEKTUR.md) 7.11 |

Was fehlt: Ein **Backtest-Lauf selbst** ist nirgends registriert. Er existiert nur als Ordner. In
der Datenbank landen nur die Kennzahlen registrierter Versionen, und die laufen über den
jeweiligen Testzeitraum der Version. Wer zwei Varianten aus verschiedenen Läufen vergleichen
will, muss die Ordner finden und die Zeiträume selbst abgleichen.

MLflow ist in den Dokumenten schon vorgesehen (ARCHITEKTUR 7.4 und 7.7 sprechen von „in MLflow
als `production` markiert“, `registry.py` von „bis MLflow läuft“, PROGNOSEMODELLE 9.3 „MLflow
statt Datei-Registry“). Dieser Vorschlag präzisiert das: Aliase statt der abgekündigten Stufe
`production`, und Spiegel statt Ersatz (Kap. 6).

## 4. Das eigentliche Problem: Vergleichbarkeit der Backtests

Drei Dinge machen heutige Läufe untereinander schwer vergleichbar:

1. **Testzeitraum je Lauf.** `--test-from`, `--test-to`, `--fold-months` und `--stride` werden
   je Aufruf gesetzt, oft verkürzt für schnelle Versuche.
2. **Snapshot je Lauf.** Jeder Snapshot hat einen anderen Datenstand (neue Tage, korrigierte
   Reihen, neue Merkmale).
3. **Kennzahlen über alle Zeilen.** `meta.model_metric` und `metrics.csv` mitteln über den ganzen
   Testzeitraum des Laufs. Ein Lauf, der nur Sommer enthält, sieht beim Preis besser aus als einer
   mit Winter.

MLflow zeigt genau die Zahlen, die man hineinschreibt. Ohne Gegenmaßnahme würde die Oberfläche
Äpfel und Birnen bequem nebeneinanderstellen. Vorschlag, unabhängig von MLflow:

- **Vergleichsfenster in der Konfiguration**, z. B. in `config/ml/de_lu_v2.yaml`:

  ```yaml
  compare:
    window: [2025-01-01, 2026-06-30]   # fest, wird nur bewusst verschoben
    name: w2025h1                      # Kennung für Tags und Spaltennamen
  ```

  `ml-train backtest` berechnet die Kennzahlen dann zweimal: wie heute über alle Testzeilen und
  zusätzlich nur auf den Zeilen im Fenster. Deckt ein Lauf das Fenster nicht vollständig ab,
  bekommt er den Tag `compare_complete=false` und erscheint in der Fensteransicht nicht als
  gleichwertig.
- **Tags für Filter:** `test_from`, `test_to`, `fold_months`, `stride`, `snapshot_id`,
  `snapshot_to` (letzter Datentag), `config_sha256`. In der Oberfläche filtert man dann etwa auf
  `tags.compare_window = 'w2025h1' and tags.compare_complete = 'true'`.
- **Gemeinsame Zeilen bleiben die Entscheidung.** Die Kennzahl im Fenster ist ein guter
  Überblick, aber die Freigabe vergleicht weiter Zeile für Zeile auf gemeinsamen Zeilen
  (`registry.compare`). Der Snapshot unterscheidet sich zwischen Läufen auch im selben Fenster
  leicht (nachträgliche Korrekturen); das steht über `snapshot_id` im Lauf und bleibt ein
  bekannter Rest.

Das Fenster wäre auch für `meta.model_metric` und das Grafana-Panel „Champion und Challenger im
Backtest“ nützlich, das heute denselben Vorbehalt trägt ([BACKTEST.md](BACKTEST.md) Kap. 9).

## 5. Abbildung von Backtest und Fit auf MLflow

| PredictionDB | MLflow | Inhalt |
|---|---|---|
| Konfiguration (`de_lu_v1`, `de_lu_v2`) | **Experiment** `de_lu_v2` | eine Linie, wie in der Registry |
| Aufruf `ml-train backtest` | **Lauf** (Elternlauf), Name z. B. `bt 2026-11-05 frei,frei_gk` | Parameter des Aufrufs, Tags aus Kap. 4, Artefakte `lauf.json`, `metrics*.csv`, `rank_corr.csv`, Konfigurations-YAML |
| eine Variante im Backtest | **Unterlauf** je Variante und je Vergleichsverfahren (`naive_day`, `bench_load_da_forecast`) | Parameter: Merkmalsgruppen, LightGBM-Parameter, Kalibrierung; Kennzahlen je Ziel und Vorlauftag, einmal gesamt und einmal im Fenster |
| Aufruf `ml-train fit` | **Lauf** mit Tag `pdb_version=<version>` und `backtest_run_id` | Trainingszeitraum, Zeilen, Laufzeit, Modelldateien als Artefakte |
| Version der Datei-Registry | **Modellversion** des registrierten Modells `de_lu_v2` | Quelle ist das Artefakt des Fit-Laufs; Tags `pdb_version`, `variant`, `snapshot_id`, `role` |

**Kennzahlen** heißen wie die Annotationen in ARCHITEKTUR 7.11.4, damit es eine Schreibweise
gibt: `<ziel>.d<vorlauf>.<maß>`, z. B. `da_price_de_lu.d1.mae`, `load_actual.d2.pinball`,
`da_price_de_lu.all.coverage80`; im Vergleichsfenster mit Präfix `w.`. Bei etwa 6 Zielen,
9 Vorlauf-Stufen und 5 Maßen sind das rund 270 Werte je Variante und Sicht, in einem
`log_metrics`-Aufruf am Ende geschrieben, nicht Schritt für Schritt.

**Nicht nach MLflow** kommen `predictions.parquet` und `backtest.parquet`. Sie sind groß, würden
Garage und den IO-Engpass der VM unnötig belasten und liegen schon im Backtest-Ordner bzw. in der
Datei-Registry. Der Lauf trägt stattdessen den Pfad als Tag (`backtest_dir`). Grafiken (z. B.
MAE je Vorlauftag) kann ein späterer Schritt als kleine PNG anhängen.

**Fortschritt** bleibt in `meta.ml_job` und im Grafana-Panel. MLflow zeigt nur „läuft“ oder
„fertig“; ein Link vom Job zum Lauf (`meta.ml_job.result` bzw. ein Tag `ml_job_id`) reicht.

**Miet-GPU und Jobs ohne Netz zur VM** (ARCHITEKTUR 7.7): Der Job hat keinen Zugang ins Heimnetz.
Er schreibt die Angaben für MLflow als `tracking.json` in sein Ergebnis, der Dispatcher auf der
VM trägt sie nach der Rückkehr ein. Derselbe Weg dient zum einmaligen Nachtragen vorhandener
Backtest-Ordner aus `lauf.json` und `metrics*.csv`.

Skizze der Schnittstelle in `ml_train` (neues Modul `tracking.py`):

```python
class Tracker:
    """Schreibt nach MLflow, wenn MLFLOW_TRACKING_URI gesetzt ist, sonst nichts."""

    def backtest(self, cfg, args, res, out_dir) -> str | None: ...   # Elternlauf + Unterläufe
    def fit(self, cfg, version, manifest, model_dir) -> str | None: ...
    def mirror_registry(self, reg) -> None: ...                       # Kap. 6, nach registry sync
```

Jede Methode fängt Fehler ab und protokolliert sie (`log.warning("mlflow.failed", …)`), damit
ein fehlender Server weder Training noch Inferenz aufhält. Kurze Zeitgrenzen über
`MLFLOW_HTTP_REQUEST_TIMEOUT` und `MLFLOW_HTTP_REQUEST_MAX_RETRIES`. Als Client reicht
`mlflow-skinny` (ohne Server, Flask und Plotting) als optionales Extra von `ml_train`.

## 6. Registry: ersetzen oder spiegeln?

| | Ersetzen (MLflow wird die Registry) | Spiegeln (Datei-Registry bleibt Quelle) |
|---|---|---|
| `ml-predict` | braucht den MLflow-Server bei jedem Lauf; fällt er aus, fehlen Prognosen | unverändert, liest Garage |
| Stack `predict/`, `ml-train bundle` | müssten MLflow-Artefakte lesen oder weiter Dateien bekommen | unverändert |
| Miet-GPU-Jobs | brauchen Zugang zum Server oder einen Umweg | unverändert |
| Freigaberegel | müsste um MLflow herum gebaut werden (MLflow kennt keine Gruppenregel) | bleibt in `registry.compare`, einmal vorhanden (STEUER-UI Grundsatz 4) |
| Mehrere Challenger | Alias ist je Modell eindeutig; mehrere Challenger nur über Tags | Tag `role=challenger` je Version, Alias nur für eindeutige Rollen |
| GHCR-Tags (7.11.3) | Ereignisse müssten aus MLflow-Verlauf rekonstruiert werden | weiter aus `registry.json`-Verlauf |
| Umbau | `Registry`, `dbsync`, `predict`, `bundle`, Tests | ein zusätzlicher Spiegel neben `dbsync` |
| Risiko | hoch, betrifft den Produktivpfad | gering, MLflow ist rein lesend für den Betrieb |

**Empfehlung: spiegeln.** Nach jedem `registry sync` überträgt `Tracker.mirror_registry` den
Zustand: je Version eine Modellversion (falls noch nicht vorhanden), Tag `role`, Aliase
`champion`, `baseline` und `previous`. Änderungen, die jemand von Hand in der MLflow-Oberfläche
macht (Alias versetzen), werden beim nächsten Abgleich überschrieben; das steht als Hinweis in
der Beschreibung des registrierten Modells. So bleibt genau eine Stelle, an der Champions gekrönt
werden.

Ein späterer Wechsel zu „MLflow als Quelle“ bleibt möglich, wenn sich der Dienst über Monate als
stabil erweist. Dann würde `registry.json` aus MLflow erzeugt statt umgekehrt; `ml-predict`
und `predict/` lesen weiter Dateien.

## 7. Betrieb auf der VM

Skizze für `compose.yaml` (Profil `mlflow`, nur nach Freigabe aufgesetzt):

```yaml
  mlflow:
    profiles: [mlflow]
    build: docker/mlflow          # FROM ghcr.io/mlflow/mlflow:v3.18.0 + psycopg2-binary, boto3
    command: >
      mlflow server --host 0.0.0.0 --port 5000
      --backend-store-uri postgresql://mlflow:${MLFLOW_DB_PASSWORD}@postgres:5432/mlflow
      --artifacts-destination s3://ml-artifacts/mlflow
    environment:
      MLFLOW_S3_ENDPOINT_URL: http://garage:3900
      AWS_ACCESS_KEY_ID: ${MLFLOW_S3_KEY_ID}         # Garage-Schlüssel nur für ml-artifacts
      AWS_SECRET_ACCESS_KEY: ${MLFLOW_S3_SECRET}
      AWS_DEFAULT_REGION: garage
    depends_on:
      postgres: {condition: service_healthy}
      garage: {condition: service_healthy}
    ports: ["127.0.0.1:5000:5000"]
    mem_limit: 1g
```

- **Datenbank:** eigene Datenbank `mlflow` mit eigenem Benutzer im vorhandenen Postgres, nicht im
  Schema von PredictionDB. MLflow verwaltet sein Schema selbst (Alembic, `mlflow db upgrade` bei
  Versionswechseln). Größenordnung: wenige MB je tausend Läufe.
- **Artefakte:** Präfix `mlflow/` im vorhandenen Bucket `ml-artifacts`, neben `models/`. Mit dem
  Verzicht auf Parquet-Dateien einige MB je Fit (Modelldateien) und wenige hundert kB je Backtest.
- **IO:** je Backtest einige hundert Zeilen in wenigen Transaktionen. Das liegt weit unter dem,
  was Ingest und `ml-predict` stündlich schreiben (der Engpass der VM liegt bei WAL und
  IOPS der Datenplatte, gemessen am 2026-10-30).
- **Geheimnisse:** `MLFLOW_DB_PASSWORD` und der Garage-Schlüssel sind echte Geheimnisse
  (OpenBao, `env-aus-openbao.sh`); Port und URL (`MLFLOW_TRACKING_URI=http://mlflow:5000`) stehen
  als normale Werte in der `.env`.
- **Zugang:** zunächst nur `127.0.0.1` auf der VM, vom Laptop per SSH-Tunnel
  (`ssh -L 5000:127.0.0.1:5000 …`). MLflow hat ohne Zusatz keine Anmeldung; wenn es über das LAN
  erreichbar sein soll, dann hinter einem Reverse Proxy mit Anmeldung, später Keycloak wie in
  [STEUER-UI.md](STEUER-UI.md) Kap. 8.
- **Lizenzen der Daten:** MLflow ist intern. Artefakte und Kennzahlen dürfen deshalb auch aus der
  Klasse „nur Training“ stammen. MLflow wird nicht öffentlich gemacht und nicht über GHCR
  exportiert; öffentlich bleiben nur Modelle und Kennzahlen nach 7.11.
- **Lizenz von MLflow:** Apache 2.0, verträglich mit AGPL-3.0-only für unseren Code, da MLflow nur
  als Client-Bibliothek eingebunden bzw. als eigener Dienst betrieben wird.

## 8. Aufwand

Schätzung, nicht gemessen:

| Schritt | Inhalt | Aufwand |
|---|---|---|
| 1. Dienst | `docker/mlflow/Dockerfile`, Compose-Profil, Datenbank und Garage-Schlüssel anlegen, Doku, Renovate-Regel für das Image | 0,5 Tag |
| 2. Tracking | `ml_train/tracking.py`, Aufrufe in `cmd_backtest`/`cmd_fit`, `tracking.json` für entfernte Jobs, Tests gegen einen lokalen Dateispeicher (`file:`-URI, ohne Server) | 1 Tag |
| 3. Vergleichsfenster | `compare:` in der Konfiguration, zweite Kennzahl-Sicht im Backtest, Spalte in `meta.model_metric` oder eigene Sicht | 0,5–1 Tag |
| 4. Registry-Spiegel | `mirror_registry` nach `registry sync`, Aliase und Tags | 0,5 Tag |
| 5. Nachtrag | vorhandene Backtest-Ordner und Registry-Versionen einmalig eintragen | 0,5 Tag |

Schritte 2 und 3 können in einen PR, 1 und 4 je in einen eigenen. Schritt 3 lohnt sich auch,
wenn MLflow nicht kommt.

## 9. Alternativen

| Werkzeug | Lizenz | Passt? | Bewertung |
|---|---|---|---|
| **Nur Grafana erweitern** (Tabelle `meta.ml_run` mit Backtest-Läufen und Kennzahlen, Panel zum Vergleichen) | – | ja | kein neuer Dienst, aber Parameterunterschiede und Artefakte nur mühsam darstellbar; günstigste Variante, wenn MLflow nicht gewollt ist |
| **MLflow** | Apache 2.0 | ja | Standard, große Verbreitung, Postgres + S3 wie bei uns, Registry mit Aliasen |
| **Aim** | Apache 2.0 | eingeschränkt | gute Vergleichsansichten, aber keine Modell-Registry und eigener Datenspeicher auf der Platte (zusätzliches IO) |
| **ClearML** | Server SSPL, SDK Apache 2.0 | nein | Server braucht MongoDB, Elasticsearch und Redis, zu schwer für die VM; SSPL ist keine freie Lizenz im Sinne der OSI |
| **DVC Experiments** | Apache 2.0 | eingeschränkt | ohne Server, Vergleich über Git und CLI bzw. VS Code; passt schlecht zu Läufen auf der VM und großen Snapshots |
| **Weights & Biases, Neptune, Comet** | proprietär (SaaS, Selbsthosten nur mit Lizenz) | nein | widerspricht der Vorgabe freier, selbst betriebener Werkzeuge |
| **Optuna Dashboard** | MIT | ergänzend | nur für eine spätere Hyperparameter-Suche (PROGNOSEMODELLE 9.3); Optuna kann seine Versuche auch nach MLflow schreiben |

## 10. Empfehlung und nächste Schritte

1. **Vergleichsfenster einführen** (Schritt 3), unabhängig von MLflow. Es behebt die
   Hauptursache, warum Läufe heute nicht vergleichbar sind.
2. **MLflow als optionalen Dienst** mit Tracking für Backtest und Fit (Schritte 1 und 2), nur
   nach Freigabe auf der VM.
3. **Registry spiegeln** (Schritt 4); Freigaben bleiben bei `ml-train registry promote`.
4. Nach einigen Wochen prüfen, ob die Oberfläche tatsächlich genutzt wird. Wenn nicht, Profil
   abschalten; die Datei-Registry und Grafana laufen unverändert weiter.

Zu entscheiden: ob MLflow auf der VM aufgesetzt werden soll, und ob es nur per SSH-Tunnel oder
im LAN erreichbar sein soll.

## 11. Quellen

- MLflow, Projektseite und Dokumentation: <https://mlflow.org/docs/latest/> (Apache 2.0)
- Tracking-Server mit Postgres und S3: <https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/>
- Model Registry, Aliase statt Stufen: <https://mlflow.org/docs/latest/ml/model-registry/>
- Paket und Version: <https://pypi.org/project/mlflow/> (3.18.0, abgerufen 2026-11-05)
- ClearML Server, Lizenz SSPL: <https://github.com/clearml/clearml-server>
- Aim: <https://github.com/aimhubio/aim>
