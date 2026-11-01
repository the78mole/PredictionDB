# Barrierefreiheit

*An English summary follows the German text.*

PredictionDB ist vor allem ein Backend-Projekt. Barrierefreiheit betrifft hier drei Stellen,
die Menschen direkt lesen oder bedienen:

| Bereich | Stand |
|---|---|
| Projektseite ([the78mole.github.io/PredictionDB](https://the78mole.github.io/PredictionDB/)) | semantisches HTML mit `lang="de"` und hellem wie dunklem Farbschema |
| Öffentliches Grafana-Dashboard „Prognose aktuell“ | Darstellung durch Grafana, überwiegend Diagramme |
| Dokumentation (README, `docs/`, Analysen als PDF) | Markdown mit Überschriftenstruktur und Tabellen; Analyse-PDFs werden aus Markdown erzeugt |

## Bekannte Einschränkungen

- Diagramme in Grafana und in den Analysen haben keine Textalternative, die den Verlauf
  beschreibt. Die zugrunde liegenden Zahlen sind über die Datei- und HTTP-Ausgabe des
  Prognose-Stacks (`predict/`) oder die Datenbank zugänglich.
- Die Farbwahl der Dashboards ist nicht durchgängig auf Farbsehschwächen geprüft.
- Die erzeugten PDFs sind nicht als barrierefreie PDFs (PDF/UA) getaggt.
- Dokumentation und Oberfläche sind überwiegend deutsch, Zusammenfassungen gibt es teils
  auf Englisch.

## Barrieren melden

Wenn Dir etwas den Zugang erschwert, eröffne bitte ein
[Issue](https://github.com/the78mole/PredictionDB/issues/new/choose) mit dem Bereich, dem
verwendeten Hilfsmittel (etwa Screenreader, Vergrößerung, Tastatur) und dem Problem. Als
Ein-Personen-Projekt gibt es keine feste Frist, Meldungen werden aber ernst genommen und
nach Aufwand priorisiert.

---

## English summary

PredictionDB is mostly a backend project. The project site, the public Grafana dashboard and
the documentation are the human-facing parts. Known limitations: charts lack text
descriptions (the underlying numbers are available via the `predict/` file and HTTP
output), dashboard colours are not fully checked for colour-vision deficiencies, generated
PDFs are not tagged (PDF/UA), and most content is in German. Please report barriers as a
GitHub issue.
