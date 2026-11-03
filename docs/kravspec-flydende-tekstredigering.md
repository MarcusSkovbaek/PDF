# Kravspecifikation – Flydende tekstredigering (som i Word)

Version 1.0 · 2026-11-03 · Folio PDF 1.1 → 1.2 · Status: **godkendt 2026-11-03**

Prioritet: **SKAL** = indgår og testes. **BØR** = indgår, medmindre det viser sig at stride mod en SKAL
(fx stabilitet), og det meldes i så fald. **KAN** = kun hvis der er tid. Alle krav har et ID, så du kan
godkende, rette eller stryge enkeltpunkter ("stryg F13", "F16: Esc skal annullere").

Gælder for alt nedenfor, som i den første kravspecifikation: 100 % offline, dansk UI, samme designsprog, alt kan
fortrydes, og ingen nye afhængigheder (**+0 MB**).

---

## Udgangspunkt (hvad programmet kan i dag)

* Teksten er delt op i **bokse** – én pr. afsnit. Musen over teksten viser en ramme, og et klik åbner afsnittet i
  en editor. Et klik mere er nødvendigt for at placere markøren, hvor man vil.
* Markøren kan ikke gå fra ét afsnit til det næste med piletasterne. Man kan ikke markere, kopiere eller formatere
  på tværs af afsnit, og Enter/Backspace kan ikke dele eller samle afsnit.
* Når et afsnit vokser, skubbes teksten nedenunder ned – men kun til sidens bund. Er der ikke plads, får man en
  advarsel og tilbuddet "Formindsk så den passer". Tekst flytter aldrig til næste side.

**Bemærk om Acrobat:** Adobe Acrobat Pro flytter heller ikke selv tekst over på næste side; der vokser
tekstboksen ud over sidens kant. Kravene nedenfor følger derfor **Word** på det punkt, som aftalt (afklaring 1).

---

## A. Klik og markør

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| F1 | SKAL | **Ét klik placerer markøren** præcis ved det tegn, man klikker på – der åbnes ingen boks først. Træk markerer med det samme, dobbeltklik markerer et ord, tredobbeltklik et afsnit, og Shift+klik udvider markeringen. | Ét klik midt i et ord i et afsnit og straks skrive: bogstavet står præcis dér. |
| F2 | SKAL | **Ingen bokse.** Ingen rammer, når musen er over teksten eller under redigering. Over redigerbar tekst er musemarkøren en tekstmarkør (I). "Vis alle tekstbokse" bliver til et valgfrit "Vis tekstflader". | Skærmbilleder af hover og redigering viser ingen rammer om afsnit. |
| F3 | SKAL | **Ren baggrund under redigering.** Mens man redigerer, vises siden bag teksten uden den tekst, der redigeres – billeder, streger og farvede felter bliver stående – så der aldrig skinner gammel tekst igennem, uanset baggrund. | Redigering af tekst oven på et farvet felt og ved siden af et billede: ingen dobbelt tekst, feltet og billedet er urørte. |

## B. Én tekstflade pr. spalte

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| F4 | SKAL | **Hele spalten er én tekst.** Alle afsnit i samme spalte på en side – overskrifter, brødtekst, punktopstillinger og billedtekster – redigeres som ét sammenhængende dokument. Piletaster, Home/End, Ctrl+pil, PgUp/PgDn, markering, kopiér, klip, indsæt og slet virker på tværs af afsnit. | Markér fra midten af ét afsnit til midten af det næste, slet: de to afsnit er samlet til ét med resten af teksten intakt. |
| F5 | SKAL | **Enter og Backspace som i Word.** Enter deler et afsnit; det nye afsnit får samme stil og samme afstand. Shift+Enter giver linjeskift i afsnittet. Backspace i starten (eller Delete i slutningen) samler to afsnit. I en punktopstilling giver Enter et nyt punkt med næste nummer eller samme punkttegn, og Enter i et tomt punkt afslutter listen. | Enter midt i et afsnit giver to afsnit med den originale afstand imellem; Enter efter "3. …" giver "4. ". |
| F6 | SKAL | **Formatering over flere afsnit.** Skrift, størrelse, farve, fed, kursiv, understreget og gennemstreget virker på markeringer over flere afsnit. Justering og linjeafstand gælder alle markerede afsnit. Inspektøren viser blandet format som "–". | Tre afsnit markeres og gøres kursive og centrerede; efter gem og genåbning er de det. |
| F7 | SKAL | **Markøren går videre.** Pil ned eller højre fra slutningen af en tekstflade fortsætter i næste flade i læserækkefølge (næste spalte, derefter næste side). Pil op eller venstre fra starten går tilbage. Tab og Shift+Tab hopper til næste eller forrige flade (fx tabelceller). | Pil ned fra sidste linje i venstre spalte står i første linje i højre spalte; fra sidste linje på siden i første linje på næste side. |
| F8 | SKAL | **Selvstændige flader.** Sidehoveder, sidefødder, tabelceller, tekst i figurer og roteret tekst er hver deres flade. De har samme klik-oplevelse (F1–F3), men flyder ikke sammen med brødteksten. | Et klik i en tabelcelle redigerer kun cellen; brødteksten flytter sig ikke. |

