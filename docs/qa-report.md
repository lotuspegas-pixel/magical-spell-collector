# QA-rapport

Live proef: 5 november 2026. Definitieve releasecontrole: 6 november 2026.

## Testomgeving

- Windows 11 Home, 64-bit.
- Intel Core Ultra 7 255H, 16 logische processors.
- 31,4 GB werkgeheugen en circa 390 GB vrije schijfruimte bij aanvang.
- Python 3.11.16, uv 0.12.15, Git 2.54 en Docker 29.9.2.
- Arctic CLI lokaal gebouwd uit de officiële broncode op commit `ad489ce02c0f`.
- Ollama 0.40.5; lokale proef uitgevoerd met `gemma4:latest`.

## Uitgevoerde controles

| Controle | Resultaat |
|---|---|
| Automatische tests | 48 geslaagd |
| Ruff-codecontrole | Geslaagd |
| Python wheel en source-distributie | Gebouwd |
| Wheel uitgevoerd buiten de repository | `version` en `doctor` geslaagd |
| Offline end-to-end fixturepilot | Geslaagd |
| Begrensde Arctic Shift-pilots | Geslaagd |
| Arctic CLI-installatie en echte help/info | Geslaagd |
| Arctic CLI live API-pilot | Bron gaf HTTP 422 time-out; fout hervatbaar vastgelegd |
| JSON, JSONL, CSV en Markdown | Aangemaakt en automatisch geverifieerd |
| Hervatten en dubbele verwerking | Automatisch getest |
| Lokale Ollama-JSON-analyse met evidencecontrole | Geslaagd op fictieve testbron |

## Werkelijke live proef

De proef gebruikte zes begrensde API-vensters voor r/Spells. Iedere aanvraag bleef op of
onder de pilotgrens van 100 records. Er zijn geen torrents, onbegrensde archieven, CAPTCHA-
omzeilingen of proxyrotaties gebruikt.

- 260 berichten en 260 reacties lokaal opgeslagen.
- 520 bronrecords geanalyseerd.
- 287 kandidaten gevonden.
- 1 complete, unieke en automatisch veilig bevonden hoofdrecord.
- 208 onvolledige records, 10 inhoudelijke reviewrecords en 66 duplicaten.
- 47 records in de gecombineerde menselijke reviewwachtrij, inclusief veiligheidsvlaggen.
- 0 publicatieklare records, omdat geen herpublicatietoestemming is geregistreerd.
- 1 onopgeloste maar vastgelegde bronfout: Arctic Shift gaf via Arctic CLI een HTTP 422
  time-out op reacties.

De lage complete-opbrengst is een bewuste kwaliteitsuitkomst. Vragen, losse discussies en
teksten met ontbrekende stappen worden niet tot complete spreuk opgewaardeerd.

## Bekende grenzen

- Alleen r/Spells is live beproefd; de overige configureerbare communities zijn nog niet
  grootschalig opgehaald.
- De doelstelling van 5.000 records is niet aangetoond en wordt niet als behaald voorgesteld.
- Heuristische en AI-analyse blijven hulpmiddelen; rechten, gevaarlijke inhoud en twijfelgevallen
  vereisen menselijke beoordeling.
- Live Reddit-inhoud en gegenereerde exports blijven lokaal en staan niet in Git.
