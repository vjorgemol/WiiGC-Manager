"""
Fila de dispositivo/partición para la página Formatear/Preparar.
Equivalente a renderDeviceCard() del frontend web.
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk


class DeviceRow(Gtk.Box):
    """
    Representa un disco o partición devuelto por get_block_devices().
    Si el dispositivo es seguro de formatear, es clicable y dispara
    on_select(device) al pulsarlo; si pertenece al sistema, se muestra
    bloqueado y no reacciona a clics.
    """

    def __init__(self, device: dict, on_select=None):
        super().__init__()
        self.device = device
        self.add_css_class('card')
        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.set_margin_start(4)
        self.set_margin_end(4)

        # Los márgenes de arriba separan una tarjeta de otra; estos separan
        # el contenido del borde de la tarjeta.
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, hexpand=True)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(12)
        content.set_margin_end(12)
        self.append(content)

        is_locked = device.get('is_system')
        is_safe = device.get('safe_to_format')
        is_removable = device.get('removable')
        is_part = device.get('type') == 'part'

        icon_name = '💾' if is_removable else ('▫' if is_part else '🖥')
        icon = Gtk.Label(label=icon_name)
        icon.set_size_request(32, 32)
        icon.add_css_class('title-1')
        content.append(icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        path_label = Gtk.Label(label=device.get('path', ''), xalign=0)
        path_label.add_css_class('monospace')
        title_box.append(path_label)
        if device.get('label'):
            lbl = Gtk.Label(label=f"“{device['label']}”", xalign=0)
            lbl.add_css_class('dim-label')
            title_box.append(lbl)
        info_box.append(title_box)

        meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        size_label = Gtk.Label(label=device.get('size_formatted', ''))
        size_label.add_css_class('heading')
        meta_box.append(size_label)

        if is_locked:
            meta_box.append(self._badge('🔒 SISTEMA', 'error'))
        if is_removable:
            meta_box.append(self._badge('USB / SD', 'accent'))
        if is_part:
            meta_box.append(self._badge('Partición', 'dim-label'))
        if device.get('mountpoint'):
            meta_box.append(self._badge(f"↳ {device['mountpoint']}", 'dim-label'))
        if device.get('fstype'):
            meta_box.append(self._badge(device['fstype'], 'warning'))
        info_box.append(meta_box)

        safe_label = Gtk.Label(xalign=0)
        if is_locked:
            safe_label.set_markup('<span foreground="#f26b6b" size="small">Protegido — No se puede formatear</span>')
        else:
            safe_label.set_markup('<span foreground="#3ecf8e" size="small">✓ Seguro para formatear</span>')
        info_box.append(safe_label)

        content.append(info_box)

        trailing = Gtk.Label(label=('🔒' if is_locked else ('▶' if is_safe else '')))
        content.append(trailing)

        if is_safe and not is_locked and on_select:
            click = Gtk.GestureClick()
            click.connect('pressed', lambda *_: on_select(device))
            self.add_controller(click)
            self.set_cursor_from_name('pointer')
        else:
            self.set_opacity(0.6)

    @staticmethod
    def _badge(text, css_class):
        badge = Gtk.Label(label=text)
        badge.add_css_class('caption')
        badge.add_css_class(css_class)
        return badge
