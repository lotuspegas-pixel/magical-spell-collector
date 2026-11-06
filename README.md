# Magical Spell Collector

Een gratis, lokaal draaiende Python-applicatie die historische Reddit-archiefdata via
[Arctic CLI](https://github.com/tamnd/arctic-cli) verzamelt, complete rituelen
conservatief herkent, bronnen/evidence bewaart, duplicaten koppelt en JSON, JSONL,
Excel-CSV en Markdown exporteert.

De tool **verzint geen ontbrekende stappen**. Een vraag, mening of losse discussie wordt
niet als complete spreuk geëxporteerd. Magische effecten worden als bronbewering behandeld,
niet als wetenschappelijk bewezen werking.

Repository: <https://github.com/lotuspegas-pixel/magical-spell-collector>

## Wat werkt

- `pilot`: maximaal 100 bronrecords uit r/Spells, met harde opslaggrens.
- `collect`: Arctic CLI-import in API-vensters van maximaal 31 dagen.
- `analyze`: streaming batches, SQLite-checkpoints en hervatten zonder dubbele records.
- Heuristische detectie zonder AI; optioneel lokale Ollama-analyse met evidencecontrole.
- Meerdere rituelen per expliciet gestructureerde post, post/reactie-aanvullingen van de auteur.
- Exacte hash- en fuzzy-ontdubbeling; bronvarianten blijven gekoppeld.
- Veiligheids- en rechtenpoort vóór een aparte publicatie-export.
- `export`: `spells.json`, `spells.jsonl`, `spells.csv`, totaal-Markdown,
  Markdown per categorie, reviewbestanden en `collection-report.md`.
- `stats`: terminaldashboard met werkelijke databaseaantallen.

## Vereisten

- Windows 10/11, Linux of macOS.
- Python 3.11 of nieuwer.
- [`uv`](https://docs.astral.sh/uv/) aanbevolen.
- [Arctic CLI](https://arctic-cli.tamnd.com/getting-started/installation/).
- Optioneel: [Ollama](https://ollama.com/) plus een lokaal model.

Arctic CLI heeft momenteel geen gepubliceerde Windows-release. Gebruik op Windows het
meegeleverde installatiescript; dit downloadt de officiële Go-zip, controleert SHA-256 en
bepaalt `arctic.exe` uit de officiële bron.

## Installatie

### Windows PowerShell

```powershell
cd "C:\pad\naar\magical-spell-collector"
powershell -ExecutionPolicy Bypass -File .\scripts\install-arctic-windows.ps1
uv sync --extra dev --python 3.11
uv run spell-collector doctor
```

Open na een eerste installatie een nieuwe PowerShell als `arctic` nog niet direct op PATH
wordt gevonden. De app vindt ook automatisch `%USERPROFILE%\.local\bin\arctic.exe`.

Start daarna eenvoudig met:

```powershell
.\start-windows.ps1 doctor
.\start-windows.ps1 pilot --limit 100
```

### Linux/macOS

Installeer Go 1.26+ en voer uit:

```bash
./scripts/install-arctic.sh
uv sync --extra dev --python 3.11
./start.sh doctor
```

## Veilige eerste proef

Bekijk eerst de grens en verwachte route:

```powershell
uv run spell-collector pilot --limit 100 --dry-run
```

Voer daarna de end-to-end pilot uit. `auto` probeert Arctic CLI en gebruikt alleen bij een
fout de begrensde Arctic Shift-API-route:

```powershell
uv run spell-collector pilot --limit 100 --source auto
```

Voor een volledig reproduceerbare offline test:

```powershell
uv run spell-collector pilot --source fixture --fixture tests/fixtures/pilot_records.jsonl
```

## Hoofdcommando's

```text
spell-collector doctor
spell-collector pilot [--limit 1..100] [--source auto|arctic|api|fixture]
spell-collector collect --after YYYY-MM-DD --before YYYY-MM-DD [-s Spells]
spell-collector analyze [--limit 0] [--use-ai --model MODEL]
spell-collector resume
spell-collector export
spell-collector verify
spell-collector stats
```

`--limit 0` bij `analyze` betekent: alle nog niet geanalyseerde lokale records. Bij
`collect --max-records 0` wordt alle reeds door Arctic geïmporteerde data streamend naar
SQLite verwerkt; de schijflimiet blijft actief.

Na een gewijzigde analysemethode kan de afgeleide collectie opnieuw worden opgebouwd zonder
de ruwe bronrecords te verwijderen:

```powershell
spell-collector analyze --rebuild
spell-collector analyze --reanalyze review --use-ai --model gemma4:latest
```

Voor stabiele JSON-uitvoer staat de globale optie vóór het commando:

```powershell
uv run spell-collector --json doctor
uv run spell-collector --json stats
```

Successen zijn JSON-objecten met `ok: true`; fouten bevatten
`{"ok": false, "error": {"type": "...", "message": "..."}}`. Tokens of cookies zijn
niet nodig en worden niet gelogd.

## Grotere verzameling

Pas eerst `config/default.yaml` of een eigen override aan. Maak daarna altijd een plan:

```powershell
uv run spell-collector collect `
  --after 2025-01-01 --before 2025-02-01 `
  --subreddit Spells --subreddit witchcraft `
  --max-records 5000 --dry-run
```

Zonder `--dry-run` splitst de app een lang datumbereik in API-taken van maximaal 31 dagen.
Arctic draait met één worker; 429-resets worden gerespecteerd. `pull`, `publish`, CAPTCHA-
omzeiling, proxyrotatie en onbegrensde torrentdownloads zijn niet via de app beschikbaar.

## Optionele Ollama-analyse

De standaardanalyse werkt zonder AI. Voor lokale AI:

```powershell
ollama list
uv run spell-collector analyze --use-ai --model gemma4:latest
```

Ollama-output moet exacte evidence uit de bron teruggeven. Niet-herleidbare velden worden
afgewezen en het record gaat naar `REVIEW`. De app gebruikt Reddit-data niet om een model te
trainen; dit is lokale inferentie.

## Exports en rechten

De hoofdexports bevatten alleen `COMPLETE`, unieke records zonder automatisch gevonden
veiligheidsrisico. Volledige Reddit-brontekst wordt niet geëxporteerd. `rights_status=UNKNOWN`
is **geen** toestemming tot herpublicatie.

Een record verschijnt pas in `publication-ready.json` en `publication-ready.md` na menselijke
controle en registratie:

```powershell
uv run spell-collector rights approve SPELL_ID `
  --status PERMISSION_GRANTED `
  --evidence-reference "lokaal dossier of toestemmingsbewijs"
```

Een verwijderingsverzoek blokkeert het record direct:

```powershell
uv run spell-collector rights remove REDDIT_SOURCE_ID --spell-id SPELL_ID
```

## Lokale opslag

```text
data/spells.sqlite3     SQLite-database, checkpoints en fouten
data/raw/arctic/        door Arctic geïmporteerde lokale data
data/logs/              Arctic stdout/stderr-logboeken
exports/                gegenereerde bestanden
config/default.yaml     subreddits, zoektermen, categorieën en grenzen
```

`data/` en gegenereerde exports worden bewust niet naar Git gecommit. De standaardlimiet is
5 GB met minimaal 2 GB vrije reserve; wijzig dit in de YAML-configuratie.

## Testen

```powershell
uv run --extra dev pytest
uv run --extra dev ruff check .
uv run spell-collector verify
```

De geteste architectuur en de werkelijke proefresultaten staan in
[`docs/architecture.md`](docs/architecture.md) en [`docs/qa-report.md`](docs/qa-report.md).

## Licenties en databronnen

- Deze applicatie: MIT.
- Arctic CLI: Apache-2.0.
- Python-afhankelijkheden: vrije/open-sourcelicenties.
- Arctic Shift wordt als externe gegevensdienst gebruikt; er wordt geen broncode uit die
  repository gebundeld. De Reddit-inhoud zelf blijft onder de rechten en voorwaarden van de
  betreffende auteurs/platforms vallen.

Respecteer de [Reddit Public Content Policy](https://support.reddithelp.com/hc/en-us/articles/26410290525844-Public-Content-Policy),
de [Reddit Data API Terms](https://redditinc.com/policies/data-api-terms) en verwijderingsverzoeken.
