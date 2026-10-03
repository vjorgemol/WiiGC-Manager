"""
Página Videoteca: stats, búsqueda/filtros, tabla de juegos (con panel lateral
de detalle + carátula al seleccionar uno) y "Añadir juego" / "Añadir
GameCube". Equivalente a view-library del frontend web (scanDevice(),
applyFilters(), renderGames(), addGame(), submitAddGC()).
"""
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, Gdk, Gio, GLib, GObject, Pango

import dialogs
import cover_loader
import cover_print
import gametdb
from backend import core, run_async

ISO_FILTERS = {'iso', 'gcm', 'ciso'}


class GameItem(GObject.Object):
    """Fila de la tabla: el dict del juego + los datos de GameTDB."""

    def __init__(self, game):
        super().__init__()
        self.game = game
        self.company, self.date = gametdb.lookup(game.get('id'))

    @property
    def platform(self):
        return 'GameCube' if self.game.get('platform') == 'gc' else 'Wii'

    @property
    def size(self):
        return self.game.get('size') or 0

    @property
    def key(self):
        """Identifica el juego entre recargas de la tabla."""
        return self.game.get('platform'), self.game.get('id'), self.game.get('folder')


def remove_games(games, part):
    """
    Elimina los juegos uno a uno (bloqueante: llamar vía run_async).
    Devuelve [(game, error o None)]. Secuencial a propósito: wwt no debe
    escribir en la misma partición WBFS desde varios procesos a la vez.
    """
    results = []
    try:
        for n, game in enumerate(games):
            core.set_step(int(n * 100 / len(games)), f"Eliminando {game.get('title', '?')} ({n + 1} de {len(games)})")
            if game.get('platform') == 'gc':
                data = core.api_gc_remove({'folder': game.get('folder', '')})
                error = None if data.get('success') else data.get('error', 'Error desconocido')
            else:
                data = core.api_remove({'part': part, 'id': game.get('id', '')})
                error = None if data.get('rc') == 0 else (
                    data.get('error') or data.get('stderr', '').strip() or f"wwt devolvió código {data.get('rc')}")
            results.append((game, error))
    finally:
        core.set_step(None)
    return results


