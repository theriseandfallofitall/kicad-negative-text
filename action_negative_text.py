"""Toolbar action and dialog for negative / positive copper text."""

import os

import pcbnew
import wx

from .negative_text import (GROUP_PREFIX, MM, MODE_NEGATIVE, MODE_POSITIVE,
                            POS_GROUP_PREFIX, POS_ZONE_PREFIX, ZONE_PREFIX,
                            all_generated, convert_batch, copper_layers,
                            find_text_by_key, generated_for, has_pour,
                            keep_counters, poly_points, preview_outlines,
                            revert_batch, selected_texts, text_key,
                            text_layers, texts_on_layer)

# Modeless singleton: the dialog stays open beside the editor so board
# clicks can sync back into it. Module-level ref keeps it from being GC'd.
_dialog = None


def _shown(text, limit=40):
    label = (text.GetShownText(True, 0) or '(empty)').replace('\n', ' ')
    return label if len(label) <= limit else label[:limit - 1] + '…'


def _fill_zones(board, parent):
    """Refill all zones with KiCad's own progress dialog when available.

    The fill is one blocking C++ call; WX_PROGRESS_REPORTER is how KiCad
    itself keeps the UI alive during it. Constructor signature varies
    across versions, so both known forms are tried; with no reporter the
    fill still runs, just without progress.
    """
    filler = pcbnew.ZONE_FILLER(board)
    reporter = None
    if hasattr(pcbnew, 'WX_PROGRESS_REPORTER'):
        for args in ((parent, 'Refilling zones', 1, False),
                     (parent, 'Refilling zones', 1)):
            try:
                reporter = pcbnew.WX_PROGRESS_REPORTER(*args)
                break
            except Exception:
                reporter = None
        if reporter is not None:
            try:
                filler.SetProgressReporter(reporter)
            except Exception:
                reporter = None
    try:
        filler.Fill(board.Zones())
    finally:
        # Reporter must outlive Fill; dropping it closes its window.
        del filler
        del reporter


class PreviewPanel(wx.Panel):
    """Renders one label's glyph polygons the way they would etch.

    Negative mode paints a copper field with substrate-coloured letters,
    positive mode the inverse. Pure drawing: the data dict comes from the
    dialog and holds plain point lists, no pcbnew objects.
    """

    COPPER = wx.Colour(184, 115, 51)
    BOARD = wx.Colour(222, 201, 158)
    EMPTY = wx.Colour(60, 60, 60)

    def __init__(self, parent):
        super().__init__(parent, size=wx.Size(340, 240))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.data = None
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, event):
        self.Refresh()
        event.Skip()

    def show(self, data):
        self.data = data
        self.Refresh()

    def on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        data = self.data

        if not data or not data['glyphs']:
            dc.SetBackground(wx.Brush(self.EMPTY))
            dc.Clear()
            dc.SetTextForeground(wx.Colour(160, 160, 160))
            message = 'Select a label to preview'
            tw, th = dc.GetTextExtent(message)
            dc.DrawText(message, (w - tw) // 2, (h - th) // 2)
            return

        negative = data['mode'] == MODE_NEGATIVE
        field, letters = ((self.COPPER, self.BOARD) if negative
                          else (self.BOARD, self.COPPER))
        dc.SetBackground(wx.Brush(field))
        dc.Clear()

        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return

        x1, y1, x2, y2 = data['bbox']
        margin = 14
        span_x = max(x2 - x1, 1)
        span_y = max(y2 - y1, 1)
        scale = min((w - 2 * margin) / span_x, (h - 2 * margin) / span_y)
        ox = (w - span_x * scale) / 2 - x1 * scale
        oy = (h - span_y * scale) / 2 - y1 * scale

        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(letters))
        for outline, holes in data['glyphs'] + data['border']:
            path = gc.CreatePath()
            for ring in [outline] + holes:
                if len(ring) < 3:
                    continue
                path.MoveToPoint(ox + ring[0][0] * scale,
                                 oy + ring[0][1] * scale)
                for x, y in ring[1:]:
                    path.AddLineToPoint(ox + x * scale, oy + y * scale)
                path.CloseSubpath()
            gc.FillPath(path, wx.ODDEVEN_RULE)


