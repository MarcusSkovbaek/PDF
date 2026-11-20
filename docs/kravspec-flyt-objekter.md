# Kravspecifikation – Flyt tekstblokke og grafik (Markér objekt)

Version 1.0 · 2026-11-20 · Folio PDF 1.2 → 1.3 · Status: **til godkendelse**

Prioritet: **SKAL** = indgår og testes. **BØR** = indgår, medmindre det strider mod en SKAL, og det meldes.
**KAN** = kun hvis der er tid. Alle krav har et ID, så du kan godkende, rette eller stryge enkeltpunkter
("stryg O9", "O4: ingen fastgørelse").

Gælder som altid: 100 % offline, dansk UI, samme designsprog, alt kan fortrydes, ingen nye afhængigheder (**+0 MB**).

---

## Udgangspunkt

* *Markér objekt* (`O`) flytter, skalerer, roterer, spejlvender, erstatter og sletter **billeder**.
* Kommentarer (tekstfelter, figurer, noter, frihånd) flyttes med *Markér kommentar*.
* **Tekst og vektorgrafik, der er en del af siden, kan ikke flyttes** – kun redigeres (tekst).

## A. Hvad kan markeres

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| O1 | SKAL | **Tekstblokke.** *Markér objekt* kan også vælge tekst: et klik på en tekstflade vælger den som én blok – et afsnit, en overskrift, et sidehoved, en tabelcelle, en billedtekst eller tekst tilføjet med *Tilføj tekst*. Musen over viser omridset, som for billeder. | Klik på en overskrift: den får ramme og håndtag; klik på et afsnit i en spalte: kun det afsnit vælges. |
| O2 | SKAL | **Grafik.** Vektorgrafik i siden kan vælges: streger, rammer, kasser, cirkler, pile og figurer. Elementer, der hører sammen (en tegnet figur af flere streger, en farvet kasse med kant), vælges som én gruppe. | Klik på en farvet boks: boksen vælges med sin kant; klik på en vandret streg: stregen vælges. |
| O3 | SKAL | **Flere ad gangen.** Shift/Ctrl+klik tilføjer og fjerner; træk en ramme på tom plads vælger alt indenfor (billeder, tekst og grafik blandet). | Vælg en boks og teksten i den: de flyttes sammen. |
| O4 | BØR | **Tekst i en kasse følger med.** Vælges en kasse/ramme, der omslutter tekst, tilbydes "Medtag indhold" (til som standard), så kasse og tekst flyttes sammen. | Flyt en faktaboks: tekst og baggrund flytter sammen. |
| O5 | SKAL | **Uden for rækkevidde.** Store baggrundsflader (helsides farver, vandmærker, sidens kant), sidehoveder gentaget fra en skabelon og tabellinjer inde i en tabel vælges ikke ved et almindeligt klik (Alt+klik vælger dem alligevel). | Et klik midt i brødteksten vælger aldrig sidens baggrund. |

## B. Hvad man kan gøre

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| O6 | SKAL | **Flyt.** Træk med musen (Shift = kun vandret/lodret), piletaster 1 pt (Shift 10 pt), eller X/Y i inspektøren (mm). Under træk vises et gennemsigtigt billede af det valgte, og stedet bagved vises uden det. | Træk en tekstblok 3 cm ned: teksten står det nye sted, samme skrift, størrelse og farve, og kan stadig søges og redigeres. |
| O7 | SKAL | **Hjælpelinjer.** Det, der flyttes, "klikker" på plads ud for andre objekters kanter og midter og sidens midte/margener (tynde accentlinjer vises). Alt slår det fra. | En kasse kan placeres præcis på linje med en anden kasses venstrekant. |
| O8 | SKAL | **Skalér grafik.** Grafik kan skaleres med hjørnehåndtagene (Shift = frit, ellers samme forhold), stregtykkelsen bevares. | En ramme gøres bredere: stregen er lige tyk som før. |
| O9 | BØR | **Tekstblokkens bredde.** Træk i en tekstbloks side-håndtag ændrer dens bredde; teksten ombrydes igen (som *Redigér tekst*). Skalering af tekstens størrelse sker i *Redigér tekst*, ikke her. | Gør et afsnit smallere: det får flere linjer med samme skrift. |
| O10 | SKAL | **Slet, kopiér, dupliker.** Delete sletter; Ctrl+C/Ctrl+V kopierer og indsætter (også til en anden side eller et andet dokument i samme program); Ctrl+D dublerer lidt forskudt. | Kopiér en kasse med tekst til side 5: den står samme sted på side 5. |
| O11 | BØR | **Grafikkens udseende.** For valgt grafik viser inspektøren stregfarve, fyldfarve og stregtykkelse, som kan ændres. | Gør en rød streg blå og 2 pt tyk. |
| O12 | BØR | **Rækkefølge.** "Læg forrest / bagerst" for det valgte (fx en kasse, der skal ligge bag tekst). | En kasse, der dækker tekst, lægges bagerst: teksten ses igen. |

