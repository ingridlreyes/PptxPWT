# Validación de Recording Studio 2.1

Fecha: 18 de noviembre de 2026.

## Resultado

**61 pruebas aprobadas, sin omisiones**, con Python 3.12.14, dependencias instaladas en un entorno virtual limpio desde `requirements.lock.txt`, FFmpeg/FFprobe 7.1.1 y Azurite 3.37.0. `pip check` confirmó que no hay dependencias incompatibles.

La comprobación incluye:

- Enlaces públicos SharePoint simulados, preferencia de descarga, conservación de parámetros, cookies efímeras por host y rechazo de login/HTML/revocación.
- Confirmación de copia fuente persistente y reintento sin acceder al vínculo ya revocado, más subida/restauración real contra Azurite.

- Firma JWT RS256 real con claves efímeras; rechazo de firma ajena, alg=none, claims ausentes, emisor/audiencia incorrectos, expiración y emisión futura. Scopes y pertenencia se verifican por separado.
- Entrada HTTPS, dominios exactos, DNS privado/mixto, IP fijada con TLS/SNI, tamaño sin Content-Length, HTML, descargas incompletas, redirecciones y retirada de credenciales entre hosts.
- Cifrado AES-GCM, protección contra alteración y vínculo al ID de trabajo.
- Endpoints registrados por Functions: autorización, campos extra, aislamiento de usuarios, estado, descarga y recuperación de despacho incierto.
- Flujo completo por URL con modelo y transporte simulados: cuatro pasos por un frame, evaluación de diez preguntas, ZIP real, limpieza de credenciales y reconocimiento de estados finales repetidos.
- Azure SDK real contra Azurite: creación privada, solicitud cifrada, pertenencia, subida/descarga de resultados y exclusión mediante leases.
- Extracción real de frames/audio desde un video sintético con FFmpeg/FFprobe.
- Pruebas originales de lotes, paralelismo, caché, cobertura, consolidación, evidencia, numeración y formas editables.

## Prueba HTTP con el runtime real de la base 2.0

Se arrancó Azure Functions Core Tools 4.16.1, runtime 4.1054.250.26428, bundle 4.41.2 y Azurite. Se verificó:

| Operación | Resultado |
|---|---|
| Crear sin JWT | 401 |
| Crear con enlace de reunión de Teams | 400 |
| Crear con JWT válido y URL de prueba | 202 + ID |
| Consultar ese trabajo con otro usuario | 404 |
| Ejecutar actividad Durable sobre una URL que no entrega video | Failed / video_download_failed, persistido y sanitizado |
| Descargar un paquete propio ya publicado en Storage | 200, bytes idénticos al ZIP aprobado |
| Descargar ese paquete con otro usuario | 404 |
| Descargar sin JWT | 401 |

La prueba de descarga exitosa utilizó un trabajo preparado con el ZIP del ejemplo aprobado, para verificar HTTP y Storage. No fue un análisis real con tus deployments de Azure. El ensayo Durable por URL comprobó el camino de error de descarga, sin invocar un modelo.

En este equipo se usó el bundle en caché, modo offline de Core Tools y polling del watcher de .NET para completar el arranque local. Son ajustes del entorno de prueba, no requisitos del despliegue Linux.

## Conservación de la versión aprobada

Se compararon los bytes de todos los archivos de `examples/aprobado`, los exportadores de `recording_studio/documents`, los fixtures y `media/pipeline.py` contra v1: son idénticos. La muestra aprobada mantiene cuatro láminas, tres capturas originales, doce grupos de marcas y pasos 1–12.

No se volvió a diseñar ni renderizar el material: esta versión cambia acceso, autenticación y hosting. Las pruebas inspeccionan marcas editables, numeración y archivos generados con los exportadores originales.

## Reproducir

```bash
python -m pip install -r requirements.lock.txt
python -m unittest discover -s tests -v
python scripts/check_documentation.py
python -m pip check
```

Para incluir Azure SDK contra Azurite, iniciar el emulador y establecer `RS_TEST_STORAGE=UseDevelopmentStorage=true` antes de ejecutar las pruebas. Sin esa variable se omiten tres pruebas de integración. Sin FFmpeg/FFprobe se omite la prueba de multimedia real. El script de auditoría verifica documentación de propósito en módulos/clases/funciones del paquete, pruebas y utilidades.

## Lo que falta validar en tu entorno

No se desplegó en tu suscripción, no se construyó la imagen Docker en este equipo y no se usaron credenciales corporativas. Deben validarse el despliegue Linux, cuotas y espacio temporal, el emisor JWT elegido, tus deployments de visión/audio y una descarga real autorizada del origen.

No se afirma que cualquier enlace privado de Teams funcione: Graph y Entra se retiraron, y un JWT propio no concede permisos de Microsoft. La precisión del modelo sobre videos reales, la ubicación de marcas y los tiempos de videos largos no se demuestran con los ejemplos ficticios ni los modelos simulados.

## Alcance de la adaptación 2.1

Las 61 pruebas corresponden a esta versión y a sus dependencias fijadas. La prueba HTTP del runtime descrita arriba corresponde a la base 2.0 y se conserva como evidencia de esa base; no se presenta como prueba real del acceso SharePoint añadido. La nueva adaptación fue probada con transporte HTTP simulado y Storage local real. No se cuenta todavía con un enlace público real del tenant del usuario; esa prueba es el siguiente paso, siguiendo `SHAREPOINT_PUBLICO.md`.
