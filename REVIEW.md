# Revisión conservadora — 2026-09-06

Alcance: únicamente estado por segmento, resolución de URLs y `proxy_url`.
Sin refactorización, cambios de FFmpeg/Shaka, parser DASH, audio, configuración
ni opciones de compilación. La instalación existente usa salida por interfaz,
no `proxy_url`; las correcciones de proxy y referencias especiales son de
compatibilidad y no se presentan como mejoras de calidad en esa instalación.

## 1. Estado por segmento

**Problema y causa.** `_prune_cache()` limita `_published` y los archivos TS,
pero no `locks`, `t_to_seq`, `_skip_t` ni `_fail_t`. Cada remux crea un lock;
cada segmento incluido en HLS recibe una secuencia. Los fallos abandonados
quedan en los dos últimos contenedores. `t_to_seq` no crece con clientes que
usan exclusivamente `stream.ts`. El cierre de sesión libera todo este estado;
el problema aparece al mantener un canal abierto continuamente.

**Demostración e impacto real.** Antes de modificar producción, una simulación
con los métodos reales de lock, secuencia y poda, segmentos de 6 segundos y
un registro de fallo por cada 100 segmentos produjo:

| Tiempo simulado | locks | secuencias HLS | skips/fallos | publicados |
| --- | ---: | ---: | ---: | ---: |
| 1 hora | 600 | 600 | 6 / 6 | 6 |
| 24 horas | 14.400 | 14.400 | 144 / 144 | 6 |

En Python 3.13, tamaño recursivo aproximado con `sys.getsizeof`: 3.009.253
bytes para locks, 1.453.912 para secuencias; 4.024.765 bytes combinados sin
contar dos veces objetos compartidos. Son unos 3,8 MiB/día/canal con HLS,
orden de decenas de MiB por semana y canal. No es una medida de RSS ni un
ensayo de 24 horas reales; la asignación de tablas no crece uniformemente.
Con TS solamente persiste el crecimiento de locks, unos 2,9 MiB/día en esta
simulación. Los fallos crecen según su frecuencia, no en cada segmento sano.

**Cambio mínimo aplicado.** En la poda existente, descartar sólo timestamps
anteriores al manifiesto y al cursor pendiente, exceptuando los publicados y
los que tienen trabajo en curso. Contar usuarios del lock desde antes de
esperar hasta después de liberarlo, incluyendo cancelaciones. No inspeccionar
atributos privados de asyncio ni usar `locked()` como única garantía. No
reiniciar `next_seq`, `ts_origin_t`, `_next_t` ni alterar la poda de archivos.
Conservar también histórico futuro si llega temporalmente un MPD anterior.

**Riesgo de regresión.** La concurrencia es el punto delicado: la poda debe
proteger al propietario, al esperador y al esperador ya despertado que aún no
ha retomado el lock. Los segmentos recuperables conservan su secuencia y los
fallos pendientes conservan su contador. No hay promesa de recuperar segmentos
que ya expiraron del manifiesto y de la caché.

**Tests previos.** Reproducción de crecimiento, fallos continuados con caché
antigua, numeración HLS, origen TS, recuperación, MPD regresivo, propietario y
esperadores, cancelación y excepciones. Ocho métodos de test, diez aserciones
fallidas en la versión anterior (incluye subtests); los fallos se reproducen
sin CDN, cuentas ni procesos multimedia.

## 2. URLs y redirects

**Problema y causa.** Concatenar directorio y referencia no resuelve rutas
padre, rutas desde raíz, consultas solas, referencias con autoridad ni URLs
absolutas de segmentos. El directorio se extrae con `rsplit`, que además
confunde las barras de una consulta con las de la ruta. `curl --path-as-is`
no corrige automáticamente las rutas `../` construidas de esta manera.

**Impacto real.** Se reproducen solicitudes a recursos incorrectos. Las
referencias simples ya funcionan. No se ha atribuido ningún corte de la
instalación actual a estos casos especiales; se documentan antes de aplicar
la corrección por compatibilidad con los orígenes admitidos por el proyecto.

**Cambio mínimo aplicado.** `urljoin` sólo en redirects y en init/media,
conservando como base la URL completa del MPD final después de redirects.
No cambiar el parser, las plantillas, cabeceras ni reintentos.

