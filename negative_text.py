"""Negative (etched-out) and positive copper text for KiCad 10.

Negative mode: each glyph becomes a zone rule area that forbids copper pour.
The pour then floods solid and the letters read as bare substrate -- the shape
you want when etching a board at home, where copper letters would otherwise
cost clearance and can pinch the pour.

Positive mode: each glyph becomes a small filled zone with no net, so the
letters are solid copper. Useful on boards with no ground plane, where there
is nothing to knock the text out of; the stroke font comes out as ordinary
single-line lettering.

Both modes can add a rectangular border frame around the label, with the
frame width and the gap to the text configurable. The frame is built from
the same zone type as the glyphs, so it is etched in negative mode and
copper in positive mode.

Why rule areas and not the built-in knockout attribute: knockout draws a
filled rectangle with the glyphs cut out, so the rectangle itself is copper
and collides with everything around it.

Why one zone per glyph: a KiCad zone persists exactly one outline. Handing a
zone a multi-glyph polygon set looks right on screen -- the pour's cached fill
still shows every letter -- but only the first outline reaches the file, and
the next refill fills the rest back in.

Why the batch functions capture everything up front: BOARD.Remove() poisons
the Python-side SWIG proxies of unrelated objects still held here. After one
removal, text.m_Uuid and board.Groups() hand back untyped SwigPyObjects with
no usable attributes. So every lookup happens before the first removal,
mutation runs as remove-all then build-all, and nothing is looked up between.
Scans that may run after a removal (the dialog refreshes its lists in place)
guard every attribute access and skip poisoned proxies.
"""

import re

import pcbnew

MM = 1_000_000
ARC_ERROR = 5000            # chord error when flattening glyph curves, in IU

MODE_NEGATIVE = 'negative'
MODE_POSITIVE = 'positive'

GROUP_PREFIX = 'NEGTEXT'
ZONE_PREFIX = 'NEGTEXT_'
POS_GROUP_PREFIX = 'POSTEXT'
POS_ZONE_PREFIX = 'POSTEXT_'

_ZONE_PREFIXES = ((ZONE_PREFIX, MODE_NEGATIVE), (POS_ZONE_PREFIX, MODE_POSITIVE))
_GROUP_PREFIXES = (GROUP_PREFIX, POS_GROUP_PREFIX)

# Copper side -> layer the readable source text is parked on.
FAB_FOR = {'F.Cu': 'F.Fab', 'B.Cu': 'B.Fab'}

# Origin-layer token written into group names: NEGTEXT_<key>_L<id>_<label>.
_ORIGIN_RE = re.compile(r'^L(\d+)$')


# --------------------------------------------------------------------------
# lookups -- these must all run before anything is removed from the board
# --------------------------------------------------------------------------

def copper_layers(board):
    """Enabled copper layers, as (layer id, name) in stack order."""
    return [(lid, pcbnew.LayerName(lid))
            for lid in board.GetEnabledLayers().CuStack()]


def text_key(text):
    """Stable per-text id tying glyph zones back to their source text.

    Group membership cannot do this job: footprint-owned text is accepted
    into a board group in memory but is not written as a member, so after a
    save/reload the link is gone and a re-run would duplicate every glyph
    instead of rebuilding it.
    """
    return text.m_Uuid.AsString()


def capture_keys(texts):
    """Keys for a batch, taken before anything is removed."""
    return [text_key(t) for t in texts]


def _key_of(text_or_key):
    if isinstance(text_or_key, str):
        return text_or_key
    return text_key(text_or_key)


def board_texts(board):
    """Every text item on the board: board text, footprint text and fields."""
    out = []
    for item in board.GetDrawings():
        if isinstance(item, pcbnew.PCB_TEXT):
            out.append(item)
    for footprint in board.GetFootprints():
        for item in footprint.GraphicalItems():
            if isinstance(item, pcbnew.PCB_TEXT):
                out.append(item)
        for field in (footprint.Reference(), footprint.Value()):
            out.append(field)
    return out


