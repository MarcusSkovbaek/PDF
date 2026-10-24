# Kravspecifikation – Tekstredigering, Gem og Kommentarer på tekst

Version 1.0 · 2026-10-24 · Folio PDF 1.0 → 1.1 · Status: **til godkendelse**

Prioritet: **SKAL** = indgår og testes. **BØR** = indgår, medmindre det viser sig at stride mod en SKAL
(fx størrelse/stabilitet), og det meldes i så fald. **KAN** = kun hvis der er tid. Alle krav har et ID, så du
kan godkende, rette eller stryge enkeltpunkter ("stryg A6", "C4 skal være Word-stil").

Gælder for alt nedenfor: 100 % offline (intet netværk, ingen nye online-afhængigheder), dansk UI, samme
designsprog (tema-tokens, lys/mørk), alt kan fortrydes (Ctrl+Z), og hverken programmet eller installationsfilen
må vokse mærkbart. Mål: **+0 MB** i nye afhængigheder; alt bygges på PyMuPDF/PySide6, som allerede er med.

---

## Udgangspunkt (hvad programmet kan i dag)

* **Tekstredigering:** et klik åbner et afsnit i en editor oven på siden, med dokumentets egen skrifttype,
  størrelse og farve. Formatering (skrift, størrelse, farve, fed, kursiv) kan kun ændres for **hele afsnittet**.
  Når et afsnit vokser, skubbes indholdet nedenunder ikke; teksten lægger sig oven på det. På tætte sider tager det
  0,2–0,35 s at åbne og gemme editoren.
* **Gem:** *Gem* og *Gem som* gemmer atomisk og subsetter skrifttyper. Der er ingen autogem eller gendannelse
  efter nedbrud. Der er ingen *Gem kopi* og ingen størrelsesreduktion. Programmet registrerer ikke, hvis filen er
  ændret af et andet program.
  **Fejl fundet:** en adgangskodebeskyttet PDF gemmes i dag **uden** sin beskyttelse, og brugeren får ingen besked.
  Hvert fortryd-trin gemmer desuden hele dokumentet (3–5 MB pr. trin efter tekstredigering).
* **Kommentarer:** fremhæv, understreg og gennemstreg (præcision på ord), noter og en liste over kommentarer.
  Der mangler: tekstmarkering på tegnniveau, kommentar på en markering i ét trin, forfatter og tidspunkt, svar
  (tråde), status (løst, accepteret, …), redigering direkte i listen, og filtrering på forfatter og status.

---

## A. Tekstredigering

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| A1 | SKAL | **Formatering af en markering inde i afsnittet.** Fed (Ctrl+B), kursiv (Ctrl+I), understreget (Ctrl+U), gennemstreget, farve, størrelse og skrifttype virker på det markerede. Uden markering gælder det for det, man skriver videre. Inspektøren viser markeringens format (blandet = "–"). | Et ord gøres fedt midt i et afsnit, gemmes og genåbnes. Kun det ord er fedt, i samme skriftfamilie (fed variant fra dokumentet, eller ellers fra systemet), og resten er uændret. |
| A2 | SKAL | **Bevar understregning og gennemstregning.** Eksisterende linjer under eller gennem tekst (som Word laver som streger) følger teksten ved redigering, og nye understregninger tegnes som rigtige streger i PDF'en. | En understreget LibreOffice-tekst redigeres: stregen flytter med og får den nye længde. Der står ingen forældreløs streg tilbage. |
| A3 | SKAL | **Justering og linjeafstand for eksisterende afsnit.** Venstre, centreret, højre og lige margener samt linjeafstand (1,0 / 1,15 / 1,5 / 2,0 / præcis). | Et venstrestillet afsnit centreres og får 1,5 i linjeafstand. Efter gem og genåbning er resultatet det samme. |
| A4 | SKAL | **Ingen overlap, når et afsnit vokser.** Hvis det redigerede afsnit fylder mere eller mindre end før, flyttes indholdet direkte under i samme spalte op eller ned (tekst, og billeder hvor det er sikkert), så længe der er plads til sidens bund. Er der ikke plads, får man et tydeligt valg i inspektøren: *Formindsk teksten, så den passer* eller *Behold (overlapper)*. | Et afsnit, der vokser med 2 linjer, skubber de 3 afsnit under sig ned uden overlap. Ved sidens bund vises valget. Det hele er ét fortryd-trin. |
| A5 | SKAL | **Søg og erstat (Ctrl+H)** i hele dokumentet eller i en markering. Tekst erstattes i samme skrifttype. *Erstat*, *Erstat alle*, forhåndsvisning af antal og samme indstillinger som søgning (forskel på store og små bogstaver, hele ord). | "Kunde" erstattes med "Klient" i 40 forekomster på 12 sider i ét trin. Linjerne ombrydes korrekt, og ét Ctrl+Z gendanner alt. |
| A6 | BØR | **Stavekontrol i editoren** med rød bølgelinje og forslag ved højreklik. Den bruger **Windows' indbyggede stavekontrol** (dansk og engelsk, offline, 0 MB ekstra). Kun for tekst, man redigerer. | Et fejlstavet dansk ord understreges, og der vises forslag. Uden stavekontrol i Windows sker der intet, og der kommer ingen fejl. |
| A7 | SKAL | **Hurtigere.** Editoren åbner på under 100 ms og gemmer på under 150 ms på en tæt side (58-siders testrapport), takket være en tekst-cache pr. side. | Målt i den automatiske test. |
| A8 | SKAL | **Manglende tegn.** Kan dokumentets skrifttype ikke vise et tegn (fx "ø" i en subset-skrift), henter programmet det fra en lignende skrifttype og fortæller diskret hvilken. Der kommer aldrig tomme firkanter. | Test med en subset-skrift uden "æøå". |
| A9 | SKAL | **Indsæt.** Indsat tekst fra andre programmer kommer ind som ren tekst i afsnittets format. Kopiering inde i editoren bevarer formatet. Ctrl+Z virker tegn for tegn i editoren og bagefter som ét dokument-trin. | UI-test. |
| A10 | SKAL | **Afsnitsgenkendelse på flere typer PDF'er.** Nummererede lister ("1.", "a)", "i.") og hængende indrykning behandles som punktopstillinger. Testsamlingen udvides med PDF'er fra Word, LibreOffice og InDesign-lignende layouts. | Testen, der redigerer alle blokke, kører uden overlap eller tabt tekst på hele samlingen. |
| A11 | BØR | **Fortryd-hukommelse.** Fortryd-trin fylder højst ca. 1/10 af i dag (komprimeret, og fælles data som skrifttyper gemmes én gang). | 60 tekstredigeringer på 58 sider bruger under 50 MB til fortryd (i dag 200–300 MB). |
| A12 | KAN | Tegnafstand og hævet/sænket skrift på en markering. | – |