class LibraryPage(Gtk.Box):
    def __init__(self, log, get_cover_prefs=lambda: ('ES', 'cover3D')):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)

        self._log = log
        self._get_cover_prefs = get_cover_prefs
        self._all_games = []
        self._active_filter = 'all'
        self._search_text = ''
        self._current_path = ''
        self._busy = False  # hay una operación de añadir/eliminar en curso
        self._covers = {}  # id → Gdk.Texture (o None si GameTDB no tiene carátula)

        self.append(self._build_stats())
        self.append(self._build_toolbar())

        self.append(self._build_status_row())

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, vexpand=True)
        self._content_stack = Gtk.Stack(hexpand=True)
        self._content_stack.add_named(self._build_empty_state(), 'empty')
        self._content_stack.add_named(self._build_table(), 'table')
        body.append(self._content_stack)
        body.append(self._build_detail_panel())
        self.append(body)

        run_async(gametdb.load, on_done=lambda _ok: self._apply_filters(),
                  on_error=lambda e: self._log.append(
                      f'⚠ No se pudo descargar GameTDB (compañía y fecha de lanzamiento no disponibles): {e}', 'err'))

    # ── Stats ────────────────────────────────────────────────────
    def _build_stats(self):
        # Dos filas: recuentos de juegos arriba, espacio de la unidad abajo
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        counts = ['Juegos totales', 'Juegos Wii', 'Juegos GameCube', 'Multidisco (2 discos)']
        space = ['Espacio usado', 'Espacio disponible', 'Espacio GameCube']
        (self._stat_total, self._stat_wii, self._stat_gc, self._stat_multi) = (
            self._stat_tile(grid, label, col, 0) for col, label in enumerate(counts))
        (self._stat_size, self._stat_free, self._stat_gc_size) = (
            self._stat_tile(grid, label, col, 1) for col, label in enumerate(space))
        return grid

    @staticmethod
    def _stat_tile(grid, label_text, column, row):
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label = Gtk.Label(label=label_text, xalign=0)
        label.add_css_class('caption')
        label.add_css_class('dim-label')
        value = Gtk.Label(label='0', xalign=0)
        value.add_css_class('title-1')
        tile.append(label)
        tile.append(value)
        grid.attach(tile, column, row, 1, 1)
        return value

    def _update_stats(self, used_gb=None, free_gb=None):
        total = len(self._all_games)
        wii = sum(1 for g in self._all_games if g.get('platform') != 'gc')
        gc = total - wii
        self._stat_total.set_label(str(total))
        self._stat_wii.set_label(str(wii))
        self._stat_gc.set_label(str(gc))
        gc_games = [g for g in self._all_games if g.get('platform') == 'gc']
        self._stat_gc_size.set_label(f"{sum(g.get('size') or 0 for g in gc_games):.1f} GB")
        self._stat_multi.set_label(str(sum(1 for g in gc_games if g.get('disc_count', 1) > 1)))
        if used_gb or free_gb:
            self._stat_size.set_label(f"{used_gb or 0:.1f} GB")
            self._stat_free.set_label(f"{free_gb or 0:.1f} GB")
        else:
            # Sin datos de la unidad (0/0 = no se pudo consultar): al menos
            # lo que ocupan los juegos listados
            size = sum(g.get('size') or 0 for g in self._all_games)
            self._stat_size.set_label(f"{size:.1f} GB")
            self._stat_free.set_label('—')

    # ── Toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search = Gtk.SearchEntry(placeholder_text='Buscar juego, ID…', hexpand=True)
        search.connect('search-changed', self._on_search_changed)
        box.append(search)

        add_btn = self._add_btn = Gtk.Button(label='+ Añadir Wii')
        add_btn.add_css_class('suggested-action')
        add_btn.connect('clicked', lambda *_: self.open_add_game_dialog())
        box.append(add_btn)

        add_gc_btn = self._add_gc_btn = Gtk.Button(label='🎮 + Añadir GameCube',
                                                   css_classes=['suggested-action', 'gamecube'])
        add_gc_btn.connect('clicked', lambda *_: self.open_add_gc_dialog())
        box.append(add_gc_btn)

        self._remove_btn = Gtk.Button(label='Eliminar', css_classes=['destructive-action'], sensitive=False,
                                      tooltip_text='Eliminar los juegos seleccionados (Supr)')
        self._remove_btn.connect('clicked', lambda *_: self._confirm_remove())
        box.append(self._remove_btn)
        return box

    # ── Estado de operaciones largas (añadir / eliminar) ──────────
    def _build_status_row(self):
        # El terminal de resultados puede estar oculto: sin esta fila, copiar
        # un juego (varios minutos en un USB) no daría ninguna señal de vida.
        self._status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=False)
        self._status_spinner = Gtk.Spinner()
        self._status_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self._status_progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER, visible=False)
        self._status_progress.set_size_request(180, -1)
        self._progress_timer = 0
        self._status_dismiss = Gtk.Button(icon_name='window-close-symbolic', css_classes=['flat', 'circular'],
                                          tooltip_text='Ocultar', valign=Gtk.Align.CENTER)
        self._status_dismiss.connect('clicked', lambda *_: self._status_row.set_visible(False))
        self._status_row.append(self._status_spinner)
        self._status_row.append(self._status_label)
        self._status_row.append(self._status_progress)
        self._status_row.append(self._status_dismiss)
        return self._status_row

    def _report(self, text, kind=''):
        """Como self._log.append, pero además lo muestra en la fila de estado."""
        self._log.append(text, kind)
        busy = kind == 'info'
        self._busy = busy
        self._status_label.set_label(text)
        for css in ('success', 'error'):
            self._status_label.remove_css_class(css)
        if kind in ('ok', 'err'):
            self._status_label.add_css_class('success' if kind == 'ok' else 'error')
        self._status_spinner.set_visible(busy)
        self._status_spinner.set_spinning(busy)
        self._status_dismiss.set_visible(not busy)
        self._status_progress.set_visible(False)
        if busy and not self._progress_timer:
            self._progress_timer = GLib.timeout_add(500, self._poll_progress)
        self._status_row.set_visible(True)
        # Una operación de escritura a la vez sobre la unidad
        self._add_btn.set_sensitive(not busy)
        self._add_gc_btn.set_sensitive(not busy)
        self._remove_btn.set_sensitive(not busy and bool(self._selected_items()))

    def _poll_progress(self):
        """Mientras dura una operación, refleja en la barra el avance que da el backend."""
        if not self._busy:
            self._progress_timer = 0
            self._status_progress.set_visible(False)
            return GLib.SOURCE_REMOVE
        progress = core.api_progress({})  # solo lee un contador del sistema: no bloquea
        if progress['active']:
            self._status_progress.set_visible(True)
            if progress['percent'] is None:
                # No se puede medir en esta unidad: barra de actividad sin porcentaje
                self._status_progress.set_text('Copiando…')
                self._status_progress.pulse()
            else:
                self._status_progress.set_fraction(progress['percent'] / 100)
                self._status_progress.set_text(f"{progress['percent']} %")
                if progress['step']:
                    self._status_label.set_label(progress['step'])
        return GLib.SOURCE_CONTINUE

    def _on_search_changed(self, entry):
        self._search_text = entry.get_text().lower()
        self._apply_filters()

    def apply_filter(self, name):
        self._active_filter = name
        self._apply_filters()

    # ── Carga de datos ───────────────────────────────────────────
    def load_games(self, path):
        self._current_path = path or ''
        self._log.append(f"Explorando {path or '(detección automática --auto)'}…", 'info')
        run_async(core.api_list, {'part': self._current_path}, on_done=self._on_games_loaded, on_error=self._on_games_error)

    def _on_games_loaded(self, data):
        self._all_games = data.get('games', [])
        self._update_stats(data.get('used_gb'), data.get('free_gb'))
        self._apply_filters()
        if data.get('error') and not self._all_games:
            self._log.append(f"✗ {data['error']}", 'err')
        else:
            self._log.append(
                f"✓ {len(self._all_games)} juego(s) · usado: {data.get('used_gb','—')} GB"
                f" · libre: {data.get('free_gb','—')} GB", 'ok')

    def clear(self):
        """Vacía la Videoteca (p. ej. al desconectar la unidad)."""
        self._current_path = ''
        self._all_games = []
        self._update_stats()
        self._apply_filters()

    def is_busy(self):
        """True mientras se añade o elimina un juego (no conviene cerrar la app)."""
        return self._busy

    def get_games(self):
        """Juegos de la última exploración (los usa la página Verificar)."""
        return list(self._all_games)

    def _on_games_error(self, error):
        self._log.append(f"✗ No se pudo conectar: {error}", 'err')

    # ── Filtro + render ──────────────────────────────────────────
    def _apply_filters(self):
        q = self._search_text
        f = self._active_filter

        def matches(g):
            if q and q not in g.get('title', '').lower() and q not in (g.get('id') or '').lower():
                return False
            if f == 'wii':
                return g.get('platform') != 'gc'
            if f == 'gc':
                return g.get('platform') == 'gc'
            if f == 'wbfs':
                return g.get('fmt') == 'wbfs'
            if f == 'iso':
                return g.get('fmt') in ISO_FILTERS
            return True

        filtered = [g for g in self._all_games if matches(g)]
        # Reconstruir la tabla sin perder la selección (ni cerrar el panel)
        selected_keys = {item.key for item in self._selected_items()}
        self._store.remove_all()
        for game in filtered:
            self._store.append(GameItem(game))
        for pos in range(self._selection.get_n_items()):
            if self._selection.get_item(pos).key in selected_keys:
                self._selection.select_item(pos, False)
        self._on_selection_changed()
        self._content_stack.set_visible_child_name('table' if filtered else 'empty')

    # ── Tabla ────────────────────────────────────────────────────
    def _build_table(self):
        self._store = Gio.ListStore.new(GameItem)
        self._column_view = Gtk.ColumnView(show_row_separators=True)
        self._column_view.add_css_class('card')

        self._add_column('Título', lambda i: i.game.get('title', '?'), expand=True)
        self._add_column('Compañía', lambda i: i.company)
        self._add_column('Plataforma', lambda i: i.platform)
        self._add_column('Tamaño', lambda i: f'{i.size:.2f} GB' if i.size else '—',
                         sort_key=lambda i: i.size, xalign=1)
        self._add_column('Lanzamiento', lambda i: gametdb.format_date(i.date),
                         sort_key=lambda i: i.date)

        sorted_model = Gtk.SortListModel(model=self._store, sorter=self._column_view.get_sorter())
        # Selección múltiple (Ctrl/Mayús+clic) para poder eliminar varios juegos a la vez
        self._selection = Gtk.MultiSelection(model=sorted_model)
        self._selection.connect('selection-changed', lambda *_: self._on_selection_changed())
        self._column_view.set_model(self._selection)

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self._on_table_key_pressed)
        self._column_view.add_controller(keys)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self._column_view)
        return scroller

    def _add_column(self, title, text_fn, sort_key=None, expand=False, xalign=0):
        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', lambda _f, cell: cell.set_child(
            Gtk.Label(xalign=xalign, ellipsize=Pango.EllipsizeMode.END)))
        factory.connect('bind', lambda _f, cell: cell.get_child().set_label(text_fn(cell.get_item())))
        column = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)

        key = sort_key or (lambda i: text_fn(i).lower())
        column.set_sorter(Gtk.CustomSorter.new(
            lambda a, b, _data: (key(a) > key(b)) - (key(a) < key(b)), None))
        self._column_view.append_column(column)

    @staticmethod
    def _build_empty_state():
        placeholder = Gtk.Label(
            valign=Gtk.Align.START, justify=Gtk.Justification.CENTER,
            label='No hay juegos. Usa "Explorar dispositivo" en la barra lateral,\n'
                  'o añade uno con los botones de arriba.')
        placeholder.add_css_class('dim-label')
        placeholder.set_margin_top(40)
        return placeholder

    # ── Panel de detalle (carátula + datos del juego seleccionado) ─
    def _build_detail_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class('card')
        panel.set_size_request(208, -1)

        close_btn = Gtk.Button(icon_name='window-close-symbolic', halign=Gtk.Align.END,
                               css_classes=['flat', 'circular'], tooltip_text='Cerrar')
        close_btn.set_margin_top(6)
        close_btn.set_margin_end(6)
        close_btn.connect('clicked', lambda *_: self._selection.unselect_all())
        panel.append(close_btn)

        # Tamaño fijo en el Overlay, no en el Picture (cuyo tamaño natural
        # sale de la imagen descargada y descuadraría el panel).
        cover_overlay = self._cover_overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
        cover_overlay.set_size_request(172, 240)
        self._cover = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self._cover_status = Gtk.Label(justify=Gtk.Justification.CENTER)
        self._cover_status.add_css_class('dim-label')
        cover_overlay.set_child(self._cover_status)
        cover_overlay.add_overlay(self._cover)
        self._cover.set_tooltip_text('Doble clic para guardar o imprimir')
        cover_click = Gtk.GestureClick()
        cover_click.connect('pressed', self._on_cover_pressed)
        self._cover.add_controller(cover_click)
        panel.append(cover_overlay)

        self._detail_title = Gtk.Label(wrap=True, max_width_chars=18, justify=Gtk.Justification.CENTER,
                                       wrap_mode=Pango.WrapMode.WORD_CHAR)
        self._detail_title.add_css_class('heading')
        self._detail_title.set_margin_start(12)
        self._detail_title.set_margin_end(12)
        panel.append(self._detail_title)

        self._detail_info = Gtk.Label(wrap=True, max_width_chars=22, xalign=0, selectable=True,
                                      wrap_mode=Pango.WrapMode.WORD_CHAR)
        self._detail_info.add_css_class('caption')
        self._detail_info.set_margin_start(12)
        self._detail_info.set_margin_end(12)
        self._detail_info.set_margin_bottom(12)
        panel.append(self._detail_info)

        self._detail_revealer = Gtk.Revealer(
            child=panel, transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT, reveal_child=False)
        return self._detail_revealer

    def _selected_items(self):
        bitset = self._selection.get_selection()
        return [self._selection.get_item(bitset.get_nth(i)) for i in range(bitset.get_size())]

    def _single_selected(self):
        items = self._selected_items()
        return items[0] if len(items) == 1 else None

    def _on_selection_changed(self):
        items = self._selected_items()
        self._remove_btn.set_sensitive(bool(items) and not self._busy)
        self._detail_revealer.set_reveal_child(bool(items))
        if not items:
            return
        self._cover_overlay.set_visible(len(items) == 1)
        if len(items) > 1:
            # Varios juegos: resumen en lugar de carátula
            self._detail_title.set_label(f'{len(items)} juegos seleccionados')
            self._detail_info.set_label(
                f'Tamaño total: {sum(i.size for i in items):.2f} GB\n\n' + self._title_list(items))
            return
        item = items[0]
        game = item.game
        self._detail_title.set_label(game.get('title', '?'))
        rows = [('ID', game.get('id')), ('Compañía', item.company), ('Plataforma', item.platform),
                ('Región', game.get('region')), ('Formato', (game.get('fmt') or '').upper()),
                ('Tamaño', f'{item.size:.2f} GB' if item.size else ''),
                ('Lanzamiento', gametdb.format_date(item.date)),
                ('Discos', game.get('disc_count') if game.get('disc_count', 1) > 1 else '')]
        self._detail_info.set_label('\n'.join(f'{k}: {v}' for k, v in rows if v))
        self._show_cover(game)

    @staticmethod
    def _title_list(items, limit=10):
        lines = [f"• {i.game.get('title', '?')}" for i in items[:limit]]
        if len(items) > limit:
            lines.append(f'… y {len(items) - limit} más')
        return '\n'.join(lines)

    # ── Eliminar juegos ──────────────────────────────────────────
    def _on_table_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self._confirm_remove()
            return True
        return False

    def _confirm_remove(self):
        items = self._selected_items()
        if not items or self._busy:
            return
        count = '1 juego' if len(items) == 1 else f'{len(items)} juegos'
        dialog = Adw.AlertDialog(
            heading=f'¿Eliminar {count}?',
            body=(f'{self._title_list(items)}\n\n'
                  f'Se liberarán {sum(i.size for i in items):.2f} GB. Esta acción no se puede deshacer.'))
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('remove', 'Eliminar')
        dialog.set_response_appearance('remove', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_remove_response, [i.game for i in items])
        dialog.present(self.get_root())

    def _on_remove_response(self, _dialog, response, games):
        if response != 'remove':
            return
        self._report(f'Eliminando {len(games)} juego(s)…', 'info')
        run_async(remove_games, games, self._current_path,
                  on_done=self._on_removed, on_error=lambda e: self._on_removed([], error=e))

    def _on_removed(self, results, error=None):
        failed = 0
        for game, game_error in results:
            name = f"{game.get('title', '?')} [{game.get('id', '?')}]"
            if game_error:
                failed += 1
                self._log.append(f'✗ No se pudo eliminar {name}: {game_error}', 'err')
            else:
                self._log.append(f'✓ Eliminado {name}', 'ok')
        if error:
            self._report(f'✗ Error al eliminar: {error}', 'err')
        elif failed:
            self._report(f'✗ No se pudieron eliminar {failed} de {len(results)} juego(s); detalles en el terminal', 'err')
        else:
            self._report(f'✓ {len(results)} juego(s) eliminados', 'ok')
        self._selection.unselect_all()
        self.load_games(self._current_path)

    # ── Carátula (GameTDB, con fallback por región/tipo) ──────────
    def _show_cover(self, game):
        game_id = game.get('id')
        if game_id in self._covers:
            self._set_cover(self._covers[game_id])
            return
        self._cover.set_visible(False)
        self._cover_status.set_label('Cargando carátula…')
        region, cover_type = self._get_cover_prefs()
        run_async(cover_loader.fetch_cover, game_id, game.get('platform') == 'gc', region, cover_type,
                  on_done=lambda data: self._on_cover_fetched(game_id, data, (region, cover_type)),
                  on_error=lambda e: self._on_cover_error(game, e))

    def _on_cover_fetched(self, game_id, data, prefs):
        if prefs != self._get_cover_prefs():
            return  # se pidió con una región/tipo que ya no es el elegido en Ajustes
        texture = None
        if data:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                pass
        self._covers[game_id] = texture
        # Puede que el usuario ya haya seleccionado otro juego mientras se descargaba
        item = self._single_selected()
        if item and item.game.get('id') == game_id:
            self._set_cover(texture)

    def _on_cover_error(self, game, error):
        # No se guarda en self._covers: así se reintenta al volver a seleccionar el juego
        self._log.append(f"✗ No se pudo descargar la carátula de {game.get('title', '?')} "
                         f"[{game.get('id', '?')}]: {error}", 'err')
        item = self._single_selected()
        if item and item.game.get('id') == game.get('id'):
            self._cover.set_visible(False)
            self._cover_status.set_label('No se pudo descargar\nla carátula')

    def reload_cover(self):
        """Al cambiar región/tipo de carátula en Ajustes: descartar las ya descargadas."""
        self._covers.clear()
        item = self._single_selected()
        if item:
            self._show_cover(item.game)

    def _set_cover(self, texture):
        self._cover.set_paintable(texture)
        self._cover.set_visible(texture is not None)
        self._cover_status.set_label('' if texture else 'Sin carátula')

    # ── Guardar / imprimir carátula (doble clic) ──────────────────
    def _on_cover_pressed(self, _gesture, n_press, _x, _y):
        item = self._single_selected()
        texture = self._covers.get(item.game.get('id')) if item else None
        if n_press != 2 or texture is None:
            return
        game = item.game
        name = f"{game.get('title', '?')} [{game.get('id')}]"
        dialog = Adw.AlertDialog(
            heading='Carátula',
            body=f'{name}\n\nSe imprime la carátula completa a {cover_print.INSERT_WIDTH_MM} × '
                 f'{cover_print.INSERT_HEIGHT_MM} mm, el tamaño de una funda de DVD.')
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('save', 'Guardar…')
        dialog.add_response('print', 'Imprimir…')
        dialog.set_default_response('save')
        dialog.set_close_response('cancel')

        def on_response(_dialog, response):
            if response == 'save':
                cover_print.save_cover(self.get_root(), texture, name, self._status_log)
            elif response == 'print':
                self._print_cover(game, texture, name)

        dialog.connect('response', on_response)
        dialog.present(self.get_root())

    def _print_cover(self, game, texture, name):
        """
        Imprime siempre la carátula completa, sea cual sea el tipo que se ve
        en el panel: la de alta resolución de GameTDB o, si no la tiene, la normal.
        """
        game_id, is_gc, region = game.get('id'), game.get('platform') == 'gc', self._get_cover_prefs()[0]

        def fetch_full():
            for cover_type in cover_print.PRINT_COVER_TYPES:
                data = cover_loader.fetch_cover(game_id, is_gc, region, cover_type, fallback_types=False)
                if data:
                    return data
            return None

        def do_print(data):
            full = None
            if data:
                try:
                    full = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                except GLib.Error:
                    pass
            if full is None and cover_print.is_full_cover(texture):
                full = texture
            if full is None:
                self._report(f'✗ GameTDB no tiene carátula completa de {name}', 'err')
                return
            cover_print.print_cover(self.get_root(), full, name, self._status_log)

        def on_error(error):
            if cover_print.is_full_cover(texture):
                do_print(None)  # sin red, pero la que se ve ya es la completa
            else:
                self._report(f'✗ No se pudo descargar la carátula completa de {name}: {error}', 'err')

        self._report('Preparando la carátula para imprimir…', 'info')
        run_async(fetch_full, on_done=do_print, on_error=on_error)

    # ── Añadir juegos ──────────────────────────────────────────────
    @property
    def _status_log(self):
        """Lo que "Añadir juego" y el guardado de carátulas usan como "log": escribe en el terminal y en la fila de estado."""
        return SimpleNamespace(append=self._report)

    def open_add_game_dialog(self):
        dialogs.open_add_game_dialog(self.get_root(), self._current_path, self._status_log,
                                      on_added=lambda: self.load_games(self._current_path))

    def open_add_gc_dialog(self):
        dialogs.open_add_gc_dialog(self.get_root(), self._current_path, self._status_log,
                                    on_added=lambda: self.load_games(self._current_path))