def texts_on_layer(board, layer_id):
    """Text items sitting on one layer, footprint-owned ones included."""
    out = []
    for text in board_texts(board):
        try:
            if text.GetLayer() == layer_id:
                out.append(text)
        except AttributeError:
            continue
    return out


def text_layers(board):
    """Layers that hold at least one text item, as (layer id, name)."""
    seen = set()
    for text in board_texts(board):
        try:
            seen.add(text.GetLayer())
        except AttributeError:
            continue
    return [(lid, pcbnew.LayerName(lid)) for lid in sorted(seen)]


def find_text_by_key(board, key):
    """Fresh proxy for the text with this UUID, or None if it is gone."""
    for text in board_texts(board):
        try:
            if text.m_Uuid.AsString() == key:
                return text
        except AttributeError:
            continue
    return None


def generated_for(board, text_or_key):
    """Zones produced by an earlier run for this text, either mode."""
    key = _key_of(text_or_key)
    tags = tuple(f'{p}{key}_' for p, _ in _ZONE_PREFIXES)
    out = []
    for zone in board.Zones():
        try:
            if zone.GetZoneName().startswith(tags):
                out.append(zone)
        except AttributeError:
            continue
    return out


def is_converted(board, text):
    return bool(generated_for(board, text))


def owning_group(board, text_or_key):
    """The NEGTEXT/POSTEXT group for this text, looked up from the board.

    Deliberately not zone.GetParentGroup(): that returns an untyped proxy
    after a save/reload, which BOARD.Remove() rejects.
    """
    key = _key_of(text_or_key)
    tags = tuple(f'{p}_{key}_' for p in _GROUP_PREFIXES)
    for group in board.Groups():
        try:
            name = group.GetName()
        except AttributeError:
            continue
        if name.startswith(tags):
            return group
    return None


def all_generated(board):
    """Every generated label on the board, one entry per source text.

    Returns dicts: key, mode, layer, label, areas. Everything is read from
    the zone names, so this works after a save/reload with no group help.
    """
    items = {}
    for zone in board.Zones():
        try:
            name = zone.GetZoneName()
            layer = zone.GetLayer()
        except AttributeError:
            continue
        for prefix, mode in _ZONE_PREFIXES:
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix):]
            key, _, tail = rest.partition('_')
            _, _, label = tail.partition('_')
            entry = items.setdefault(key, {
                'key': key, 'mode': mode, 'layer': layer,
                'label': label, 'areas': 0})
            entry['areas'] += 1
            break
    return sorted(items.values(), key=lambda e: e['label'])


def has_pour(board, layer_id):
    """True if the layer has a real pour (generated glyph zones excluded)."""
    for zone in board.Zones():
        try:
            if (not zone.GetIsRuleArea() and zone.IsOnLayer(layer_id)
                    and not zone.GetZoneName().startswith(POS_ZONE_PREFIX)):
                return True
        except AttributeError:
            continue
    return False


def selected_texts(board):
    """Text items in the current selection, footprint-owned ones included."""
    out = []
    try:
        selection = pcbnew.GetCurrentSelection()
    except Exception:
        selection = []
    for item in selection:
        if isinstance(item, pcbnew.PCB_TEXT):
            out.append(item)
        elif isinstance(item, pcbnew.PCB_GROUP):
            for member in item.GetItems():
                if isinstance(member, pcbnew.PCB_TEXT):
                    out.append(member)
    return out


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------

def glyph_outlines(text):
    """Polygon set of the rendered glyphs, in board coordinates."""
    polys = pcbnew.SHAPE_POLY_SET()
    text.TransformTextToPolySet(polys, 0, ARC_ERROR, pcbnew.ERROR_INSIDE)
    return polys


def preview_outlines(text, mirrored, height_mm):
    """Glyph polygons as they would build, leaving the text untouched.

    Applies the height and mirror adjustments temporarily, renders, then
    restores the original values -- pure geometry, nothing added to the
    board and nothing left changed.
    """
    old_size = text.GetTextSize()
    old_w, old_h = old_size.x, old_size.y
    old_mirror = text.IsMirrored()
    try:
        if height_mm:
            size = int(height_mm * MM)
            text.SetTextSize(pcbnew.VECTOR2I(size, size))
        text.SetMirrored(mirrored)
        return glyph_outlines(text)
    finally:
        text.SetTextSize(pcbnew.VECTOR2I(old_w, old_h))
        text.SetMirrored(old_mirror)


