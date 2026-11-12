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
- Nach der Korrektur wird das Advisory veröffentlicht. Du wirst darin als Melder genannt, es
  sei denn, Du möchtest anonym bleiben.

Es gibt kein Bug-Bounty-Programm.

## Ablauf nach einer Meldung

1. **Eingang:** Der Maintainer bestätigt die Meldung im privaten Advisory (bis 7 Tage).
2. **Einschätzung:** Er prüft, ob und welche Bestandteile betroffen sind, und bewertet den
   Schweregrad mit CVSS im Advisory (bis 14 Tage). Ist es keine Lücke, wird das Advisory mit
   Begründung geschlossen.
3. **Korrektur:** Die Korrektur entsteht in einem privaten temporären Fork des Advisorys, mit
   Regressionstest. Kritische und hohe Schweregrade gehen allen anderen Arbeiten vor; Ziel ist
   eine Korrektur innerhalb von 60 Tagen nach Eingang.
4. **Release:** Der Merge auf `main` erzeugt automatisch ein neues Release `vX.Y.Z` und, falls
   betroffen, neue Images bzw. `predict/vX.Y.Z`. Die Referenzinstallation rollt es per Auto-Update
   aus.
5. **Veröffentlichung:** Das Advisory wird veröffentlicht, bei Bedarf mit CVE-Kennung über
   GitHub als CNA. Es nennt betroffene und korrigierte Versionen, den Melder (siehe oben) und,
   wo möglich, Umgehungen. Die Release-Notes verweisen auf das Advisory.
6. **Nachbereitung:** Lässt sich die Fehlerklasse allgemein verhindern (Lint-Regel, Fuzzer,
   Test), kommt diese Prüfung hinzu.

Was Du an Sicherheit erwarten kannst und wie sich Images und Release-Anhänge prüfen lassen,
steht in [docs/SICHERHEIT.md](docs/SICHERHEIT.md).

## Geltungsbereich

Gemeint ist der Code in diesem Repository: die Python-Pakete und Dienste, die Container-Images
auf GHCR, `compose.yaml`, die GitHub-Actions-Workflows und die Projektseite.

Nicht gemeint sind

- Bereits öffentlich dokumentierte Advisories zu Abhängigkeiten (CVE, GHSA). Ein Hinweis auf
  das Advisory darf als normales Issue kommen, aber ohne eigene Angaben dazu, wie es sich in
  PredictionDB ausnutzen lässt. Neue oder projektbezogene Erkenntnisse bitte immer privat
  melden.
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
assessment within 14 days. Fixes are developed in a private advisory fork with a regression
test, released automatically from `main`, and the advisory (with CVE via GitHub where needed) is
published afterwards. Reporters are credited in the advisory unless they ask to stay anonymous.
There is no bug bounty. Already public advisories for dependencies may be
filed as a normal issue without project-specific exploit details; anything new or specific
to PredictionDB must be reported privately. Third-party deployments are out of scope.
Security expectations, the assurance case and how to verify signatures:
[docs/SICHERHEIT.md](docs/SICHERHEIT.md).
