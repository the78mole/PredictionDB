#!/usr/bin/env bash
# Monatlicher Backtest der aktiven Modelle im mitlaufenden Vergleichsfenster
# (docs/BACKTEST.md, Kapitel 11). Läuft werktags per systemd-Timer um 03:00 Ortszeit
# (deploy/systemd/predictiondb-backtest@.*) und beginnt nach PDB_BT_STOP keinen Teil mehr.
# Die nächste Nacht setzt mit demselben Snapshot an den Zwischenständen fort, bis alle
# Konfigurationen des Monats fertig sind; danach tut das Skript bis zum Monatswechsel nichts.
#
#   scripts/backtest-monatlich.sh
#
# Ablage im Volume ml-data unter /data/monatlich/<JJJJMM>/:
#   snap-<konfiguration>/   Snapshot des Monats (einmal je Monat exportiert)
#   <konfiguration>/        Backtest (metrics*.csv, vergleich_jahreszeit*.csv, lauf.json)
# Ältere Monate als PDB_BT_KEEP werden entfernt.
#
# Umgebung: PDB_BT_CONFIGS   Konfigurationen (Vorgabe: "de_lu_v1 de_lu_v2")
#           PDB_BT_STOP      Ende des Zeitfensters, HH:MM Ortszeit (Vorgabe: 08:00)
#           PDB_BT_VARIANTS  active (Varianten der aktiven Versionen) oder all (Vorgabe: active)
#           PDB_BT_SNAP_H    Stunden, die ein Snapshot-Export braucht (Vorgabe: 2)
#           PDB_BT_KEEP      aufbewahrte Monate (Vorgabe: 3)
set -euo pipefail
cd "$(dirname "$0")/.."

export TZ=Europe/Berlin
configs=${PDB_BT_CONFIGS:-"de_lu_v1 de_lu_v2"}
stop=${PDB_BT_STOP:-08:00}
variants=${PDB_BT_VARIANTS:-active}
snap_h=${PDB_BT_SNAP_H:-2}
keep=${PDB_BT_KEEP:-3}
monat=$(date +%Y%m)
root=/data/monatlich/$monat

# Frist: nächstes Auftreten von $stop (von Hand am Nachmittag gestartet: morgen früh)
stop_s=$(date -d "today $stop" +%s)
if ((stop_s <= $(date +%s))); then
  stop_s=$(date -d "tomorrow $stop" +%s)
fi
stop_iso=$(date -d "@$stop_s" --iso-8601=seconds)

# Einmal-Container ohne Abhängigkeiten, mit niedriger CPU-Priorität
ml() { docker compose --profile ml run --rm --no-deps -T --entrypoint nice ml-train -n 19 ml-train "$@"; }
in_volume() { docker compose --profile ml run --rm --no-deps -T --entrypoint sh ml-train -c "$1"; }

# alte Monate aufräumen (nur Ordner dieses Skripts)
in_volume "mkdir -p /data/monatlich && ls -1 /data/monatlich | sort -r | tail -n +$((keep + 1)) \
  | while read -r d; do rm -rf \"/data/monatlich/\$d\"; done"

for cfg in $configs; do
  conf=config/ml/$cfg.yaml
  snap=$root/snap-$cfg
  out=$root/$cfg
  if in_volume "test -f $out/lauf.json"; then
    continue
  fi
  if ! in_volume "test -f $snap/manifest.json"; then
    if (($(date +%s) + snap_h * 3600 > stop_s)); then
      echo "$cfg: Snapshot passt nicht mehr vor $stop_iso, nächste Nacht"
      exit 0
    fi
    echo "$cfg: Snapshot $snap"
    ml --config "$conf" snapshot --out "$snap"
  fi
  args=(--config "$conf" backtest --snapshot "$snap" --out "$out" --rolling --stop-at "$stop_iso")
  [[ $variants == active ]] && args+=(--active)
  echo "$cfg: Backtest $out bis spätestens $stop_iso"
  rc=0
  ml "${args[@]}" || rc=$?
  if ((rc == 75)); then
    echo "$cfg: angehalten, Fortsetzung in der nächsten Nacht"
    exit 0
  elif ((rc != 0)); then
    exit "$rc"
  fi
done
echo "Monat $monat fertig: $configs"
