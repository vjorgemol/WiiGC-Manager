"""
Guardar e imprimir la carátula del juego seleccionado en la Videoteca.

La carátula completa (portada + lomo + contraportada) se imprime a tamaño
real de funda de DVD, que es también el de las cajas de Wii y GameCube:
273 × 183 mm, apaisada y centrada en la hoja, con marcas de corte.
"""
import io

import cairo
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio

INSERT_WIDTH_MM = 273
INSERT_HEIGHT_MM = 183
# Carátulas completas de GameTDB, por orden de preferencia: alta resolución (1024×680) y normal (512×340)
PRINT_COVER_TYPES = ['coverfullHQ', 'coverfull']

# Marcas de corte: separación respecto a la esquina de la imagen y longitud del trazo
_MARK_GAP_MM = 2
_MARK_LENGTH_MM = 5


def is_full_cover(texture):
    """
    True si la textura es una carátula completa. Se mira la imagen y no el
    tipo elegido en Ajustes: si GameTDB no tiene la completa, cover_loader
    devuelve otra (portada, disco…), y esas no son apaisadas.
    """
    return texture is not None and texture.get_width() > texture.get_height() * 1.2


def save_cover(parent, texture, name, log):
    """Pide dónde guardar la carátula (PNG) y la escribe; el resultado se anota en log."""
    dialog = Gtk.FileDialog(title='Guardar carátula', initial_name=f"{name.replace('/', '-')}.png")

    def on_response(dlg, result):
        try:
            f = dlg.save_finish(result)
        except Exception:
            return  # cancelado
        try:
            f.replace_contents(texture.save_to_png_bytes().get_data(), None, False,
                               Gio.FileCreateFlags.REPLACE_DESTINATION, None)
        except Exception as e:
            log.append(f'✗ No se pudo guardar la carátula: {e}', 'err')
            return
        log.append(f'✓ Carátula guardada en {f.get_path()}', 'ok')

    dialog.save(parent, None, on_response)


def _draw_page(_op, context, _page_nr, surface, log):
    """Dibuja la carátula centrada en la hoja, a tamaño real (o reducida si no cabe), con sus marcas de corte."""
    cr = context.get_cairo_context()
    page_w, page_h = context.get_width(), context.get_height()  # en mm, hoja completa
    scale = min(1, page_w / INSERT_WIDTH_MM, page_h / INSERT_HEIGHT_MM)
    if scale < 1:
        log.append(f'⚠ La hoja es más pequeña que la carátula ({INSERT_WIDTH_MM} × {INSERT_HEIGHT_MM} mm): '
                   f'se imprime reducida al {scale:.0%}', 'err')
    w, h = INSERT_WIDTH_MM * scale, INSERT_HEIGHT_MM * scale
    x, y = (page_w - w) / 2, (page_h - h) / 2

    cr.save()
    cr.translate(x, y)
    cr.scale(w / surface.get_width(), h / surface.get_height())
    cr.set_source_surface(surface, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BEST)
    cr.paint()
    cr.restore()

    # Marcas de corte en las cuatro esquinas, por fuera de la imagen
    cr.set_source_rgb(0, 0, 0)
    cr.set_line_width(0.1)
    for cx, dx in ((x, -1), (x + w, 1)):
        for cy, dy in ((y, -1), (y + h, 1)):
            cr.move_to(cx + dx * _MARK_GAP_MM, cy)
            cr.rel_line_to(dx * _MARK_LENGTH_MM, 0)
            cr.move_to(cx, cy + dy * _MARK_GAP_MM)
            cr.rel_line_to(0, dy * _MARK_LENGTH_MM)
    cr.stroke()


def print_cover(parent, texture, name, log, export_path=None):
    """Imprime la carátula completa a tamaño de funda de DVD. export_path: a PDF, sin diálogo."""
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(texture.save_to_png_bytes().get_data()))

    page_setup = Gtk.PageSetup()
    page_setup.set_orientation(Gtk.PageOrientation.LANDSCAPE)

    op = Gtk.PrintOperation(n_pages=1, unit=Gtk.Unit.MM, use_full_page=True,
                            job_name=f'Carátula {name}', default_page_setup=page_setup)
    op.connect('draw-page', _draw_page, surface, log)
    action = Gtk.PrintOperationAction.PRINT_DIALOG
    if export_path:
        op.set_export_filename(export_path)
        action = Gtk.PrintOperationAction.EXPORT
    try:
        result = op.run(action, parent)
    except Exception as e:
        log.append(f'✗ No se pudo imprimir la carátula: {e}', 'err')
        return
    if result == Gtk.PrintOperationResult.ERROR:
        log.append('✗ No se pudo imprimir la carátula', 'err')
    elif result == Gtk.PrintOperationResult.CANCEL:
        log.append('Impresión cancelada')
    elif result == Gtk.PrintOperationResult.APPLY:
        log.append(f'✓ Carátula de {name} enviada a imprimir '
                   f'({INSERT_WIDTH_MM} × {INSERT_HEIGHT_MM} mm)', 'ok')
