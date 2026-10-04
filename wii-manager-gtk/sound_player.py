"""
Reproductor de los sonidos de la Videoteca (banner de Wii y arranque de
GameCube). Usa directamente el «playbin» de GStreamer, solo con audio:
Gtk.MediaFile reproduce con playbin3, que aborta el programa entero con
algunos MP3 (fallo de GStreamer en decodebin3).
"""
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)

_AUDIO_ONLY = 0x2  # GstPlayFlags: sin vídeo (carátula incrustada en un MP3), texto ni visualización


class SoundPlayer:
    """Reproduce un archivo de audio cada vez; play() corta el que estuviera sonando."""

    def __init__(self):
        self._playbin = Gst.ElementFactory.make('playbin')  # None si falta el complemento de GStreamer
        if self._playbin:
            self._playbin.set_property('flags', _AUDIO_ONLY)

    def play(self, path):
        if not self._playbin:
            return
        self._playbin.set_state(Gst.State.NULL)
        self._playbin.set_property('uri', Gst.filename_to_uri(path))
        self._playbin.set_state(Gst.State.PLAYING)

    def stop(self):
        if self._playbin:
            self._playbin.set_state(Gst.State.NULL)
