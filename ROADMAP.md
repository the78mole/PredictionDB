# Roadmap

*Stand 2026-11-12, wird mindestens halbjährlich überprüft. An English summary follows the German text.*

Ziele für die nächsten zwölf Monate. Die Reihenfolge ist eine Absicht, kein Versprechen; das
Projekt wird in der Freizeit gepflegt. Entscheidungen und Details stehen in
[docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md), Anforderungen unter [reqs/](reqs/README.md).

## Bis Anfang 2027

- **Prognosegüte über mehrere Tage:** Güte je Vorlauftag messen und kalibrieren, das
  GEFS-Ensemble (bis D+16) als Unsicherheitsband nutzen.
- **Preisniveau aus freien Daten:** Grenzkostenindex und Brennstoffniveau weiter ausbauen, damit
  Preisabweichungen wie am 29.10.2026 kleiner werden.
- **Lieferkette:** Herkunftsnachweise auch für Modell- und Silber-Artefakte auf GHCR
  (SYSR-SEC-005), Fuzzing der übrigen Energiequellen-Parser (SYSR-SEC-009).

## 2027

- **Neutraining und Veröffentlichung der Modelle:** Sobald mindestens drei Jahre GFS-Daten als
  Trainingsbasis vorliegen, wöchentliches Neutraining nach Challenger/Champion und
  Veröffentlichung der Modelle auf GHCR.
- **Batteriespeicher und Anlagenbestand:** Wirkung des wachsenden Speicherbestands und der
  Anlagen in den Nachbarländern als Merkmale ins Modell.
- **Anlagen-API:** Prognosen für einzelne PV-, Wind- und Wasserkraftanlagen (Kapitel 7.6).
- **Python 3.15**, sobald eccodes/eckit passende Wheels liefern (Issue #84).

## Später oder offen

- Steuer-UI als optionale Control Plane (Konzept in [docs/STEUER-UI.md](docs/STEUER-UI.md)).
- Wärmepumpen- und Klimalast als eigene Prognosen.

## Nicht geplant

Bewusst nicht Ziel des Projekts, auch nicht im kommenden Jahr:

- **Unfreie Daten:** keine Quellen, deren Lizenz die Veröffentlichung von Modellen und Daten
  verbietet (etwa EEX-Terminpreise oder Gebotskurven der Börse). Was nur fürs Training erlaubt
  ist, bleibt aus öffentlichen Exporten heraus.
- **Handel und Steuerung:** keine Handelsempfehlungen, kein Anschluss an Börsen oder
  Handelssysteme, keine direkte Anlagensteuerung; Prognosen sind Eingänge für eigene Systeme.
- **Andere Märkte:** keine Prognosen für Gebotszonen außerhalb DE-LU und kein Intraday-Preis,
  solange es dafür keine freie Datenquelle gibt.
- **Mandantenfähiger Dienst:** kein gehosteter Mehrbenutzerdienst mit Konten und Rechten; eine
  Installation gehört einem Betreiber.
- **Langzeitsupport:** keine gepflegten Altversionen, nur der jeweils neueste Stand.

---

## English summary

Roadmap for the next twelve months: multi-day forecast quality and calibration with the
GEFS ensemble, better price-level features from free data, provenance for model and data
artifacts and more fuzzing (by early 2027); weekly retraining and publication of models once
three years of GFS training data are available, battery storage and neighbouring-country
capacity as features, a per-plant forecast API and Python 3.15 (2027); an optional control UI
and heat-pump/air-conditioning load later. Not planned: non-free data sources, trading advice or
direct plant control, bidding zones other than DE-LU or intraday prices without a free source, a
hosted multi-tenant service, and long-term support branches.
