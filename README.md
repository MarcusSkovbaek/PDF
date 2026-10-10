# Folio PDF

Et professionelt PDF-redigeringsværktøj til Windows – i stil med Adobe Acrobat Pro –
der arbejder **100 % lokalt**. Intet sendes, læses eller gemmes online.

> Status: under udvikling. Se `STATUS.md` for, hvad der er færdigt, og hvad der mangler.

## Funktioner
| Område | Indhold |
|---|---|
| Vis | Faner med flere dokumenter, kontinuerlig visning, zoom, sideminiaturer, bogmærker, søgning, udskrivning |
| Rediger | Ret eksisterende tekst i dokumentets egen skrifttype, størrelse og farve · tilføj ny tekst og billeder |
| Kommentér | Fremhæv, understreg, gennemstreg, noter, frihåndstegning, figurer og pile |
| Sider | Flyt (træk og slip), rotér, slet, duplikér, indsæt tomme sider eller sider fra andre filer, udtræk, opdel |
| Opret | Kombinér PDF'er og billeder til én PDF · lav PDF af skærmprints (PNG/JPG m.fl.) uden kvalitetstab |
| Sikkerhed | Fortryd/gentag på alt, sikker gemning, adgangskodebeskyttede PDF'er |

## Privatliv og offline-garanti
- Programmet indeholder ingen netværksmoduler (ingen `QtNetwork`, web-motor, opdateringstjek eller telemetri).
- Ved opstart aktiveres en **netværksspærre** (`pdfstudio/security/netguard.py`), som blokerer
  enhver forbindelse og navneopslag i programmet. Testsuiten kører med spærren aktiv og kontrollerer den.
- Valgfrit: `packaging/block_network_windows.ps1` tilføjer en Windows Firewall-regel,
  så styresystemet også blokerer programmet.
- Filer læses og gemmes kun dér, hvor du selv vælger. Indstillinger og "seneste filer" gemmes lokalt.

## Kom i gang (Windows)
1. Installér Python 3.11 eller nyere fra python.org (sæt flueben ved "Add python.exe to PATH").
2. Byg programmet:
   ```bat
   packaging\build_windows.bat
   ```
   Det installerer afhængigheder (kun ved byg – det færdige program bruger aldrig nettet),
   kører alle tests og bygger `dist\FolioPDF\FolioPDF.exe`.
3. Start `dist\FolioPDF\FolioPDF.exe`, eller kør direkte fra kildekoden:
   ```bat
   python -m pip install -r requirements.txt
   python main.py [fil.pdf ...]
   ```

## Udvikling
```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                                   # alle tests (kører uden skærm)
QT_QPA_PLATFORM=offscreen python main.py --screenshot ud.png --size 1440x900 fil.pdf
```
Arkitektur, API-kontrakter og designregler står i `CLAUDE.md`. Kort fortalt:
- `pdfstudio/core` – PDF-motoren (uden Qt, fuldt testet). Alle ændringer går gennem
  `PdfDocument.edit(...)`, så de automatisk kan fortrydes.
- `pdfstudio/ui` – brugerfladen. Nye værktøjer registreres i `ToolRegistry`, nye kommandoer i
  `ActionRegistry` (vises automatisk i menuer og kommandopaletten, Ctrl+K).
- `pdfstudio/i18n` – alle tekster; tilføj et sprog med en ny strengtabel.
