# Architectuur

Magical Spell Collector is een lokale Python 3.11-applicatie. De CLI gebruikt Typer,
de duurzame opslag is SQLite en de verwerking werkt zonder extern AI-model. Ollama is een
optionele, lokale aanvulling. De standaardconfiguratie staat in `config/default.yaml`; een
eigen YAML-bestand wordt daar overheen gelegd. Relatieve opslagpaden worden altijd vanaf de
projectmap opgelost en zijn daardoor niet afhankelijk van de actieve terminalmap.

## Onderdelen

- `cli.py`: commando's, stabiele JSON-uitvoer en het Rich-terminaldashboard.
- `sources/arctic_cli.py`: veilige subprocess-laag rond Arctic CLI. Alleen begrensde
  acquisitie en read-only JSONL-query's zijn beschikbaar.
- `sources/arctic_shift.py`: begrensde API-route met responsslimiet, vertraging,
  rate-limitafhandeling en koppeling van reacties aan berichten.
- `pipeline.py`: opslagcontrole, normalisatie, ingestie, batchanalyse en hervatten.
- `analyzer.py`: kandidaatdetectie, sectie-extractie, volledigheid en categorisering.
- `ollama.py`: optionele lokale analyse; alleen uitvoer met letterlijke bronevidence wordt
  geaccepteerd.
- `dedupe.py` en `safety.py`: tekstnormalisatie, hashes/fuzzy vergelijking en conservatieve
  risicosignalering.
- `db.py`: SQLite-schema, idempotente writes, checkpoints, fouten en rechtenbeheer.
- `exporters.py`: atomaire JSON-, JSONL-, CSV- en Markdown-exports plus verificatie.

## Datastroom

```text
doctor / opslagcontrole
        |
Arctic CLI, Arctic Shift API of offline fixture
        |
normalisatie van post/reactie + bronmetadata
        |
SQLite raw_records
        |
streamende batchanalyse -> kandidaat -> extractie -> evidence
        |                         |          |
        |                     veiligheid   deduplicatie
        |                         |          |
        +---------------------- SQLite spells
                                  |
                         rechten- en exportpoort
                                  |
                   lokale hoofdexport / publicatie-export
```

Een pilot bevat hard maximaal 100 bronrecords. Grotere Arctic-taken worden per subreddit
opgesplitst in vensters van maximaal 31 dagen, draaien met één worker en gebruiken een
projectlokale datamap. Voor en tijdens acquisitie controleert de pipeline zowel de ingestelde
projectlimiet als de vereiste vrije schijfreserve. `pull`, `publish`, onbegrensde torrents en
toegangs-omzeiling worden niet aangeboden.

Ingestie zet verschillende Arctic/API-velden om naar één `RawRecord`. Reddit-prefixen worden
genormaliseerd, post- en comment-ID's blijven bewaard en iedere bron krijgt een herleidbare
URL, collectiebron en verzameldatum. De unieke sleutel
`(platform, source_kind, source_id)` maakt opnieuw importeren idempotent.

De analyse leest nog niet verwerkte records in configureerbare batches. Detectie gebruikt
zoektermen én procedurele signalen. Vragen en losse discussies kunnen als kandidaat of
`INCOMPLETE` worden bewaard, maar bereiken de hoofdcollectie niet. Expliciet gescheiden
rituelen in één tekst kunnen afzonderlijke records worden. Een reactie wordt alleen als
aanvulling aan een post gekoppeld wanneer zij van dezelfde auteur komt en een herkenbare
aanvullings- of correctieformulering bevat.

Voor ieder ritueel worden alleen aangetroffen onderdelen vastgelegd: doel, materialen,
voorbereiding, stappen, gesproken tekst, timing, afsluiting, traditie en symboliek. Ontbrekende
onderdelen blijven leeg. De statussen zijn `COMPLETE`, `INCOMPLETE`, `REVIEW` en `DUPLICATE`.
Exacte genormaliseerde hashes worden eerst vergeleken; daarna volgt fuzzy vergelijking met
woordvolgorde, tokens en SimHash. Grensgevallen gaan naar `REVIEW`, zodat echte varianten niet
stil worden verwijderd.