class CopperTextDialog(wx.Dialog):
    """One dialog for everything: manage existing labels, convert new ones.

    The dialog stays open across operations so several labels can be
    converted, rebuilt or removed in one sitting; every operation refreshes
    the lists from the board afterwards. Resizable: the lists and the
    preview grow with the window.
    """

    MODES = [MODE_NEGATIVE, MODE_POSITIVE]

    def __init__(self, parent, board):
        super().__init__(parent, title='Copper Text',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.copper = copper_layers(board)
        # Keys only: text proxies held across a board mutation go stale, so
        # objects are re-fetched by UUID at the moment they are needed.
        self.selection = [(text_key(t), _shown(t)) for t in selected_texts(board)]
        self.items = []
        self.layer_text_keys = []
        self.preview_key = self.selection[0][0] if self.selection else None
        self.highlight_key = None
        # True while a board-side selection is being mirrored into the
        # dialog, so the resulting list events don't pan the canvas back.
        self._syncing = False
        self._last_editor_key = None
        # True while a board operation runs; blocks the poll timer from
        # re-entering the board mid-mutation.
        self._busy = False

        root = wx.BoxSizer(wx.HORIZONTAL)
        left = wx.BoxSizer(wx.VERTICAL)

        # ------------------------------------------------ generated labels
        gen_box = wx.StaticBoxSizer(wx.VERTICAL, self, 'Generated labels')
        self.item_list = wx.ListCtrl(self, style=wx.LC_REPORT,
                                     size=wx.Size(500, 110))
        for col, (name, width) in enumerate(
                (('Label', 220), ('Mode', 80), ('Layer', 100), ('Areas', 60))):
            self.item_list.InsertColumn(col, name, width=width)
        self.item_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_item_selected)
        gen_box.Add(self.item_list, 1, wx.EXPAND | wx.ALL, 5)

        row = wx.BoxSizer(wx.HORIZONTAL)
        remove = wx.Button(self, label='Remove selected')
        remove_all = wx.Button(self, label='Remove all')
        rebuild = wx.Button(self, label='Rebuild selected with options below')
        remove.Bind(wx.EVT_BUTTON, self.on_remove)
        remove_all.Bind(wx.EVT_BUTTON, self.on_remove_all)
        rebuild.Bind(wx.EVT_BUTTON, self.on_rebuild)
        row.Add(remove, 0, wx.RIGHT, 5)
        row.Add(remove_all, 0, wx.RIGHT, 5)
        row.Add(rebuild, 0)
        gen_box.Add(row, 0, wx.LEFT | wx.BOTTOM, 5)
        left.Add(gen_box, 1, wx.EXPAND | wx.ALL, 8)

        # ------------------------------------------------------- source
        src_box = wx.StaticBoxSizer(wx.VERTICAL, self, 'Convert new text')
        n = len(self.selection)
        self.src_sel = wx.RadioButton(
            self, label=f'Current selection ({n} text item(s))',
            style=wx.RB_GROUP)
        src_box.Add(self.src_sel, 0, wx.ALL, 4)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.src_lay = wx.RadioButton(self, label='Text on layer:')
        self.src_layers = text_layers(board)
        names = [name for _, name in self.src_layers]
        self.src_layer_choice = wx.Choice(
            self, choices=names or ['(no text on board)'])
        row.Add(self.src_lay, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        row.Add(self.src_layer_choice, 1, wx.EXPAND)
        src_box.Add(row, 0, wx.EXPAND | wx.ALL, 4)

        self.text_check = wx.CheckListBox(self, choices=[],
                                          size=wx.Size(500, 90))
        self.text_check.Bind(wx.EVT_LISTBOX, self.on_text_selected)
        src_box.Add(self.text_check, 1, wx.EXPAND | wx.ALL, 4)
        left.Add(src_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        if n == 0:
            self.src_sel.Enable(False)
            self.src_lay.SetValue(True)
        else:
            self.src_sel.SetValue(True)
        if not self.src_layers:
            self.src_lay.Enable(False)
            self.src_layer_choice.Enable(False)
        elif 'F.Fab' in names:
            self.src_layer_choice.SetSelection(names.index('F.Fab'))
        self.src_layer_choice.Bind(wx.EVT_CHOICE, self.on_src_layer)

        # ------------------------------------------------------ options
        opt_box = wx.StaticBoxSizer(wx.VERTICAL, self, 'Options')
        self.mode = wx.RadioBox(self, label='Style', choices=[
            'Negative — etched out of copper pour',
            'Positive — copper letters (no pour needed)'])
        self.mode.Bind(wx.EVT_RADIOBOX, self.update_preview)
        opt_box.Add(self.mode, 0, wx.EXPAND | wx.ALL, 4)

        grid = wx.FlexGridSizer(3, 2, 6, 10)
        grid.AddGrowableCol(1)

        grid.Add(wx.StaticText(self, label='Copper layer'), 0,
                 wx.ALIGN_CENTER_VERTICAL)
        self.layer_choice = wx.Choice(self,
                                      choices=[n for _, n in self.copper])
        self.layer_choice.SetSelection(0)
        self.layer_choice.Bind(wx.EVT_CHOICE, self.on_layer)
        grid.Add(self.layer_choice, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label='Mirrored'), 0,
                 wx.ALIGN_CENTER_VERTICAL)
        self.mirror = wx.CheckBox(self, label='reads correctly from this side')
        self.mirror.Bind(wx.EVT_CHECKBOX, self.update_preview)
        grid.Add(self.mirror, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label='Height (mm)'), 0,
                 wx.ALIGN_CENTER_VERTICAL)
        self.height = wx.TextCtrl(self, value='')
        self.height.SetHint('blank = keep current')
        self.height.Bind(wx.EVT_TEXT, self.update_preview)
        grid.Add(self.height, 1, wx.EXPAND)

        opt_box.Add(grid, 0, wx.EXPAND | wx.ALL, 4)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.border_on = wx.CheckBox(self, label='Border')
        self.border_on.Bind(wx.EVT_CHECKBOX, self.update_preview)
        row.Add(self.border_on, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        row.Add(wx.StaticText(self, label='width (mm)'), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.border_width = wx.TextCtrl(self, value='0.3',
                                        size=wx.Size(50, -1))
        self.border_width.Bind(wx.EVT_TEXT, self.update_preview)
        row.Add(self.border_width, 0, wx.RIGHT, 10)
        row.Add(wx.StaticText(self, label='gap (mm)'), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.border_gap = wx.TextCtrl(self, value='0.5', size=wx.Size(50, -1))
        self.border_gap.Bind(wx.EVT_TEXT, self.update_preview)
        row.Add(self.border_gap, 0)
        opt_box.Add(row, 0, wx.ALL, 4)

        self.counters = wx.CheckBox(
            self, label='keep islands inside O, B, D (pour: never remove; '
                        'negative only)')
        self.counters.SetValue(True)
        opt_box.Add(self.counters, 0, wx.ALL, 4)

        self.refill = wx.CheckBox(self, label='Refill zones after each change')
        self.refill.SetValue(True)
        opt_box.Add(self.refill, 0, wx.ALL, 4)

        left.Add(opt_box, 0, wx.EXPAND | wx.ALL, 8)

        # ------------------------------------------------------ actions
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        convert = wx.Button(self, label='Convert')
        convert.SetDefault()
        convert.Bind(wx.EVT_BUTTON, self.on_convert)
        close = wx.Button(self, wx.ID_CANCEL, 'Close')
        close.Bind(wx.EVT_BUTTON, lambda _e: self.Close())
        buttons.Add(convert, 0, wx.RIGHT, 5)
        buttons.Add(close, 0)
        left.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        self.status = wx.StaticText(self, label='')
        self.status.SetForegroundColour(wx.Colour(110, 110, 110))
        left.Add(self.status, 0,
                 wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # ------------------------------------------------------ preview
        preview_box = wx.StaticBoxSizer(wx.VERTICAL, self, 'Preview')
        self.preview = PreviewPanel(self)
        preview_box.Add(self.preview, 1, wx.EXPAND | wx.ALL, 5)
        self.preview_label = wx.StaticText(self, label='')
        self.preview_label.SetForegroundColour(wx.Colour(110, 110, 110))
        preview_box.Add(self.preview_label, 0, wx.EXPAND | wx.ALL, 5)

        root.Add(left, 0, wx.EXPAND)
        root.Add(preview_box, 1, wx.EXPAND | wx.ALL, 8)

        self.SetSizerAndFit(root)
        self.SetMinSize(self.GetSize())
        self.on_layer(None)
        self.refresh_items()
        self.refresh_layer_texts()
        self.update_preview()

        # Board -> dialog selection sync: poll the editor's selection and
        # mirror clicks on our texts/zones/groups into the lists.
        self.sel_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_poll, self.sel_timer)
        self.sel_timer.Start(500)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    # ------------------------------------------------------------ helpers

    def on_layer(self, _event=None):
        """Back-side text is normally mirrored so it reads from the back."""
        name = self.copper[self.layer_choice.GetSelection()][1]
        self.mirror.SetValue(name.startswith('B.'))
        self.update_preview()

    def on_src_layer(self, _event):
        if not self.src_layers:
            return
        self.src_lay.SetValue(True)
        self.refresh_layer_texts()
        # Follow the source side: B.Fab source defaults to a B.* copper layer.
        src_name = self.src_layers[self.src_layer_choice.GetSelection()][1]
        side = 'B.' if src_name.startswith('B.') else 'F.'
        for i, (_lid, name) in enumerate(self.copper):
            if name.startswith(side):
                self.layer_choice.SetSelection(i)
                break
        self.on_layer(None)

    def on_item_selected(self, event):
        index = event.GetIndex()
        if 0 <= index < len(self.items):
            self.preview_key = self.items[index]['key']
        self.update_preview()
        if not self._syncing:
            self.highlight(self.preview_key)

    def on_text_selected(self, event):
        index = event.GetSelection()
        if 0 <= index < len(self.layer_text_keys):
            self.preview_key = self.layer_text_keys[index]
        self.update_preview()
        if not self._syncing:
            self.highlight(self.preview_key)

    # ---------------------------------------- board -> dialog selection

    def _key_from_editor(self):
        """Label key for whatever is selected in pcbnew right now."""
        try:
            selection = pcbnew.GetCurrentSelection()
        except Exception:
            return None
        for item in selection:
            if isinstance(item, pcbnew.PCB_TEXT):
                try:
                    return item.m_Uuid.AsString()
                except AttributeError:
                    continue
            if isinstance(item, pcbnew.ZONE):
                try:
                    name = item.GetZoneName()
                except AttributeError:
                    continue
                for prefix in (ZONE_PREFIX, POS_ZONE_PREFIX):
                    if name.startswith(prefix):
                        return name[len(prefix):].split('_', 1)[0]
            if isinstance(item, pcbnew.PCB_GROUP):
                try:
                    parts = item.GetName().split('_')
                except AttributeError:
                    continue
                if (len(parts) >= 2
                        and parts[0] in (GROUP_PREFIX, POS_GROUP_PREFIX)):
                    return parts[1]
        return None

    def on_poll(self, _event):
        if self._busy:
            return
        key = self._key_from_editor()
        if key is None:
            self._last_editor_key = None
            return
        if key == self._last_editor_key:
            return
        self._last_editor_key = key
        self._syncing = True
        try:
            self.preview_key = key
            for i, item in enumerate(self.items):
                if item['key'] == key:
                    self.item_list.Select(i, True)
                    self.item_list.EnsureVisible(i)
                else:
                    self.item_list.Select(i, False)
            if key in self.layer_text_keys:
                self.text_check.SetSelection(self.layer_text_keys.index(key))
            self.update_preview()
        finally:
            self._syncing = False

    def on_close(self, _event):
        global _dialog
        self.sel_timer.Stop()
        self.clear_highlight()
        _dialog = None
        self.Destroy()

    # -------------------------------------------------- editor highlight

    def _highlight_items(self, key):
        """Fresh proxies for everything belonging to a label. Never cached:
        proxies held across a board mutation go stale."""
        items = []
        text = find_text_by_key(self.board, key)
        if text is not None:
            items.append(text)
        items.extend(generated_for(self.board, key))
        return items

    def clear_highlight(self, refresh=True):
        if self.highlight_key is None:
            return
        for item in self._highlight_items(self.highlight_key):
            try:
                item.ClearBrightened()
            except AttributeError:
                pass
        self.highlight_key = None
        if refresh:
            pcbnew.Refresh()

    def highlight(self, key):
        """Brighten the label's text and zones in pcbnew and pan to them."""
        self.clear_highlight(refresh=False)
        if key is None:
            pcbnew.Refresh()
            return
        items = self._highlight_items(key)
        for item in items:
            try:
                item.SetBrightened()
            except AttributeError:
                pass
        if items:
            self.highlight_key = key
            try:
                pcbnew.FocusOnItem(items[0])
            except Exception:
                pass
        pcbnew.Refresh()

    def refresh_items(self):
        self.items = all_generated(self.board)
        self.item_list.DeleteAllItems()
        for i, item in enumerate(self.items):
            self.item_list.InsertItem(i, item['label'] or '(unnamed)')
            self.item_list.SetItem(i, 1, item['mode'])
            self.item_list.SetItem(i, 2, pcbnew.LayerName(item['layer']))
            self.item_list.SetItem(i, 3, str(item['areas']))

    def refresh_layer_texts(self):
        self.layer_text_keys = []
        labels = []
        if self.src_layers:
            lid = self.src_layers[self.src_layer_choice.GetSelection()][0]
            for text in texts_on_layer(self.board, lid):
                self.layer_text_keys.append(text_key(text))
                labels.append(_shown(text, 48))
        self.text_check.Set(labels)
        for i in range(len(labels)):
            self.text_check.Check(i, True)

    def list_keys(self):
        keys = []
        i = self.item_list.GetFirstSelected()
        while i != -1:
            keys.append(self.items[i]['key'])
            i = self.item_list.GetNextSelected(i)
        return keys

    def source_keys(self):
        if self.src_sel.GetValue():
            return [key for key, _ in self.selection]
        return [key for i, key in enumerate(self.layer_text_keys)
                if self.text_check.IsChecked(i)]

    def _mm(self, ctrl, default=None):
        try:
            return float(ctrl.GetValue().strip())
        except ValueError:
            return default

    def options(self):
        border = None
        if self.border_on.GetValue():
            border = {'width': self._mm(self.border_width, 0.3),
                      'gap': self._mm(self.border_gap, 0.5)}
        return {
            'mode': self.MODES[self.mode.GetSelection()],
            'layer': self.copper[self.layer_choice.GetSelection()][0],
            'mirrored': self.mirror.GetValue(),
            'height': self._mm(self.height),
            'border': border,
            'counters': self.counters.GetValue(),
            'refill': self.refill.GetValue(),
        }

    def update_preview(self, _event=None):
        data = None
        caption = ''
        if self.preview_key:
            text = find_text_by_key(self.board, self.preview_key)
            if text is not None:
                opts = self.options()
                polys = preview_outlines(text, opts['mirrored'],
                                         opts['height'])
                glyphs = poly_points(polys)
                if glyphs:
                    bbox = polys.BBox()
                    x1, y1 = bbox.GetLeft(), bbox.GetTop()
                    x2, y2 = bbox.GetRight(), bbox.GetBottom()
                    border = []
                    if opts['border']:
                        gap = int(opts['border']['gap'] * MM)
                        width = int(opts['border']['width'] * MM)
                        outer = [(x1 - gap - width, y1 - gap - width),
                                 (x2 + gap + width, y1 - gap - width),
                                 (x2 + gap + width, y2 + gap + width),
                                 (x1 - gap - width, y2 + gap + width)]
                        inner = [(x1 - gap, y1 - gap), (x2 + gap, y1 - gap),
                                 (x2 + gap, y2 + gap), (x1 - gap, y2 + gap)]
                        border.append((outer, [inner]))
                        x1 -= gap + width
                        y1 -= gap + width
                        x2 += gap + width
                        y2 += gap + width
                    data = {'mode': opts['mode'], 'glyphs': glyphs,
                            'border': border, 'bbox': (x1, y1, x2, y2)}
                    caption = _shown(text, 60)
        self.preview.show(data)
        self.preview_label.SetLabel(caption)

    def _after(self, message):
        if self.refill.GetValue():
            _fill_zones(self.board, self)
        pcbnew.Refresh()
        self.refresh_items()
        self.refresh_layer_texts()
        self.update_preview()
        self.highlight(self.preview_key)
        self.status.SetLabel(message)
        self.Layout()

    # ------------------------------------------------------------ actions

    def _run(self, keys):
        if not keys:
            self.status.SetLabel('Nothing selected to convert.')
            return
        # Brightened flags must come off before anything is removed; the
        # flagged zones may be about to be purged.
        self.clear_highlight(refresh=False)
        opts = self.options()
        texts = [t for t in (find_text_by_key(self.board, k) for k in keys)
                 if t is not None]
        if not texts:
            self.status.SetLabel('Source text not found on the board.')
            return

        self._busy = True
        progress = wx.ProgressDialog(
            'Copper text', 'Converting…', maximum=len(texts), parent=self,
            style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_SMOOTH)
        glyphs = 0
        try:
            # One text per step: convert_batch is idempotent per text, and
            # updating between steps keeps the window painted.
            for i, text in enumerate(texts):
                progress.Update(i, f'Converting {i + 1}/{len(texts)}…')
                glyphs += convert_batch(self.board, [text], opts['layer'],
                                        opts['mirrored'], opts['height'],
                                        opts['mode'], opts['border'])
            if opts['mode'] == MODE_NEGATIVE and opts['counters']:
                keep_counters(self.board, opts['layer'])
            progress.Update(len(texts), 'Refilling…')
        finally:
            progress.Destroy()

        try:
            message = f"{len(texts)} label(s) -> {glyphs} copper areas."
            if opts['mode'] == MODE_NEGATIVE and not has_pour(self.board,
                                                              opts['layer']):
                message += (' No pour on this layer: negative text only '
                            'shows where a zone fills around it.')
            self._after(message)
        finally:
            self._busy = False

    def on_convert(self, _event):
        self._run(self.source_keys())

    def on_rebuild(self, _event):
        keys = self.list_keys()
        if not keys:
            self.status.SetLabel('Select generated labels in the list first.')
            return
        self._run(keys)

    def _remove(self, keys, prefix):
        self.clear_highlight(refresh=False)
        self._busy = True
        progress = wx.ProgressDialog(
            'Copper text', 'Removing…', maximum=len(keys), parent=self,
            style=wx.PD_APP_MODAL | wx.PD_AUTO_HIDE | wx.PD_SMOOTH)
        removed = 0
        try:
            for i, key in enumerate(keys):
                progress.Update(i, f'Removing {i + 1}/{len(keys)}…')
                removed += revert_batch(self.board, [key])
            progress.Update(len(keys), 'Refilling…')
        finally:
            progress.Destroy()
        try:
            self._after(f'{prefix}, {removed} areas deleted; source text '
                        'restored to its original layer.')
        finally:
            self._busy = False

    def on_remove(self, _event):
        keys = self.list_keys()
        if not keys:
            self.status.SetLabel('Select generated labels in the list first.')
            return
        self._remove(keys, f'{len(keys)} label(s) removed')

    def on_remove_all(self, _event):
        keys = [item['key'] for item in self.items]
        if not keys:
            self.status.SetLabel('No generated labels on the board.')
            return
        self._remove(keys, f'All {len(keys)} label(s) removed')


class NegativeTextPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = 'Copper text (negative / positive)'
        self.category = 'Modify PCB'
        self.description = ('Etch text out of a copper pour, or build it as '
                            'copper letters; manage generated labels.')
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), 'icon.png')
        self.icon_file_name = icon if os.path.exists(icon) else ''

    def Run(self):
        global _dialog
        if _dialog is not None:
            try:
                _dialog.Raise()
                return
            except RuntimeError:
                # Underlying window already destroyed; fall through.
                _dialog = None
        board = pcbnew.GetBoard()
        _dialog = CopperTextDialog(None, board)
        _dialog.Show()


NegativeTextPlugin().register()
