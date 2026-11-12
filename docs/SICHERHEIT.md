# Sicherheit: Erwartungen, Begründung und Prüfung

*An English summary follows the German text. The German version is authoritative.*

Diese Seite beschreibt, was Nutzer von PredictionDB an Sicherheit erwarten können und was nicht,
begründet das Sicherheitskonzept (Assurance Case) und erklärt, wie sich veröffentlichte Images
und Release-Anhänge prüfen lassen. Lücken meldest Du über [SECURITY.md](../SECURITY.md).
Die zugehörigen Anforderungen stehen als SYSR-SEC-001 bis -009 in
[reqs/20-syrs/syrs.sdoc](../reqs/20-syrs/syrs.sdoc).

## 1. Was Du erwarten kannst

- **Lieferkette:** Jede Änderung an `main` läuft über einen Pull Request mit grüner CI, CodeQL
  und pre-commit (Ruff, mypy). Abhängigkeiten sind fest gepinnt (`uv.lock` mit Hashes, Actions
  per Commit-SHA, Basisimages per Digest) und werden von Renovate aktuell gehalten. Workflows
  vergeben Schreibrechte nur auf Job-Ebene und checken bei `workflow_run` nur Commits von `main`
  aus.
- **Herkunft:** Die Images `ghcr.io/the78mole/predictiondb` und
  `ghcr.io/the78mole/predictiondb-predict` sowie Quellcode-Archiv und Analyse-PDFs jedes Releases tragen eine
  signierte Build-Provenance (Sigstore, SLSA), siehe [Abschnitt 3](#3-signaturen-prüfen).
- **Robuste Eingaben:** Parser für externe Daten (GRIB-Index, GHCR-Artefakte, ENTSO-E) sind mit
  Property-Tests und Fuzzern (Atheris) geprüft, Archive werden mit Pfad- und Größengrenzen
  entpackt und per Digest geprüft (`wx_exchange.safe`).
- **Keine Geheimnisse im Code:** Zugangsdaten kommen nur aus der Umgebung (`.env`, OpenBao);
  Secret Scanning mit Push Protection ist aktiv.
- **Betrieb mit wenig Rechten:** Die Container laufen als Nicht-Root-Nutzer `app`. Datenbank,
  S3 (Garage) und Grafana lauschen in der Vorlage `compose.override.yaml` nur auf `127.0.0.1`;
  Grafana verlangt ein Admin-Passwort, Selbstregistrierung ist aus.
- **Lücken:** Meldungen werden innerhalb von 7 Tagen bestätigt und innerhalb von 14 Tagen
  eingeschätzt; Korrekturen gibt es für `main` und das jeweils neueste Release.

## 2. Was Du nicht erwarten kannst

- **Keine Zugriffskontrolle für die Dienste selbst.** Die HTTP-Schnittstelle des Prognose-Stacks
  (`pdb-predict`, nur lesende `GET`-Endpunkte) und die internen Dienste haben keine eigene
  Anmeldung. Wer sie außerhalb eines vertrauenswürdigen Netzes erreichbar macht, muss einen
  Reverse Proxy mit TLS und Authentifizierung davorsetzen. `PREDICT_BIND` steht in der Vorlage
  auf `0.0.0.0`; für einen nur lokalen Betrieb auf `127.0.0.1` setzen.
- **Keine Gewähr für Prognosen.** Prognosen sind keine Handels- oder Steuerungsempfehlung und
  dürfen nicht ohne eigene Plausibilisierung zur Steuerung sicherheitsrelevanter Anlagen dienen.
- **Keine Gewähr für externe Daten.** Inhalt und Verfügbarkeit von NOAA, SMARD, ENTSO-E usw.
  liegen außerhalb des Projekts.
- **Kein Mehrbenutzerbetrieb.** Das System ist für eine Installation je Betreiber gedacht, nicht
  für mehrere Mandanten mit getrennten Rechten.
- **Kein Langzeitsupport** für ältere Releases.

## 3. Signaturen prüfen

Alle Nachweise sind keyless mit Sigstore signiert: Es gibt keinen Projektschlüssel, der
öffentliche Teil ist ein kurzlebiges Zertifikat, das an den GitHub-Workflow dieses Repositorys
gebunden ist und im öffentlichen Transparenzlog (Rekor) steht. Zum Prüfen genügt die
[GitHub CLI](https://cli.github.com/) ab Version 2.49:

```sh
# Container-Image (Tag oder Digest)
gh attestation verify oci://ghcr.io/the78mole/predictiondb:latest -R the78mole/PredictionDB
gh attestation verify oci://ghcr.io/the78mole/predictiondb-predict:latest -R the78mole/PredictionDB

# Analyse-PDF eines Releases, online ...
gh attestation verify analyse-<name>.pdf -R the78mole/PredictionDB
# ... oder mit dem Bundle aus demselben Release
gh attestation verify analyse-<name>.pdf -R the78mole/PredictionDB \
  --bundle analysen-pdf.intoto.jsonl
```

Die Prüfung ist erfolgreich, wenn die Ausgabe den Workflow dieses Repositorys und den
zugehörigen Commit auf `main` nennt.

**Quellcode:** Jedes Release `vX.Y.Z` trägt das Quellcode-Archiv `PredictionDB-vX.Y.Z.tar.gz`
(`git archive` des Release-Commits) mit SHA-256-Prüfsumme und signierter Build-Provenance:

```sh
gh release download vX.Y.Z -R the78mole/PredictionDB -p 'PredictionDB-*' -p quellcode.intoto.jsonl
sha256sum -c PredictionDB-vX.Y.Z.tar.gz.sha256
gh attestation verify PredictionDB-vX.Y.Z.tar.gz -R the78mole/PredictionDB \
  --bundle quellcode.intoto.jsonl
```

Die von GitHub automatisch erzeugten Archive („Source code (zip/tar.gz)“) sind nicht signiert;
für eine geprüfte Kopie das Archiv `PredictionDB-vX.Y.Z.tar.gz` verwenden. Die Release-Tags
zeigen auf Commits von `main`; diese entstehen als Squash-Merge auf GitHub und tragen dessen
Signatur (in der Commit-Ansicht als „Verified“ markiert, `git log --show-signature`). Die Tags
selbst sind nicht signiert.

**Wo die Schlüssel liegen:** Signiert wird keyless über Sigstore. Der private Schlüssel ist
kurzlebig, entsteht im GitHub-Actions-Lauf und wird nach dem Signieren verworfen; er liegt nie
auf GitHub Releases, GHCR oder einer anderen Verteilstelle. Das öffentliche Zertifikat steckt im
Bundle und im Transparenzlog Rekor, die Vertrauensanker liefert `gh attestation verify` aus dem
Sigstore-Root mit.

## 4. Assurance Case

Die Begründung, warum die Anforderungen an die Sicherheit erfüllt sind, gliedert sich in
Bedrohungsmodell, Vertrauensgrenzen, Designprinzipien und den Umgang mit typischen Schwächen.

### 4.1 Bedrohungsmodell

| Bedrohung | Beispiel | Gegenmaßnahme |
|---|---|---|
| Manipulierte Lieferkette | kompromittierte Action oder Abhängigkeit, Tag wird umgebogen | Pinning per SHA/Digest/Hash, Renovate, minimale Token-Rechte, Provenance (SYSR-SEC-002, -003, -005) |
| Fremder Code mit Schreibrechten | PR aus einem Fork löst Workflow mit Secrets aus | `workflow_run` checkt nur Commits von `main` aus, keine `pull_request_target`-Muster (SYSR-SEC-001) |
| Bösartige Eingabedaten | präparierte GRIB-, idx-, XML- oder tar-Dateien aus externen Quellen oder GHCR | Parser mit Grenzen, Digest-Prüfung, Fuzzing und Property-Tests (SYSR-SEC-009) |
| Abfluss von Geheimnissen | API-Token im Repository oder in Logs | Secrets nur aus der Umgebung, Secret Scanning mit Push Protection (SYSR-SEC-006) |
| Unbefugte Änderung an `main` | Direkt-Push, Force-Push | Ruleset „main schützen“ mit Pflicht-Checks (SYSR-SEC-007) |
| Unbefugter Zugriff auf den Betrieb | offene Datenbank oder Grafana | Bindung an `127.0.0.1`, Passwörter aus `.env`, Hinweise in Abschnitt 2 |
| Bekannte Schwachstellen | CVE in einer Abhängigkeit | Dependency Graph, Renovate, CodeQL, OpenSSF Scorecard (SYSR-SEC-004, -008) |

Nicht im Modell: Angriffe auf die Infrastruktur von GitHub, Sigstore oder der Datenquellen und
physischer Zugriff auf den Server des Betreibers.

### 4.2 Vertrauensgrenzen

1. **Externe Datenquellen → Ingest-Dienste:** Alle Daten von NOAA, SMARD, ENTSO-E, MaStR usw.
   gelten als nicht vertrauenswürdig und werden beim Parsen geprüft (Formate, Größen, Wertebereiche).
2. **GHCR-Artefakte → Import:** Silber- und Modellpakete werden per Digest geprüft und mit
   Grenzen entpackt, bevor sie gelesen werden.
3. **Netz → Dienste des Stacks:** Nur Grafana und `pdb-predict` sind zum Veröffentlichen gedacht;
   alles Weitere bleibt im internen Docker-Netz bzw. auf `127.0.0.1`.
4. **Beiträge → `main`:** Code von außen gelangt nur per Pull Request mit Prüfungen auf `main`;
   Workflows mit Schreibrechten führen keinen ungeprüften Fremdcode aus.

### 4.3 Sichere Designprinzipien

- **Minimale Rechte:** Token-Rechte je Job, Container als Nicht-Root, Ports nur lokal.
- **Sichere Voreinstellungen:** Unfreie Quellen sind ausgeschaltet (`PDB_ALLOW_*=false`), anonymer
  Grafana-Zugang und Selbstregistrierung sind aus.
- **Einfachheit:** wenige, verbreitete Bausteine (PostgreSQL/TimescaleDB, Garage, Python), keine
  eigene Kryptographie; Hashes über `hashlib` (SHA-256), TLS über die Standardbibliotheken.
- **Vollständige Prüfung von Eingaben** an jeder Vertrauensgrenze (4.2).
- **Nachvollziehbarkeit:** Anforderungen, Code und Tests sind über `@relation` verknüpft und auf
  der Projektseite verfolgbar.

### 4.4 Umgang mit typischen Schwächen

| Schwäche (CWE/OWASP) | Umgang |
|---|---|
| Injection (CWE-89, CWE-78) | SQL über SQLAlchemy, Werte immer als gebundene Parameter; in SQL-Texte eingesetzt werden nur feste Bezeichner aus dem Code. Prozesse nur mit fester Argumentliste ohne Shell |
| Path Traversal (CWE-22) | `wx_exchange.safe.read_tar` lehnt absolute Pfade, `..` und alles außer regulären Dateien ab |
| Ressourcenerschöpfung (CWE-400) | Größen- und Punktgrenzen beim Entpacken und Parsen (z. B. `MAX_POINTS` bei ENTSO-E) |
| Unsichere Deserialisierung (CWE-502) | keine Pickle-Daten aus externen Quellen; Modellpakete nur aus eigenen, per Digest geprüften Artefakten |
| Fehlerhafte Eingabeprüfung (CWE-20) | Property-Tests (Hypothesis) und Fuzzer für die Parser |
| Hartkodierte Zugangsdaten (CWE-798) | Secrets nur aus der Umgebung, Secret Scanning |
| Klartext-Übertragung (CWE-319) | externe Quellen nur über HTTPS; `http://` nur für Dienste im internen Netz |
| Bekannte Schwachstellen in Komponenten (OWASP A06) | Renovate, Dependency Graph, CodeQL |

---

## English summary

**What you can expect:** pull-request-only changes with CI, CodeQL and linters; pinned
dependencies kept current by Renovate; minimal workflow token permissions; signed build
provenance (Sigstore/SLSA) for the container images and the analysis PDFs of every release;
fuzzed and bounded parsers for external input; no secrets in the code; non-root containers and
localhost-only ports in the compose template; acknowledgement of reports within 7 days.

**What you cannot expect:** no authentication in the services themselves (put a TLS reverse proxy
with authentication in front of anything exposed beyond a trusted network), no warranty for
forecasts or external data, no multi-tenant operation, no long-term support for old releases.

**Verifying signatures:** `gh attestation verify oci://ghcr.io/the78mole/predictiondb:<tag> -R
the78mole/PredictionDB` for images and `gh attestation verify analyse-<name>.pdf -R
the78mole/PredictionDB [--bundle analysen-pdf.intoto.jsonl]` for release PDFs. Signing is keyless
(Sigstore); there is no long-lived project key, and the short-lived signing key never resides on
a distribution site. Every release `vX.Y.Z` carries a source archive `PredictionDB-vX.Y.Z.tar.gz`
with SHA-256 checksum and provenance bundle `quellcode.intoto.jsonl`; verify it with
`gh attestation verify PredictionDB-vX.Y.Z.tar.gz -R the78mole/PredictionDB --bundle
quellcode.intoto.jsonl`. GitHub's auto-generated source archives are not signed. Release tags
point to GitHub-signed squash-merge commits on `main`; the tags themselves are not signed.

The assurance case (threat model, trust boundaries, secure design principles, handling of common
weaknesses) is in section 4 of the German text.
