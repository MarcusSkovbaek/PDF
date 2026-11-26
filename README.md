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
| **Redigér** | *Redigér tekst* som i Word: ét klik i teksten sætter markøren præcis dér – hele spalten er én sammenhængende tekst uden bokse, `Enter`/`Backspace` deler og samler afsnit (nyt punkt i lister), piletasterne fortsætter i næste spalte og på næste side; teksten flyder videre til næste side, mens du skriver, nye sider får samme sidehoved og sidefod, sidetal ("Side 13 af 58") opdateres, kapitelstarter bliver på deres side, billeder og tabeller bliver liggende, og et afsnit efterlader aldrig én linje alene øverst eller nederst på en side; ret i dokumentets egen skrifttype, størrelse og farve – afsnit ombrydes og justeres som i originalen, også roteret tekst, tabelceller, sidehoveder og punktopstillinger; skrifttypevælger med søgning; fed, kursiv, understreget, gennemstreget, farve og størrelse på markeret tekst (`Ctrl+B/I/U`) eller hele afsnittet; justering (venstre, centreret, højre, lige margener) og linjeafstand; nummererede lister og hængende indrykning bevares; tekst nedenunder flyttes med, når et afsnit vokser (ellers tilbydes "Formindsk så den passer"); indsæt fra andre programmer som ren tekst; "Vis alle tekstbokse" · *Søg og erstat* (`Ctrl+H`, bevarer skrifttype og farve, "Erstat alle" er ét fortryd-trin) · *Markér objekt* som i et designprogram: tekstblokke (afsnit, overskrifter, sidehoveder, celler, billedtekster), grafik (streger, rammer, kasser, figurer – det, der hører sammen, vælges som én gruppe) og billeder vælges med ét klik, Shift/Ctrl+klik eller en ramme om flere; flyt med musen (det valgte løftes som et gennemsigtigt billede, hjælpelinjer klikker det på plads ud for andre objekters kanter og midter – `Alt` slår dem fra), med piletasterne eller X/Y i mm; skalér grafik (stregen bevarer sin tykkelse) og billeder; træk i en tekstbloks side for at ændre dens bredde (teksten ombrydes igen); streg- og fyldfarve og stregtykkelse på grafik; *Læg forrest/bagerst*; *Medtag indhold* (en kasse flytter sin tekst med); slet, klip, kopiér og indsæt (også på andre sider og i andre dokumenter) og duplikér. Flyttet tekst skrives med sine egne skrifter og kan stadig søges og redigeres, og alt andet på siden bliver urørt; billeder kan desuden roteres, spejlvendes, erstattes og gemmes. `Alt`+klik når baggrunde, skabelongrafik og tabellinjer · *Tilføj tekst* (venstre/centreret/højre) · *Tilføj billede* (fil eller udklipsholder) |
| **Kommentér** | *Kommentér markeret tekst* som i Acrobat og Word: markér tekst med musen (præcis til tegnet, dobbeltklik = ord, tredobbeltklik = afsnit) og vælg i miniværktøjslinjen eller højreklikmenuen *Kommentér* (`Ctrl+Alt+M`), *Fremhæv*, *Understreg*, *Gennemstreg*, *Erstat tekst*, *Indsæt tekst* eller *Kopiér* (`Ctrl+C`) · Panelet *Kommentarer* til højre: tråde med svar, forfatter (dit Windows-navn) og tidspunkt, status (Accepteret, Afvist, Annulleret, Fuldført) og *Løst*, filter efter status/forfatter/side, søgning, sortering, `F8`/`Shift+F8` springer mellem kommentarer · *Opsummér kommentarer* til en PDF og udskrivning med eller uden kommentarer · *Markér kommentar* (vælg flere med Shift eller ved at trække, flyt, skift farve/gennemsigtighed/streg, slet) · *Fremhæv*, *Understreg*, *Gennemstreg* · *Note* · *Tekstfelt* · *Tegn* (frihånd) · *Figur* (rektangel, ellipse, linje, pil). Alt gemmes som standard-PDF-kommentarer, der kan læses i alle PDF-læsere |
| **Sider** | Stort sidegitter: flyt (træk og slip), rotér, duplikér, slet, indsæt tomme sider, sider fra andre filer eller *fra udklipsholderen* (`Ctrl+V` i sidegitteret og miniaturerne eller højreklik "før/efter": et skærmbillede bliver en side i nabosidens papirstørrelse, tekst sættes i dokumentets brødtekst, kopierede PDF'er og billeder indsættes), udtræk sider som ny fil, opdel dokumentet |
| Værktøjer | *Ny PDF fra udklipsholder* (`Ctrl+Shift+N`, også på startsiden): ét tryk gør et skærmbillede, kopieret tekst (også formateret fra Word/Outlook/web), PDF'er og billeder kopieret i Stifinder eller objekter kopieret i Folio til en ny PDF – billeder tabsfrit, tekst søgbar med indlejret skrift; findes både billede og tekst (fx celler fra Excel), bruges billedet, og beskeden tilbyder *Brug teksten i stedet* · *Kombinér filer* (PDF'er og billeder, sideudvalg pr. fil, bogmærker) · *Billeder til PDF* (skærmbilleder og fotos, også direkte fra udklipsholderen) |
| Gem | Hurtig gemning i baggrunden (vinduet fryser ikke), sikker gemning (skrives færdig, før den gamle fil erstattes; skrifttyper subsettes), automatisk sikkerhedskopi hvert 2. minut og gendannelse efter nedbrud (gemmes kun lokalt i `%LOCALAPPDATA%\Folio PDF\Gendannelse`), advarsel hvis filen er ændret af et andet program eller er skrivebeskyttet, *Gem en kopi*, adgangskodebeskyttelse og rettigheder bevares (AES-256) og kan ændres under *Adgangskode…*, *Reducér filstørrelse* (billedkvalitet, metadata, forfatternavne), *Indstillinger* (`Ctrl+,`) |
| Generelt | Fortryd/gentag på alt (hukommelsesbesparende historik), adgangskodebeskyttede PDF'er, kommandopalet (`Ctrl+K`), lyst og mørkt tema |

