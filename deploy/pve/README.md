# Bandbreite der VM nach Tageszeit (PVE-Host)

Die Download-Drosselung der PredictionDB-VM ist das `rate=` der Netzwerkkarte in Proxmox
(MB/s, nicht MBit/s). `pdb-netzrate.sh` setzt es nach Zeitplan:

| Zeit (Europe/Berlin) | Grenze |
|---|---|
| Mo–Fr 9–17 Uhr | 50 MBit/s (`rate=6.25`) |
| sonst | 85 MBit/s (`rate=10.625`) |

Proxmox übernimmt den Wert im laufenden Betrieb. Der Timer prüft alle 5 Minuten und nach
dem Boot, daher stimmt der Wert auch nach Ausfällen oder Handänderungen wieder.

Installation auf dem PVE-Host, auf dem die VM läuft (als root):

```sh
install -m 755 pdb-netzrate.sh /usr/local/sbin/
install -m 644 pdb-netzrate.service pdb-netzrate.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pdb-netzrate.timer
pdb-netzrate.sh --dry-run    # zeigt, was gesetzt würde
```

Werte lassen sich per Drop-in ändern (`systemctl edit pdb-netzrate.service`,
`Environment=PDB_TAG_MBIT=…`), siehe Kopf des Skripts. Zieht die VM auf einen anderen
Knoten um, Skript und Timer dort installieren und auf dem alten deaktivieren.
