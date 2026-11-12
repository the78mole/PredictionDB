# Projektsteuerung (Governance)

*An English summary follows the German text. The German version is authoritative.*

PredictionDB ist ein Ein-Personen-Projekt mit offenem Code. Diese Datei beschreibt, wer was
entscheidet, wer welche Rolle hat und wie das Projekt weiterläuft, wenn der Hauptentwickler
ausfällt.

## Rollen und Verantwortlichkeiten

| Rolle | Wer | Aufgaben und Rechte |
|---|---|---|
| Projektinhaber und Maintainer | Daniel (the78mole) Glaser | Entscheidet über Ziele, Architektur, Lizenz und Releases; merged Pull Requests; verwaltet Repository, GHCR-Pakete, Secrets und den Betrieb der Referenzinstallation; bearbeitet Sicherheitsmeldungen ([SECURITY.md](SECURITY.md)). |
| Vertretung | [@mrwiora](https://github.com/mrwiora) | Übernimmt bei Ausfall des Maintainers Issues, Merges und Releases. |
| Beitragende | alle, die Pull Requests, Issues oder Kommentare einreichen | Schlagen Änderungen vor; Voraussetzung sind die [Beitragsbedingungen](CONTRIBUTING.md) mit `Signed-off-by`. |
| Automatisierte Helfer | Renovate, CodeRabbit, GitHub Actions, KI-Assistenten (Claude Code) | Aktualisieren Abhängigkeiten, prüfen und bauen. Renovate merged nur grüne Minor-/Patch-Gruppen selbst ([renovate.json](renovate.json)); alles andere entscheidet der Maintainer. Von Assistenten erstellte Änderungen laufen wie jede andere über einen Pull Request. |

## Entscheidungen

- Der Maintainer entscheidet. Größere Entscheidungen werden mit Datum und Begründung in
  [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md) (Abschnitt 12, offene Punkte und Entscheidungen) bzw.
  als Anforderung unter [reqs/](reqs/README.md) festgehalten.
- Vorschläge und Fragen kommen als Issue oder Pull Request. Jede Änderung an `main` läuft über
  einen Pull Request mit grüner CI (Ruleset „main schützen“, SYSR-SEC-007).
- Die Ziele für die nächsten Monate stehen in [ROADMAP.md](ROADMAP.md).

## Kontinuität

Fällt der Maintainer aus, soll das Projekt innerhalb einer Woche weiterlaufen können: Issues
bearbeiten, Pull Requests annehmen, Releases erzeugen.

- Releases entstehen automatisch aus `main` (Workflow „Release“); dafür reicht ein Merge.
- Code, Doku, Anforderungen, Workflows und Betriebsanleitungen liegen vollständig im Repository.
  Geheimnisse liegen nicht im Repository, sondern in OpenBao bzw. `.env`; welche Schlüssel eine
  neue Installation braucht, beschreibt [docs/DATENQUELLEN-ZUGANG.md](docs/DATENQUELLEN-ZUGANG.md).
- Vertretung mit Schreibrechten auf das Repository: [@mrwiora](https://github.com/mrwiora).

Was die Vertretung braucht und was nicht:

| Aufgabe | nötig | Stand |
|---|---|---|
| Issues anlegen und schließen, Pull Requests mergen | Collaborator mit Schreibrecht auf `the78mole/PredictionDB` | erfüllt (@mrwiora) |
| Releases und Images erzeugen | nichts weiter: Workflows „Release“, „Image“, „Predict“ laufen mit dem `GITHUB_TOKEN` | erfüllt |
| Pakete auf GHCR verwalten (Rechte, Löschen) | Admin-Rolle je Paket in den Paketeinstellungen | offen, für die Kontinuität nicht zwingend |
| Einstellungen, Rulesets, Secrets des Repositorys | Admin: Nachfolger im GitHub-Konto („Account successor“) oder Übertragung des Repositorys auf eine Organisation mit zwei Inhabern | offen |
| Referenzinstallation (VM) weiterbetreiben | nicht nötig für das Projekt; jeder kann nach [docs/DATENQUELLEN-ZUGANG.md](docs/DATENQUELLEN-ZUGANG.md) neu aufsetzen | — |
| Lizenzrechte (kommerzielle Lizenz) | gehen per Nachlassregelung auf den Rechtsnachfolger über ([CONTRIBUTING.md](CONTRIBUTING.md), Abschnitt 4) | offen |

## Änderungen an dieser Datei

Änderungen beschließt der Maintainer per Pull Request.

---

## English summary

PredictionDB is maintained by one person, Daniel (the78mole) Glaser, who decides on goals,
architecture, licensing and releases, merges pull requests and handles security reports.
Contributors propose changes via issues and pull requests under the
[contribution terms](CONTRIBUTING.md). Bots (Renovate, CodeRabbit, GitHub Actions) and AI
assistants only act through pull requests; Renovate merges green minor/patch groups on its own.
Decisions are recorded in `docs/ARCHITEKTUR.md` (section 12) and `reqs/`. Releases are created
automatically from `main`. The deputy with write access who takes over if the maintainer is
unavailable is @mrwiora.
