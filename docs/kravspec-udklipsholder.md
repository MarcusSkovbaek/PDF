# Kravspecifikation – PDF og sider fra udklipsholderen

Version 1.0 · 2026-11-23 · Folio PDF 1.3 → 1.4 · Status: **godkendt 2026-11-26** (forslagene i afsnit G) · **bygget**

Prioritet: **SKAL** = indgår og testes. **BØR** = indgår, medmindre det strider mod en SKAL, og det meldes.
**KAN** = kun hvis der er tid. Alle krav har et ID, så du kan godkende, rette eller stryge enkeltpunkter
("stryg U4", "U12: som billedet").

Gælder som altid: 100 % offline, dansk UI, samme designsprog, alt kan fortrydes, ingen nye afhængigheder (**+0 MB**).

---

## Udgangspunkt

* *Billeder til PDF* kan allerede tage skærmbilleder og billedfiler fra udklipsholderen (`Ctrl+V` i dialogen) –
  men man skal åbne dialogen, indsætte og trykke *Opret*.
* *Tilføj billede* kan lægge et skærmbillede **oven på** en eksisterende side.
* Der er ingen direkte vej til at gøre udklipsholderens indhold til en **ny PDF** eller en **ny side** i et
  dokument, og tekst fra udklipsholderen kan slet ikke bruges.

## A. Hvad kan bruges fra udklipsholderen (gælder både B og C)

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| U1 | SKAL | **Billeder.** Skærmbilleder (Print Screen, `Win+Shift+S`/Klippeværktøj) og billeder kopieret fra browser, Word, PowerPoint, Paint, Teams osv. | `Win+Shift+S` → *Ny PDF fra udklipsholder*: én side med skærmbilledet, skarpt i fuld opløsning. |
| U2 | SKAL | **Filer kopieret i Stifinder.** PDF'er (alle sider; adgangskode spørges om) og billedfiler (PNG, JPG, TIFF med flere sider, BMP, GIF, WEBP) i den rækkefølge, de står. Andre filtyper springes over med en besked. | Kopiér to PDF'er og et billede i Stifinder: én PDF med alle sider i rækkefølge. |
| U3 | SKAL | **Tekst.** Almindelig tekst (Notesblok, mail, chat) sættes op på sider med margener i en læsbar skrift; afsnit, tomme linjer og indrykning bevares, lang tekst fortsætter på nye sider, og teksten kan søges, markeres og redigeres bagefter. | 3 siders tekst fra Notesblok giver 3 A4-sider med de samme afsnit; "æøå" kan søges. |
| U4 | BØR | **Formateret tekst** (Word, Outlook, hjemmesider): overskrifter, fed, kursiv, understregning, farver, punkt- og nummererede lister og enkle tabeller bevares. | En overskrift og en punktliste fra Word ses som i Word (skrifttypen kan afvige). |
| U5 | SKAL | **Objekter kopieret i Folio** (*Markér objekt*, `Ctrl+C`) bliver en side i kildesidens størrelse med objekterne samme sted. | Kopiér en faktaboks: den nye PDF har boksen samme sted som på originalsiden. |
| U6 | SKAL | **Flere slags på én gang.** Har udklipsholderen flere slags indhold, bruges det første af: Folio-objekter → filer → billede → formateret tekst → tekst. Er der både billede og tekst (fx celler fra Excel), bruges billedet (det ser ud som originalen), og beskeden bagefter har knappen *Brug teksten i stedet* (søgbar tekst, samme fortryd-trin). | Kopiér celler i Excel: siden viser cellerne som billede; *Brug teksten i stedet* giver søgbar tekst. |

## B. Ny PDF fra udklipsholderen

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| U7 | SKAL | **Kommandoen *Ny PDF fra udklipsholder*** i *Filer*-menuen (under *Ny*), som kort på startsiden og i kommandopaletten, genvej **`Ctrl+Shift+N`**. Er udklipsholderen tom eller uden noget brugbart, er kommandoen nedtonet, og værktøjstippet siger hvorfor. | Med et skærmbillede i udklipsholderen åbner `Ctrl+Shift+N` straks en ny fane. |
| U8 | SKAL | **Uden dialog.** Den nye PDF åbner i en ny fane, ugemt; *Gem* foreslår navnet "Udklipsholder 2026-11-23 10.30.pdf" i den senest brugte mappe. | `Ctrl+S` lige efter: gem-dialogen har navnet klar. |
| U9 | SKAL | **Sidestørrelse.** Et billede får en side i billedets egen størrelse (som i Acrobat: ingen hvide kanter, 96 dpi som på skærmen). Tekst sættes på stående A4. Begge kan ændres under *Indstillinger → Udklipsholder* (som billedet / A4 / Letter, margen). | Et skærmbillede på 1920 × 1080 giver én liggende side uden kanter. |

