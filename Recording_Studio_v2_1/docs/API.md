# API HTTP

Base: `https://TU_APP.azurewebsites.net/api`. Todas las rutas requieren JWT. Las URL de respuesta son rutas relativas al mismo host, sin credenciales ni system keys de Durable.

## Crear

`POST /recordings` — scope `recordings:write`; `Content-Type: application/json`.

```json
{
  "video_url": "https://videos.example.com/capacitacion.mp4",
  "brief": "Crear capacitación agrupando pasos por pantalla y conservando controles y excepciones.",
  "theme": {"client": "Cliente", "primary": "#142D4E", "accent": "#007F78", "font": "Arial"},
  "include_word": true
}
```

Opcionales: `source_token` (credencial Bearer del origen, distinta del JWT de esta API) y `subtitles` (texto VTT/SRT, no URL ni ruta). Sin subtítulos se requiere `RS_TRANSCRIBER_MODEL`. No se obtiene automáticamente la transcripción de Teams.

Límites: body 2 MiB; URL 16 000 caracteres; objetivo 1–12 000; token 20 000; subtítulos 1 000 000. Campos extra, identidades y rutas se rechazan. Fuentes: Arial, Aptos o Calibri.

Respuesta 202, con `Location` y `Retry-After: 10`:

```json
{
  "job_id": "ID_HEXADECIMAL_32_CARACTERES",
  "status": "Queued",
  "status_url": "/api/recordings/ID",
  "result_url": "/api/recordings/ID/result"
}
```

No se envían bytes del video: el servidor lo descarga después de aceptar la solicitud.

## Consultar

`GET /recordings/{job_id}` — scope `recordings:read` y mismo `iss/sub`.

| Estado | Acción |
|---|---|
| Queued / Running | Consultar cada diez segundos |
| Completed | Descargar `result_url` con JWT |
| Failed | Mostrar `message`, corregir causa y crear otro trabajo |
| DispatchPending | Hacer POST a `retry_url` |
| Interrupted | Revisar despliegue; el runtime interrumpió el trabajo |

Se devuelven progreso, fechas y, al completar, archivos y tiempo total. Nunca se devuelve el body original. Un JWT nuevo del mismo emisor/sujeto sirve para consultar si el anterior expiró. La expiración del JWT de la API no cancela un trabajo aceptado.

## Descargar

`GET /recordings/{job_id}/result` — scope `recordings:read` y mismo propietario.

Devuelve `application/zip`, archivo `capacitacion.zip`: PPTX, Word opcional, evaluación, respuestas, plan, tema, capturas y reporte. El video fuente no se incluye. Máximo 100 MiB de ZIP (`RS_MAX_RESULT_BYTES` puede reducirlo). Este adaptador carga el ZIP en memoria para responder: dimensiona RAM según descargas simultáneas. No genera SAS ni hace públicos los blobs.

Antes de completar: 409. Trabajo ausente o ajeno: 404.

## Despacho incierto

Si Durable no confirma la creación, el POST inicial devuelve 202 con `status: DispatchUnknown`, ID y `retry_url`. `POST /recordings/{job_id}/dispatch` exige escritura y mismo propietario; comprueba si la instancia existe antes de crearla, usando lease para coordinar reintentos. No reemplaza una instancia existente.

Conserva el ID recibido y consulta antes de crear otro trabajo. Cada POST inicial nuevo genera otro ID: no existe deduplicación de envíos del usuario. Las actividades Durable pueden entregarse más de una vez; el worker usa lease y reconoce estados finales para no volver a publicar.

## Errores

400: campos/enlace/dominio inválido. 401: JWT ausente o inválido. 403: scope faltante. 404: trabajo ausente o ajeno. 409: resultado no listo. 413: body excesivo. 415: Content-Type incorrecto. 503: configuración o servicio no disponible.

Un fallo posterior al 202 se comunica como `Failed` al consultar. Los errores no incluyen tracebacks ni secretos de terceros.

## Cliente incluido

```bash
export RS_API_TOKEN='JWT_DE_RECORDING_STUDIO'
python scripts/submit_url.py \
  --api https://TU_APP.azurewebsites.net \
  --request examples/job-request.example.json \
  --output capacitacion.zip
```

Edita la URL de ejemplo. Si necesitas credencial del origen, pásala al cliente en `RS_SOURCE_TOKEN`; se añade como `source_token` sin imprimirse.

## Copia fuente y revocación del enlace público (2.1)

El body de creación no cambia. Para un enlace público de SharePoint configura `RS_SHAREPOINT_PUBLIC_HOSTS` en el servidor y no suministres `source_token`. El JWT del header sigue siendo obligatorio para utilizar la API.

El estado incluye `source_downloaded` y `source_link_can_be_revoked`, inicialmente falsos. Solo pasan a verdaderos después de que el archivo completo está descargado y respaldado en el blob privado `source.mp4`. Se añaden `source_ready_at` y `source_bytes`. No esperes una frase exacta en `message`: el progreso puede cambiar mientras el análisis continúa.

Cuando `source_link_can_be_revoked=true`, se puede eliminar el enlace público del origen para ese trabajo. Un reinicio restaura la copia privada sin reutilizar el enlace. El borrado del enlace en SharePoint sigue siendo una acción manual del propietario: la API no cambia permisos Microsoft. La copia fuente se intenta borrar al completar o fallar; trabajos nuevos requieren un enlace válido nuevo.