## C. Tekst flyder til næste side

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| F9 | SKAL | **Flyd til næste side.** Når teksten ikke længere kan være i spalten, flyttes de overskydende linjer automatisk til toppen af samme spalte på næste side, og indholdet der skubbes ned – side for side, til der er plads. Det sker, mens man skriver (senest ca. ½ sekund efter, man holder en pause), og markøren følger med, hvis den står i tekst, der flyttes. | Et nyt afsnit på 6 linjer i en fuld spalte: 6 linjer flytter til næste side, der skubbes ned tilsvarende, og ingen tekst går tabt eller overlapper. |
| F10 | SKAL | **Nye sider.** Er der ikke plads på den sidste side i forløbet, indsættes en ny side med samme størrelse lige efter den. | Tilføj tekst nok til at fylde mere end sidste side: der kommer en ny side, og teksten fortsætter dér. |
| F11 | SKAL | **Træk tilbage, når teksten bliver kortere.** Fortsætter et afsnit fra næste side (delt over sideskiftet), trækkes dets linjer op, når der bliver plads. Et nyt afsnit, et kapitel eller anden selvstændig tekst øverst på næste side trækkes ikke op – det behandles som et sideskift. En side, som Folio selv har indsat, fjernes igen, hvis den bliver tom. | Slet en sætning i et afsnit, der fortsætter på næste side: de fortsatte linjer rykker op, og næste side rykker op tilsvarende. |
| F12 | SKAL | **Sideskift respekteres.** Starter næste side med en overskrift (en kapitelstart), eller har den en helt anden opsætning (andre spalter, en helsides tabel eller figur), indsættes i stedet en ny side til den overskydende tekst, så kapitlet ikke skubbes ned. | Overløb på siden før en kapitelstart: der indsættes en ny side imellem, og kapitlets første side er uændret. |
| F13 | BØR | **Sidehoved, sidefod og sidetal.** Nye sider får samme sidehoved og sidefod som siden før. Sidetal i sidefødder ("Side 13 af 58", "13") opdateres på alle efterfølgende sider, når en side indsættes eller fjernes. | Efter en ny side 14 står der "Side 15 af 59" på den tidligere side 14. |
| F14 | BØR | **Horeunge/enke-kontrol (som i Word).** Et afsnit deles ikke, så der står én enkelt linje alene nederst eller øverst på en side. | Et afsnit, der ville efterlade én linje nederst, flyttes, så mindst to linjer står sammen. |
| F15 | SKAL | **Billeder, grafik og tabeller bliver liggende.** Tekst løber uden om dem: et afsnit, der ville ramme et billede, en figur eller en tabel i spalten, fortsætter under det. Teksten lægger sig aldrig oven i dem. | Et afsnit over et billede vokser med 3 linjer: teksten fortsætter under billedet, og billedet er urørt. |

## D. Gem, fortryd og kvalitet

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| F16 | SKAL | **Gemmes automatisk.** Ændringer skrives ind i PDF'en, når man klikker uden for teksten, skifter værktøj eller side, og når teksten flyder (F9). **Esc afslutter redigeringen og beholder ændringerne** (som i Word); fortryd sker med Ctrl+Z. Kun ændrede eller flyttede linjer skrives om – resten af siden er uændret. | Redigér ét ord og tryk Esc: ændringen er der, og resten af siden er pixel for pixel som før. |
| F17 | SKAL | **Ét fortryd-trin.** Et helt redigeringsforløb – også med automatisk flyd over flere sider og nye sider – er ét fortryd-trin i dokumentet. Ctrl+Z inde i editoren fortryder det skrevne. | Skriv et afsnit, der flyder over 3 sider og giver en ny side; ét Ctrl+Z efter redigeringen gendanner dokumentet præcist. |
| F18 | SKAL | **Samme kvalitet som i dag.** Samme skrifttype, størrelse, farve og placering. Lige margener, lister og hængende indrykning bevares. Teksten kan søges i og kopieres bagefter, også i andre PDF-læsere, og filen vokser ikke unødigt. | Stresstesten (F19) finder ingen tabt tekst, ingen overlap og ingen forkerte mellemrum; filen er gyldig. |
| F19 | SKAL | **Ydelse.** Klik til markør: under 150 ms på en tæt side (58-siders rapport). Automatisk flyd: under ½ sekund pr. berørt side. Det fryser ikke, mens man skriver. | Målt i tests på årsrapporten (58 sider). |

