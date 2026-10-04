#!/usr/bin/env python3
"""
Punto de entrada de WiiGC Manager GTK4.
App de escritorio nativa para Fedora/GNOME: sustituye al cliente web
(wii-manager.html) llamando directamente a las funciones de
wii-manager-server.py en el mismo proceso, sin servidor HTTP.
"""
import sys
from pathlib import Path

# Los módulos de la app (window, pages, widgets…) se importan por su nombre, sin paquete
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gtk

from window import MainWindow

APP_ID = 'org.wiigcmanager.Gtk'

# Índigo de la GameCube original, para los botones de añadir juegos GameCube.
# Se combina con .suggested-action, que ya aporta los estados hover/pulsado.
CSS = """
button.suggested-action.gamecube {
    background-color: #6a5fbb;
    color: white;
}
"""


class WiiManagerApp(Adw.Application):
    """
    Aplicación de instancia única: si ya hay una en marcha, un segundo lanzamiento
    solo trae su ventana al frente.
    """

    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_startup(self):
        """Se ejecuta una vez por proceso: registra el CSS propio de la app."""
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        """Se ejecuta en cada lanzamiento: crea la ventana o presenta la que ya existe."""
        win = self.props.active_window
        if not win:
            win = MainWindow(self)
        win.present()


def main():
    app = WiiManagerApp()
    return app.run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