def poly_points(polys):
    """SHAPE_POLY_SET as plain point lists: [(outline, [holes...]), ...]."""
    def points(chain):
        return [(chain.CPoint(j).x, chain.CPoint(j).y)
                for j in range(chain.PointCount())]

    return [(points(polys.Outline(i)),
             [points(polys.Hole(i, h)) for h in range(polys.HoleCount(i))])
            for i in range(polys.OutlineCount())]


def _rect_chain(bbox, grow):
    """Closed rectangle around a bounding box, grown outward by `grow` IU."""
    chain = pcbnew.SHAPE_LINE_CHAIN()
    x1, y1 = bbox.GetLeft() - grow, bbox.GetTop() - grow
    x2, y2 = bbox.GetRight() + grow, bbox.GetBottom() + grow
    for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        chain.Append(int(x), int(y))
    chain.SetClosed(True)
    return chain


def make_glyph_zone(board, layer, outline, holes, key, index, label,
                    mode=MODE_NEGATIVE):
    """One zone holding a single glyph (or border), counters kept as holes.

    Negative mode: a rule area that forbids copper fill.
    Positive mode: a filled zone with no net -- the glyph IS the copper.
    Island removal is set to Never on the zone itself, since a floating
    letter is exactly the island the pour logic wants to delete.
    """
    sub = pcbnew.SHAPE_POLY_SET()
    sub.AddOutline(outline)
    for hole in holes:
        sub.AddHole(hole, 0)

    zone = pcbnew.ZONE(board)
    if mode == MODE_NEGATIVE:
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowZoneFills(True)
        zone.SetDoNotAllowTracks(False)
        zone.SetDoNotAllowVias(False)
        zone.SetDoNotAllowPads(False)
        zone.SetDoNotAllowFootprints(False)
        prefix = ZONE_PREFIX
    else:
        zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
        # Default 0.25 mm min thickness erodes thin strokes to blobs.
        zone.SetMinThickness(int(0.02 * MM))
        prefix = POS_ZONE_PREFIX
    zone.SetLayer(layer)
    # The key must stay immediately after the prefix: lookup matches on it.
    zone.SetZoneName(f'{prefix}{key}_{index}_{label}')

    # SetOutline takes ownership. Without releasing the SWIG proxy the
    # polygon set is freed twice and the next zone fill segfaults.
    sub.thisown = 0
    zone.SetOutline(sub)
    zone.thisown = 0
    board.Add(zone)
    return zone


def _build(board, text, key, layer_id, mirrored, height_mm,
           mode=MODE_NEGATIVE, border=None):
    """Create glyphs and group for one text. Assumes old ones are gone.

    `border` is None or {'width': mm, 'gap': mm} for a rectangular frame.
    """
    if height_mm:
        size = int(height_mm * MM)
        text.SetTextSize(pcbnew.VECTOR2I(size, size))
    text.SetMirrored(mirrored)

    origin_layer = text.GetLayer()

    polys = glyph_outlines(text)
    if polys.OutlineCount() == 0:
        return 0

    label = text.GetShownText(True, 0) or 'text'
    safe = ''.join(c if c.isalnum() else '_' for c in label)[:24]

    zones = []
    for i in range(polys.OutlineCount()):
        holes = [polys.Hole(i, h) for h in range(polys.HoleCount(i))]
        zones.append(make_glyph_zone(board, layer_id, polys.Outline(i),
                                     holes, key, i, safe, mode))

    if border:
        gap = int(border['gap'] * MM)
        width = int(border['width'] * MM)
        bbox = polys.BBox()
        outer = _rect_chain(bbox, gap + width)
        inner = _rect_chain(bbox, gap)
        zones.append(make_glyph_zone(board, layer_id, outer, [inner],
                                     key, 'border', safe, mode))

    # The source text must stop being copper: in negative mode the glyphs
    # are the void, in positive mode the zones are the copper and the text
    # would double it. Text already parked off-copper (an F.Fab source)
    # stays where it is.
    if pcbnew.IsCopperLayer(origin_layer):
        layer_name = pcbnew.LayerName(layer_id)
        text.SetLayer(board.GetLayerID(FAB_FOR.get(layer_name, 'F.Fab')))

    group = pcbnew.PCB_GROUP(board)
    group_prefix = GROUP_PREFIX if mode == MODE_NEGATIVE else POS_GROUP_PREFIX
    # L<id> records the origin layer so revert can put the text back.
    group.SetName(f'{group_prefix}_{key}_L{origin_layer}_{safe}')
    group.thisown = 0
    board.Add(group)
    # Footprint-owned text is accepted into a board group but never written
    # as a member, so it is left out rather than silently half-grouped.
    try:
        if text.IsGroupableType() and text.GetParentFootprint() is None:
            group.AddItem(text)
    except Exception:
        pass
    for zone in zones:
        group.AddItem(zone)

    return len(zones)