**Riesgo de regresión.** Bajo para referencias simples, verificadas con
consultas, nombres habituales de plantilla y rutas con/sin barra final.
Referencias especiales pasan a resolverse correctamente; una CDN que
dependiera de una ruta mal concatenada recibiría ahora otra URL.

**Tests previos.** Los seis tipos de referencia solicitados, cinco códigos de
redirect y ambas descargas init/media; URL final del MPD, consultas con barras
y preservación de plantillas normales. Los casos defectuosos fallan antes de
modificar producción.

## 3. `proxy_url`

**Problema y causa.** El `ProxyConnector` configura una sesión aiohttp, pero
MPD, init y media se descargan mediante `_curl_hop()`. Curl no recibía el
proxy configurado.

**Impacto real.** Una instalación configurada sólo con `proxy_url` podía
descargar directamente o depender del proxy del entorno. No afecta a la
instalación actual, que usa `egress_bind`. Se documenta este caso no utilizado
antes de corregirlo: el proxy explícitamente configurado debe aplicarse al
camino real de descarga.

**Cambio mínimo aplicado.** Añadir `--proxy` a curl únicamente cuando no hay
`egress_bind`, conservando la prioridad existente de la interfaz. Desactivar
excepciones `NO_PROXY` en esa rama para no omitir el proxy explícito. No migrar
a aiohttp, cambiar DNS de la interfaz ni añadir fallback directo.

**Riesgo de regresión.** La configuración sólo-proxy empieza a usar realmente
esa salida; un proxy incorrecto fallará, sin recurrir a conexión directa. La
ruta de interfaz y la ruta sin proxy configurado permanecen iguales.

**Tests previos.** Comando curl para MPD/init/media y caché init, SOCKS5,
SOCKS5h, HTTP, precedencia de interfaz y DNS ligado, configuración vacía y
ausencia de fallback. Los casos con proxy explícito fallan en la versión
anterior; la precedencia de interfaz y la ruta vacía ya pasan.

## Validación posterior

- Versión 1.0.1; tres correcciones de comportamiento. `src/proxy.py`: 36 líneas
  añadidas y 15 eliminadas, incluyendo comentarios y cambios de indentación.
- Los 23 tests anteriores siguen pasando. Con 17 tests nuevos: 40 tests del
  proxy y 42 de la instalación integrada (incluye dos del generador), todos
  pasan tanto con fuente Python como con el módulo compilado. Sin actualizar
  dependencias ni opciones de compilación.
- Simulación posterior de 100.800 segmentos, equivalente a siete días a
  6 segundos/segmento: 14 locks, 14 secuencias, un skip y un fallo conservados,
  seis publicados y cero usuarios de lock pendientes. Los mismos tamaños a
  1 hora, 24 horas y 168 horas; `next_seq` termina en 100.801, sin reinicios.
  Es simulación de estado, no una prueba de reproducción de siete días.
- Estrés adicional: 40 coroutines, 400 entradas en secciones protegidas de
  dos segmentos, podando durante la concurrencia; nunca dos propietarios
  simultáneos del mismo segmento.
- Curl real contra servidores HTTP/SOCKS5/SOCKS5h ficticios en loopback,
  incluyendo redirect, MPD/init/media, caché init y `NO_PROXY=*`. Sin acceder
  a la CDN ni cambiar la salida del servicio durante esas pruebas.
- Comparación de ocho URLs de init/media de dos canales existentes: idénticas
  antes y después. La configuración real conserva salida por WireGuard;
  `proxy_url` sigue vacío.
- Instalación integrada actualizada con respaldo previo y reinicio sin
  sesiones activas. La instancia principal no se modifica ni reinicia.
  Configuración, credenciales y generador conservan sus hashes anteriores.
- Reproducción real por WireGuard tras el despliegue: tres conexiones TS
  sobre dos canales, una incorporada 20 segundos más tarde; cada captura
  dura 125 segundos. Cero discontinuidades de contador MPEG-TS, cero saltos
  de DTS de audio/vídeo y cero avisos de ffprobe. No se ha ejecutado la GUI
  de VLC: se comprueba el flujo servido y sus paquetes.
- HLS en paralelo: 36 consultas de índice, 21 segmentos distintos, secuencia
  inicial avanzando de 1 a 16 sin renumerar segmentos ya vistos; descargas
  de segmentos correctas. Cero warnings, errores o segmentos saltados en el
  registro del servicio durante esta comprobación. Es una prueba breve de
  regresión, no una garantía de reproducción ininterrumpida indefinida.
