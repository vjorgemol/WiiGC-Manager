"""
Panel de registro/terminal, equivalente al <div class="term-output"> con
spans .ok/.err/.info/.cmd del frontend web.
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

_TAG_COLORS = {
    'ok':   '#3ecf8e',
    'err':  '#f26b6b',
    'info': '#6ea8e8',
    'cmd':  '#c084fc',
}


class LogView(Gtk.ScrolledWindow):
    """TextView de solo lectura con colores por etiqueta (ok/err/info/cmd)."""

    def __init__(self):
        super().__init__()
        self.set_vexpand(True)
        self.set_min_content_height(160)
        self.add_css_class('card')

        self._buffer = Gtk.TextBuffer()
        for name, color in _TAG_COLORS.items():
            self._buffer.create_tag(name, foreground=color)

        self._view = Gtk.TextView(buffer=self._buffer)
        self._view.set_editable(False)
        self._view.set_cursor_visible(False)
        self._view.set_monospace(True)
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_top_margin(8)
        self._view.set_bottom_margin(8)
        self._view.set_left_margin(10)
        self._view.set_right_margin(10)
        self.set_child(self._view)

    def append(self, text, tag=None):
        """Añade una línea de texto, opcionalmente coloreada por tag (ok/err/info/cmd)."""
        end = self._buffer.get_end_iter()
        if not text.endswith('\n'):
            text += '\n'
        if tag and tag in _TAG_COLORS:
            self._buffer.insert_with_tags_by_name(end, text, tag)
        else:
            self._buffer.insert(end, text)
        self._scroll_to_end()

    def clear(self):
        self._buffer.set_text('')

    def _scroll_to_end(self):
        end_mark = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
        self._view.scroll_mark_onscreen(end_mark)
