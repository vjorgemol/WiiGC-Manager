"""
Imagen que gira sobre su centro: la carátula de tipo «Disco» de la Videoteca,
que da vueltas como el disco en el menú de la consola. Gtk.Picture no se puede
heredar, así que el giro va en el Gdk.Paintable que se le pasa.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Graphene', '1.0')
from gi.repository import Gdk, GObject, Graphene


class SpinningPaintable(GObject.Object, Gdk.Paintable):
    """Envuelve una textura y la dibuja girada angle grados (ver set_angle)."""

    def __init__(self, texture):
        super().__init__()
        self._texture = texture
        self._angle = 0.0

    def set_angle(self, angle):
        self._angle = angle % 360
        self.invalidate_contents()

    def do_snapshot(self, snapshot, width, height):
        center = Graphene.Point().init(width / 2, height / 2)
        snapshot.save()
        snapshot.translate(center)
        snapshot.rotate(self._angle)
        snapshot.translate(Graphene.Point().init(-width / 2, -height / 2))
        self._texture.snapshot(snapshot, width, height)
        snapshot.restore()

    def do_get_intrinsic_width(self):
        return self._texture.get_intrinsic_width()

    def do_get_intrinsic_height(self):
        return self._texture.get_intrinsic_height()

    def do_get_intrinsic_aspect_ratio(self):
        return self._texture.get_intrinsic_aspect_ratio()
