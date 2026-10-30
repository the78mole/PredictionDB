"""BDEW-Standardlastprofil H0 (Haushalt) je Stunde und Tagtyp für gewichtete Tagesmittel

Revision ID: 0025_slp_h0
Revises: 0024_gfs_0p5_source
Create Date: 2026-10-30

``meta.slp_h0``: Viertelstundenwerte des H0-Profils (BDEW 1999, normiert auf 1.000.000 kWh/a)
je Ortszeit-Stunde summiert, nach Jahreszeit (Winter 1.11. bis 20.3., Sommer 15.5. bis 14.9., sonst
Übergang) und Tagtyp (WT Werktag, SA Samstag, FT Sonn- und Feiertag). Werte aus demandlib
0.2.2 (``bdew_data/selp_series.csv``, MIT-Lizenz, oemof developer group). Die Dynamisierung je
Tag ist ein gemeinsamer Faktor und fällt beim gewichteten Tagesmittel heraus.

``meta.slp_h0_gewicht(lt)`` liefert das Gewicht einer Ortszeit-Stunde. Bundesweite Feiertage
zählen als FT, der 24. und 31.12. wie im BDEW-Verfahren als SA (sofern kein Sonntag).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025_slp_h0"
down_revision: str | None = "0024_gfs_0p5_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# kWh je Stunde (Ortszeit): Jahreszeit, Tagtyp, erste Stunde der Zeile, dann acht Werte
H0 = """
winter    WT  0 0.23324 0.17260 0.15820 0.15404 0.15652 0.18736 0.36036 0.51772
winter    WT  8 0.53400 0.49288 0.46484 0.47328 0.52284 0.51800 0.46140 0.41928
winter    WT 16 0.42084 0.51468 0.66476 0.74832 0.67296 0.56388 0.46668 0.34556
winter    SA  0 0.26816 0.21148 0.16840 0.15880 0.15384 0.16032 0.21240 0.32780
winter    SA  8 0.47884 0.56692 0.59148 0.61996 0.66640 0.66320 0.61380 0.57520
winter    SA 16 0.57888 0.72196 0.83764 0.84048 0.68784 0.53636 0.49000 0.41564
winter    FT  0 0.31264 0.22520 0.18028 0.16448 0.15492 0.15448 0.16384 0.19996
winter    FT  8 0.35412 0.55400 0.69668 0.81112 0.82680 0.69208 0.54448 0.46640
winter    FT 16 0.42680 0.50280 0.62564 0.70516 0.64048 0.54216 0.45904 0.33856
uebergang WT  0 0.26632 0.19668 0.17716 0.17244 0.17628 0.21056 0.37164 0.51888
uebergang WT  8 0.54988 0.54388 0.52816 0.53312 0.59844 0.58264 0.50712 0.45376
uebergang WT 16 0.42456 0.46532 0.57940 0.68756 0.67220 0.62380 0.54272 0.40108
uebergang SA  0 0.29260 0.22508 0.18580 0.17504 0.17260 0.18060 0.23436 0.37304
uebergang SA  8 0.51880 0.60160 0.65312 0.68724 0.72556 0.70980 0.64512 0.59456
uebergang SA 16 0.58612 0.63744 0.73316 0.79216 0.72152 0.58936 0.55428 0.46604
uebergang FT  0 0.33716 0.24880 0.19560 0.17716 0.17260 0.17304 0.18300 0.26224
uebergang FT  8 0.44488 0.63816 0.74036 0.82076 0.83120 0.67460 0.54492 0.48404
uebergang FT 16 0.42920 0.44700 0.55020 0.64368 0.61888 0.57568 0.51060 0.37592
sommer    WT  0 0.29460 0.21936 0.19912 0.18744 0.19512 0.23704 0.39160 0.51884
sommer    WT  8 0.56656 0.57628 0.55696 0.56784 0.63132 0.61076 0.52708 0.47516
sommer    WT 16 0.45916 0.48996 0.56868 0.66068 0.66632 0.63020 0.57804 0.44832
sommer    SA  0 0.33200 0.25732 0.20924 0.20016 0.20192 0.20652 0.25484 0.37244
sommer    SA  8 0.51348 0.62300 0.65944 0.68452 0.72848 0.70392 0.62872 0.59344
sommer    SA 16 0.57944 0.59584 0.66592 0.72156 0.69152 0.61528 0.59436 0.49864
sommer    FT  0 0.35844 0.26660 0.21792 0.20244 0.19992 0.19508 0.20532 0.27788
sommer    FT  8 0.44988 0.64096 0.74636 0.82296 0.83776 0.70148 0.57628 0.49748
sommer    FT 16 0.43352 0.43836 0.50660 0.60808 0.63764 0.60216 0.56080 0.43228
"""


def _werte() -> str:
    rows = []
    for line in H0.split("\n"):
        if line:
            season, daytype, first, *ws = line.split()
            rows += [f"('{season}', '{daytype}', {int(first) + i}, {w})" for i, w in enumerate(ws)]
    return ", ".join(rows)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE meta.slp_h0 (
          season   text NOT NULL CHECK (season IN ('winter', 'uebergang', 'sommer')),
          daytype  text NOT NULL CHECK (daytype IN ('WT', 'SA', 'FT')),
          hour     smallint NOT NULL CHECK (hour BETWEEN 0 AND 23),
          w        double precision NOT NULL,
          PRIMARY KEY (season, daytype, hour)
        )""")
    op.execute(f"INSERT INTO meta.slp_h0 VALUES {_werte()}")
    # Ostersonntag nach der Gaußschen Osterformel (gregorianisch, anonymer Algorithmus)
    op.execute("""
        CREATE FUNCTION meta.ostersonntag(y int) RETURNS date
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
          SELECT make_date(y, (h + l - 7 * m + 114) / 31, (h + l - 7 * m + 114) % 31 + 1)
            FROM (SELECT h, l, (a + 11 * h + 22 * l) / 451 AS m
                    FROM (SELECT a, h, (32 + 2 * (b % 4) + 2 * (c / 4) - h - c % 4) % 7 AS l
                            FROM (SELECT a, b, c,
                                         (19 * a + b - b / 4 - (b - (b + 8) / 25 + 1) / 3 + 15)
                                           % 30 AS h
                                    FROM (SELECT y % 19 AS a, y / 100 AS b, y % 100 AS c) s1) s2)
                         s3) s4
        $$""")
    op.execute("""
        CREATE FUNCTION meta.slp_tagtyp(d date) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
          SELECT CASE
                   WHEN extract(isodow FROM d) = 7
                     OR to_char(d, 'MMDD') IN ('0101', '0501', '1003', '1225', '1226')
                     OR d - meta.ostersonntag(extract(year FROM d)::int) IN (-2, 1, 39, 50)
                     THEN 'FT'
                   WHEN extract(isodow FROM d) = 6 OR to_char(d, 'MMDD') IN ('1224', '1231')
                     THEN 'SA'
                   ELSE 'WT' END
        $$""")
    op.execute("""
        CREATE FUNCTION meta.slp_h0_gewicht(lt timestamp) RETURNS double precision
        LANGUAGE sql STABLE PARALLEL SAFE AS $$
          SELECT w FROM meta.slp_h0
           WHERE hour = extract(hour FROM lt)
             AND daytype = meta.slp_tagtyp(lt::date)
             AND season = CASE
                   WHEN to_char(lt, 'MMDD') >= '1101' OR to_char(lt, 'MMDD') <= '0320' THEN 'winter'
                   WHEN to_char(lt, 'MMDD') BETWEEN '0515' AND '0914' THEN 'sommer'
                   ELSE 'uebergang' END
        $$""")


def downgrade() -> None:
    op.execute("DROP FUNCTION meta.slp_h0_gewicht(timestamp)")
    op.execute("DROP FUNCTION meta.slp_tagtyp(date)")
    op.execute("DROP FUNCTION meta.ostersonntag(int)")
    op.execute("DROP TABLE meta.slp_h0")