---

## B. Gem

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| B1 | SKAL | **Adgangskode og rettigheder bevares.** En beskyttet PDF gemmes med samme beskyttelse (AES-256) og samme adgangskode. *Gem som* tilbyder at fjerne eller ændre adgangskoden. | Åbn med adgangskode → redigér → gem → genåbn: der spørges igen efter adgangskoden. |
| B2 | SKAL | **Autogem og gendannelse.** Hvert 2. minut (kan ændres eller slås fra), og kun når der er ændringer, gemmes en gendannelseskopi **lokalt** i `%LOCALAPPDATA%\Folio PDF\Gendannelse`. Originalen røres aldrig. Efter et nedbrud tilbyder programmet ved næste start at gendanne. Kopien slettes, når man gemmer eller lukker normalt. Beskyttede dokumenter gendannes kun, hvis brugeren indtaster adgangskoden igen. | Programmet dræbes midt i en redigering. Ved næste start: "Gendan 1 dokument" med ændringerne bevaret. |
| B3 | SKAL | **Gem uden at fryse.** Gem og autogem kører i baggrunden med en lille statusindikator, så UI'et kan bruges imens. Man kan ikke redigere, mens dokumentet skrives. | Et 350-siders dokument gemmes, uden at vinduet hænger. |
| B4 | SKAL | **Sikkerhed mod konflikter.** Er filen ændret af et andet program siden åbning, får man et valg før overskrivning: *Overskriv*, *Gem som…* eller *Annuller*. Er filen skrivebeskyttet eller låst (fx åben i et andet program på Windows), får man en klar dansk besked og *Gem som…*. | UI-test med ændret mtime og en skrivebeskyttet fil. |
| B5 | SKAL | **Gem kopi…** gemmer en kopi uden at ændre, hvilken fil man arbejder i. | – |
| B6 | SKAL | **Alt i dokumentet bevares ved gem:** bogmærker, links, formularfelter, lag, metadata, sidelabels og eksisterende kommentarer, og der er ingen "reparationer" ved genåbning. Indeholder dokumentet digitale signaturer, advares der én gang om, at ændringer gør dem ugyldige. | Test på en samling med alle disse elementer. Validering med MuPDF og qpdf (i test). |
| B7 | SKAL | **Reducér filstørrelse…** (*Filer → Reducér størrelse*): billedkvalitet (Høj 300 dpi / Mellem 150 dpi / Lav 96 dpi), fjern ubrugte objekter, subset skrifttyper, valgfrit fjern metadata og personlige oplysninger (forfatter på kommentarer). Viser størrelsen før og efter, og der gemmes altid som ny fil eller med bekræftelse. | Et 20 MB skærmbillede-PDF bliver tydeligt mindre (forventet over 60 %). Indholdet er uændret bortset fra billedopløsningen. |
| B8 | SKAL | **Hurtigere gem.** Kun det, der er ændret, genskrives effektivt, og programmet genindlæser ikke unødigt fra disk efter gem. | Gem af 58-siders rapport efter én redigering tager under 0,5 s. |
| B9 | BØR | **Gem-indstillinger** i Indstillinger: autogem-interval, standardmappe, "spørg før overskrivning af ændret fil". | – |

