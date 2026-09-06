# Margen de arranque — 1.0.2

## Problema observado

En un canal con segmentos de seis segundos, el primer segmento se entrega en
ráfaga y los siguientes se dosifican. La observación del MPD y la preparación
de cada segmento no tienen una cadencia perfectamente uniforme. Se midieron
pausas de entrega al comienzo, compartidas por la conexión local y la URL
pública, sin discontinuidades MPEG-TS ni saltos de timestamps en el contenido.
El margen disponible podía quedar por debajo del necesario para reproducir
sin una espera inicial del cliente.

## Cambio limitado

Sólo se modifica la entrada de cada conexión `stream.ts`:

1. Seleccionar el último segmento preparado cuando aparece el primer dato.
2. Conservar ese punto de entrada y esperar media duración del segmento,
   con un máximo de tres segundos, mientras el productor sigue trabajando.
3. Entregar el segmento y continuar con el envío existente, sin repetir
   esta espera en los siguientes segmentos.

No se persigue un segmento cada vez más reciente durante la espera, pues eso
eliminaría el margen. Tampoco se comienza por el más antiguo de toda la caché.
La política es idéntica para un cliente que abre el canal y para otro que se
incorpora posteriormente. La sincronía exacta de reproducción sigue dependiendo
del instante de entrada y del búfer de cada reproductor.

Si el segmento elegido desaparece de la caché durante la espera, se selecciona
el último disponible de nuevo. Se conserva el límite previo de 25 segundos
para empezar: cerca del límite se reduce la espera. El bucle sigue comprobando
desconexión, parada y cambio de pistas mientras espera.

No se cambia la configuración ni se añade caché TS en RAM. HLS, secuencias,
timestamps, continuidad MPEG-TS, procesamiento Shaka/FFmpeg y ruta de salida
permanecen iguales. En las instalaciones con interfaz de salida, se conserva
WireGuard.

## Impacto y riesgo

El coste deliberado es hasta unos tres segundos más antes de recibir el primer
contenido en los canales habituales de seis segundos; con segmentos de dos
segundos, la espera es de uno. El sondeo existente tiene una granularidad de
120 ms. Ese margen añade latencia respecto al envío inmediato anterior.

No garantiza absorber una parada prolongada de la CDN, todos los patrones
posibles de latencia ni los búferes particulares de cada reproductor.
No se alteran conexiones ya empezadas por una espera adicional a mitad del
stream. Un despliegue requiere reiniciar el servicio y debe programarse sin
clientes conectados, o avisando previamente si no es posible.

## Pruebas

Doce tests nuevos, además de los cuatro tests anteriores de entrada compartida:
espera desde disponibilidad real; segmentos cortos/largos; entrada tardía;
avance y expulsión de caché durante la espera; límite de arranque; ausencia de
media; desconexión, parada y cambio de pistas. Los tests usan reloj virtual.

La reproducción determinista de una cadencia medida con disponibilidad a
1,74 / 9,97 / 15,45 / 19,32 / 23,31 segundos fallaba antes del cambio con un
déficit de entrega de 2,28 segundos. Con el margen, pasa sin ese déficit.

La suite completa del proxy contiene 52 tests y se verifica tanto en fuente
como con el módulo compilado, incluyendo la prueba multimedia existente.

## Validación real y limitación pendiente

Tras el despliegue se capturaron cinco conexiones TS sobre dos instalaciones,
cada una durante 125 segundos; dos clientes entraron 20 segundos más tarde.
También se comprobaron índices y segmentos HLS. Los paquetes mantuvieron sus
contadores MPEG-TS y las capturas analizadas no tuvieron saltos DTS ni avisos
de ffprobe. No se ejecutaron las interfaces de VLC o TiviMate.

El primer byte llegó en aproximadamente 4,8–5,0 segundos en frío y 3,0–3,2
segundos para las incorporaciones al canal ya abierto. Durante los primeros
30 segundos, el margen calculado de contenido recibido frente al reloj real
fue positivo en las cinco conexiones.

En una instalación, las dos conexiones iniciadas en frío sí agotaron ese
margen posteriormente, alrededor del segundo 50, con un déficit máximo de
2,38 segundos. La conexión incorporada más tarde y las dos de la otra
instalación conservaron margen durante sus capturas. Este cálculo no mide el
búfer adicional que pueda tener el reproductor.

Por tanto, el ajuste mitiga el arranque, pero no es una solución completa para
las pausas posteriores. Recuperar margen durante una sesión en curso requiere
estudiar por separado la dosificación existente; esa lógica no se ha cambiado
en esta versión.