def _purge(board, stale):
    """Remove previously generated zones and groups. Returns zones removed."""
    removed = 0
    for zones, group in stale:
        for zone in zones:
            board.Remove(zone)
            removed += 1
        if group is not None:
            board.Remove(group)
    return removed


def _origin_layer(group):
    """Origin layer recorded in a group name, or None for legacy groups."""
    if group is None:
        return None
    try:
        parts = group.GetName().split('_')
    except AttributeError:
        return None
    if len(parts) >= 3:
        match = _ORIGIN_RE.match(parts[2])
        if match:
            return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# public operations
# --------------------------------------------------------------------------

def convert_batch(board, texts, layer_id, mirrored, height_mm=None,
                  mode=MODE_NEGATIVE, border=None):
    """Convert texts to negative or positive copper. Returns area count."""
    keys = capture_keys(texts)
    stale = [(generated_for(board, k), owning_group(board, k)) for k in keys]

    _purge(board, stale)

    return sum(_build(board, text, key, layer_id, mirrored, height_mm,
                      mode, border)
               for text, key in zip(texts, keys))


def revert_batch(board, texts_or_keys):
    """Remove generated areas and put source texts back where they were.

    Accepts text objects or bare keys (the dialog's item list only has
    keys). Texts whose group recorded an origin layer go back to it; legacy
    groups fall back to the fab -> copper mapping.
    """
    keys = [_key_of(t) for t in texts_or_keys]
    stale = [(generated_for(board, k), owning_group(board, k)) for k in keys]

    # Resolve destination layers before mutating; after the first Remove()
    # every held proxy is suspect.
    restore = []
    for key, (_zones, group) in zip(keys, stale):
        text = find_text_by_key(board, key)
        if text is None:
            continue
        target = _origin_layer(group)
        if target is None:
            for copper, fab in FAB_FOR.items():
                if text.GetLayerName() == fab:
                    target = board.GetLayerID(copper)
        if target is not None:
            restore.append((text, target))

    removed = _purge(board, stale)

    for text, layer in restore:
        text.SetLayer(layer)
    return removed


def convert(board, text, layer_id, mirrored, height_mm=None,
            mode=MODE_NEGATIVE, border=None):
    """Single-item convenience wrapper."""
    return convert_batch(board, [text], layer_id, mirrored, height_mm,
                         mode, border)


def revert(board, text):
    """Single-item convenience wrapper."""
    return revert_batch(board, [text])


def keep_counters(board, layer_id):
    """Stop the pour discarding the isolated islands inside O, B, D, R.

    Those counters are copper unconnected to anything, so the default island
    removal deletes them and the letters fill in solid.
    """
    changed = 0
    for zone in board.Zones():
        try:
            if not zone.GetIsRuleArea() and zone.IsOnLayer(layer_id):
                zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
                changed += 1
        except AttributeError:
            continue
    return changed