## C. Kvalitet

| ID | Prio | Krav | Accepteres når |
|----|------|------|----------------|
| O13 | SKAL | **Kun det valgte ændres.** Alt andet på siden er uændret pixel for pixel; flyttet tekst skrives med sine egne skrifter (glyf for glyf, som når *Redigér tekst* flytter linjer) og kan søges og kopieres i andre PDF-læsere. Grafik flyttes uden at blive tegnet om (samme kurver, farver og gennemsigtighed). | Flyt en tekstblok og tilbage igen: siden ser ud som før. |
| O14 | SKAL | **Ét fortryd-trin** pr. træk/handling; en serie piletast-ryk er ét trin. | Ctrl+Z efter et træk gendanner præcist. |
| O15 | SKAL | **Roterede sider** og roteret tekst virker. | Testes på sider med /Rotate 90/180/270. |
| O16 | SKAL | **Ydelse.** Musen over/klik reagerer straks (< 100 ms på en tæt side); slip efter træk < 0,5 s. | Målt på årsrapporten (58 sider). |
| O17 | SKAL | **Flyttet tekst i flydende tekst.** En flyttet tekstblok bliver en selvstændig tekstflade (den flyder ikke længere med spalten); teksten, der var under den, rykker *ikke* op af sig selv. | Flyt et afsnit ud af spalten: de andre afsnit bliver stående. |

## Bevidste begrænsninger

* Grafik **roteres** ikke i første omgang (billeder kan stadig).
* Tekst i **formularfelter**, scannede sider uden tekst og tekst tegnet som kurver kan ikke vælges som tekst
  (scannet tekst og kurver kan vælges som grafik/billede).
* Tabeller flyttes ikke som én helhed; deres celler/linjer kan vælges enkeltvis eller samlet med en ramme.

## D. Teknisk plan (kort)

* **Kerne:** `core/objects.py` udvides fra billeder til tre slags objekter: billede, tekstblok, grafik.
  Grafik findes med den eksisterende indholdsstrøm-tolker (stier med deres `q…Q`/`cm`), flyttes ved at indsætte
  én `cm` om stiens blok – som billeder i dag, så strømmen ikke vokser. Tekstblokke flyttes med den
  eksisterende "flyt glyf for glyf" (`text_edit._moved_runs` + fjern de gamle linjer).
* **UI:** `tools/edit_object.py` får tekst og grafik med samme håndtag, træk-forhåndsvisning, inspektør og
  genveje; hjælpelinjer som overlay.
* Tests: kerne (alle slags objekter, roterede sider, fortryd, "kun det valgte ændres" som pixel-sammenligning),
  UI (vælg, træk, piletaster, ramme-markering, kopiér/indsæt), stresstest på årsrapporten (flyt hver blok og
  tilbage).

## E. Rækkefølge

1. Kerne: grafik og tekstblokke – find, flyt, slet, kopiér (O1, O2, O5, O6, O10, O13–O17).
2. UI: markering, træk, flere ad gangen, hjælpelinjer (O3, O6, O7, O10).
3. Skalering, bredde, udseende, rækkefølge, kasse med indhold (O4, O8, O9, O11, O12).
4. Stresstest, Windows-byg, dokumentation.
