# Sicherheitsrichtlinie

*An English summary follows the German text. The German version is authoritative.*

## Unterstützte Versionen

PredictionDB wird von einer Person in der Freizeit gepflegt. Sicherheitskorrekturen gibt
es nur für den jeweils neuesten Stand:

| Bestandteil | Unterstützt |
|---|---|
| `main` und das neueste Release `vX.Y.Z` | ja |
| neuestes Release des Prognose-Stacks `predict/vX.Y.Z` und dessen Container-Image | ja |
| ältere Releases und Images | nein, bitte aktualisieren |

## Eine Lücke melden

Bitte **keine öffentlichen Issues, Discussions oder Pull Requests** für Sicherheitslücken.

Melde sie vertraulich über
[„Report a vulnerability“](https://github.com/the78mole/PredictionDB/security/advisories/new)
(GitHub Private Vulnerability Reporting). Hilfreich sind:

- betroffener Bestandteil (Dienst, Paket, Image, Workflow) und Version oder Commit,
- Beschreibung und Auswirkung der Lücke,
- Schritte zum Nachvollziehen, gern mit minimalem Beispiel,
- falls bekannt, ein Vorschlag zur Behebung.

## Was Du erwarten kannst

- Eine Eingangsbestätigung innerhalb von **7 Tagen**.
- Eine erste Einschätzung innerhalb von **14 Tagen**.
- Eine Korrektur so schnell, wie es ein Ein-Personen-Projekt erlaubt. Den Stand erfährst
  Du im Advisory.
- Nach der Korrektur wird das Advisory veröffentlicht. Auf Wunsch wirst Du darin genannt.

Es gibt kein Bug-Bounty-Programm.

## Geltungsbereich

Gemeint ist der Code in diesem Repository: die Python-Pakete und Dienste, die Container-Images
auf GHCR, `compose.yaml`, die GitHub-Actions-Workflows und die Projektseite.

Nicht gemeint sind

- Lücken in Abhängigkeiten, die schon öffentlich bekannt sind. Diese werden über Renovate
  aktualisiert, ein normales Issue genügt.
- Eigene Installationen Dritter und deren Konfiguration, etwa offen erreichbare Datenbanken
  oder Grafana-Instanzen ohne Passwort.
- Inhalte und Verfügbarkeit externer Datenquellen (NOAA, SMARD, ENTSO-E usw.).
- Prognosewerte selbst. Auffällige Prognosen bitte als normales Issue melden.

---

## English summary

Only the latest release (`vX.Y.Z`, `predict/vX.Y.Z`) and `main` receive security fixes.
Please do not report vulnerabilities publicly; use
[GitHub private vulnerability reporting](https://github.com/the78mole/PredictionDB/security/advisories/new).
This is a one-person project: expect an acknowledgement within 7 days and a first
assessment within 14 days. There is no bug bounty. Known vulnerabilities in dependencies
and third-party deployments are out of scope.
