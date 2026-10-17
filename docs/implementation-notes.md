# Implementation notes

pcbnew API behaviour worth knowing before editing this plugin. Each one cost
real debugging time.

- A KiCad zone saves exactly **one** outline. If you give a zone a multi-glyph
  polygon set it looks correct on screen, because the pour's cached fill still
  shows every letter. But only the first outline reaches the file, and the next
  refill fills the rest back in. Hence one zone per glyph.
- `SetOutline` takes ownership of the polygon set. Without releasing the SWIG
  proxy (`thisown = 0`), it is freed twice and the next fill segfaults.
- Glyph zones are tied to their text by the text's UUID, which is embedded in
  the zone name (`NEGTEXT_<uuid>_<n>_<label>` / `POSTEXT_...`; the border area
  uses `border` in place of `<n>`). Group membership cannot do this job, because
  of the footprint-text limitation in [usage.md](usage.md#moving-labels-afterwards).
  Also, after a reload `zone.GetParentGroup()` returns an untyped proxy that
  `BOARD.Remove()` rejects, so groups are looked up from the board instead.
- `BOARD.Remove()` corrupts any Python-side proxies of unrelated objects the
  script still holds. Batch operations therefore look everything up before the
  first removal. The dialog's rescans after each operation guard every attribute
  access and skip anything already corrupted. The dialog also stores only text
  UUIDs, never text objects, and fetches each text again by UUID when it uses it.
- Positive glyphs are no-net filled zones with island removal set to Never on
  the zone itself, and a 0.02 mm minimum thickness: the 0.25 mm default erodes
  thin strokes.
