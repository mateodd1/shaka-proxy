# Despliegue de varias instancias

Los ejemplos utilizan `tv.example.com`, los prefijos `/first` y `/second` y los
puertos locales 8090 y 8091. Sustitúyelos por tu dominio y puertos disponibles.

## Preparar los directorios

Instala el proyecto en `/opt/proxy-shaka` y compílalo con `./build.sh` después
de crear su `.venv`, como indica el README principal. El usuario del servicio
necesita lectura y ejecución sobre ese directorio y los binarios.

Ejecuta estos pasos como administrador; crea el usuario sólo si no existe:

```bash
id proxy-shaka || useradd --system --user-group --home-dir /var/lib/proxy-shaka --shell /usr/sbin/nologin proxy-shaka
install -d -o proxy-shaka -g proxy-shaka -m 0750 /var/lib/proxy-shaka
install -d -o proxy-shaka -g proxy-shaka -m 0700 /var/lib/proxy-shaka/first /var/lib/proxy-shaka/second
```

Copia `config.example.json` como `config.json` y `channels.example.m3u` como
`channels.m3u` en cada directorio. Edita ambas configuraciones:

| Campo | first | second |
| --- | --- | --- |
| `listen_host` | `127.0.0.1` | `127.0.0.1` |
| `listen_port` | `8090` | `8091` |
| `public_base` | `https://tv.example.com/first` | `https://tv.example.com/second` |
| `source_m3u` | `channels.m3u` | `channels.m3u` |
| `token_file` | `token.json` | `token.json` |
| `packager` | `/opt/proxy-shaka/bin/packager` | `/opt/proxy-shaka/bin/packager` |
| `ffmpeg` | `/usr/bin/ffmpeg` | `/usr/bin/ffmpeg` |

Conserva `hls_dir: "hls"` y `log_dir: "logs"`: se crean dentro del directorio
de cada instancia. Usa la lista y el token que corresponden a cada una. Los
archivos privados deben pertenecer a `proxy-shaka` y tener permisos `0600`.

## Activar los servicios

```bash
install -m 0644 /opt/proxy-shaka/deploy/proxy-shaka@.service /etc/systemd/system/proxy-shaka@.service
systemctl daemon-reload
systemctl enable --now proxy-shaka@first proxy-shaka@second
journalctl -u proxy-shaka@first -f
```

Si el origen requiere WireGuard, configura `egress_bind` con la IP de la
interfaz y `egress_dns` si procede. Añade una dependencia de su unidad
`wg-quick@NOMBRE.service` al servicio cuando necesites garantizar que la VPN
esté lista al arrancar. La configuración del túnel se administra por separado.

## Publicar por HTTPS

Integra `Caddyfile.example` en Caddy o `nginx-locations.example.conf` dentro
de un servidor HTTPS Nginx. Configura tu dominio/certificado y valida la
configuración antes de recargar el servidor. Si hay otro proxy delante, también
debe permitir respuestas largas sin buffering para las rutas `/live/`.

Cada instancia ofrece estas URLs:

- `https://tv.example.com/first/playlist.m3u8` para VLC.
- `https://tv.example.com/first/status` para consultar el estado.
- `https://tv.example.com/first/epg` para la guía XMLTV.
- `https://tv.example.com/first/live/<slug>/stream.ts` para un canal.

La segunda instancia usa las mismas rutas bajo `/second`. `public_base` debe
coincidir exactamente con el prefijo publicado para que la lista apunte a la
instancia correcta.

La renovación de tokens, si el origen la requiere, se configura en su propio
generador o tarea programada. Actualiza únicamente el `token_file` de la
instancia correspondiente; no hace falta reiniciar el proxy.
