#!/usr/bin/env bash
# Staffelt die Bandbreitenbegrenzung der PredictionDB-VM in Proxmox nach Tageszeit:
# werktags (Mo–Fr) 9–17 Uhr Europe/Berlin TAG_MBIT, sonst NACHT_MBIT.
# Läuft auf dem PVE-Host (nicht in der VM) per systemd-Timer pdb-netzrate.timer
# alle 5 Minuten und nach dem Boot. Der Lauf ist idempotent: qm set wird nur
# aufgerufen, wenn sich der Wert ändert. Proxmox übernimmt den neuen Wert ohne
# Neustart der VM.
#
#   pdb-netzrate.sh [--dry-run]
#
# Angepasst werden alle Netzwerkkarten der VM, die schon ein rate= haben
# (oder die in PDB_NETS genannten, z. B. "net0 net1").
# Umgebung: PDB_VMID (108), PDB_TAG_MBIT (50), PDB_NACHT_MBIT (85),
#           PDB_TAG_VON (9), PDB_TAG_BIS (17), PDB_NETS (leer = alle mit rate=).
set -euo pipefail

vmid=${PDB_VMID:-108}
tag_mbit=${PDB_TAG_MBIT:-50}
nacht_mbit=${PDB_NACHT_MBIT:-85}
von=${PDB_TAG_VON:-9}
bis=${PDB_TAG_BIS:-17}
dry=0
[[ ${1:-} == --dry-run ]] && dry=1

wochentag=$(TZ=Europe/Berlin date +%u)   # 1 = Montag … 7 = Sonntag
stunde=$((10#$(TZ=Europe/Berlin date +%H)))
if ((wochentag <= 5 && stunde >= von && stunde < bis)); then
  mbit=$tag_mbit
else
  mbit=$nacht_mbit
fi
# Proxmox erwartet rate in MB/s
rate=$(awk -v m="$mbit" 'BEGIN { s = sprintf("%.3f", m / 8); sub(/\.?0+$/, "", s); print s }')

konfig=$(qm config "$vmid")
if [[ -n ${PDB_NETS:-} ]]; then
  read -r -a netze <<<"$PDB_NETS"
else
  mapfile -t netze < <(sed -nE 's/^(net[0-9]+): .*rate=.*/\1/p' <<<"$konfig")
fi
if ((${#netze[@]} == 0)); then
  echo "VM $vmid: keine Netzwerkkarte mit rate= gefunden, PDB_NETS setzen" >&2
  exit 1
fi

for net in "${netze[@]}"; do
  alt=$(sed -n "s/^$net: //p" <<<"$konfig")
  [[ -n $alt ]] || { echo "VM $vmid: $net fehlt" >&2; exit 1; }
  if [[ $alt == *rate=* ]]; then
    neu=$(sed -E "s/rate=[0-9.]+/rate=$rate/" <<<"$alt")
  else
    neu="$alt,rate=$rate"
  fi
  [[ $neu == "$alt" ]] && continue
  echo "VM $vmid $net: rate=$rate MB/s ($mbit MBit/s)"
  ((dry)) || qm set "$vmid" "--$net" "$neu" >/dev/null
done
