# Autenticación y enlaces

Existen dos autorizaciones: usar Recording Studio y descargar el video. No se intercambian automáticamente.

## JWT para esta API

Todos los endpoints exigen `Authorization: Bearer <JWT>` firmado con RS256. Se verifican firma, algoritmo fijo, emisor, audiencia, sujeto, emisión y expiración. Claims obligatorios: `iss`, `aud`, `sub`, `iat`, `exp`; `nbf` se verifica cuando está presente. Hay 30 segundos de tolerancia de reloj.

`scope` es una cadena con permisos separados por espacios:

- `recordings:write`: crear o recuperar el despacho de un trabajo propio.
- `recordings:read`: consultar y descargar trabajos propios.

El propietario se deriva de `iss/sub` verificados. No se aceptan identidades ni rutas elegidas en el body. Un trabajo ajeno devuelve 404, también en la descarga.

Configura siempre `RS_JWT_ISSUER` y `RS_JWT_AUDIENCE`, y elige **una** fuente de clave pública:

- `RS_JWT_PUBLIC_KEY`: PEM; admite saltos `\n`. Alternativamente `RS_JWT_PUBLIC_KEY_FILE`: ruta en el servidor.
- `RS_JWT_JWKS_URL`: endpoint HTTPS de tu proveedor; selección por `kid`, caché de cinco minutos. El header `jku` del token no puede reemplazar esta URL.

La configuración falla cerrada si falta algo. `AuthLevel.ANONYMOUS` en el trigger significa que el runtime no pide function key; cada ruta aplica la validación JWT del código. No se requiere Entra ID ni Easy Auth.

Como aún no tienen emisor, en producción deberán elegir un backend de identidad que haga login y emita estos claims. Este proyecto no implementa contraseñas, MFA, renovación ni revocación. `scripts/dev_jwt.py` sirve solo para pruebas: genera una clave privada local y tokens de una hora. La Function App productiva solo necesita la clave pública/JWKS, nunca la clave privada del emisor.

## Permiso para descargar el video

Se admiten URL de descarga pública autorizada, URL firmada/preautorizada o endpoint que acepte un Bearer suministrado mediante `source_token`. El token del origen puede ser JWT u otro access token; el servidor del video decide si lo acepta. Este paquete no lo emite, renueva ni intercambia.

El JWT recibido en el header de esta API **nunca se reenvía** al origen. Si el origen requiere un token, el sistema autorizado de ese origen debe obtenerlo para el usuario. Una URL firmada debe seguir vigente cuando el worker empiece a descargar, después del tiempo en cola.

`RS_VIDEO_ALLOWED_HOSTS` admite dominios exactos, incluidos los CDN de redirecciones. `RS_VIDEO_TOKEN_HOSTS` es el subconjunto que puede recibir `source_token`. Al cambiar de host se elimina Authorization permanentemente para esa descarga, incluso si una redirección posterior regresa al host original.

Se rechazan HTTP, puertos alternativos, credenciales en URL, redirecciones a hosts no autorizados, DNS privado/loopback/link-local y destinos de metadatos. La conexión queda fijada a una IP pública validada, conservando TLS/SNI para el host. No se importan cookies o sesiones del navegador ni se usan proxies de entorno. En el modo público de SharePoint se aceptan cookies anónimas nuevas del mismo intercambio HTTPS, efímeras y limitadas al host exacto. Esta política no admite endpoints privados por VNet; requieren una adaptación para tu red.

## Teams / SharePoint / OneDrive

Un enlace de reunión o chat no es el archivo. La página del reproductor normalmente requiere sesión y devuelve HTML. La sesión del navegador no existe en la Function App, y añadirle un JWT propio no concede permisos.

Se retiraron Graph, OBO y Entra del código. No se adivinan IDs, no se utilizan APIs internas de Stream ni se automatiza el login. La versión 2.1 intenta resolver enlaces públicos de compartir mediante preferencia de descarga y redirecciones, únicamente para hosts exactos configurados; esto no convierte un enlace privado en público. Un endpoint real como `download.aspx` puede intentarse si está autorizado y entrega video directamente; sigue dependiendo de permisos válidos del origen.

Si solo tienen la página privada del reproductor, necesitan que el administrador habilite una vía de descarga autorizada o que la organización proporcione un endpoint de entrega. **El flujo de cualquier enlace privado de Teams no queda resuelto con un JWT propio.** [Documentación oficial de almacenamiento y permisos](https://learn.microsoft.com/microsoftteams/delete-exchange-online-transcripts).

## Persistencia

AES-256-GCM cifra todo el body privado (URL, token, objetivo y subtítulos) con una clave de 32 bytes en `RS_JOB_ENCRYPTION_KEY`. El ID del trabajo se autentica junto al ciphertext. Durable guarda solo el ID. Los resultados permanecen en un contenedor privado y se entregan después de verificar scope y propietario.

La solicitud cifrada se elimina normalmente al terminar; configura retención para cubrir reinicios, trabajos interrumpidos y abandonados. No se entregan claves privadas, secretos ni `local.settings.json` reales en el ZIP.

El video fuente también se conserva temporalmente como blob privado para poder revocar el enlace público tras la señal de copia completa. Ese blob usa el cifrado en reposo de Azure Storage; el cifrado AES-GCM de la aplicación corresponde a la solicitud privada. No existe una ruta HTTP para descargar la fuente.