### Tastaturgenveje
| Genvej | Funktion | Genvej | Funktion |
|---|---|---|---|
| `Ctrl+O` | Åbn | `Ctrl+S` / `Ctrl+Shift+S` | Gem / Gem som |
| `Ctrl+N` | Ny tom PDF | `Ctrl+Shift+N` | Ny PDF fra udklipsholder |
| `Ctrl+Z` | Fortryd | `Ctrl+Y`, `Ctrl+Shift+Z` | Gentag |
| `Ctrl+F` | Søg i dokumentet | `Ctrl+K` | Kommandopalet |
| `Ctrl+P` | Udskriv | `Ctrl+W` | Luk fane |
| `Ctrl++` / `Ctrl+-` | Zoom ind / ud | `Ctrl+0` / `Ctrl+1` / `Ctrl+2` | Tilpas side / 100 % / Tilpas bredde |
| `PgUp` / `PgDn`, `Home` / `End` | Forrige/næste, første/sidste side | `Ctrl+G` | Gå til side |
| `Alt+1` … `Alt+4` | Vis / Redigér / Kommentér / Sider | `Ctrl+Tab` | Næste fane |
| `Ctrl+[` / `Ctrl+]` | Rotér side mod venstre / højre | `Ctrl+D` | Duplikér sider (i *Markér objekt*: det markerede) |
| `Ctrl+Shift+E` | Udtræk sider | `Ctrl+Shift+I` | Indsæt sider fra fil |
| `Ctrl+V` (i sidegitteret / miniaturerne) | Indsæt sider fra udklipsholderen | | |
| `Ctrl+\` | Vis/skjul sidepanel | `Ctrl+Shift+L` | Lyst/mørkt tema |
| `Delete` | Slet det markerede (sider, kommentarer, objekter) | `F11` | Fuld skærm |
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

Mens du redigerer tekst: `Ctrl+B` / `Ctrl+I` / `Ctrl+U` gør markeret tekst fed / kursiv / understreget, `Tab` / `Shift+Tab` går til næste/forrige tekstflade (sidehoved, celle, billedtekst …),
piletasterne fortsætter i næste spalte/side, `Esc` afslutter og beholder ændringerne (som i Word), `Ctrl+Z` fortryder. Et helt redigeringsforløb er ét fortryd-trin. Klikker du uden for teksten eller skifter værktøj, gemmes den.

I *Markér objekt*: piletasterne flytter det markerede 1 pt (`Shift`: 10 pt – en serie ryk er ét fortryd-trin), `Shift` under træk holder retningen vandret/lodret, `Ctrl+C` / `Ctrl+X` / `Ctrl+V` kopierer, klipper og indsætter, `Ctrl+D` duplikerer, `Ctrl+A` markerer alt på siden, `Tab` / `Shift+Tab` går til næste/forrige objekt, `Ctrl+Shift+]` / `Ctrl+Shift+[` lægger forrest/bagerst, `Delete` sletter, `Esc` fjerner markeringen, og et dobbeltklik på tekst åbner *Redigér tekst* dér. Hvert træk eller hver handling er ét fortryd-trin.

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
