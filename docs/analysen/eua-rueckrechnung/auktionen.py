"""EUA-Auktionsreihe aus den Berichten "Auctions by the Common Auction Platform" (DG CLIMA).

Einmaliger Import (zulässig laut Nutzer für diese Analyse, nicht als laufender DB-Input):

1. Die Auktionierungsseite der Kommission wird nach Berichten ``cap_report_YYYYMM_en.pdf``
   ab 2015 durchsucht und jeder Bericht einmal geladen (Ratenbegrenzung: Pausen, Wiederholung).
2. ``pdftotext -layout`` macht daraus Text. Aus Annex 2 ("Results of individual auctions")
   wird jede Auktionszeile gelesen: Datum, Clearingpreis, Minimum/Maximum/Mittel/Median der
   Gebote, Auktionsvolumen, Gebotsvolumen, Cover Ratio, Bieter, erfolgreiche Bieter.
   Jede Zeile wird geprüft: Clearingpreis x Volumen muss als Erlös in derselben Zeile stehen,
   Gebotsvolumen / Volumen muss die Cover Ratio ergeben.
3. Aus Table 1 ("main statistics on the results of the auctions per month") werden die
   ausgewiesenen Monats- und Jahresmittel gelesen. Die volumengewichteten Mittel der
   Einzelauktionen werden dagegen abgeglichen.

Lizenz der Berichte: Weiterverwendung nach Beschluss 2011/833/EU, CC BY 4.0
(https://commission.europa.eu/legal-notice_en). Quelle: Europäische Kommission, DG CLIMA,
"Auctions by the Common Auction Platform", auf Grundlage von Angaben der EEX.

Aufruf aus dem Repository-Wurzelverzeichnis::

    uv run --no-project --with pandas --with httpx \\
        python docs/analysen/eua-rueckrechnung/auktionen.py --cache /tmp/eua
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

import httpx
import pandas as pd

HIER = Path(__file__).resolve().parent
SEITE = "https://climate.ec.europa.eu/eu-action/eu-emissions-trading-system-eu-ets/auctioning_en"
BASIS = "https://climate.ec.europa.eu"
ERSTES_JAHR = 2015

MONATE = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DATUM = re.compile(
    r"^\s*(?:\((?P<tag>[A-Z]{2})\)\s*)?(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})\s+(.*)$"
)
PREIS = re.compile(r"\d+\.\d+")
ABSCHNITT = re.compile(r"(General|Aviation)\s+Allowances\s*[-\u2013]\s*([A-Za-z]+)\s+(\d{4})")
MONATSZEILE = re.compile(
    r"^\s*(?P<label>(?:[A-Z][a-z]{2,8}\.?\s+(?:\d{4}|\d{2}))|(?:\d{4}\s+total))\s+(?P<rest>.*\S)\s*$"
)


# --- Laden -------------------------------------------------------------------------------


def berichte_laden(cache: Path) -> list[Path]:
    """Alle CAP-Berichte ab 2015 von der Auktionierungsseite laden (mit Cache)."""
    pdf = cache / "pdf"
    pdf.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (PredictionDB-Analyse)"},
        follow_redirects=True,
        timeout=120,
    )
    seite = c.get(SEITE).text
    links = sorted(
        set(re.findall(r'href="(/document/download/[^"]*cap_report_(\d{6})_en\.pdf)"', seite))
    )
    out = []
    for pfad, stamm in links:
        if int(stamm[:4]) < ERSTES_JAHR:
            continue
        datei = pdf / f"cap_report_{stamm}_en.pdf"
        for versuch in range(8):
            if datei.exists() and datei.read_bytes()[:4] == b"%PDF":
                break
            r = c.get(BASIS + pfad)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                datei.write_bytes(r.content)
                time.sleep(10)
                break
            time.sleep(30 * (versuch + 1))  # 429: die Seite begrenzt die Abrufrate
        else:
            raise RuntimeError(f"Bericht {stamm} nicht ladbar")
        out.append(datei)
    return sorted(set(out))


def pdf_datum(pdf: Path) -> pd.Timestamp | None:
    """Änderungsdatum laut PDF-Metadaten als Näherung für den Veröffentlichungstag.

    Die Kommission nennt kein Veröffentlichungsdatum; ModDate ist der letzte Stand der Datei.
    Liegt die Veröffentlichung später, ist der Verzug in der Auswertung eher unterschätzt.
    """
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"^ModDate:\s+(.*)$", info, re.M) or re.search(
        r"^CreationDate:\s+(.*)$", info, re.M
    )
    return pd.Timestamp(m.group(1).strip()).tz_localize(None).normalize() if m else None


def text(pdf: Path) -> str:
    txt = pdf.with_suffix(".txt")
    if not txt.exists():
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
    return txt.read_text(encoding="utf-8", errors="replace")


# --- Einzelauktionen (Annex 2) -----------------------------------------------------------


def _zahl(tok: str) -> float | None:
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def _ganzzahlen(text: str) -> list[tuple[int, int]]:
    """Alle ganzen Zahlen, die der Text darstellen kann, mit Position des ersten Tokens.

    Tausender sind mal durch Komma, mal durch Leerzeichen, mal gar nicht getrennt. Deshalb
    zählt jede Folge von Tokens, deren zweites und weitere Glieder genau drei Ziffern haben,
    als Kandidat ("2 918 000" ergibt 2, 2918, 2918000, 918, 918000 und 0).
    """
    tokens = [t for t in text.split() if re.fullmatch(r"\d{1,3}(,\d{3})+|\d+", t)]
    out = []
    for i, t in enumerate(tokens):
        wert = t.replace(",", "")
        out.append((i, int(wert)))
        for k in range(i + 1, len(tokens)):
            if not re.fullmatch(r"\d{3}", tokens[k]):
                break
            wert += tokens[k]
            out.append((i, int(wert)))
    return out


def _gebote(
    zahlen: list[tuple[int, int]], pos_v: int, vol: int, cover: tuple[float, float] | None
) -> int | None:
    """Gebotsvolumen: eine Zahl nach dem Volumen, die zur (gerundeten) Cover Ratio passt."""
    if not cover:
        return None
    wert, toleranz = cover
    for pos, b in zahlen:
        if pos > pos_v and b >= vol and abs(b / vol - wert) <= toleranz:
            return b
    return None


def auktionszeile(rest: str, folge: str) -> dict[str, object] | None:
    """Werte einer Auktion; ``rest`` ist die Zeile nach dem Datum, ``folge`` die Folgezeilen.

    Die ersten fünf Felder sind Preise (Clearing, Minimum, Maximum, Mittel, Median). Volumen
    und Erlös stehen je nach Bericht in derselben oder der nächsten Zeile, mit Leerzeichen,
    Komma oder ohne Tausendertrennung. Gesucht wird das Paar (Volumen V, Erlös R) unter allen
    darstellbaren Zahlen, für das R = Clearingpreis x V gilt (Prüfung "ok"). Gelingt das
    nicht, wird V über die Cover Ratio bestimmt (Gebotsvolumen / V = Cover Ratio) und die Zeile
    als "erloes_passt_nicht" markiert; fehlt auch das, als "volumen_fehlt".
    """
    tokens = rest.split()
    if len(tokens) < 5:
        return None
    n = 0
    while n < 5 and re.fullmatch(r"\d+(\.\d+)?", tokens[n]):
        n += 1
    preise = [_zahl(t) for t in tokens[:n]] if n == 5 else [_zahl(tokens[0])] + [None] * 4
    acp = preise[0]
    if acp is None or acp <= 0:
        return None
    pos = 0
    for t in tokens[: max(n, 1)]:
        pos = rest.index(t, pos) + len(t)
    gesamt = rest[pos:] + " " + folge
    dezimal = re.findall(r"(?<![\d,])\d{1,2}\.\d{1,3}(?![\d,])", gesamt)
    cover_text = next((x for x in dezimal if 0.3 < float(x) < 30), None)
    cover = (
        (float(cover_text), 0.5 * 10 ** -len(cover_text.split(".")[1]) + 0.001)
        if cover_text
        else None
    )
    werte: dict[str, object] = {
        "preis": acp,
        "gebot_min": preise[1],
        "gebot_max": preise[2],
        "gebot_mittel": preise[3],
        "gebot_median": preise[4],
        "cover": cover[0] if cover else None,
    }
    zahlen = _ganzzahlen(gesamt)
    menge = {z for _, z in zahlen}
    kandidaten = [(p, v) for p, v in zahlen if v >= 50_000 and v % 500 == 0]
    for pos_v, vol in kandidaten:
        erloes = round(acp * vol)
        if erloes in menge:
            gebote = _gebote(zahlen, pos_v, vol, cover)
            return {
                **werte,
                "volumen": vol,
                "gebotsvolumen": gebote,
                "erloes": erloes,
                "pruefung": "ok",
            }
    for pos_v, vol in kandidaten:
        if gebote := _gebote(zahlen, pos_v, vol, cover):
            return {
                **werte,
                "volumen": vol,
                "gebotsvolumen": gebote,
                "erloes": None,
                "pruefung": "erloes_passt_nicht",
            }
    return {
        **werte,
        "volumen": None,
        "gebotsvolumen": None,
        "erloes": None,
        "pruefung": "volumen_fehlt",
    }


def einzelauktionen(name: str, txt: str) -> tuple[list[dict], list[str]]:
    zeilen, fehler = [], []
    m = re.search(r"Annex\s+2\s+Results of individual auctions", txt)
    if m is None:
        return [], [f"{name}: kein Annex 2"]
    teil = txt[m.start() :].splitlines()
    art = None
    abgesagt = 0
    for n, zeile in enumerate(teil):
        a = ABSCHNITT.search(zeile)
        if a:
            art = "general" if a.group(1) == "General" else "aviation"
            continue
        d = DATUM.match(zeile)
        if not d or art is None:
            continue
        tag, monat, jahr = (int(x) for x in d.groups()[1:4])
        jahr += 2000 if jahr < 100 else 0
        try:
            datum = pd.Timestamp(year=jahr, month=monat, day=tag)
        except ValueError:
            fehler.append(f"{name}: Datum {zeile.strip()[:40]}")
            continue
        folge = []
        for z in teil[n + 1 : n + 8]:
            if DATUM.match(z) or ABSCHNITT.search(z) or re.match(r"\s*Monthly", z):
                break
            if "Auctions by the" not in z:  # Fußzeile beim Seitenumbruch
                folge.append(z)
        if d.group(5).split()[0] in {"-", "\u2013"}:
            abgesagt += 1
            zeilen.append(
                {
                    "datum": datum,
                    "art": art,
                    "plattform": d.group("tag") or "CAP",
                    "bericht": name,
                    "abgesagt": True,
                }
            )
            continue
        werte = auktionszeile(d.group(5), " ".join(folge))
        if werte is None:
            fehler.append(f"{name}: nicht lesbar {zeile.strip()[:120]}")
            continue
        zeilen.append(
            {
                "datum": datum,
                "art": art,
                "plattform": d.group("tag") or "CAP",
                "bericht": name,
                "abgesagt": False,
                **werte,
            }
        )
    return zeilen, fehler


# --- Monats- und Jahresmittel (Table 1) --------------------------------------------------


def _monat(label: str) -> pd.Period | None:
    teile = label.replace(".", "").split()
    if teile[-1] == "total":
        return pd.Period(teile[0], "Y")
    m = MONATE.get(teile[0][:3].lower())
    if m is None:
        return None
    j = int(teile[1])
    j += 2000 if j < 100 else 0
    return pd.Period(year=j, month=m, freq="M")


def monatsmittel(name: str, txt: str) -> list[dict]:
    """Ausgewiesene Mittel aus Table 1 (Allgemeinzertifikate)."""
    m = re.search(r"Table 1\s*:\s*General allowances", txt, re.I)
    if m is None:
        return []
    ende = re.search(r"Table 2\s*:", txt[m.end() :])
    teil = txt[m.end() : m.end() + (ende.start() if ende else 20000)]
    out = []
    for zeile in teil.splitlines():
        z = MONATSZEILE.match(zeile)
        if not z:
            continue
        periode = _monat(z.group("label"))
        preise = PREIS.findall(z.group("rest"))
        if periode is None or not preise:
            continue
        vol = re.match(r"\s*([\d ,]+?)\s{2,}", z.group("rest"))
        out.append(
            {
                "periode": str(periode),
                "art": "jahr" if periode.freqstr.startswith("Y") else "monat",
                "preis_bericht": float(preise[-1]),
                "volumen_bericht": int(re.sub(r"\D", "", vol.group(1))) if vol else None,
                "bericht": name,
            }
        )
    return out


# --- Bereinigung und Abgleich -----------------------------------------------------------

# Offensichtliche Fehler in den Berichten, jeweils mit Beleg aus demselben Bericht.
KORREKTUREN = [
    {
        "datum": "2015-01-26",
        "feld": "preis",
        "wert": 6.76,
        "grund": "Bericht nennt 7.76, Erlös 19 725 680 / Volumen 2 918 000 = 6.76; "
        "Mittel und Median der Gebote 6.75/6.74 (cap_report_201503)",
    },
    {
        "datum": "2017-03-29",
        "feld": "erloes",
        "wert": 27_028_335,
        "grund": "Bericht nennt Erlös 20 028 335, Preis 4.71 x Volumen 5 738 500 = 27 028 335; "
        "Monatsmittel passt mit 4.71 (cap_report_201703)",
    },
]
# Im Annex fehlende Auktion: Preis aus Monatssumme in Table 1 desselben Berichts abgeleitet.
ERGAENZUNG = {
    "datum": "2017-02-28",
    "monat": "2017-02",
    "volumen": 4_261_500,
    "monatsvolumen": 51_138_000,
    "monatserloes": 261_528_255,
    "grund": "Auktion im Auktionskalender von cap_report_201703 genannt, fehlt in Annex 2; "
    "Preis = (Monatserlös Table 1 - Summe der übrigen Erlöse) / Volumen",
}


def bereinigen(roh: pd.DataFrame) -> pd.DataFrame:
    df = roh.copy()
    df["korrektur"] = ""
    df = df[~df["abgesagt"].astype(bool)].drop(columns="abgesagt")
    # Überlappende Berichte: gleiche Auktion nur einmal (neuerer Bericht gilt).
    df = df.sort_values("bericht").drop_duplicates(
        ["datum", "art", "plattform", "preis", "volumen"], keep="last"
    )
    # Manche Berichte führen Luftverkehrsauktionen zusätzlich in der Tabelle der
    # Allgemeinzertifikate; Table 1 zählt sie dort nicht mit.
    luft = df[df.art == "aviation"].set_index(["datum", "preis", "volumen"]).index
    doppelt = (df.art == "general") & df.set_index(["datum", "preis", "volumen"]).index.isin(luft)
    df = df[~doppelt]
    for k in KORREKTUREN:
        m = (df.datum == pd.Timestamp(k["datum"])) & (df.art == "general")
        df.loc[m, k["feld"]] = k["wert"]
        df.loc[m, "korrektur"] = k["grund"]
        if k["feld"] == "preis":
            df.loc[m, "erloes"] = (df.loc[m, "preis"] * df.loc[m, "volumen"]).round()
        df.loc[m, "pruefung"] = "korrigiert"
    e = ERGAENZUNG
    monat = df[(df.art == "general") & (df.datum.dt.to_period("M").astype(str) == e["monat"])]
    preis = (e["monatserloes"] - monat.erloes.sum()) / e["volumen"]
    if monat.volumen.sum() + e["volumen"] == e["monatsvolumen"]:
        df = pd.concat(
            [
                df,
                pd.DataFrame(
                    [
                        {
                            "datum": pd.Timestamp(e["datum"]),
                            "art": "general",
                            "plattform": "CAP",
                            "bericht": "cap_report_201703_en",
                            "preis": round(preis, 2),
                            "volumen": e["volumen"],
                            "erloes": e["monatserloes"] - monat.erloes.sum(),
                            "pruefung": "abgeleitet",
                            "korrektur": e["grund"],
                        }
                    ]
                ),
            ]
        )
    return df.sort_values(["datum", "art", "plattform"]).reset_index(drop=True)


def abgleich(df: pd.DataFrame, mittel: pd.DataFrame) -> pd.DataFrame:
    """Volumengewichtete Mittel der Einzelauktionen gegen die Mittel in Table 1."""
    g = df[df.art == "general"].copy()
    zeilen = []
    for art, freq in (("monat", "M"), ("jahr", "Y")):
        g["periode"] = g.datum.dt.to_period(freq).astype(str)
        eigen = g.groupby("periode").apply(
            lambda x: pd.Series(
                {
                    "auktionen": len(x),
                    "volumen": x.volumen.sum(),
                    "preis": (x.preis * x.volumen).sum() / x.volumen.sum(),
                }
            ),
            include_groups=False,
        )
        ber = (
            mittel[mittel.art == art]
            .sort_values("bericht")
            .drop_duplicates("periode", keep="last")
            .set_index("periode")[["preis_bericht", "volumen_bericht", "bericht"]]
        )
        j = eigen.join(ber, how="outer")
        j["art"] = art
        zeilen.append(j)
    out = pd.concat(zeilen).reset_index()
    out = out[out.periode >= str(ERSTES_JAHR)]
    out["abweichung"] = (out.preis.round(2) - out.preis_bericht).round(2)
    out["volumen_gleich"] = out.volumen == out.volumen_bericht
    return out


def tagesreihe(df: pd.DataFrame) -> pd.DataFrame:
    """Ein Wert je Auktionstag: volumengewichtetes Mittel aller Allgemeinzertifikat-Auktionen."""
    g = df[df.art == "general"]
    return (
        g.groupby("datum")
        .apply(
            lambda x: pd.Series(
                {
                    "preis": round((x.preis * x.volumen).sum() / x.volumen.sum(), 4),
                    "volumen": x.volumen.sum(),
                    "auktionen": len(x),
                    "cover": round(x.gebotsvolumen.sum() / x.volumen.sum(), 3)
                    if x.gebotsvolumen.notna().all()
                    else None,
                    "bericht": x.bericht.iloc[-1],
                    "korrektur": "; ".join(k for k in x.korrektur if k),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )


# --- Hauptprogramm -----------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, default=Path("/tmp/eua"))
    args = ap.parse_args()
    pdfs = berichte_laden(args.cache)
    alle, fehler, mittel = [], [], []
    for pdf in pdfs:
        t = text(pdf)
        z, f = einzelauktionen(pdf.stem, t)
        alle += z
        fehler += f
        mittel += monatsmittel(pdf.stem, t)
    roh = pd.DataFrame(alle)
    df = bereinigen(roh)
    mt = pd.DataFrame(mittel)
    ab = abgleich(df, mt)
    tage = tagesreihe(df)
    daten = HIER / "daten"
    df.to_csv(daten / "eua_auktionen.csv", index=False)
    tage.to_csv(daten / "eua_tagesreihe.csv", index=False)
    ab.to_csv(HIER / "tabellen" / "abgleich_berichtsmittel.csv", index=False)
    ber = pd.DataFrame([{"bericht": p.stem, "pdf_datum": pdf_datum(p)} for p in pdfs]).merge(
        df.groupby("bericht").datum.agg(erste_auktion="min", letzte_auktion="max"),
        on="bericht",
        how="left",
    )
    ber["verzug_tage"] = (ber.pdf_datum - ber.letzte_auktion).dt.days
    ber.to_csv(daten / "eua_berichte.csv", index=False)
    (daten / "eua_import_protokoll.txt").write_text(
        f"Berichte: {len(pdfs)} ({pdfs[0].stem} bis {pdfs[-1].stem})\n"
        f"Auktionszeilen roh: {len(roh)}, abgesagt: {int(roh.abgesagt.sum())}, "
        f"bereinigt: {len(df)}\n"
        f"Prüfung: {df.pruefung.value_counts().to_dict()}\n"
        f"Nicht lesbare Zeilen: {len(fehler)}\n" + "\n".join(fehler) + "\n"
    )
    m = ab[ab.art == "monat"]
    print(
        f"{len(pdfs)} Berichte, {len(df)} Auktionen, {len(tage)} Auktionstage; "
        f"Monatsmittel gleich (±0,01): {(m.abweichung.abs() <= 0.011).sum()}/{len(m)}, "
        f"Volumen gleich: {m.volumen_gleich.sum()}/{len(m)}"
    )


if __name__ == "__main__":
    main()
