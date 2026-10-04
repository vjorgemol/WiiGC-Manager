"""
Ventana principal. Usa Adw.NavigationSplitView (el mismo patrón de
Configuración/Archivos de GNOME): panel lateral + contenido, con divisor
redimensionable de verdad y colapso adaptativo en ventanas estrechas —
sustituye al <aside>+<main>+showView() manual del frontend web.
"""
import os
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib

import about
import settings_store
from backend import core, run_async
from pages.library import LibraryPage
from pages.format import FormatPage
from pages.convert import ConvertPage
from pages.migrate import MigratePage
from pages.verify import VerifyPage
from pages.settings import SettingsPage
from widgets.log_view import LogView

# Vistas del panel lateral: (nombre de la página en el Gtk.Stack, icono, texto)
VIEWS = [
    ('library', 'view-list-symbolic', 'Videoteca'),
    ('format', 'drive-harddisk-symbolic', 'Formatear / Preparar'),
    ('convert', 'edit-copy-symbolic', 'Convertir'),
    ('migrate', 'drive-multidisk-symbolic', 'Migrar'),
    ('verify', 'object-select-symbolic', 'Verificar'),
    ('settings', 'preferences-system-symbolic', 'Ajustes'),
]

# Filtros de la Videoteca: (nombre que entiende LibraryPage.apply_filter, icono, texto)
FILTERS = [
    ('all', 'view-list-symbolic', 'Todos los juegos'),
    ('wii', 'applications-games-symbolic', 'Juegos Wii'),
    ('gc', 'input-gaming-symbolic', 'Juegos GameCube'),
    ('wbfs', 'folder-symbolic', 'Formato WBFS'),
    ('iso', 'media-optical-symbolic', 'Formato ISO / GCM'),
]

# Ancho máximo del contenido: en ventanas muy anchas queda centrado en vez de estirarse
CLAMP_WIDTH = 1100


