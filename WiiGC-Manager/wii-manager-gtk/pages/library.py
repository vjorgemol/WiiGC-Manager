"""
Página Videoteca: stats, búsqueda/filtros, tabla de juegos (con panel lateral
de detalle + carátula al seleccionar uno) y "Añadir juego" / "Añadir
GameCube". Equivalente a view-library del frontend web (scanDevice(),
applyFilters(), renderGames(), addGame(), submitAddGC()). Además, copia los
juegos seleccionados a otra unidad, los extrae a una carpeta del PC y hace
sonar el banner del juego de Wii seleccionado.
"""
import os
import threading
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, Gdk, Gio, GLib, GObject, Pango

import banner_sound
import dialogs
import cover_loader
import cover_print
import gametdb
import migrate
from sound_player import SoundPlayer
from backend import core, run_async
from widgets.spinning_paintable import SpinningPaintable

# Giro de la carátula de tipo «Disco»: una vuelta cada 4 segundos
SPIN_DEGREES_PER_SECOND = 90

# Formatos (campo fmt del juego) que entran en el filtro «Formato ISO / GCM»
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
    """
    log es el terminal de resultados; get_cover_prefs() devuelve la (región, tipo)
    de carátula elegidos en Ajustes. Solo se permite una operación de escritura
    a la vez sobre la unidad (ver _report).
    """

    def __init__(self, log, get_cover_prefs=lambda: ('ES', 'cover3D'), sound_enabled=True, on_sound_toggled=None,
                 get_gc_sound=lambda: ''):
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
        self._busy = False  # hay una operación de añadir/copiar/eliminar en curso
        self._copy_cancel = None  # threading.Event de la copia a otra unidad (o extracción al PC) en curso
        self._covers = {}  # id → Gdk.Texture (o None si GameTDB no tiene carátula)
        self._spin_tick = 0         # tick callback del giro de la carátula de tipo «Disco»
        self._sound_enabled = sound_enabled
        self._on_sound_toggled = on_sound_toggled
        self._get_gc_sound = get_gc_sound  # archivo de audio elegido en Ajustes para los juegos de GameCube
        self._sound_player = SoundPlayer()
        self._sound_request = None  # ID del juego cuyo sonido se ha pedido (el último gana)
        self._sound_timer = 0

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
        """Añade a la rejilla un indicador (título + valor) y devuelve la etiqueta del valor."""
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
        """Recalcula los indicadores a partir de self._all_games y del espacio usado/libre de la unidad."""
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

        self._copy_btn = Gtk.Button(label='Copiar a…', sensitive=False,
                                    tooltip_text='Copiar los juegos seleccionados a otra unidad')
        self._copy_btn.connect('clicked', lambda *_: self._choose_copy_destination())
        box.append(self._copy_btn)

        self._extract_btn = Gtk.Button(label='Extraer…', sensitive=False,
                                       tooltip_text='Guardar los juegos seleccionados en una carpeta del PC')
        self._extract_btn.connect('clicked', lambda *_: self._choose_extract_format())
        box.append(self._extract_btn)

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
        self._status_cancel = Gtk.Button(label='Cancelar', valign=Gtk.Align.CENTER, visible=False)
        self._status_cancel.connect('clicked', lambda *_: self._on_cancel_copy())
        self._status_row.append(self._status_progress)
        self._status_row.append(self._status_cancel)
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
        self._status_cancel.set_visible(busy and self._copy_cancel is not None)
        self._status_progress.set_visible(False)
        if busy and not self._progress_timer:
            self._progress_timer = GLib.timeout_add(500, self._poll_progress)
        self._status_row.set_visible(True)
        # Una operación de escritura a la vez sobre la unidad
        self._add_btn.set_sensitive(not busy)
        self._add_gc_btn.set_sensitive(not busy)
        self._remove_btn.set_sensitive(not busy and bool(self._selected_items()))
        self._copy_btn.set_sensitive(not busy and bool(self._selected_items()))
        self._extract_btn.set_sensitive(not busy and bool(self._selected_items()))

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
        """Filtro del panel lateral: all, wii, gc, wbfs o iso."""
        self._active_filter = name
        self._apply_filters()

    # ── Carga de datos ───────────────────────────────────────────
    def load_games(self, path):
        """Explora la unidad en un hilo y recarga la tabla. Con path vacío, detección automática."""
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
        """True mientras se añade, copia o elimina un juego (no conviene cerrar la app)."""
        return self._busy

    def get_games(self):
        """Juegos de la última exploración (los usa la página Verificar)."""
        return list(self._all_games)

    def _on_games_error(self, error):
        self._log.append(f"✗ No se pudo conectar: {error}", 'err')

    # ── Filtro + render ──────────────────────────────────────────
    def _apply_filters(self):
        """Aplica la búsqueda y el filtro a self._all_games y reconstruye la tabla."""
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
        """
        Columna de texto ordenable. text_fn(item) da el texto de la celda; sort_key(item),
        el valor por el que se ordena (por defecto, el propio texto sin distinguir mayúsculas).
        """
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

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_margin_top(6)
        header.set_margin_start(6)
        header.set_margin_end(6)
        self._sound_btn = Gtk.ToggleButton(active=self._sound_enabled, css_classes=['flat', 'circular'])
        self._sound_btn.connect('toggled', self._on_sound_btn_toggled)
        self._update_sound_btn()
        header.append(self._sound_btn)
        close_btn = Gtk.Button(icon_name='window-close-symbolic', halign=Gtk.Align.END, hexpand=True,
                               css_classes=['flat', 'circular'], tooltip_text='Cerrar')
        close_btn.connect('clicked', lambda *_: self._selection.unselect_all())
        header.append(close_btn)
        panel.append(header)

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
        """Los GameItem seleccionados en la tabla."""
        bitset = self._selection.get_selection()
        return [self._selection.get_item(bitset.get_nth(i)) for i in range(bitset.get_size())]

    def _single_selected(self):
        """El GameItem seleccionado, o None si no hay exactamente uno."""
        items = self._selected_items()
        return items[0] if len(items) == 1 else None

    def _on_selection_changed(self):
        """Actualiza los botones y el panel de detalle: ficha y carátula con un juego, resumen con varios."""
        items = self._selected_items()
        self._remove_btn.set_sensitive(bool(items) and not self._busy)
        self._copy_btn.set_sensitive(bool(items) and not self._busy)
        self._extract_btn.set_sensitive(bool(items) and not self._busy)
        self._detail_revealer.set_reveal_child(bool(items))
        self._stop_sound()
        if len(items) != 1:
            self._stop_spin()
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
        self._play_sound(game)

    @staticmethod
    def _title_list(items, limit=10):
        """Lista con viñetas de los títulos, recortada a limit."""
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
        """Pide confirmación antes de eliminar los juegos seleccionados."""
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
        """Informa del resultado de remove_games() y recarga la Videoteca."""
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

    # ── Copiar juegos a otra unidad ──────────────────────────────
    def _choose_copy_destination(self):
        """«Copiar a…»: busca las demás unidades conectadas y pregunta a cuál copiar."""
        items = self._selected_items()
        if not items or self._busy:
            return
        if not self._current_path:
            self._report('✗ Explora primero la unidad de la que quieres copiar', 'err')
            return
        games = [i.game for i in items]
        summary = (f'{self._title_list(items)}\n\nTamaño total: {sum(i.size for i in items):.2f} GB. '
                   'Los que ya estén en el destino se omiten.')
        run_async(core.api_devices, {}, on_done=lambda data: self._show_copy_dialog(games, summary, data),
                  on_error=lambda e: self._report(f'✗ {e}', 'err'))

    def _show_copy_dialog(self, games, summary, data):
        """Diálogo con el desplegable de unidades de destino (todas menos la explorada)."""
        src = os.path.realpath(self._current_path)
        vols = [v for v in migrate.volumes(data.get('devices', [])) if os.path.realpath(v['path']) != src]
        if not vols:
            self._report('✗ No hay otra unidad a la que copiar: conecta la de destino '
                         '(si es FAT32/NTFS, tiene que estar montada)', 'err')
            return
        count = '1 juego' if len(games) == 1 else f'{len(games)} juegos'
        dialog = Adw.AlertDialog(heading=f'Copiar {count} a otra unidad', body=summary)
        dropdown = Gtk.DropDown.new_from_strings([v['name'] for v in vols])
        dialog.set_extra_child(dropdown)
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('copy', 'Copiar')
        dialog.set_response_appearance('copy', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('copy')
        dialog.set_close_response('cancel')

        def on_response(_dialog, response):
            if response == 'copy' and not self._busy:
                self._start_copy(games, vols[dropdown.get_selected()]['path'])

        dialog.connect('response', on_response)
        dialog.present(self.get_root())

    def _start_copy(self, games, dst):
        """Lanza la copia en un hilo: migrate.migrate() limitado a los juegos seleccionados."""
        self._copy_cancel = threading.Event()
        self._status_cancel.set_sensitive(True)
        self._report(f'Copiando {len(games)} juego(s) a {dst}… Puede tardar, no desconectes las unidades.', 'info')
        only = {('gc', g.get('folder')) if g.get('platform') == 'gc' else ('wii', g.get('id')) for g in games}

        def on_game(n, total, game):  # llega desde el hilo de la copia
            GLib.idle_add(self._on_copy_game,
                          f"Copiando {game.get('title', '?')} [{game.get('id', '?')}] ({n + 1} de {total})…")

        run_async(migrate.migrate, self._current_path, dst, self._copy_cancel, on_game, only,
                  on_done=lambda result: self._on_copied(dst, *result), on_error=self._on_copy_error)

    def _on_copy_game(self, text):
        if self._copy_cancel and not self._copy_cancel.is_set():
            self._log.append(text, 'info')
            self._status_label.set_label(text)
        return GLib.SOURCE_REMOVE

    def _on_cancel_copy(self):
        self._copy_cancel.set()
        self._status_cancel.set_sensitive(False)
        self._status_label.set_label('Cancelando… Se descarta el juego que estaba a medias.')

    def _on_copy_error(self, error):
        self._copy_cancel = None
        self._report(f'✗ {error}', 'err')

    def _on_copied(self, dst, copied, failed, cancelled, todo):
        """Informa del resultado de la copia: copiados, ya presentes, no admitidos y fallidos."""
        self._copy_cancel = None
        for game in copied:
            self._log.append(f"✓ Copiado {game['title']} [{game['id']}]", 'ok')
        for game, error in failed:
            self._log.append(f"✗ No se pudo copiar {game['title']} [{game['id']}]: {error}", 'err')
        parts = [f'{len(copied)} juego(s) copiados a {dst}']
        if todo['present']:
            parts.append(f"{len(todo['present'])} ya estaban en el destino")
        if todo['unsupported']:
            parts.append(f"{len(todo['unsupported'])} de GameCube no se pueden copiar a una partición WBFS (solo admite Wii)")
        if failed:
            parts.append(f'{len(failed)} con errores; detalles en el terminal')
        text = ' · '.join(parts)
        if cancelled:
            self._report(f'Copia cancelada: {text}', 'err' if failed else '')
        else:
            self._report(('✗ ' if failed else '✓ ') + text, 'err' if failed else 'ok')

    # ── Extraer juegos al PC ─────────────────────────────────────
    def _choose_extract_format(self):
        """«Extraer…»: pregunta el formato (si hay juegos de Wii) y después la carpeta de destino."""
        items = self._selected_items()
        if not items or self._busy:
            return
        if not self._current_path:
            self._report('✗ Explora primero la unidad de la que quieres extraer', 'err')
            return
        games = [i.game for i in items]
        if all(g.get('platform') == 'gc' for g in games):
            # Los de GameCube se copian tal cual: no hay formato que elegir
            self._choose_extract_folder(games, 'iso')
            return
        count = '1 juego' if len(games) == 1 else f'{len(games)} juegos'
        dialog = Adw.AlertDialog(
            heading=f'Extraer {count} al PC',
            body=(f'{self._title_list(items)}\n\nTamaño total: {sum(i.size for i in items):.2f} GB. '
                  'Los juegos siguen en la unidad. Elige el formato de los juegos de Wii '
                  '(los de GameCube se copian tal cual):'))
        formats = [('iso', 'ISO — imagen completa del disco, compatible con todo'),
                   ('wbfs', 'WBFS — solo los datos usados, ocupa menos')]
        dropdown = Gtk.DropDown.new_from_strings([text for _fmt, text in formats])
        dialog.set_extra_child(dropdown)
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('extract', 'Elegir carpeta…')
        dialog.set_response_appearance('extract', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('extract')
        dialog.set_close_response('cancel')

        def on_response(_dialog, response):
            if response == 'extract':
                self._choose_extract_folder(games, formats[dropdown.get_selected()][0])

        dialog.connect('response', on_response)
        dialog.present(self.get_root())

    def _choose_extract_folder(self, games, fmt):
        """Pide la carpeta del PC en la que guardar los juegos y lanza la extracción."""
        def on_response(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return  # diálogo cancelado
            if folder and folder.get_path() and not self._busy:
                self._start_extract(games, folder.get_path(), fmt)

        Gtk.FileDialog(title='Carpeta en la que guardar los juegos').select_folder(self.get_root(), None, on_response)

    def _start_extract(self, games, dest, fmt):
        """Lanza la extracción en un hilo (migrate.extract()); se cancela igual que la copia a otra unidad."""
        self._copy_cancel = threading.Event()
        self._status_cancel.set_sensitive(True)
        self._report(f'Extrayendo {len(games)} juego(s) a {dest}… Puede tardar, no desconectes la unidad.', 'info')

        def on_game(n, total, game):  # llega desde el hilo de la extracción
            GLib.idle_add(self._on_copy_game,
                          f"Extrayendo {game.get('title', '?')} [{game.get('id', '?')}] ({n + 1} de {total})…")

        run_async(migrate.extract, games, self._current_path, dest, fmt, self._copy_cancel, on_game,
                  on_done=lambda result: self._on_extracted(dest, *result), on_error=self._on_copy_error)

    def _on_extracted(self, dest, done, failed, cancelled, present):
        """Informa del resultado de la extracción: extraídos, ya presentes y fallidos."""
        self._copy_cancel = None
        for game in done:
            self._log.append(f"✓ Extraído {game['title']} [{game['id']}] → {game['target']}", 'ok')
        for game, error in failed:
            self._log.append(f"✗ No se pudo extraer {game['title']} [{game['id']}]: {error}", 'err')
        parts = [f'{len(done)} juego(s) extraídos a {dest}']
        if present:
            parts.append(f'{len(present)} ya estaban en la carpeta')
        if failed:
            parts.append(f'{len(failed)} con errores; detalles en el terminal')
        text = ' · '.join(parts)
        if cancelled:
            self._report(f'Extracción cancelada: {text}', 'err' if failed else '')
        else:
            self._report(('✗ ' if failed else '✓ ') + text, 'err' if failed else 'ok')

    # ── Sonido del banner (el del menú de la Wii al elegir el disco) ──
    def _update_sound_btn(self):
        on = self._sound_btn.get_active()
        self._sound_btn.set_icon_name('audio-volume-high-symbolic' if on else 'audio-volume-muted-symbolic')
        self._sound_btn.set_tooltip_text('Sonido del juego al seleccionarlo: ' + ('activado' if on else 'desactivado'))

    def _on_sound_btn_toggled(self, button):
        self._sound_enabled = button.get_active()
        self._update_sound_btn()
        if self._on_sound_toggled:
            self._on_sound_toggled(self._sound_enabled)
        item = self._single_selected()
        if self._sound_enabled and item:
            self._play_sound(item.game)
        else:
            self._stop_sound()

    def _play_sound(self, game):
        """Hace sonar el banner del juego de Wii seleccionado; con uno de GameCube, el sonido elegido en Ajustes."""
        self._stop_sound()
        if game.get('platform') == 'gc':
            # Los discos de GameCube no traen sonido de banner
            path = self._get_gc_sound()
            if self._sound_enabled and path and os.path.isfile(path):
                self._sound_player.play(path)
            return
        # Con una operación en curso no se lee de la unidad: wwt no debe compartir la partición
        if not self._sound_enabled or self._busy or not self._current_path:
            return
        self._sound_request = game.get('id')
        # Al recorrer la tabla con las flechas solo interesa el juego en el que se para
        self._sound_timer = GLib.timeout_add(350, self._fetch_sound, game, self._current_path)

    def _fetch_sound(self, game, part):
        self._sound_timer = 0
        run_async(banner_sound.wav_path, game, part,
                  on_done=lambda path: self._on_sound_ready(game.get('id'), path), on_error=lambda _e: None)
        return GLib.SOURCE_REMOVE

    def _on_sound_ready(self, game_id, path):
        if not path or game_id != self._sound_request or not self._sound_enabled:
            return  # sin sonido, o ya se ha seleccionado otro juego
        self._sound_player.play(path)

    def _stop_sound(self):
        self._sound_request = None
        if self._sound_timer:
            GLib.source_remove(self._sound_timer)
            self._sound_timer = 0
        self._sound_player.stop()

    # ── Carátula (GameTDB, con fallback por región/tipo) ──────────
    def _show_cover(self, game):
        """Muestra la carátula del juego: la que ya está en memoria o, si no, la pide en un hilo."""
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
        self._stop_spin()
        paintable = texture
        if texture is not None and self._is_disc(texture):
            paintable = SpinningPaintable(texture)
            self._start_spin(paintable)
        self._cover.set_paintable(paintable)
        self._cover.set_visible(texture is not None)
        self._cover_status.set_label('' if texture else 'Sin carátula')

    def _is_disc(self, texture):
        """True si en Ajustes se pide la carátula «Disco» y la imagen lo es (cuadrada), no una de reserva de otro tipo."""
        if self._get_cover_prefs()[1] != 'disc':
            return False
        width, height = texture.get_intrinsic_width(), texture.get_intrinsic_height()
        return height > 0 and 0.9 < width / height < 1.1

    def _start_spin(self, paintable):
        """Hace girar la carátula mientras esté a la vista (salvo con las animaciones del sistema desactivadas)."""
        if not self._cover.get_settings().get_property('gtk-enable-animations'):
            return
        start = None

        def on_tick(_widget, clock):
            nonlocal start
            now = clock.get_frame_time()  # microsegundos
            if start is None:
                start = now
            paintable.set_angle((now - start) / 1e6 * SPIN_DEGREES_PER_SECOND)
            return GLib.SOURCE_CONTINUE

        self._spin_tick = self._cover.add_tick_callback(on_tick)

    def _stop_spin(self):
        if self._spin_tick:
            self._cover.remove_tick_callback(self._spin_tick)
            self._spin_tick = 0

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
