# Prueba de enlaces públicos de SharePoint — versión 2.1

Esta adaptación permite **intentar** descargar vínculos de video/archivo compartidos como «Cualquiera con el vínculo», sin Graph, sin Entra ID en la aplicación y sin importar una sesión de navegador. Sigue pendiente comprobarla con un enlace real de tu tenant. No se modifica ningún permiso en SharePoint desde el código.

## Lo implementado

- Reconoce rutas de compartir `/:v:/`, `/:u:/` y `guestaccess.aspx` únicamente en hosts exactos habilitados `.sharepoint.com`.
- Conserva el token de acceso del vínculo y solicita preferencia de descarga. No se construyen IDs ni se usan endpoints internos de Stream.
- Sigue redirecciones HTTPS autorizadas y acepta cookies anónimas nuevas solo durante esa descarga. No comparte cookies entre hosts, subdominios o trabajos y no las guarda en Storage.
- Rechaza login, contraseña, HTML, bloqueos de descarga y enlaces revocados/expirados sin publicar un video parcial.
- Respalda el video completo en el contenedor privado de la Function App antes de emitir `source_link_can_be_revoked=true`.
- Restaura esa copia en reintentos, sin volver al enlace que el usuario ya pudo revocar.

La preferencia `download=1` es un intento de compatibilidad; no es una API de descarga garantizada para todos los vínculos ni concede permisos. Si SharePoint devuelve un reproductor que necesita JavaScript, el código informa que hace falta una descarga directa. No ejecuta HTML ni evita restricciones del origen.

## Configurar

En las Application Settings de la Function App, o en `local.settings.json` para desarrollo:

```text
RS_VIDEO_ALLOWED_HOSTS=TU_TENANT.sharepoint.com,TU_TENANT-my.sharepoint.com
RS_SHAREPOINT_PUBLIC_HOSTS=TU_TENANT.sharepoint.com,TU_TENANT-my.sharepoint.com
RS_VIDEO_TOKEN_HOSTS=
```

Sustituye los dominios por los reales. Autoriza solo los que vayan a utilizar. Si el origen redirige hacia un CDN, su host exacto también debe estar en `RS_VIDEO_ALLOWED_HOSTS`; no debe recibir tokens ni se habilita automáticamente. No añadas servidores de login para intentar pasar la autenticación.

Esta versión reconoce SharePoint Online comercial `.sharepoint.com`; no incorpora OneDrive personal (`1drv.ms`) ni dominios de nubes soberanas. Se conserva la entrada por URL directa para otros servidores autorizados.

## Prueba cuando tengan un enlace

1. El propietario crea un vínculo «Cualquiera con el vínculo» para un video cuyo uso de esta forma esté permitido. La descarga debe estar habilitada. No basta «Personas de mi organización» ni «Personas específicas». No es necesario hacer público todo el sitio.
2. Se envía el vínculo en `video_url` al endpoint habitual, con el JWT de Recording Studio en Authorization. Para este modo se omite `source_token`.
3. Se consulta el estado del trabajo hasta que aparezca `source_link_can_be_revoked=true`. El 202 inicial y el mensaje de «descargando» todavía no autorizan a retirar el vínculo.
4. Con esa señal, el propietario elimina **ese enlace público** en Administrar acceso. La aplicación no puede hacer ese cambio automáticamente sin una integración Microsoft autorizada. Otros enlaces o permisos existentes no se eliminan al borrar este.
5. Se continúa consultando y se descarga el paquete al completar. La fuente privada permite continuar aunque se reinicie el worker; el cliente incluido muestra una notificación al detectar la señal.

Ejemplo del estado relevante:

```json
{
  "status": "Running",
  "source_downloaded": true,
  "source_link_can_be_revoked": true,
  "source_bytes": 12345678
}
```

No uses una frase exacta de `message` para automatizar la interfaz: los mensajes siguen cambiando durante el análisis. Usa el booleano. `source_ready_at` indica cuándo se confirmó la copia.

## Persistencia y límites de la señal

La señal significa que el archivo completo existe en el blob privado `jobs/{job_id}/source.mp4`. El archivo usa cifrado en reposo de Azure Storage; el body con URL y secretos sigue cifrado con AES-GCM por la aplicación. La fuente no tiene un endpoint público ni una ruta de descarga en la API.

No borres esa copia ni configures una retención que pueda eliminarla mientras el trabajo esté pendiente. La aplicación intenta retirarla al finalizar o fallar; lifecycle cubre interrupciones. El plazo debe ser mayor que cola, ejecución y reintentos. El video ocupa temporalmente espacio en Storage además del disco del worker.

La revocación no borra copias que otra persona haya descargado mientras el enlace estaba activo. Un trabajo nuevo, después de un fallo final o de completar, necesita una URL válida nuevamente; la copia temporal no es una biblioteca permanente.

## Validación realizada

Se probaron redirecciones y cookies con respuestas HTTP simuladas, páginas de login/HTML, errores de permisos, vínculos revocados y un reinicio simulado después del respaldo. El reintento completó el material sin otra descarga al origen. También se probó subida/restauración de la fuente con Azure SDK real contra Azurite.

No se ha probado un enlace real de tu organización, no se alteró la configuración de tu SharePoint y no se ha desplegado esta versión en Azure. Es una entrega preparada para esa prueba, no una garantía de que todos los formatos de vínculo público sean compatibles.

Referencias: [enlaces públicos y revocación en SharePoint](https://learn.microsoft.com/en-us/sharepoint/shareable-links-anyone-specific-people-organization), [restricciones de descarga](https://support.microsoft.com/en-us/onedrive/block-downloads-for-view-only-files-in-sharepoint-and-onedrive).
