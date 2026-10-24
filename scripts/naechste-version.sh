#!/usr/bin/env bash
# Berechnet die nächste Version (Semantic Versioning) aus den Commits seit dem
# letzten Tag vX.Y.Z, nach Conventional Commits (bei Squash-Merges der PR-Titel):
#
#   BREAKING CHANGE im Text oder "typ!:" / "typ(scope)!:"  -> Major
#   feat: / feat(scope):                                   -> Minor
#   alles andere (fix, docs, ci, Analyse: ...)             -> Patch
#
# Solange die Version 0.x ist, hebt ein Breaking Change nur die Minor-Stelle.
# Ohne bisherigen Tag ist die erste Version v0.1.0.
#
#   scripts/naechste-version.sh [commit]     Standard: HEAD
#   scripts/naechste-version.sh [commit] --prefix predict/ -- predict services packages uv.lock
#
# Mit --prefix entsteht eine eigene Reihe <präfix>vX.Y.Z (z. B. predict/v0.1.0), die
# die Haupt-Tags vX.Y.Z nicht berührt. Pfade nach -- zählen nur Commits, die dort etwas
# ändern; ohne solche Commits gibt es keine neue Version.
#
# Ausgabe (auch nach $GITHUB_OUTPUT, wenn gesetzt):
#   vorher=<letzter Tag oder leer>  version=<präfix>vX.Y.Z oder leer
#   nummer=<X.Y.Z oder leer>  stufe=major|minor|patch|keine
set -euo pipefail

ziel=HEAD
praefix=""
pfade=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --prefix) praefix=$2; shift 2 ;;
    --) shift; pfade=("$@"); break ;;
    *) ziel=$1; shift ;;
  esac
done

vorher=$(git describe --tags --abbrev=0 --match "${praefix}v[0-9]*.[0-9]*.[0-9]*" "$ziel" 2>/dev/null || true)

if [[ -z $vorher ]]; then
  if [[ ${#pfade[@]} -gt 0 && -z $(git rev-list "$ziel" -- "${pfade[@]}") ]]; then
    version=""
    stufe=keine
  else
    version=${praefix}v0.1.0
    stufe=minor
  fi
else
  bereich="$vorher..$ziel"
  if [[ -z $(git rev-list "$bereich" -- "${pfade[@]}") ]]; then
    version=""
    stufe=keine
  else
    log=$(git log --format='%s%n%b%n--ende--' "$bereich" -- "${pfade[@]}")
    if grep -qE '^[a-zA-Z]+(\([^)]*\))?!:|^BREAKING[ -]CHANGE:' <<<"$log"; then
      stufe=major
    elif grep -qE '^feat(\([^)]*\))?:' <<<"$log"; then
      stufe=minor
    else
      stufe=patch
    fi
    IFS=. read -r major minor patch <<<"${vorher#"${praefix}"v}"
    if [[ $stufe == major && $major == 0 ]]; then
      stufe=minor
    fi
    case $stufe in
      major) version="${praefix}v$((major + 1)).0.0" ;;
      minor) version="${praefix}v$major.$((minor + 1)).0" ;;
      patch) version="${praefix}v$major.$minor.$((patch + 1))" ;;
    esac
  fi
fi

nummer=${version#"${praefix}"v}
ausgabe=$(printf 'vorher=%s\nversion=%s\nnummer=%s\nstufe=%s\n' "$vorher" "$version" "$nummer" "$stufe")
echo "$ausgabe"
if [[ -n ${GITHUB_OUTPUT:-} ]]; then
  echo "$ausgabe" >>"$GITHUB_OUTPUT"
fi
