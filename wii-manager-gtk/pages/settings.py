"""
Página Ajustes: rutas de wit/wwt/WFS, preferencias de carátulas de GameTDB
y visibilidad del terminal de resultados. Equivalente a view-settings +
loadSettings()/saveSettings()/detectTool() del frontend web, persistido en
disco por settings_store (no hay localStorage).
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from backend import core, run_async

# Códigos de GameTDB y sus textos en los desplegables (mismo orden en cada pareja de listas)
COVER_REGIONS = ['ES', 'EN', 'DE', 'FR', 'IT', 'PT', 'US', 'JA']
COVER_REGION_LABELS = ['España (ES)', 'English (EN)', 'Deutschland (DE)', 'France (FR)',
                        'Italia (IT)', 'Portugal (PT)', 'USA (US)', 'Japan (JA)']
COVER_TYPES = ['cover3D', 'cover', 'disc', 'coverfull']
COVER_TYPE_LABELS = ['3D Cover', 'Cover plano', 'Disco', 'Cover completo']


class SettingsPage(Gtk.Box):
    """
    settings es el dict de MainWindow y se modifica en el sitio; cada cambio se
    notifica por su callback, que recibe ese mismo dict.
    """

    def __init__(self, settings: dict, on_save=None, on_toggle_terminal=None, on_cover_prefs_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._settings = settings
        self._on_save = on_save
        self._on_toggle_terminal = on_toggle_terminal
        self._on_cover_prefs_changed = on_cover_prefs_changed

        # Gtk.Box en vez de Adw.PreferencesPage: PreferencesPage añade un
        # padding vertical pensado para ocupar toda una ventana de diálogo,
        # lo que aquí (una pestaña más dentro de un Gtk.Stack) hacía que la
        # página se viera innecesariamente alargada.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.append(self._build_terminal_group())
        content.append(self._build_tools_group())
        content.append(self._build_covers_group())

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(content)
        self.append(scroller)

    # ── Terminal de resultados ──────────────────────────────────────
    def _build_terminal_group(self):
        group = Adw.PreferencesGroup(title='Terminal')

        row = Adw.SwitchRow(
            title='Mostrar terminal de resultados',
            subtitle='Abre un panel en la parte inferior con la salida de los comandos (wit/wwt, formateo, copias…)')
        row.set_active(bool(self._settings.get('show_terminal', False)))
        row.connect('notify::active', self._on_terminal_toggled)
        group.add(row)
        return group

    def _on_terminal_toggled(self, row, _pspec):
        self._settings['show_terminal'] = row.get_active()
        if self._on_toggle_terminal:
            self._on_toggle_terminal(self._settings)

    # ── Rutas de herramientas ────────────────────────────────────
    def _build_tools_group(self):
        group = Adw.PreferencesGroup(title='Rutas de herramientas')
        save_btn = Gtk.Button(label='Guardar', valign=Gtk.Align.CENTER, css_classes=['suggested-action'])
        save_btn.connect('clicked', lambda *_: self._save())
        group.set_header_suffix(save_btn)

        self._wit_row = Adw.EntryRow(title='Ruta de wit')
        self._wit_row.set_text(self._settings.get('wit_path', ''))
        wit_detect = Gtk.Button(label='Detectar', valign=Gtk.Align.CENTER)
        wit_detect.connect('clicked', lambda *_: self._detect_tool('wit', self._wit_row))
        self._wit_row.add_suffix(wit_detect)
        group.add(self._wit_row)

        self._wwt_row = Adw.EntryRow(title='Ruta de wwt')
        self._wwt_row.set_text(self._settings.get('wwt_path', ''))
        wwt_detect = Gtk.Button(label='Detectar', valign=Gtk.Align.CENTER)
        wwt_detect.connect('clicked', lambda *_: self._detect_tool('wwt', self._wwt_row))
        self._wwt_row.add_suffix(wwt_detect)
        group.add(self._wwt_row)

        self._wfs_row = Adw.EntryRow(title='Ruta WFS / USB')
        self._wfs_row.set_text(self._settings.get('wfs_path', ''))
        group.add(self._wfs_row)
        return group

    def _detect_tool(self, tool, row):
        """Busca la herramienta en el sistema (core.api_status) y, si aparece, pone su ruta en la fila."""
        def on_done(data):
            path = data.get(tool)
            if path:
                row.set_text(path)

        run_async(core.api_status, {}, on_done=on_done)

    # ── Carátulas ────────────────────────────────────────────────
    # Se aplican al instante (notify::selected), no dependen del botón
    # "Guardar" de arriba — si no, cambiar el tipo/región aquí "no hacía
    # nada" hasta acordarse de pulsar un botón en un grupo distinto.
    def _build_covers_group(self):
        group = Adw.PreferencesGroup(title='Carátulas (GameTDB)',
                                      description='https://art.gametdb.com/wii/[tipo]/[región]/[ID].png')

        self._region_row = Adw.ComboRow(title='Región preferida')
        self._region_row.set_model(Gtk.StringList.new(COVER_REGION_LABELS))
        region = self._settings.get('cover_region', 'ES')
        self._region_row.set_selected(COVER_REGIONS.index(region) if region in COVER_REGIONS else 0)
        self._region_row.connect('notify::selected', self._on_cover_pref_changed)
        group.add(self._region_row)

        self._type_row = Adw.ComboRow(title='Tipo de carátula')
        self._type_row.set_model(Gtk.StringList.new(COVER_TYPE_LABELS))
        ctype = self._settings.get('cover_type', 'cover3D')
        self._type_row.set_selected(COVER_TYPES.index(ctype) if ctype in COVER_TYPES else 0)
        self._type_row.connect('notify::selected', self._on_cover_pref_changed)
        group.add(self._type_row)
        return group

    def _on_cover_pref_changed(self, _row, _pspec):
        self._settings['cover_region'] = COVER_REGIONS[self._region_row.get_selected()]
        self._settings['cover_type'] = COVER_TYPES[self._type_row.get_selected()]
        if self._on_cover_prefs_changed:
            self._on_cover_prefs_changed(self._settings)

    # ── Guardar (rutas de herramientas) ───────────────────────────
    def _save(self):
        self._settings['wit_path'] = self._wit_row.get_text()
        self._settings['wwt_path'] = self._wwt_row.get_text()
        self._settings['wfs_path'] = self._wfs_row.get_text()
        if self._on_save:
            self._on_save(self._settings)