---

## C. Markér tekst og kommentér (som Acrobat Pro og Word)

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| C1 | SKAL | **Præcis tekstmarkering på tegnniveau**, i læserækkefølge, også hen over linjer, spalter og sider. Dobbeltklik markerer et ord, tredobbeltklik et afsnit, og Shift+klik udvider. Mellemrum og linjeskift kopieres korrekt. Fungerer på roterede sider. | "markér fra midt i ord A til midt i ord B" giver præcis den tekst. Test på 2-spaltet og på 90°-roteret side. |
| C2 | SKAL | **Flydende mini-værktøjslinje ved markering** (Acrobat-stil) og det samme i højreklik-menuen: **Kommentér**, Fremhæv, Understreg, Gennemstreg, *Erstat tekst* (gennemstreg + forslag), *Indsæt tekst her* (indsættelsesmærke), Kopiér. | Markér → klik "Kommentér" → skriv → Enter. Det er 2 klik og ingen skift af værktøj. |
| C3 | SKAL | **Kommentar på en markering = én standard PDF-kommentar:** en fremhævning (farve kan vælges) med kommentartekst, forfatter, oprettelses- og ændringstidspunkt og den markerede tekst som citat. Ét fortryd-trin. Kommentarfeltet får fokus med det samme. Genvej **Ctrl+Alt+M** (som Word). | Kommentaren ses med tekst, forfatter og dato i Adobe Acrobat/Reader, Edge og Chrome (kontrolleres i test mod PDF-standardens felter og ved manuel kontrol). |
| C4 | SKAL | **Kommentarpanel til højre** med et kort pr. kommentar i dokumentrækkefølge. Kortet viser forfatterens initialer i en farvet cirkel, navn og tidspunkt ("for 5 min. siden"), citat af den markerede tekst, kommentarteksten, som kan redigeres direkte, svar og status. Panelets kort kan placeres ud for deres sted på siden som Word-bobler, med en tynd forbindelseslinje til markeringen, når der er plads. Ellers vises de som liste. Klik på kort ↔ markering vælger begge, scroller og pulserer kort. | UI-test plus skærmbilleder i lys og mørk. Hver kommentar kan nås og redigeres uden at forlade panelet. |
| C5 | SKAL | **Svar i tråde.** *Svar* under et kort. Svar gemmes som standard PDF-svar, så Acrobat viser dem som svar, og svar skrevet i Acrobat vises som tråd i Folio. | Rundtur: en PDF med Acrobat-svar vises korrekt. Folios svar har de felter, Acrobat bruger. |
| C6 | SKAL | **Status.** *Markér som løst* og *Genåbn* (Word), samt Acrobats gennemgangsstatus *Accepteret / Afvist / Annulleret / Fuldført*, gemt på Acrobats standardmåde. Løste kommentarer vises nedtonet og kan skjules. | Rundtur Folio ↔ Acrobat-format i test. |
| C7 | SKAL | **Filtrér, sortér og søg** i kommentarer: på type, forfatter, status (åben/løst) og side, med fritekstsøgning i kommentartekst og citat. Sortering efter side, dato eller forfatter. Antal vises. | UI-test. |
| C8 | SKAL | **Navigation:** *Næste/forrige kommentar* (F8/Shift+F8 og knapper i panelet). Panelet åbner automatisk, når man laver en kommentar. | – |
| C9 | SKAL | **Redigér og slet:** sletning af en kommentar sletter også dens svar (efter bekræftelse, hvis der er svar). Tekst skrevet i et kort er ét fortryd-trin, når kortet forlades eller med Ctrl+Enter, og Esc fortryder. Flyt markeringen ved at markere ny tekst og vælge *Flyt kommentar hertil* (BØR). | Test af alle handlinger plus fortryd og annuller fortryd. |
| C10 | SKAL | **Forfatternavn:** spørges om én gang ved første kommentar, forudfyldt med Windows-brugerens navn (læses lokalt). Det gemmes lokalt og kan ændres i Indstillinger. "Reducér størrelse" (B7) kan fjerne det igen. | – |
| C11 | SKAL | **Alle kommentartyper får kort, svar og status**, også eksisterende noter, figurer, frihåndstegning og tekstfelter, ikke kun tekstmarkeringer. | – |
| C12 | BØR | **Opsummér kommentarer:** eksportér en PDF med en oversigt over alle kommentarer (side, citat, forfatter, dato, svar, status), og udskriv med eller uden kommentarer. | Den genererede oversigt kan åbnes og har ét afsnit pr. kommentar. |
| C13 | KAN | Importér og eksportér kommentarer som FDF/XFDF-fil (udveksling med Acrobat uden at sende hele PDF'en). | – |

---

## D. Arkitektur og størrelse (hvordan det passer ind)

* **Ingen nye afhængigheder.** Stavekontrollen (A6) bruger Windows' egen COM-API via `ctypes`, som er
  standardbiblioteket. Er den ikke tilgængelig, er funktionen bare slået fra.
* **Kerne (`pdfstudio/core`, uden Qt, fuldt unit-testet):**
  * `core/comments.py` (ny): tråde, svar, status, forfatter og tidspunkt. Bygger på `annotations.py` og
    duplikerer det ikke. Selve tegningen af fremhævning, understregning osv. bliver i `annotations.py`.
  * `core/textsel.py` (ny): markering på tegnniveau (fra punkt til punkt → tekst + rektangler i display-koordinater).
    Bruges af markeringsværktøjet, markup-værktøjerne, søg og erstat og kommentarer, så den gamle ordbaserede
    kode i UI'et fjernes.
  * `core/text_edit.py` udvides med `replace_block_rich(pdf, block, runs, layout=None)` for formatering af en
    markering, justering og linjeafstand. Det nuværende `replace_block_text` bliver en tynd indpakning.
    `reflow_below(...)` håndterer A4, og der kommer en tekst-cache pr. side, som nulstilles ved ændringer.
  * `core/replace.py` (ny): søg og erstat, der genbruger `search.py` og `text_edit.py`.
  * `core/document.py`: `save(path, *, options)` med kryptering bevaret, `save_copy()` og
    `changed_on_disk()`. `core/history.py` får komprimerede snapshots.
  * `core/optimize.py` (ny): reducering af filstørrelse.
  * `core/recovery.py` (ny): autogem og gendannelse, kun lokale filer.
* **UI:**
  * Panelet `panels/comments.py` omskrives til kort med `widgets/comment_card.py`.
  * `widgets/selection_bar.py` er mini-værktøjslinjen.
  * Nye dialoger: `dialogs/find_replace.py` og `dialogs/reduce_size.py`.
  * Gem i baggrunden går via én `QThread`-hjælper.
  * Alle nye tekster lægges i `i18n/da_*.py`.
* `CLAUDE.md` opdateres med de nye API-signaturer. Den eksisterende kode, som de nye moduler erstatter
  (ordmarkering i `select.py`, den simple kommentarliste), **fjernes**, så der ikke ligger to udgaver.

## E. Test og kvalitetssikring (løbende, indtil kravene er opfyldt)

1. **Kerne:** unit-tests for hvert ID, herunder roterede sider, og rundture gem → genåbn → kontrollér for
   kryptering, svar og status.
2. **UI:** offscreen-tests af hver arbejdsgang (markér → kommentér → svar → løs → fortryd), plus skærmbilleder i
   lys og mørk tema, som jeg selv gennemser.
3. **Rigtige dokumenter:** testsamlingen fra LibreOffice (rapport, slides, lister, tabeller) udvides med
   Word-lignende og beskyttede PDF'er. Stresstesten redigerer alle blokke uden overlap eller tabt tekst.
4. **Kompatibilitet:** kommentarernes felter kontrolleres mod PDF-standarden (ISO 32000) og mod en PDF med
   Acrobat-kommentarer. Gemte filer valideres med MuPDF og `qpdf --check` (kun i udviklingsmiljøet).
5. **Ydelse:** måltider (A7, B3, B8) og hukommelse (A11) måles i tests.
6. **Windows:** hele testsuiten og installationsfilen bygges og røgtestes på Windows i GitHub Actions.
   Installationsfilens størrelse må ikke vokse mere end ca. 1 MB.

Fejler et krav, rettes det og testes igen, før næste del går i gang.

**Rækkefølge:**
1. C (kommentarer, inkl. C1 tekstmarkering)
2. B (gem: først B1-fejlen, så autogem)
3. A (tekstredigering)

Jeg pusher løbende og melder status efter hver del.

## F. Afklaringer (mine forslag står først)

1. **Forfatternavn på kommentarer (C10):** *spørg én gang, forudfyldt med Windows-navnet* · altid Windows-navnet
   uden at spørge · intet navn (anonymt).
2. **Kommentarvisning (C4):** *kort ud for deres sted, Word-stil, med liste som reserve* · kun liste (Acrobat-stil).
3. **Stavekontrol (A6):** *med, via Windows (0 MB)* · udelad.
4. **Autogem (B2):** *hvert 2. minut, slået til* · andet interval · slået fra som standard.
