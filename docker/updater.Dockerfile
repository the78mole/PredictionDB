# Updater-Dienst (compose.yaml, Profil "updater"): Docker-CLI mit Compose-Plugin, dazu git
# und bash für scripts/vm-update.sh.
FROM docker:28.5-cli
RUN apk add --no-cache bash git util-linux
