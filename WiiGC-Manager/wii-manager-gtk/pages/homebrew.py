"""
Página Homebrew: el catálogo de la Open Shop Channel (oscwii.org/library) con
la misma disposición que la Videoteca —indicadores, búsqueda, tabla y panel de
detalle con el icono y la ficha de la app—, para descargar las apps
seleccionadas en la unidad explorada (carpeta apps/, de donde las lee el
Homebrew Channel).

El filtro «Instalado en la unidad» muestra lo que ya hay en esa carpeta, esté
o no en el catálogo (p. ej. los cargadores), y permite eliminarlo.
"""
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, Gdk, Gio, GLib, GObject, Pango

import hbc_apps
import oscwii
from backend import core, run_async

CATEGORY_LABELS = dict(oscwii.CATEGORIES)

# Carpetas de apps/ de los cargadores que instala y actualiza «Formatear /
# Preparar» (Nintendont, WiiFlow Lite, USB Loader GX). También están en el
# catálogo, pero con otra numeración de versiones y a veces más antiguos: una
# vez instalados no se ofrecen como actualizables, para no pisarlos.
LOADER_FOLDERS = {app['path'].rsplit('/', 1)[-1].lower() for app in core.MANAGED_APPS.values()}


def format_size(size):
    """Bytes → '512 KB', '14.3 MB' o '1.28 GB'."""
    if not size:
        return '—'
    if size < 1024 ** 2:
        return f'{max(1, round(size / 1024))} KB'
    if size < 1024 ** 3:
        return f'{size / 1024 ** 2:.1f} MB'
    return f'{size / 1024 ** 3:.2f} GB'


class AppItem(GObject.Object):
    """
    Fila de la tabla. app es el dict de la app en el catálogo (None si lo que
    hay en la unidad no está en él) y local, el de hbc_apps.list_apps() con lo
    instalado en la unidad (None si no está instalada). Al menos hay uno.
    """

    def __init__(self, app, local):
        super().__init__()
        self.app = app
        self.local = local

    @property
    def folder(self):
        """Carpeta de la app dentro de apps/."""
        return self.local['folder'] if self.local else self.app['slug']

    @property
    def name(self):
        return (self.app.get('name') if self.app else self.local['name']) or self.folder

    @property
    def author(self):
        return (self.app.get('author') if self.app else self.local['author']) or ''

    @property
    def category(self):
        category = self.app.get('category') if self.app else ''
        return CATEGORY_LABELS.get(category, category or '')

    @property
    def version(self):
        """La versión del catálogo o, si la app no está en él, la instalada."""
        return (self.app.get('version') if self.app else self.local['version']) or ''

    @property
    def size(self):
        """Bytes que ocupa en la unidad: lo que ocupará descomprimida o, si no está en el catálogo, lo que ocupa."""
        return (self.app.get('uncompressed_size') if self.app else self.local['size']) or 0

    @property
    def date(self):
        return self.app.get('release_date') or 0 if self.app else 0

    @property
    def is_current(self):
        """True si la unidad ya tiene la versión del catálogo (o es un cargador, que se actualiza en otra vista)."""
        return bool(self.app and self.local) and (self.is_loader or self.local['version'] == self.version)

    @property
    def is_loader(self):
        """True si es uno de los cargadores de LOADER_FOLDERS."""
        return self.folder.lower() in LOADER_FOLDERS

    @property
    def state(self):
        if self.local is None:
            return ''
        if self.app is None:
            return 'Instalada (no está en el catálogo)'
        if self.is_loader:
            return 'Instalada (cargador)'
        if self.is_current:
            return 'Instalada'
        installed = self.local['version']
        return f'Actualizable ({installed} → {self.version})' if installed else 'Instalada (versión desconocida)'


def format_date(timestamp):
    """Segundos desde 1970 → '11/04/2008'."""
    return time.strftime('%d/%m/%Y', time.gmtime(timestamp)) if timestamp else ''


