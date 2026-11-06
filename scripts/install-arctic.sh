#!/usr/bin/env sh
set -eu
if ! command -v go >/dev/null 2>&1; then
  echo 'Go 1.26+ ontbreekt. Installeer Go via https://go.dev/dl/ en voer dit script opnieuw uit.' >&2
  exit 1
fi
mkdir -p "$HOME/.local/bin"
GOBIN="$HOME/.local/bin" go install github.com/tamnd/arctic-cli/cmd/arctic@latest
"$HOME/.local/bin/arctic" version
echo 'Voeg $HOME/.local/bin toe aan PATH wanneer arctic nog niet gevonden wordt.'
