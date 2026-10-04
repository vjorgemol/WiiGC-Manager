"""
Fila de ruta de solo lectura, usada en Convertir y Verificar.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

BUTTON_WIDTH = 64


class PathRow(Adw.ActionRow):
    """
    Fila con una ruta que no se escribe a mano: solo se elige con su botón.
    Tiene get_text()/set_text() como Adw.EntryRow, para los selectores de archivos.
    """

    def __init__(self, title, on_browse, placeholder='', clearable=False, on_cleared=None):
        super().__init__(title=title, use_markup=False, css_classes=['property'])
        self._placeholder = placeholder
        self._on_cleared = on_cleared
        self._clear_btn = self._icon_button('edit-clear-symbolic', 'Quitar', css_classes=['flat'], visible=False)
        self._clear_btn.connect('clicked', lambda *_: self._clear())
        if clearable:
            self.add_suffix(self._clear_btn)
        browse_btn = self._icon_button('folder-open-symbolic', 'Elegir…')
        browse_btn.connect('clicked', lambda *_: on_browse(self))
        self.add_suffix(browse_btn)
        self.set_text('')

    @staticmethod
    def _icon_button(icon_name, tooltip, **props):
        """Botón de icono más alargado que el de serie (que queda pequeño en la fila)."""
        button = Gtk.Button(icon_name=icon_name, valign=Gtk.Align.CENTER, tooltip_text=tooltip, **props)
        button.set_size_request(BUTTON_WIDTH, -1)
        return button

    def _clear(self):
        self.set_text('')
        if self._on_cleared:
            self._on_cleared()

    def get_text(self):
        return self._path

    def set_text(self, path):
        self._path = path or ''
        self.set_subtitle(self._path or self._placeholder)
        self._clear_btn.set_visible(bool(self._path))
