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
#
# Ausgabe (auch nach $GITHUB_OUTPUT, wenn gesetzt):
#   vorher=<letzter Tag oder leer>  version=<vX.Y.Z oder leer>  stufe=major|minor|patch|keine
set -euo pipefail

ziel=${1:-HEAD}
vorher=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' "$ziel" 2>/dev/null || true)

if [[ -z $vorher ]]; then
  version=v0.1.0
  stufe=minor
else
  bereich="$vorher..$ziel"
  if [[ -z $(git rev-list "$bereich") ]]; then
    version=""
    stufe=keine
  else
    log=$(git log --format='%s%n%b%n--ende--' "$bereich")
    if grep -qE '^[a-zA-Z]+(\([^)]*\))?!:|^BREAKING[ -]CHANGE:' <<<"$log"; then
      stufe=major
    elif grep -qE '^feat(\([^)]*\))?:' <<<"$log"; then
      stufe=minor
    else
      stufe=patch
    fi
    IFS=. read -r major minor patch <<<"${vorher#v}"
    if [[ $stufe == major && $major == 0 ]]; then
      stufe=minor
    fi
    case $stufe in
      major) version="v$((major + 1)).0.0" ;;
      minor) version="v$major.$((minor + 1)).0" ;;
      patch) version="v$major.$minor.$((patch + 1))" ;;
    esac
  fi
fi

echo "vorher=$vorher"
echo "version=$version"
echo "stufe=$stufe"
if [[ -n ${GITHUB_OUTPUT:-} ]]; then
  {
    echo "vorher=$vorher"
    echo "version=$version"
    echo "stufe=$stufe"
  } >>"$GITHUB_OUTPUT"
fi