class MainWindow(Adw.ApplicationWindow):
    """
    Crea las páginas y las conecta entre sí: todas comparten el terminal de
    resultados (LogView) y la ruta de la unidad explorada, que vive en el campo
    del panel lateral (ver _get_device_path).
    """

    def __init__(self, app):
        super().__init__(application=app, title='WiiGC Manager',
                          default_width=1180, default_height=760, width_request=360, height_request=480)

        self.settings = settings_store.load()
        self.terminal = LogView()

        sidebar_page = self._build_sidebar_page()

        self.library_page = LibraryPage(log=self.terminal, get_cover_prefs=self._get_cover_prefs,
                                        sound_enabled=bool(self.settings.get('banner_sound', True)),
                                        on_sound_toggled=self._on_sound_toggled)
        self.format_page = FormatPage(log=self.terminal, on_formatted=self._on_drive_formatted,
                                      get_device_path=self._get_device_path)
        self.convert_page = ConvertPage(get_device_path=self._get_device_path, log=self.terminal)
        self.migrate_page = MigratePage(get_device_path=self._get_device_path, log=self.terminal,
                                        on_migrated=self._on_migrated)
        self.verify_page = VerifyPage(get_device_path=self._get_device_path, log=self.terminal,
                                      get_games=self.library_page.get_games)
        self.settings_page = SettingsPage(self.settings, on_save=self._on_settings_saved,
                                           on_toggle_terminal=self._on_toggle_terminal,
                                           on_cover_prefs_changed=self._on_cover_prefs_changed)

        content_page = self._build_content_page()

        split_view = Adw.NavigationSplitView(sidebar=sidebar_page, content=content_page)
        split_view.set_min_sidebar_width(230)
        split_view.set_max_sidebar_width(340)
        split_view.set_sidebar_width_fraction(0.22)

        self.set_content(split_view)
        self.connect('close-request', self._on_close_request)
        self._select_view('library')
        # Autoselección de la unidad USB: al arrancar y en caliente, cada vez
        # que se conecta, monta, desmonta o retira una unidad.
        self._usb_state = None      # última situación vista (para no repetir avisos/exploraciones)
        self._usb_auto_path = ''    # ruta que puso la autoselección (para limpiarla al retirar la unidad)
        self._usb_check_id = 0
        self._volume_monitor = Gio.VolumeMonitor.get()
        for signal in ('drive-connected', 'drive-disconnected', 'volume-added', 'volume-removed',
                       'mount-added', 'mount-removed'):
            self._volume_monitor.connect(signal, self._schedule_usb_check)
        # Esas señales no bastan: una partición WBFS no tiene volumen ni montaje,
        # y dentro del Flatpak el monitor no ve las unidades. Se vigila además
        # la lista de dispositivos de bloques y de montajes del sistema.
        self._block_state = self._read_block_state()
        GLib.timeout_add_seconds(2, self._poll_block_devices)
        self._check_usb_drives()

    # ── Panel lateral (sidebar) ─────────────────────────────────────
    def _build_sidebar_page(self):
        """Panel lateral: lista de vistas, filtros de la Videoteca y, abajo, la ruta de la unidad a explorar."""
        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar(show_end_title_buttons=False)
        header.set_title_widget(Gtk.Label(label='WiiGC Manager', css_classes=['title']))
        self._theme_btn = Gtk.Button()
        Adw.StyleManager.get_default().connect('notify::dark', self._sync_theme_icon)
        self._sync_theme_icon()
        self._theme_btn.set_tooltip_text('Cambiar tema claro/oscuro')
        self._theme_btn.connect('clicked', self._on_toggle_theme)
        header.pack_end(self._theme_btn)
        about_btn = Gtk.Button(icon_name='help-about-symbolic', tooltip_text='Acerca de WiiGC Manager')
        about_btn.connect('clicked', lambda *_: about.present(self))
        header.pack_start(about_btn)
        toolbar_view.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.set_margin_top(8)
        body.set_margin_bottom(8)
        body.set_margin_start(8)
        body.set_margin_end(8)

        body.append(self._section_label('Vistas'))
        views_list = Gtk.ListBox()
        views_list.add_css_class('navigation-sidebar')
        views_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for name, icon, label in VIEWS:
            views_list.append(self._nav_row(icon, label, name))
        views_list.connect('row-selected', self._on_view_row_selected)
        body.append(views_list)
        self._views_list = views_list

        self._filters_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._filters_box.append(self._section_label('Filtros'))
        filters_list = Gtk.ListBox()
        filters_list.add_css_class('navigation-sidebar')
        filters_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for name, icon, label in FILTERS:
            filters_list.append(self._nav_row(icon, label, name))
        filters_list.connect('row-selected', self._on_filter_row_selected)
        self._filters_box.append(filters_list)
        body.append(self._filters_box)

        body.append(Gtk.Box(vexpand=True))  # empuja lo siguiente al fondo

        body.append(self._section_label('Ruta WFS / USB / FAT32'))
        path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._device_path_entry = Gtk.Entry(placeholder_text='/dev/sdb1 o /media/usb', hexpand=True)
        self._device_path_entry.set_text(self.settings.get('wfs_path', ''))
        self._device_path_entry.connect('activate', self._on_scan_device)
        browse_btn = Gtk.Button(icon_name='folder-open-symbolic')
        browse_btn.set_tooltip_text('Examinar carpeta o unidad')
        browse_btn.connect('clicked', self._on_browse_device_path)
        path_row.append(self._device_path_entry)
        path_row.append(browse_btn)
        body.append(path_row)

        scan_btn = Gtk.Button(label='⊙ Explorar dispositivo')
        scan_btn.add_css_class('flat')
        scan_btn.connect('clicked', self._on_scan_device)
        scan_btn.set_margin_top(6)
        body.append(scan_btn)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(body)
        toolbar_view.set_content(scroller)

        return Adw.NavigationPage(title='WiiGC Manager', child=toolbar_view)

    @staticmethod
    def _section_label(text):
        """Título de sección del panel lateral."""
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class('caption-heading')
        label.add_css_class('dim-label')
        label.set_margin_top(8)
        return label

    @staticmethod
    def _nav_row(icon_name, label, name):
        """Fila de navegación (icono + texto). name identifica la vista o el filtro al seleccionarla."""
        row = Gtk.ListBoxRow()
        row.set_name(name)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.append(Gtk.Image.new_from_icon_name(icon_name))
        box.append(Gtk.Label(label=label, xalign=0))
        row.set_child(box)
        return row

    def _on_view_row_selected(self, _listbox, row):
        if row is not None:
            self._select_view(row.get_name())

    def _on_filter_row_selected(self, _listbox, row):
        if row is not None:
            self.library_page.apply_filter(row.get_name())

    def _on_toggle_theme(self, _btn):
        # get_dark() es el tema efectivo: con el esquema DEFAULT (el inicial) sigue al del SO
        mgr = Adw.StyleManager.get_default()
        mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT if mgr.get_dark() else Adw.ColorScheme.FORCE_DARK)

    def _sync_theme_icon(self, *_):
        dark = Adw.StyleManager.get_default().get_dark()
        self._theme_btn.set_icon_name('weather-clear-symbolic' if dark else 'weather-clear-night-symbolic')

    def _on_browse_device_path(self, _btn):
        """Elegir con el explorador de archivos la carpeta o unidad a explorar."""
        dialog = Gtk.FileDialog()
        dialog.select_folder(self, None, self._on_device_path_chosen)

    def _on_device_path_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder:
            self._device_path_entry.set_text(folder.get_path())
            self._on_scan_device()

    def _on_close_request(self, _window):
        # Cerrar la app en mitad de una copia la interrumpe y deja el juego a medias
        if not (self.library_page.is_busy() or self.migrate_page.is_busy()):
            return False
        dialog = Adw.AlertDialog(
            heading='Hay una operación en curso',
            body='Se está copiando o eliminando un juego en la unidad. Si cierras ahora, '
                 'la operación se interrumpirá y el juego quedará incompleto.')
        dialog.add_response('wait', 'Seguir esperando')
        dialog.add_response('close', 'Cerrar de todos modos')
        dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('wait')
        dialog.set_close_response('wait')
        dialog.connect('response', self._on_close_response)
        dialog.present(self)
        return True

    def _on_close_response(self, _dialog, response):
        if response == 'close':
            self.migrate_page.abort()
            self.destroy()

    def _schedule_usb_check(self, *_args):
        # Enchufar una unidad dispara varias señales seguidas (drive, volume,
        # mount): esperar a que se asiente antes de consultar lsblk.
        if self._usb_check_id:
            GLib.source_remove(self._usb_check_id)
        self._usb_check_id = GLib.timeout_add(1000, self._check_usb_drives)

    @staticmethod
    def _read_block_state():
        """Dispositivos de bloques y montajes actuales (lectura barata, sin lanzar lsblk)."""
        state = []
        for path, read in (('/sys/class/block', lambda p: sorted(os.listdir(p))),
                           ('/proc/self/mounts', lambda p: Path(p).read_text())):
            try:
                state.append(read(path))
            except OSError:
                state.append(None)
        return state

    def _poll_block_devices(self):
        """Cada 2 s: si se ha conectado, retirado, montado o desmontado algo, revisar las unidades USB."""
        state = self._read_block_state()
        if state != self._block_state:
            self._block_state = state
            self._schedule_usb_check()
        return GLib.SOURCE_CONTINUE

    def _check_usb_drives(self):
        """Consulta en un hilo las unidades conectadas y aplica la autoselección."""
        self._usb_check_id = 0
        run_async(core.api_devices, {}, on_done=self._autoselect_usb_drive)
        return GLib.SOURCE_REMOVE

    def _autoselect_usb_drive(self, data):
        """Si hay exactamente una unidad USB conectada, usarla y explorarla."""
        self.migrate_page.set_devices(data.get('devices', []))
        drives = [d for d in data.get('devices', []) if d.get('transport') == 'usb' and not d.get('is_system')]
        state = [(d.get('path'), [(v.get('path'), v.get('mountpoint')) for v in (d.get('partitions') or [d])])
                 for d in drives]
        if state == self._usb_state:
            return
        self._usb_state = state

        def usable_paths(drive):
            # Una unidad montada (FAT32/NTFS) se explora por su punto de montaje;
            # una partición WBFS no se monta y se explora por su dispositivo.
            paths = [v.get('mountpoint') or (v['path'] if v.get('fstype') == 'wbfs' else '')
                     for v in (drive.get('partitions') or [drive])]
            return [p for p in paths if p]

        if self._usb_auto_path and not any(self._usb_auto_path in usable_paths(d) for d in drives):
            # Se ha retirado (o desmontado) la unidad que se había autoseleccionado
            if self._get_device_path() == self._usb_auto_path:
                self.terminal.append('Unidad USB desconectada.', 'info')
                self._device_path_entry.set_text('')
                self.library_page.clear()
            self._usb_auto_path = ''
        if len(drives) != 1:
            return
        drive = drives[0]
        paths = usable_paths(drive)
        if paths == [self._usb_auto_path]:
            return  # sigue siendo la unidad ya explorada
        name = drive.get('model') or drive.get('path')
        if len(paths) != 1:
            if paths:
                reason = 'tiene varias particiones utilizables.'
            elif any(v.get('unreadable') for v in (drive.get('partitions') or [drive])):
                # Sin permiso de lectura no se puede saber si es una partición WBFS
                reason = ('no está montada y no hay permiso para leerla. Si es una partición WBFS, añade tu '
                          'usuario al grupo «disk» (sudo usermod -aG disk $USER) y vuelve a iniciar sesión.')
            else:
                reason = 'no está montada.'
            self.terminal.append(
                f'⚠ Unidad USB detectada ({name}), pero no se pudo elegir automáticamente: {reason}', 'info')
            return
        self.terminal.append(f'Unidad USB detectada: {name} → {paths[0]}', 'info')
        self._usb_auto_path = paths[0]
        self._device_path_entry.set_text(paths[0])
        self._on_scan_device()

    def _on_drive_formatted(self, path):
        """Tras formatear: usar la unidad recién preparada y recargar la Videoteca."""
        if not path:
            return
        self._usb_auto_path = path
        self._device_path_entry.set_text(path)
        self._on_scan_device()

    def _on_migrated(self):
        """Tras migrar: la unidad explorada puede haber ganado o perdido juegos."""
        if self._get_device_path():
            self._on_scan_device()

    def _on_scan_device(self, *_args):
        """Explora la ruta del panel lateral y carga sus juegos en la Videoteca."""
        self.library_page.load_games(self._get_device_path())

    def _get_device_path(self):
        """Ruta de la unidad explorada: punto de montaje, partición WBFS (/dev/…) o vacío (detección automática)."""
        return self._device_path_entry.get_text().strip()

    def _on_cover_prefs_changed(self, settings):
        """Ajustes → Carátulas: guardar y volver a pedir la carátula que está a la vista."""
        settings_store.save(settings)
        self.library_page.reload_cover()

    def _on_sound_toggled(self, enabled):
        """Videoteca → botón del altavoz: recordar si se quiere el sonido de los juegos."""
        self.settings['banner_sound'] = enabled
        settings_store.save(self.settings)

    def _get_cover_prefs(self):
        """(región, tipo) de carátula elegidos en Ajustes."""
        return self.settings.get('cover_region', 'ES'), self.settings.get('cover_type', 'cover3D')

    def _on_settings_saved(self, settings):
        """Ajustes → Guardar: persistir y llevar la ruta WFS/USB al panel lateral."""
        settings_store.save(settings)
        self._device_path_entry.set_text(settings.get('wfs_path', ''))

    # ── Terminal de resultados (compartido por todas las páginas) ──
    def _build_terminal_panel(self):
        """Panel inferior: cabecera con el botón de limpiar y, debajo, el LogView."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class('background')

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(4)
        header.set_margin_bottom(4)
        header.set_margin_start(10)
        header.set_margin_end(6)
        header.append(Gtk.Image.new_from_icon_name('utilities-terminal-symbolic'))
        title = Gtk.Label(label='Terminal', xalign=0, hexpand=True)
        title.add_css_class('caption-heading')
        header.append(title)
        clear_btn = Gtk.Button(icon_name='edit-clear-all-symbolic', css_classes=['flat'])
        clear_btn.set_tooltip_text('Limpiar')
        clear_btn.connect('clicked', lambda *_: self.terminal.clear())
        header.append(clear_btn)
        box.append(header)
        box.append(Gtk.Separator())
        box.append(self.terminal)
        return box

    def _on_toggle_terminal(self, settings):
        """Ajustes → Mostrar terminal: guardar y mostrar u ocultar el panel inferior."""
        settings_store.save(settings)
        self._content_toolbar_view.set_reveal_bottom_bars(bool(settings.get('show_terminal', False)))

    # ── Contenido ────────────────────────────────────────────────
    def _build_content_page(self):
        """Zona de contenido: un Gtk.Stack con las seis páginas y, debajo, el terminal de resultados."""
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        stack = Gtk.Stack()
        stack.set_hexpand(True)
        stack.set_vexpand(True)
        # Por defecto Gtk.Stack es homogéneo: su tamaño MÍNIMO es el máximo
        # de TODAS las páginas combinadas, no solo la visible. Con 6 vistas
        # apiladas eso vuelve la ventana casi imposible de encoger.
        stack.set_hhomogeneous(False)
        stack.set_vhomogeneous(False)
        stack.add_named(self._clamp(self.library_page), 'library')
        # Formatear tiene muchas secciones: con scroll para no forzar una ventana muy alta
        format_scroller = Gtk.ScrolledWindow(child=self.format_page, hscrollbar_policy=Gtk.PolicyType.NEVER)
        stack.add_named(self._clamp(format_scroller), 'format')
        stack.add_named(self._clamp(self.convert_page), 'convert')
        stack.add_named(self._clamp(self.migrate_page), 'migrate')
        stack.add_named(self._clamp(self.verify_page), 'verify')
        stack.add_named(self._clamp(self.settings_page), 'settings')
        stack.set_margin_top(8)
        stack.set_margin_bottom(8)
        stack.set_margin_start(16)
        stack.set_margin_end(16)
        self._stack = stack
        toolbar_view.set_content(stack)

        toolbar_view.add_bottom_bar(self._build_terminal_panel())
        toolbar_view.set_reveal_bottom_bars(bool(self.settings.get('show_terminal', False)))
        self._content_toolbar_view = toolbar_view

        self._content_page = Adw.NavigationPage(title='Videoteca', child=toolbar_view)
        return self._content_page

    @staticmethod
    def _clamp(widget):
        """Limita el ancho de una página a CLAMP_WIDTH."""
        return Adw.Clamp(child=widget, maximum_size=CLAMP_WIDTH, tightening_threshold=800,
                          hexpand=True, vexpand=True)

    def _select_view(self, name):
        """Muestra la página name y refresca lo que en ella depende de la unidad explorada."""
        self._stack.set_visible_child_name(name)
        self._filters_box.set_visible(name == 'library')
        self._content_page.set_title(next(label for n, _i, label in VIEWS if n == name))
        if name == 'verify':
            self.verify_page.refresh_games()
        if name == 'migrate':
            self.migrate_page.refresh()
        if name == 'format':
            self.format_page.refresh_loaders()
