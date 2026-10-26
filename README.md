# Folio PDF

Et professionelt PDF-redigeringsværktøj til Windows – i stil med Adobe Acrobat Pro –
der arbejder **100 % lokalt**. Intet sendes, læses eller gemmes online.

> Status: under udvikling. Se `STATUS.md` for, hvad der er færdigt, og hvad der mangler.

## Funktioner
Programmet har fire tilstande (øverst i vinduet, `Alt+1`–`Alt+4`). Hver tilstand har sine værktøjer i den
svævende værktøjslinje, og panelet *Egenskaber* til højre viser indstillingerne for det valgte værktøj.

| Tilstand | Værktøjer og funktioner |
|---|---|
| **Vis** | Faner med flere dokumenter, kontinuerlig visning, zoom og tilpasning, sideminiaturer, bogmærker, søgning med fremhævede fund, kommentaroversigt med filter · *Markér tekst* (kopiér) · *Hånd* (panorér) · udskrivning og eksport af sider som billeder |
| **Redigér** | *Redigér tekst*: ret eksisterende tekst direkte på siden i dokumentets egen skrifttype, størrelse og farve – afsnit ombrydes og justeres som i originalen, også roteret tekst, tabelceller og punktopstillinger; skrifttypevælger med søgning; fed, kursiv, understreget, gennemstreget, farve og størrelse på markeret tekst (`Ctrl+B/I/U`) eller hele afsnittet; justering (venstre, centreret, højre, lige margener) og linjeafstand; nummererede lister og hængende indrykning bevares; tekst nedenunder flyttes med, når et afsnit vokser (ellers tilbydes "Formindsk så den passer"); indsæt fra andre programmer som ren tekst; "Vis alle tekstbokse" · *Søg og erstat* (`Ctrl+H`, bevarer skrifttype og farve, "Erstat alle" er ét fortryd-trin) · *Markér objekt*: flyt, skalér, rotér, spejlvend, erstat, gem og slet billeder i PDF'en · *Tilføj tekst* (venstre/centreret/højre) · *Tilføj billede* (fil eller udklipsholder) |
| **Kommentér** | *Kommentér markeret tekst* som i Acrobat og Word: markér tekst med musen (præcis til tegnet, dobbeltklik = ord, tredobbeltklik = afsnit) og vælg i miniværktøjslinjen eller højreklikmenuen *Kommentér* (`Ctrl+Alt+M`), *Fremhæv*, *Understreg*, *Gennemstreg*, *Erstat tekst*, *Indsæt tekst* eller *Kopiér* (`Ctrl+C`) · Panelet *Kommentarer* til højre: tråde med svar, forfatter (dit Windows-navn) og tidspunkt, status (Accepteret, Afvist, Annulleret, Fuldført) og *Løst*, filter efter status/forfatter/side, søgning, sortering, `F8`/`Shift+F8` springer mellem kommentarer · *Opsummér kommentarer* til en PDF og udskrivning med eller uden kommentarer · *Markér kommentar* (vælg flere med Shift eller ved at trække, flyt, skift farve/gennemsigtighed/streg, slet) · *Fremhæv*, *Understreg*, *Gennemstreg* · *Note* · *Tekstfelt* · *Tegn* (frihånd) · *Figur* (rektangel, ellipse, linje, pil). Alt gemmes som standard-PDF-kommentarer, der kan læses i alle PDF-læsere |
| **Sider** | Stort sidegitter: flyt (træk og slip), rotér, duplikér, slet, indsæt tomme sider eller sider fra andre filer, udtræk sider som ny fil, opdel dokumentet |
| Værktøjer | *Kombinér filer* (PDF'er og billeder, sideudvalg pr. fil, bogmærker) · *Billeder til PDF* (skærmbilleder og fotos, også direkte fra udklipsholderen) |
| Gem | Hurtig gemning i baggrunden (vinduet fryser ikke), sikker gemning (skrives færdig, før den gamle fil erstattes; skrifttyper subsettes), automatisk sikkerhedskopi hvert 2. minut og gendannelse efter nedbrud (gemmes kun lokalt i `%LOCALAPPDATA%\Folio PDF\Gendannelse`), advarsel hvis filen er ændret af et andet program eller er skrivebeskyttet, *Gem en kopi*, adgangskodebeskyttelse og rettigheder bevares (AES-256) og kan ændres under *Adgangskode…*, *Reducér filstørrelse* (billedkvalitet, metadata, forfatternavne), *Indstillinger* (`Ctrl+,`) |
| Generelt | Fortryd/gentag på alt (hukommelsesbesparende historik), adgangskodebeskyttede PDF'er, kommandopalet (`Ctrl+K`), lyst og mørkt tema |

### Tastaturgenveje
| Genvej | Funktion | Genvej | Funktion |
|---|---|---|---|
| `Ctrl+O` | Åbn | `Ctrl+S` / `Ctrl+Shift+S` | Gem / Gem som |
| `Ctrl+Z` | Fortryd | `Ctrl+Y`, `Ctrl+Shift+Z` | Gentag |
| `Ctrl+F` | Søg i dokumentet | `Ctrl+K` | Kommandopalet |
| `Ctrl+P` | Udskriv | `Ctrl+W` | Luk fane |
| `Ctrl++` / `Ctrl+-` | Zoom ind / ud | `Ctrl+0` / `Ctrl+1` / `Ctrl+2` | Tilpas side / 100 % / Tilpas bredde |
| `PgUp` / `PgDn`, `Home` / `End` | Forrige/næste, første/sidste side | `Ctrl+G` | Gå til side |
| `Alt+1` … `Alt+4` | Vis / Redigér / Kommentér / Sider | `Ctrl+Tab` | Næste fane |
| `Ctrl+[` / `Ctrl+]` | Rotér side mod venstre / højre | `Ctrl+D` | Duplikér sider |
| `Ctrl+Shift+E` | Udtræk sider | `Ctrl+Shift+I` | Indsæt sider fra fil |
| `Ctrl+\` | Vis/skjul sidepanel | `Ctrl+Shift+L` | Lyst/mørkt tema |
| `Delete` | Slet det markerede (sider, kommentarer, billeder) | `F11` | Fuld skærm |
| `Ctrl+H` | Søg og erstat | `Ctrl+Alt+M` | Kommentér markeret tekst |
| `F8` / `Shift+F8` | Næste / forrige kommentar | `Ctrl+,` | Indstillinger |

Værktøjer vælges med ét bogstav. Bogstaver, som flere tilstande deler, vælger værktøjet i den aktuelle tilstand
(i en tilstand uden et sådant værktøj vælges værktøjet i *Vis*):

| Tast | Værktøj | Tast | Værktøj |
|---|---|---|---|
| `V` | Markér tekst (Vis) · Markér kommentar (Kommentér) | `H` | Hånd (Vis) · Fremhæv (Kommentér) |
| `E` | Redigér tekst | `O` | Markér objekt |
| `T` | Tilføj tekst (Redigér) · Tekstfelt (Kommentér) | `I` | Tilføj billede |
| `U` / `K` | Understreg / Gennemstreg | `N` | Note |
| `D` | Tegn | `R` | Figur |

Mens du redigerer tekst: `Ctrl+B` / `Ctrl+I` / `Ctrl+U` gør markeret tekst fed / kursiv / understreget, `Ctrl+Enter` gemmer, `Tab` / `Shift+Tab` gemmer og går til næste/forrige afsnit,
`Esc` annullerer. Skifter du værktøj eller tilstand midt i en redigering, gemmes den.

## Privatliv og offline-garanti
- Programmet indeholder ingen netværksmoduler (ingen `QtNetwork`, web-motor, opdateringstjek eller telemetri) –
  en test kontrollerer, at hele programmet kan indlæses uden ét eneste netværksmodul.
- Ved opstart aktiveres en **netværksspærre** (`pdfstudio/security/netguard.py`) som det allerførste, og den
  blokerer enhver forbindelse og navneopslag i programmet. Testsuiten kører med spærren aktiv og kontrollerer den.
- Ingen webskrifttyper eller ikoner hentes udefra – alt ligger i programmet.
- Valgfrit: installationen (eller `packaging/block_network_windows.ps1`) tilføjer en Windows Firewall-regel,
  så styresystemet også blokerer programmet.
- Filer læses og gemmes kun dér, hvor du selv vælger. Indstillinger og "seneste filer" gemmes lokalt.

## Installation (Windows)
**Den nemme vej – færdig installationsfil:**
1. Hent `FolioPDF-Setup-<version>.exe` (bygges automatisk af GitHub Actions-workflowet
   *Windows installer*; filen ligger under "Artifacts" på den seneste kørsel).
2. Dobbeltklik på filen og følg den danske installationsguide. Du kan vælge:
   - installation kun for dig (kræver ikke administrator) eller for alle brugere,
   - genvej på skrivebordet,
   - at Folio PDF åbner PDF-filer som standard,
   - (som administrator) en Windows Firewall-regel, der blokerer programmet fra nettet.
3. Programmet findes derefter i Start-menuen og kan afinstalleres under *Indstillinger → Apps*.

Installationsfilen er ikke kodesigneret, så Windows SmartScreen kan vise "Windows beskyttede din pc"
første gang – vælg *Flere oplysninger → Kør alligevel*.

**Byg selv på din egen pc:**
1. Installér Python 3.11+ fra python.org (flueben ved "Add python.exe to PATH") og
   Inno Setup 6 fra jrsoftware.org.
2. Kør `packaging\build_windows.bat`. Scriptet installerer byggeværktøjerne (kun her bruges nettet),
   kører alle tests, bygger `dist\FolioPDF\FolioPDF.exe` og derefter installationsfilen
   `dist\FolioPDF-Setup-<version>.exe`.
3. Uden installation kan programmet også køres direkte fra kildekoden:
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
