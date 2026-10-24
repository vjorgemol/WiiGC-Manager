# WiiGC Manager para Debian 13 (Trixie)

Este documento explica cómo construir e instalar WiiGC Manager como paquete `.deb` nativo en Debian 13 con XFCE u otros entornos de escritorio.

## Requisitos del sistema

### Sistema operativo
- Debian 13 (Trixie) o posterior
- Cualquier entorno de escritorio (GNOME, XFCE, KDE, etc.)

### Dependencias de ejecución

El paquete `.deb` instalará automáticamente estas dependencias:

- **Python 3.8+** con PyGObject
- **GTK 4** y **libadwaita**
- **GStreamer** (para reproducción de sonido)
- **wit** (Wiimm's ISO Tools) - **OBLIGATORIO**

Las dependencias recomendadas (opcionales):
- **dolphin-emu** - Para jugar directamente desde la aplicación

### Instalación de wit

`wit` es **obligatorio** para que WiiGC Manager funcione. En Debian 13:

```bash
sudo apt install wit
```

Si no está disponible en los repositorios, descárgalo manualmente:

```bash
wget https://wit.wiimm.de/download/wit-v3.05a-r8638-x86_64.tar.gz
tar -xzf wit-v3.05a-r8638-x86_64.tar.gz
sudo cp wit-v3.05a-r8638-x86_64/bin/* /usr/local/bin/
```

### Permisos para leer particiones WBFS

Para leer particiones WBFS, tu usuario necesita estar en el grupo `disk`:

```bash
sudo usermod -aG disk $USER
# Cierra sesión y vuelve a entrar para que tenga efecto
```

## Construcción del paquete .deb

### 1. Instalar dependencias de construcción

```bash
sudo apt install debhelper dh-python python3-all python3-gi \
                 gir1.2-gtk-4.0 gir1.2-adw-1
```

### 2. Clonar el repositorio (si no lo has hecho)

```bash
git clone https://github.com/vjorgemol/WiiGC-Manager.git
cd WiiGC-Manager
```

### 3. Ejecutar el script de construcción

```bash
./build-deb.sh
```

Este script:
- Verifica que todas las dependencias de construcción estén instaladas
- Limpia construcciones anteriores
- Construye el paquete `.deb`
- Muestra información del paquete generado

El paquete se generará en el directorio padre como `wiigc-manager_1.0.0-1_all.deb`.

### 4. Construcción manual (alternativa)

Si prefieres construir manualmente sin el script:

```bash
dpkg-buildpackage -us -uc -b
```

## Instalación del paquete

### Instalación básica

```bash
cd ..
sudo dpkg -i wiigc-manager_1.0.0-1_all.deb
```

Si hay dependencias faltantes:

```bash
sudo apt install -f
```

### Verificar la instalación

```bash
dpkg -l | grep wiigc-manager
which wiigc-manager
```

## Uso

### Lanzar desde el menú de aplicaciones

Busca **WiiGC Manager** en tu menú de aplicaciones (en XFCE, en el menú de Aplicaciones → Utilidades).

### Lanzar desde la terminal

```bash
wiigc-manager
```

## Desinstalación

```bash
sudo apt remove wiigc-manager
```

Para eliminar también los archivos de configuración:

```bash
sudo apt purge wiigc-manager
```

## Estructura del paquete

El paquete instala los siguientes archivos:

```
/usr/bin/wiigc-manager                              # Ejecutable principal
/usr/share/wiigc-manager/                           # Código de la aplicación
/usr/share/applications/org.wiigcmanager.Gtk.desktop # Entrada del menú
/usr/share/icons/hicolor/scalable/apps/             # Icono de la aplicación
/usr/share/doc/wiigc-manager/                       # Documentación
```

## Solución de problemas

### Script de diagnóstico automático

Si tienes problemas para que la aplicación detecte tu disco USB, ejecuta el script de diagnóstico:

```bash
cd WiiGC-Manager
bash diagnostico-usb.sh
```

Este script verificará:
- Si tu usuario está en el grupo `disk`
- Si los dispositivos USB se detectan correctamente
- Si hay problemas de permisos
- Si las dependencias están instaladas

### La aplicación no arranca

1. Verifica que `wit` esté instalado:
   ```bash
   which wit wwt
   ```

2. Comprueba las dependencias:
   ```bash
   dpkg -s python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
   ```

3. Ejecuta desde la terminal para ver errores:
   ```bash
   wiigc-manager
   ```

### No detecta el disco USB

**Problema**: La aplicación muestra "No se detectaron unidades de almacenamiento".

**Causas comunes**:
1. **Falta de permisos**: Tu usuario no está en el grupo `disk`
2. **USB montado en ruta del sistema**: El USB está montado en `/home`, `/var`, etc.
3. **Todas las particiones marcadas como sistema**: El dispositivo completo se está filtrando

**Solución**:

1. **Verifica que estés en el grupo `disk`**:
   ```bash
   groups $USER
   ```

   Si no aparece `disk`, añádelo:
   ```bash
   sudo usermod -aG disk $USER
   ```

   **IMPORTANTE**: Debes cerrar sesión completamente y volver a entrar para que los cambios tengan efecto. No basta con cerrar la terminal.

2. **Ejecuta con modo debug** para ver qué está pasando:
   ```bash
   wiigc-manager 2>&1 | grep DEBUG
   ```

   Esto mostrará información detallada sobre los dispositivos detectados y por qué se están filtrando.

3. **Verifica que el USB esté conectado**:
   ```bash
   lsblk
   ```

   Busca tu dispositivo USB (generalmente `/dev/sdb`, `/dev/sdc`, etc.). Debe aparecer con `RM` (removable) marcado como `1`.

4. **Comprueba el punto de montaje**:
   ```bash
   lsblk -o NAME,PATH,MOUNTPOINT,RM
   ```

   Si tu USB está montado en `/home/<usuario>/...`, desmóntalo primero:
   ```bash
   umount /dev/sdX1  # Reemplaza X con la letra correcta
   ```

### No puede leer la partición WBFS

Asegúrate de que tu usuario esté en el grupo `disk`:

```bash
groups $USER
```

Si no aparece `disk`, añádelo:

```bash
sudo usermod -aG disk $USER
```

Luego cierra sesión y vuelve a entrar.

### Error de permisos al instalar

Asegúrate de usar `sudo` al instalar con `dpkg`:

```bash
sudo dpkg -i wiigc-manager_*.deb
```

## Diferencias con otras versiones

### vs. Flatpak
- **Paquete .deb**: Se integra completamente con el sistema, usa los recursos del sistema
- **Flatpak**: Entorno aislado, puede incluir dependencias duplicadas

### vs. AppImage
- **Paquete .deb**: Gestión de actualizaciones con `apt`, integración total
- **AppImage**: Archivo único portable, sin instalación

### vs. Código fuente
- **Paquete .deb**: Instalación limpia, desinstalación fácil, gestión de dependencias
- **Código fuente**: Requiere gestión manual de dependencias

## Optimizaciones para XFCE

El paquete funciona perfectamente en XFCE. Si usas temas oscuros en XFCE:

1. Instala `gnome-themes-extra`:
   ```bash
   sudo apt install gnome-themes-extra
   ```

2. Configura el tema en XFCE:
   - Configuración → Apariencia → Estilo → Adwaita-dark

## Contribuir

Para reportar problemas específicos del paquete Debian:
- Abre un issue en: https://github.com/vjorgemol/WiiGC-Manager/issues
- Etiquétalo con `debian` o `packaging`

## Licencia

MIT - Ver el archivo `LICENSE` en el repositorio.

## Créditos

- **WiiGC Manager**: Desarrollado por el equipo de WiiGC Manager
- **wit/wwt**: Herramientas de Wiimm (https://wit.wiimm.de/)
- **Empaquetado Debian**: Optimizado para Debian 13 (Trixie)