## SQLite en bronevidence

SQLite draait met foreign keys en WAL-modus. De belangrijkste tabellen zijn:

- `raw_records`: lokale posts/reacties, originele payload, bronroute en verwerkingsstatus.
- `spells`: de gestructureerde rituelen, scores, statussen, bronvelden en publicatievlaggen.
- `spell_sources`: koppeling van een ritueel aan de primaire bron en eventuele aanvullingen.
- `spell_evidence`: exact bronfragment, veldnaam, raw-record-ID en begin/eindoffset.
- `duplicate_links`: hoofdrecord, gekoppelde variant, vergelijkingsmethode en score.
- `jobs`, `checkpoints` en `subreddit_progress`: taakstatus, cursor en werkelijke voortgang.
- `errors`: herleidbare, eventueel opnieuw uitvoerbare fouten.
- `publication_permissions` en `removal_requests`: menselijke rechtencontrole en blokkades.

Evidence wordt pas toegevoegd wanneer het fragment letterlijk in de post of gekoppelde reactie
voorkomt. `original_text_reference` wijst naar het lokale raw-record; volledige Reddit-tekst
wordt niet naar de standaardexports gekopieerd. Daarmee blijft iedere extractie controleerbaar
zonder automatisch herpublicatierechten te veronderstellen.

## Onderbreken en hervatten

Na ieder geanalyseerd bronrecord worden `processed_at`, een subredditcheckpoint en
voortgang bijgewerkt. Jobs eindigen als `COMPLETE`, `PAUSED` of `FAILED`. `resume` zet alleen
herstelbare analysefouten opnieuw klaar en verwerkt vervolgens de resterende raw-records.
Een afgebroken verzameltaak kan veilig met hetzelfde begrensde `collect`-commando opnieuw
worden gestart, omdat raw-records via een unieke sleutel worden geüpsert.

`analyze --reanalyze review|incomplete|all` zet geselecteerde bronnen opnieuw klaar.
`analyze --rebuild` verwijdert uitsluitend afgeleide analyses, evidence en
publicatietoestemmingen; de ruwe bronrecords blijven behouden.

## Veiligheid en rechten

De veiligheidsmodule markeert onder meer giftige stoffen/planten, onveilige inname,
zelfbeschadiging, vuur, wapens, dierenleed en mogelijk handelen zonder toestemming.
`DANGEROUS` wordt uitgesloten; `REVIEW` vereist menselijke controle. Een niet-gematcht record
krijgt `SAFE` in de database, maar de export vermeldt uitdrukkelijk dat dit geen
veiligheidsgarantie is.

Reddit-inhoud begint met `rights_status=UNKNOWN`. De lokale hoofdexport bevat complete,
unieke, automatisch veilig bevonden records zonder volledige brontekst, maar is nog geen
publicatietoestemming. De afzonderlijke publicatie-export vereist daarnaast:

- menselijke goedkeuring;
- `PERMISSION_GRANTED` of `PUBLIC_DOMAIN`;
- geregistreerde publicatietoestemming;
- geen verwijderingsverzoek.

Een verwijderingsverzoek blokkeert het record direct op spell-ID of bron-ID. Magische effecten
worden steeds gepresenteerd als bronbewering, niet als wetenschappelijk bewezen werking.

## Exports en controle

`export` schrijft atomair naar `exports/`: `spells.json`, `spells.jsonl`, Excel-compatibele
UTF-8-CSV, de totale Markdown-collectie, bestanden per categorie, incomplete records, de
reviewwachtrij, de juridisch gecontroleerde publicatieselectie en `collection-report.md`.
Markdown laat lege brononderdelen weg.

`verify` controleert vervolgens formaten, aantallen, verplichte bronvelden, veiligheidsfilters
en de publicatiepoort. `stats` leest de werkelijke aantallen, categorieën, fouten, voortgang en
opslag rechtstreeks uit SQLite. Zo zijn gerapporteerde totalen gebaseerd op lokaal opgeslagen
records en niet op een vooraf aangenomen streefgetal.