## C. Indsæt side fra udklipsholderen

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| U10 | SKAL | **Kommandoen *Indsæt fra udklipsholder*** i *Sider*-tilstanden (værktøjslinje og *Sider*-menu), i højreklikmenuen på sider i sidegitteret og miniaturerne ("Indsæt fra udklipsholder før / efter"), i kommandopaletten og som **`Ctrl+V`**, når sidegitteret eller miniaturerne har fokus. | Markér side 3 og tryk `Ctrl+V`: skærmbilledet bliver side 4. |
| U11 | SKAL | **Placering og besked.** Indsættes efter den markerede side (ellers efter den aktuelle side), bliver markeret og vist. Beskeden "1 side indsat fra udklipsholderen" har *Fortryd*; det hele er ét fortryd-trin. | `Ctrl+Z` fjerner præcis de indsatte sider. |
| U12 | SKAL | **Passer til dokumentet.** Et billede skaleres ind på en side i nabosidens papirstørrelse, centreret med 10 mm margen; retningen følger billedet (et bredt skærmbillede får en liggende side). Tekst sættes i nabosidens størrelse med dokumentets brødtekstskrift. *Som billedet* (siden får billedets størrelse) kan vælges under *Indstillinger → Udklipsholder*. | Et skærmbillede i et A4-dokument giver en liggende A4-side med hele billedet. |
| U13 | SKAL | **Alt fra A kan indsættes**: PDF-filer (med deres egne sidestørrelser), billedfiler, tekst, formateret tekst og Folio-objekter. | Kopiér en PDF-fil i Stifinder og tryk `Ctrl+V` i sidegitteret: dens sider indsættes. |

## D. Kvalitet

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| U14 | SKAL | Billeder indsættes **tabsfrit** (PNG) i fuld opløsning med gennemsigtighed bevaret, skarpe på HiDPI-skærme. | Pixelsammenligning af en indsat PNG med originalen: identisk. |
| U15 | SKAL | Tekst får en **indlejret skrift** med alle tegn (æøå, €, “ ”, –), kan søges og kopieres i andre PDF-læsere og redigeres med *Redigér tekst*. | "Æblegrød – 25 €" kan søges i Folio og i Edge. |
| U16 | SKAL | **100 % lokalt.** Udklipsholderen læses kun lokalt (for at vise, om kommandoen kan bruges, og når du vælger den); intet hentes fra nettet – heller ikke billeder, som kopieret HTML henviser til. | Netværksspærren testes som altid. |
| U17 | SKAL | **Ydelse.** Et 4K-skærmbillede bliver til en PDF eller side på under 1 s; 50 siders tekst på under 2 s. | Målt i test. |

## Bevidste begrænsninger

* Komplekse layouts fra Word og hjemmesider (spalter, tekst rundt om billeder, sidehoveder) gengives ikke 1:1
  som tekst – brug billedet eller "Udskriv til PDF" i det program.
* Billeder i kopieret HTML, der kun findes på nettet, kommer ikke med (offline).
* Office-figurer og diagrammer indsættes som billede, ikke som vektorgrafik.
* Kun det aktuelle indhold i udklipsholderen (ikke historikken i `Win+V`).

## E. Teknisk plan (kort)

* **Kerne** (Qt-fri, `core/clipboard_pdf.py`): `ClipContent` (billeder som bytes, filstier, PDF-bytes, HTML,
  tekst, Folio-klip) → `content_to_pdf(content, options) -> bytes` (billeder via `images.images_to_pdf`, PDF'er
  via `insert_pdf`, tekst og HTML via PyMuPDF's `Story` med en indlejret lokal skrift, Folio-klip via
  `paste_clip` på en tom side) og `insert_content(pdf, content, at, options) -> list[int]` (ét `pdf.edit`).
* **UI**: `ui/clipboard_source.py` læser `QClipboard` → `ClipContent`; handlingerne `file.new_from_clipboard`
  og `pages.insert_clipboard`; kort på startsiden; `Ctrl+V` og højreklik i sidegitteret og miniaturerne;
  *Indstillinger → Udklipsholder*; beskeden med *Brug teksten i stedet*.
* **Tests**: kerne (hver indholdstype, sidestørrelser, roterede nabosider, danske tegn, gennemsigtighed, mange
  sider, ydelse), UI (kommandoer nedtonet/aktive, ny fane, `Ctrl+V` i sidegitteret, besked og fortryd),
  Windows-CI.

## F. Rækkefølge

1. Kerne og tests.
2. UI (ny PDF, indsæt side, indstillinger), tests og skærmbilleder i lyst og mørkt tema.
3. Dokumentation og Windows-byg.

## G. Beslutninger (mit forslag står først)

1. Sidestørrelse for en **ny PDF** fra et billede: **som billedet** (U9) – eller A4?
2. Sidestørrelse for en **indsat side**: **som nabosiden** (U12) – eller som billedet?
3. Både billede og tekst i udklipsholderen (Excel, hjemmesider): **billedet**, med *Brug teksten i stedet* (U6) – eller teksten?
4. Genvej til *Ny PDF fra udklipsholder*: **`Ctrl+Shift+N`** (U7)?
