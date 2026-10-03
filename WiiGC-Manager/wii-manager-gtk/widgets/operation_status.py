"""
Fila de estado para operaciones largas (verificar, convertir…): spinner,
texto, barra de progreso con porcentaje y, al terminar, el resultado en
verde o rojo. Existe porque el terminal de resultados puede estar oculto:
sin ella, una tarea de varios minutos no daría ninguna señal de vida.
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib

from backend import core


class OperationStatus(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.busy = False
        self._timer = 0

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._spinner = Gtk.Spinner()
        self._label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        row.append(self._spinner)
        row.append(self._label)
        self.append(row)

        self._bar = Gtk.ProgressBar(show_text=True)
        self.append(self._bar)

    def start(self, text):
        """Empieza una operación: muestra el texto y la barra, y sigue el avance del backend."""
        self.busy = True
        self._set_text(text, None)
        self._spinner.set_visible(True)
        self._spinner.set_spinning(True)
        self._bar.set_fraction(0)
        self._bar.set_text('0 %')
        self._bar.set_visible(True)
        self.set_visible(True)
        if not self._timer:
            self._timer = GLib.timeout_add(300, self._poll)

    def finish(self, text, ok):
        """Termina la operación y deja a la vista el resultado (ok=None: ni éxito ni error)."""
        self.busy = False
        self._spinner.set_spinning(False)
        self._spinner.set_visible(False)
        self._bar.set_visible(False)
        self._set_text(text, None if ok is None else 'success' if ok else 'error')

    def _set_text(self, text, css_class):
        self._label.set_label(text)
        for css in ('success', 'error'):
            self._label.remove_css_class(css)
        if css_class:
            self._label.add_css_class(css_class)

    def _poll(self):
        if not self.busy:
            self._timer = 0
            return GLib.SOURCE_REMOVE
        progress = core.api_progress({})  # solo lee contadores del sistema: no bloquea
        if progress['active'] and progress['percent'] is not None:
            self._bar.set_fraction(progress['percent'] / 100)
            self._bar.set_text(f"{progress['percent']} %" + (f" — {progress['step']}" if progress['step'] else ''))
        else:
            # Tarea cuyo avance no se puede medir: barra de actividad
            self._bar.set_text('En curso…')
            self._bar.pulse()
        return GLib.SOURCE_CONTINUE