## Bevidste begrænsninger

* En markering kan ikke strække sig over flere spalter eller sider. Til at kopiere på tværs bruges *Markér tekst*.
* Billeder, figurer og tabeller flytter ikke med teksten (F15). Det kan tilføjes senere for almindelige billeder.
* Tekst flyder ikke fra én spalte til den næste på samme side (afklaring 2); den flyder til samme spalte på næste
  side.
* Indholdsfortegnelser, krydshenvisninger og links i dokumentet opdateres ikke, når tekst flytter side.

---

## E. Arkitektur (hvordan det passer ind)

* **Ingen nye afhængigheder.** Ingen ny tekstmotor: skrifttyper, formatering, ombrydning og skrivning bygger på det
  eksisterende `core/text_edit.py`.
* **Kerne (`pdfstudio/core`, uden Qt, fuldt unit-testet):**
  * `core/flow.py` (ny): tekstflader (spalte, rammer, afsnit), genkendelse af spalter, sidehoveder/-fødder,
    tabeller og sideskift, opsætning linje for linje i rammer på tværs af sider og uden om billeder (F9–F15), og
    `apply_story(pdf, story, paragraphs)`, som skriver det hele i ét trin.
  * `core/render.py`: gengivelse af en side uden bestemte tekstlinjer (baggrunden i F3).
  * `core/document.py`/`history.py`: et fortryd-trin, der kan forlænges (F17).
  * `core/pages.py`: nye sider og opdatering af sidetal (F10, F13).
* **UI:** `tools/edit_text.py` omskrives til én editor pr. tekstflade (ét Qt-tekstafsnit pr. PDF-afsnit).
  Den gamle "én boks pr. afsnit"-kode **fjernes**, så der ikke ligger to udgaver. *Tilføj tekst* bruger den samme
  editor.
* `CLAUDE.md` opdateres med de nye API'er.

## F. Test og kvalitetssikring

1. **Kerne:** tests for hvert ID på genererede dokumenter med én og to spalter, lister, tabeller, billeder,
   sidehoveder/-fødder med sidetal og roterede sider.
2. **Rigtige dokumenter:** en ny stresstest indsætter tekst midt i hvert kapitel i årsrapporten, så det flyder over
   flere sider. Den kontrollerer, at brødteksten står i samme rækkefølge, at intet overlapper, at sidetallene
   passer, at ét fortryd gendanner originalen, og at filen er gyldig.
3. **UI:** offscreen-tests af hver arbejdsgang (klik → skriv → Enter → flyd → fortryd) plus skærmbilleder i lyst og
   mørkt tema, som jeg selv gennemser.
4. **Ydelse:** måltiderne i F19 måles i tests.
5. **Windows:** hele testsuiten og installationsfilen bygges og røgtestes i GitHub Actions.

Fejler et krav, rettes det og testes igen, før næste trin går i gang.

**Rækkefølge:**
1. Kernen: tekstflader, opsætning og skrivning, inkl. flyd til næste side (F4, F5, F9–F12, F15, F17, F18).
2. Editoren (F1–F8, F16).
3. Flyd under skrivning, sidehoved/sidefod og horeunger (F9, F13, F14).
4. Stresstest, ydelse, Windows og dokumentation (F18, F19).

Jeg pusher løbende og melder status efter hvert trin.

## G. Afklaringer (besluttet 2026-11-03)

1. Tekst flyder til næste side, når den ikke kan være (F9–F12): **ja**.
2. Spalter: **hver spalte er sin egen tekstflade**, og markøren bevæger sig mellem dem (F4, F7). Kan rettes
   senere, hvis det viser sig besværligt i brug.