class HomebrewPage(Gtk.Box):
    """
    log es el terminal de resultados; get_device_path() devuelve la ruta de la
    unidad explorada, que es donde se instalan las apps.
    """

    def __init__(self, log, get_device_path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)

        self._log = log
        self._get_device_path = get_device_path
        self._apps = []             # catálogo completo
        self._loading = False       # se está descargando el catálogo
        self._installed = {}        # carpeta (en minúsculas) → app de hbc_apps.list_apps() que hay en la unidad
        self._drive_ok = False      # la unidad explorada está montada: se puede leer y escribir en su apps/
        self._active_filter = 'all'
        self._search_text = ''
        self._busy = False          # hay una descarga o una eliminación en curso
        self._cancel = None         # threading.Event de la descarga en curso
        self._removing = False      # se están eliminando apps de la unidad
        self._icons = {}            # slug → Gdk.Texture (o None si no tiene icono)

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

    # ── Stats ────────────────────────────────────────────────────
    def _build_stats(self):
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        labels = ['Apps en el catálogo', 'Instaladas en la unidad', 'Con actualización', 'Espacio disponible']
        (self._stat_total, self._stat_installed, self._stat_outdated, self._stat_free) = (
            self._stat_tile(grid, label, col) for col, label in enumerate(labels))
        return grid

    @staticmethod
    def _stat_tile(grid, label_text, column):
        """Añade a la rejilla un indicador (título + valor) y devuelve la etiqueta del valor."""
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label = Gtk.Label(label=label_text, xalign=0)
        label.add_css_class('caption')
        label.add_css_class('dim-label')
        value = Gtk.Label(label='0', xalign=0)
        value.add_css_class('title-1')
        tile.append(label)
        tile.append(value)
        grid.attach(tile, column, 0, 1, 1)
        return value

    def _update_stats(self, free=None):
        """Recalcula los indicadores; free son los bytes libres de la unidad (None: no hay unidad montada)."""
        outdated = sum(1 for app in self._apps
                       if (local := self._installed.get(app['slug'].lower()))
                       and not AppItem(app, local).is_current)
        self._stat_total.set_label(str(len(self._apps)))
        self._stat_installed.set_label(str(len(self._installed)))
        self._stat_outdated.set_label(str(outdated))
        self._stat_free.set_label('—' if free is None else f'{free / 1024 ** 3:.1f} GB')

    # ── Toolbar ──────────────────────────────────────────────────
    def _build_toolbar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search = Gtk.SearchEntry(placeholder_text='Buscar app, autor…', hexpand=True)
        search.connect('search-changed', self._on_search_changed)
        box.append(search)

        self._reload_btn = Gtk.Button(icon_name='view-refresh-symbolic',
                                      tooltip_text='Volver a descargar el catálogo de oscwii.org')
        self._reload_btn.connect('clicked', lambda *_: self._load_catalog(force=True))
        box.append(self._reload_btn)

        # Con toda la lista seleccionada pasa a ser «Deseleccionar todo» (ver _update_buttons)
        self._select_all_btn = Gtk.Button(label='Seleccionar todo', sensitive=False)
        self._select_all_btn.connect('clicked', self._on_select_all_clicked)
        box.append(self._select_all_btn)

        self._install_btn = Gtk.Button(label='Descargar en la unidad', css_classes=['suggested-action'],
                                       sensitive=False,
                                       tooltip_text='Descargar las apps seleccionadas en la unidad explorada')
        self._install_btn.connect('clicked', lambda *_: self._confirm_install())
        box.append(self._install_btn)

        self._remove_btn = Gtk.Button(label='Eliminar', css_classes=['destructive-action'], sensitive=False,
                                      tooltip_text='Eliminar de la unidad las apps seleccionadas (Supr)')
        self._remove_btn.connect('clicked', lambda *_: self._confirm_remove())
        box.append(self._remove_btn)
        return box

    # ── Estado de operaciones largas (descargar / eliminar) ──────
    def _build_status_row(self):
        # El terminal de resultados puede estar oculto: sin esta fila, una
        # descarga larga no daría ninguna señal de vida.
        self._status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, visible=False)
        self._status_spinner = Gtk.Spinner()
        self._status_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self._status_progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER, visible=False)
        self._status_progress.set_size_request(180, -1)
        self._progress_timer = 0
        self._status_cancel = Gtk.Button(label='Cancelar', valign=Gtk.Align.CENTER, visible=False)
        self._status_cancel.connect('clicked', lambda *_: self._on_cancel())
        self._status_dismiss = Gtk.Button(icon_name='window-close-symbolic', css_classes=['flat', 'circular'],
                                          tooltip_text='Ocultar', valign=Gtk.Align.CENTER)
        self._status_dismiss.connect('clicked', lambda *_: self._status_row.set_visible(False))
        self._status_row.append(self._status_spinner)
        self._status_row.append(self._status_label)
        self._status_row.append(self._status_progress)
        self._status_row.append(self._status_cancel)
        self._status_row.append(self._status_dismiss)
        return self._status_row

    def _report(self, text, kind=''):
        """Como self._log.append, pero además lo muestra en la fila de estado ('info' = operación en curso)."""
        self._log.append(text, kind)
        busy = kind == 'info'
        self._busy = busy and (self._cancel is not None or self._removing)
        self._status_label.set_label(text)
        for css in ('success', 'error'):
            self._status_label.remove_css_class(css)
        if kind in ('ok', 'err'):
            self._status_label.add_css_class('success' if kind == 'ok' else 'error')
        self._status_spinner.set_visible(busy)
        self._status_spinner.set_spinning(busy)
        self._status_dismiss.set_visible(not busy)
        self._status_cancel.set_visible(busy and self._cancel is not None)
        self._status_progress.set_visible(False)
        if self._busy and not self._progress_timer:
            self._progress_timer = GLib.timeout_add(300, self._poll_progress)
        self._status_row.set_visible(True)
        self._update_buttons()

    def _poll_progress(self):
        """Mientras dura la operación, refleja en la barra el avance que publica (core.set_step)."""
        if not self._busy:
            self._progress_timer = 0
            self._status_progress.set_visible(False)
            return GLib.SOURCE_REMOVE
        progress = core.api_progress({})
        if progress['active'] and progress['percent'] is not None:
            self._status_progress.set_visible(True)
            self._status_progress.set_fraction(progress['percent'] / 100)
            self._status_progress.set_text(f"{progress['percent']} %")
            if progress['step'] and not (self._cancel and self._cancel.is_set()):
                self._status_label.set_label(progress['step'])
        return GLib.SOURCE_CONTINUE

    def _all_selected(self):
        """True si están seleccionadas todas las apps de la lista (y hay alguna)."""
        total = self._selection.get_n_items()
        return total > 0 and self._selection.get_selection().get_size() == total

    def _on_select_all_clicked(self, _btn):
        if self._all_selected():
            self._selection.unselect_all()
        else:
            self._selection.select_all()

    def _update_buttons(self):
        items = self._selected_items()
        all_selected = self._all_selected()
        self._select_all_btn.set_label('Deseleccionar todo' if all_selected else 'Seleccionar todo')
        self._select_all_btn.set_tooltip_text('Quitar la selección' if all_selected
                                              else 'Seleccionar todas las apps de la lista (Ctrl + A)')
        self._select_all_btn.set_sensitive(self._selection.get_n_items() > 0)
        self._install_btn.set_sensitive(any(i.app for i in items) and not self._busy)
        self._remove_btn.set_sensitive(any(i.local for i in items) and not self._busy)
        self._reload_btn.set_sensitive(not self._busy and not self._loading)

    def is_busy(self):
        """True mientras se descargan o eliminan apps en la unidad (no conviene cerrar la app)."""
        return self._busy

    def abort(self):
        """Cancela la descarga en curso, si la hay."""
        if self._cancel:
            self._cancel.set()

    # ── Catálogo ─────────────────────────────────────────────────
    def refresh(self):
        """
        Al entrar en la vista o al cambiar de unidad: descarga el catálogo la
        primera vez y vuelve a mirar qué apps hay ya en la unidad explorada.
        """
        if not self._apps:
            self._load_catalog()
        self._scan_drive()

    def _load_catalog(self, force=False):
        if self._loading or self._busy:
            return
        self._loading = True
        self._report('Descargando el catálogo de homebrew de oscwii.org…', 'info')
        run_async(oscwii.load, force, on_done=self._on_catalog_loaded, on_error=self._on_catalog_error)

    def _on_catalog_loaded(self, apps):
        self._loading = False
        self._apps = apps
        self._log.append(f'✓ {len(apps)} apps en el catálogo de oscwii.org', 'ok')
        self._status_row.set_visible(False)
        self._status_spinner.set_spinning(False)
        self._scan_drive()

    def _on_catalog_error(self, error):
        self._loading = False
        self._apply_filters()
        self._report(f'✗ No se pudo descargar el catálogo de oscwii.org: {error}', 'err')

    def _scan_drive(self):
        """Mira en un hilo qué homebrew hay ya en la unidad explorada."""
        path = self._get_device_path()
        if self._busy:
            return
        if not core.is_mounted_dir(path):
            self._on_drive_scanned(path, None)
            return
        run_async(hbc_apps.list_apps, path, on_done=lambda result: self._on_drive_scanned(path, result),
                  on_error=lambda _e: self._on_drive_scanned(path, None))

    def _on_drive_scanned(self, path, result):
        """result es lo que devuelve hbc_apps.list_apps(), o None si no hay unidad montada o no se pudo leer."""
        if path != self._get_device_path():
            return  # mientras se miraba, se ha cambiado de unidad
        self._drive_ok = result is not None
        local_apps, free = result or ([], None)
        # En minúsculas: en FAT32 apps/WiiMC y apps/wiimc son la misma carpeta
        self._installed = {a['folder'].lower(): a for a in local_apps}
        self._update_stats(free)
        self._apply_filters()

    # ── Filtro + render ──────────────────────────────────────────
    def _on_search_changed(self, entry):
        self._search_text = entry.get_text().lower()
        self._apply_filters()

    def apply_filter(self, name):
        """Filtro del panel lateral: all, una de las categorías de oscwii.CATEGORIES o installed."""
        self._active_filter = name
        self._apply_filters()

    def _apply_filters(self):
        """Aplica la búsqueda y el filtro al catálogo y a lo instalado, y reconstruye la tabla."""
        q = self._search_text
        f = self._active_filter

        items = [AppItem(app, self._installed.get(app['slug'].lower())) for app in self._apps]
        if f == 'installed':
            # Lo que hay en la unidad, esté o no en el catálogo (p. ej. los cargadores)
            in_catalog = {app['slug'].lower() for app in self._apps}
            items = [i for i in items if i.local] + [
                AppItem(None, local) for key, local in self._installed.items() if key not in in_catalog]
            items.sort(key=lambda i: i.name.lower())
        elif f != 'all':
            items = [i for i in items if i.app.get('category') == f]
        if q:
            items = [i for i in items if any(q in text.lower() for text in (i.name, i.folder, i.author))]

        # Reconstruir la tabla sin perder la selección (ni cerrar el panel)
        selected = {item.folder.lower() for item in self._selected_items()}
        self._store.remove_all()
        for item in items:
            self._store.append(item)
        for pos in range(self._selection.get_n_items()):
            if self._selection.get_item(pos).folder.lower() in selected:
                self._selection.select_item(pos, False)
        self._on_selection_changed()
        if not items:
            self._placeholder.set_label(self._empty_text())
        self._content_stack.set_visible_child_name('table' if items else 'empty')

    def _empty_text(self):
        """Por qué la tabla está vacía, según el filtro y lo que se haya podido cargar."""
        if self._search_text and (self._apps or self._installed):
            return 'Ninguna app coincide con la búsqueda.'
        if self._active_filter == 'installed':
            if not self._drive_ok:
                return ('No hay ninguna unidad montada. Pon su ruta en la barra lateral\n'
                        'y pulsa "Explorar dispositivo". No sirve una partición WBFS.')
            return 'No hay homebrew en la unidad. Descárgalo desde los demás filtros.'
        if self._loading:
            return 'Descargando el catálogo de oscwii.org…'
        if not self._apps:
            return ('No se pudo descargar el catálogo de oscwii.org.\n'
                    'Comprueba la conexión y pulsa el botón de recargar.')
        return 'Ninguna app coincide con el filtro.'

    # ── Tabla ────────────────────────────────────────────────────
    def _build_table(self):
        self._store = Gio.ListStore.new(AppItem)
        self._column_view = Gtk.ColumnView(show_row_separators=True)
        self._column_view.add_css_class('card')

        self._add_column('Nombre', lambda i: i.name, expand=True)
        self._add_column('Categoría', lambda i: i.category)
        self._add_column('Versión', lambda i: i.version)
        self._add_column('Tamaño', lambda i: format_size(i.size), sort_key=lambda i: i.size, xalign=1)
        self._add_column('En la unidad', lambda i: i.state)

        sorted_model = Gtk.SortListModel(model=self._store, sorter=self._column_view.get_sorter())
        # Selección múltiple (Ctrl/Mayús+clic, Ctrl+A) para descargar o eliminar varias apps a la vez
        self._selection = Gtk.MultiSelection(model=sorted_model)
        self._selection.connect('selection-changed', lambda *_: self._on_selection_changed())
        self._column_view.set_model(self._selection)

        # Doble clic (o Intro) sobre una app: descargarla
        self._column_view.connect('activate', lambda *_: self._confirm_install())

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

    def _build_empty_state(self):
        self._placeholder = Gtk.Label(valign=Gtk.Align.START, justify=Gtk.Justification.CENTER)
        self._placeholder.add_css_class('dim-label')
        self._placeholder.set_margin_top(40)
        return self._placeholder

    # ── Panel de detalle (icono + ficha de la app seleccionada) ───
    def _build_detail_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.add_css_class('card')
        panel.set_size_request(240, -1)

        close_btn = Gtk.Button(icon_name='window-close-symbolic', halign=Gtk.Align.END,
                               css_classes=['flat', 'circular'], tooltip_text='Cerrar')
        close_btn.set_margin_top(6)
        close_btn.set_margin_end(6)
        close_btn.connect('clicked', lambda *_: self._selection.unselect_all())
        panel.append(close_btn)

        # Tamaño fijo en el Overlay, no en el Picture (cuyo tamaño natural
        # sale de la imagen descargada y descuadraría el panel). Los iconos
        # del Homebrew Channel son de 128 × 48.
        icon_overlay = self._icon_overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
        icon_overlay.set_size_request(192, 72)
        self._icon = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self._icon_status = Gtk.Label(justify=Gtk.Justification.CENTER)
        self._icon_status.add_css_class('dim-label')
        icon_overlay.set_child(self._icon_status)
        icon_overlay.add_overlay(self._icon)
        panel.append(icon_overlay)

        self._detail_title = Gtk.Label(wrap=True, max_width_chars=20, justify=Gtk.Justification.CENTER,
                                       wrap_mode=Pango.WrapMode.WORD_CHAR)
        self._detail_title.add_css_class('heading')
        self._detail_title.set_margin_start(12)
        self._detail_title.set_margin_end(12)
        panel.append(self._detail_title)

        # La descripción larga de algunas apps ocupa varias pantallas
        self._detail_info = Gtk.Label(wrap=True, max_width_chars=26, xalign=0, yalign=0, selectable=True,
                                      wrap_mode=Pango.WrapMode.WORD_CHAR)
        self._detail_info.add_css_class('caption')
        self._detail_info.set_margin_start(12)
        self._detail_info.set_margin_end(12)
        self._detail_info.set_margin_bottom(12)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self._detail_info)
        panel.append(scroller)

        self._detail_revealer = Gtk.Revealer(
            child=panel, transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT, reveal_child=False)
        return self._detail_revealer

    def _selected_items(self):
        """Los AppItem seleccionados en la tabla."""
        bitset = self._selection.get_selection()
        return [self._selection.get_item(bitset.get_nth(i)) for i in range(bitset.get_size())]

    def _on_selection_changed(self):
        """Actualiza los botones y el panel de detalle: ficha e icono con una app, resumen con varias."""
        items = self._selected_items()
        self._update_buttons()
        self._detail_revealer.set_reveal_child(bool(items))
        if not items:
            return
        self._icon_overlay.set_visible(len(items) == 1)
        if len(items) > 1:
            self._detail_title.set_label(f'{len(items)} apps seleccionadas')
            self._detail_info.set_label(
                f'Tamaño total: {format_size(sum(i.size for i in items))}\n\n' + self._name_list(items))
            return
        item = items[0]
        app, local = item.app or {}, item.local or {}
        self._detail_title.set_label(item.name)
        description = app.get('description') or {'short': local.get('short'), 'long': local.get('long')}
        date = format_date(item.date) if item.app else hbc_apps.format_date(local.get('date', ''))
        rows = [('Autor', item.author), ('Categoría', item.category), ('Versión', item.version),
                ('Publicación', date), ('Tamaño', format_size(item.size)),
                ('Descarga', format_size(app['assets']['archive'].get('size')) if item.app else ''),
                ('Mandos y accesorios', self._labels(app.get('peripherals'), oscwii.PERIPHERALS)),
                ('Consolas', self._labels(app.get('supported_platforms'), oscwii.PLATFORMS)),
                ('Carpeta', f'apps/{item.folder}'), ('En la unidad', item.state),
                ('Ocupa en la unidad', format_size(local['size']) if item.app and item.local else '')]
        flags = app.get('flags') or []
        warnings = []
        if 'WRITES_TO_NAND' in flags:
            warnings.append('⚠ Puede escribir en la memoria interna de la consola (NAND): úsala con cuidado.')
        if 'DEPRECATED' in flags:
            warnings.append('⚠ Obsoleta: sus autores ya no recomiendan usarla.')
        if item.local and item.is_loader:
            warnings.append('Los cargadores se actualizan desde Formatear / Preparar.')
        blocks = [(description.get('short') or '').strip(),
                  '\n'.join(f'{k}: {v}' for k, v in rows if v),
                  '\n'.join(warnings),
                  (description.get('long') or '').strip()]
        self._detail_info.set_label('\n\n'.join(b for b in blocks if b))
        self._show_icon(item)

    @staticmethod
    def _labels(values, names):
        """['wii_remote', 'wii_remote', 'nunchuk'] → 'Wiimote ×2, Nunchuk' (la API repite el mando por cada jugador)."""
        values = values or []
        return ', '.join(names.get(v, v) + (f' ×{values.count(v)}' if values.count(v) > 1 else '')
                         for v in dict.fromkeys(values))

    @staticmethod
    def _name_list(items, limit=10):
        """Lista con viñetas de los nombres, recortada a limit."""
        lines = [f'• {i.name}' for i in items[:limit]]
        if len(items) > limit:
            lines.append(f'… y {len(items) - limit} más')
        return '\n'.join(lines)

    # ── Icono ────────────────────────────────────────────────────
    def _show_icon(self, item):
        """Muestra el icono de la app: el del catálogo (en memoria o pedido en un hilo) o el que tiene en la unidad."""
        if item.app is None:
            texture = None
            if item.local['icon']:
                try:
                    texture = Gdk.Texture.new_from_filename(item.local['icon'])
                except GLib.Error:
                    pass
            self._set_icon(texture)
            return
        app, slug = item.app, item.app['slug']
        if slug in self._icons:
            self._set_icon(self._icons[slug])
            return
        self._icon.set_visible(False)
        self._icon_status.set_label('Cargando icono…')
        run_async(oscwii.fetch_icon, app, on_done=lambda data: self._on_icon_fetched(slug, data),
                  on_error=lambda _e: self._on_icon_fetched(slug, None, remember=False))

    def _on_icon_fetched(self, slug, data, remember=True):
        texture = None
        if data:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                pass
        if remember:  # un fallo de red no se recuerda: así se reintenta al volver a seleccionar la app
            self._icons[slug] = texture
        # Puede que ya se haya seleccionado otra app mientras se descargaba
        items = self._selected_items()
        if len(items) == 1 and items[0].app and items[0].app['slug'] == slug:
            self._set_icon(texture)

    def _set_icon(self, texture):
        self._icon.set_paintable(texture)
        self._icon.set_visible(texture is not None)
        self._icon_status.set_label('' if texture else 'Sin icono')

    # ── Descargar en la unidad ───────────────────────────────────
    def _confirm_install(self):
        """Pide confirmación antes de descargar las apps seleccionadas en la unidad explorada."""
        items = [i for i in self._selected_items() if i.app]  # lo que no está en el catálogo no se puede descargar
        if not items or self._busy:
            return
        path = self._get_device_path()
        if not core.is_mounted_dir(path):
            self._report('✗ Para descargar homebrew hace falta una unidad montada (FAT32): pon su ruta en el '
                         'panel lateral y pulsa «Explorar dispositivo». No sirve una partición WBFS.', 'err')
            return
        todo = [i for i in items if not i.is_current]
        if not todo:
            self._report('✓ Las apps seleccionadas ya están en la unidad en su última versión', 'ok')
            return
        count = '1 app' if len(todo) == 1 else f'{len(todo)} apps'
        notes = [f'Ocuparán {format_size(sum(i.size for i in todo))} en {path}.']
        if len(todo) < len(items):
            notes.append(f'{len(items) - len(todo)} ya están al día y se omiten.')
        nand = sum(1 for i in todo if 'WRITES_TO_NAND' in (i.app.get('flags') or []))
        if nand:
            notes.append(f'⚠ {nand} de ellas pueden escribir en la memoria interna de la consola (NAND): '
                         'úsalas con cuidado.')
        dialog = Adw.AlertDialog(heading=f'¿Descargar {count} en la unidad?',
                                 body=f'{self._name_list(todo)}\n\n' + ' '.join(notes))
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('install', 'Descargar')
        dialog.set_response_appearance('install', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('install')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_install_response, [i.app for i in todo], path)
        dialog.present(self.get_root())

    def _on_install_response(self, _dialog, response, apps, path):
        if response != 'install' or self._busy:
            return
        self._cancel = threading.Event()
        self._status_cancel.set_sensitive(True)
        self._report(f'Descargando {len(apps)} app(s) en {path}… No desconectes la unidad.', 'info')
        run_async(oscwii.install, apps, path, self._cancel,
                  on_done=lambda result: self._on_installed(path, *result), on_error=self._on_install_error)

    def _on_cancel(self):
        self._cancel.set()
        self._status_cancel.set_sensitive(False)
        self._status_label.set_label('Cancelando… Se descarta la descarga que estaba a medias.')

    def _on_install_error(self, error):
        self._cancel = None
        self._report(f'✗ {error}', 'err')
        self._scan_drive()

    def _on_installed(self, path, done, failed, cancelled):
        """Informa del resultado de oscwii.install() y vuelve a mirar qué hay en la unidad."""
        self._cancel = None
        for app in done:
            self._log.append(f"✓ Descargada {app.get('name', app['slug'])} {app.get('version', '')} "
                             f"→ apps/{app['slug']}", 'ok')
        for app, error in failed:
            self._log.append(f"✗ No se pudo descargar {app.get('name', app['slug'])}: {error}", 'err')
        parts = [f'{len(done)} app(s) descargadas en {path}']
        if failed:
            parts.append(f'{len(failed)} con errores; detalles en el terminal')
        text = ' · '.join(parts)
        if cancelled:
            self._report(f'Descarga cancelada: {text}', 'err' if failed else '')
        else:
            self._report(('✗ ' if failed else '✓ ') + text, 'err' if failed else 'ok')
        self._scan_drive()

    # ── Eliminar de la unidad ────────────────────────────────────
    def _on_table_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self._confirm_remove()
            return True
        return False

    def _confirm_remove(self):
        """Pide confirmación antes de eliminar de la unidad las apps seleccionadas que estén instaladas."""
        items = [i for i in self._selected_items() if i.local]
        if not items or self._busy:
            return
        count = '1 app' if len(items) == 1 else f'{len(items)} apps'
        dialog = Adw.AlertDialog(
            heading=f'¿Eliminar {count} de la unidad?',
            body=(f'{self._name_list(items)}\n\n'
                  f"Se liberarán {format_size(sum(i.local['size'] for i in items))}. Se borra la carpeta de cada app "
                  'dentro de apps/; las partidas y los datos que guarde en otras carpetas de la unidad se conservan. '
                  'Esta acción no se puede deshacer.'))
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('remove', 'Eliminar')
        dialog.set_response_appearance('remove', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_remove_response, [i.local for i in items], self._get_device_path())
        dialog.present(self.get_root())

    def _on_remove_response(self, _dialog, response, apps, path):
        if response != 'remove' or self._busy:
            return
        self._removing = True
        self._report(f'Eliminando {len(apps)} app(s) de {path}…', 'info')
        run_async(hbc_apps.remove_apps, path, apps,
                  on_done=self._on_removed, on_error=lambda e: self._on_removed([], error=e))

    def _on_removed(self, results, error=None):
        """Informa del resultado de hbc_apps.remove_apps() y vuelve a mirar qué hay en la unidad."""
        self._removing = False
        failed = 0
        for app, app_error in results:
            name = f"{app['name']} (apps/{app['folder']})"
            if app_error:
                failed += 1
                self._log.append(f'✗ No se pudo eliminar {name}: {app_error}', 'err')
            else:
                self._log.append(f'✓ Eliminada {name}', 'ok')
        if error:
            self._report(f'✗ Error al eliminar: {error}', 'err')
        elif failed:
            self._report(f'✗ No se pudieron eliminar {failed} de {len(results)} app(s); detalles en el terminal', 'err')
        else:
            self._report(f'✓ {len(results)} app(s) eliminadas', 'ok')
        self._selection.unselect_all()
        self._scan_drive()
