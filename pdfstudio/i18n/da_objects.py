"""Danish strings for "Markér objekt" (images in the page content):
``pdfstudio.core.objects`` undo labels and ``ui/viewer/tools/edit_object.py``."""

STRINGS: dict[str, str] = {
    # -- tool -------------------------------------------------------------------
    "objects.tool.label": "Markér objekt",
    "objects.tool.hint": "Klik på et billede for at markere det · træk for at flytte · "
                         "træk i et hjørne for at skalere · Delete sletter",
    # -- undo labels (core) -----------------------------------------------------
    "objects.undo.move": "Flyt billede",
    "objects.undo.resize": "Skalér billede",
    "objects.undo.rotate": "Rotér billede",
    "objects.undo.flip": "Spejlvend billede",
    "objects.undo.delete": "Slet billede",
    "objects.undo.replace": "Erstat billede",
    # -- inspector: nothing selected -------------------------------------------
    "objects.none.title": "Intet billede markeret",
    "objects.none.hint": "Klik på et billede på siden for at flytte, skalere, erstatte eller slette det – "
                         "både billeder, du selv har tilføjet, og billeder, der allerede er i PDF'en.",
    "objects.count.zero": "Der er ingen billeder på side {page}.",
    "objects.count.one": "Der er 1 billede på side {page}.",
    "objects.count.many": "Der er {n} billeder på side {page}.",
    # -- inspector: selected image -----------------------------------------------
    "objects.title": "Billede",
    "objects.title.n": "Billede {i} af {n}",
    "objects.pixels": "{w} × {h} px",
    "objects.format.jpeg": "JPEG",
    "objects.format.jpx": "JPEG 2000",
    "objects.format.jbig2": "JBIG2",
    "objects.format.ccitt": "CCITT-fax",
    "objects.format.lossless": "tabsfrit",
    "objects.alpha": "gennemsigtigt",
    "objects.resolution": "{ppi} ppi på siden",
    "objects.lowres": "{ppi} ppi på siden – lav opløsning til tryk",
    "objects.rotated": "Roteret {deg}°",
    "objects.flipped": "spejlvendt",
    "objects.inline": "Indlejret direkte i sidens indhold.",
    "objects.in_form": "Del af en skabelon – ændringer gælder kun denne side.",
    "objects.section.position": "Placering og størrelse",
    "objects.section.image": "Billede",
    "objects.section.arrange": "Arrangér",
    "objects.field.x": "X",
    "objects.field.y": "Y",
    "objects.field.w": "B",
    "objects.field.h": "H",
    "objects.field.x.tip": "Afstand fra sidens venstre kant",
    "objects.field.y.tip": "Afstand fra sidens øverste kant",
    "objects.field.w.tip": "Bredde på siden",
    "objects.field.h.tip": "Højde på siden",
    "objects.lock.tip": "Bevar proportioner",
    "objects.unit.mm": " mm",
    "objects.action.replace": "Erstat billede…",
    "objects.action.replace.tip": "Vælg et nyt billede – det placeres i samme ramme med bevarede proportioner",
    "objects.action.save": "Gem billede som…",
    "objects.action.save.tip": "Gem billedet som fil i den oprindelige kvalitet",
    "objects.action.rotate": "Rotér 90°",
    "objects.action.flip": "Spejlvend",
    "objects.action.delete": "Slet billede",
    "objects.tips": "Træk for at flytte (Shift: lige linje). Hjørnerne skalerer med bevarede proportioner "
                    "(Shift: frit, Alt: fra midten). Piletaster flytter 1 pt (Shift: 10 pt), "
                    "Tab vælger næste billede, Esc fjerner markeringen.",
    # -- dialogs / feedback --------------------------------------------------------
    "objects.dialog.replace": "Vælg nyt billede",
    "objects.dialog.save": "Gem billede som",
    "objects.filter.images": "Billeder ({patterns})",
    "objects.filter.ext": "{kind}-billede (*.{ext})",
    "objects.default_name": "billede",
    "objects.saved": "Billedet er gemt som {name}",
    "objects.replaced": "Billedet er erstattet",
    "objects.deleted": "Billedet er slettet",
    "objects.undo_action": "Fortryd",
    "objects.error.save": "Billedet kunne ikke gemmes: {err}",
    "objects.error.replace": "Billedet kunne ikke erstattes: {err}",
    "objects.error.edit": "Billedet kunne ikke ændres: {err}",
    "objects.error.unreadable": "{name} kunne ikke læses som billede",
    # -- size badge while dragging ---------------------------------------------------
    "objects.badge.size": "{w} × {h} mm",
    "objects.badge.pos": "X {x}   Y {y} mm",
}
