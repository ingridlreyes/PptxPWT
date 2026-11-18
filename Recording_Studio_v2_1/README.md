# Recording Studio 2.1 — URL, JWT y Function App

Convierte una grabación en una presentación de capacitación con varios pasos por frame, marcas editables y una evaluación de diez preguntas. Esta entrega parte del proyecto independiente de grabaciones v1 y conserva su pipeline y sus ejemplos aprobados.

Incluye **Function App Python v2, Durable Functions, entrada por enlace HTTPS y JWT RS256 configurable**. Se retiraron el cliente Graph, OBO, los contratos Graph y la autenticación Entra ID. La inferencia usa la clave de API de tus deployments existentes.

## Qué enlaces funcionan

| Entrada | Comportamiento |
|---|---|
| URL HTTPS que entrega bytes de video | Descarga en el servidor, sin subida manual del usuario |
| URL firmada o preautorizada | Funciona mientras siga vigente y su host esté autorizado |
| Endpoint que acepta `Authorization: Bearer` | Admite `source_token` independiente, limitado a hosts configurados |
| Enlace de reunión/chat de Teams | Se rechaza: no identifica un archivo descargable |
| Enlace público de compartir de SharePoint | Intento anónimo si su host exacto está configurado; depende de que SharePoint entregue el video |
| Página de reproductor o enlace privado | Requiere una URL de descarga autorizada; no se inicia sesión |
| Grabación privada de Microsoft + JWT propio | El JWT de esta API no concede permiso sobre el archivo de Microsoft |

JWT es un formato de token; no reemplaza los permisos de Teams. Las grabaciones están en OneDrive/SharePoint. Sin integración Microsoft, tu organización debe proporcionar una URL preautorizada o un endpoint autorizado que entregue el video. No se puede garantizar «pegar cualquier link privado de Teams y procesarlo» con las restricciones actuales. [Referencia oficial de permisos](https://learn.microsoft.com/microsoftteams/delete-exchange-online-transcripts).

## Flujo

```text
POST /api/recordings + JWT de la API + URL autorizada
  → 202 y job_id
  → Durable: descarga → lotes paralelos → consolidación → evaluación → exportación
GET /api/recordings/{job_id} + JWT → estado y progreso
GET /api/recordings/{job_id}/result + JWT → ZIP completo
```

Las solicitudes se cifran y los resultados se guardan en un contenedor privado. Durable recibe solo el ID. El video se descarga al worker y se respalda temporalmente en un blob privado para permitir reintentos después de revocar el enlace. Las copias se eliminan normalmente al finalizar; lifecycle debe cubrir interrupciones. Se reutiliza `AzureWebJobsStorage` por defecto; el runtime Durable también necesita ese Storage. No hace falta una nueva biblioteca de videos ni otra cuenta Blob. La caché de análisis es temporal por ejecución; el estado y los entregables sí son persistentes.

## Material que conserva

- Uno a cuatro pasos relacionados por frame; no se genera una lámina por clic.
- Flechas, recuadros y etiquetas nativos editables sobre capturas originales.
- Numeración continua, campos variables y cobertura con exclusiones justificadas.
- Lotes de 15 minutos, solapamiento de 15 segundos y hasta tres lotes simultáneos.
- Consolidación de repeticiones; presupuesto inicial de 30 diapositivas incluida portada.
- PPTX, Word opcional, diez preguntas, respuestas, JSON de trazabilidad y capturas.

El límite de láminas no elimina contenido silenciosamente: si las actividades no caben se informa el exceso. El muestreo puede perder pantallas fugaces; valida la calidad con tus videos reales.

## Empezar

1. Sigue [Desplegar en Function App](docs/DEPLOY_FUNCTION_APP.md).
2. Configura proveedor JWT, deployments de visión/audio y dominios permitidos.
3. Usa el [contrato HTTP](docs/API.md) o `scripts/submit_url.py`.

La ruta incluida utiliza **Function App Linux Premium o Dedicated con Always On**, Python 3.12 y contenedor con FFmpeg/FFprobe. El ZIP completo es código y documentación; no se debe publicar directamente en Consumption clásica: esta actividad larga excede su límite y el ZIP no instala binarios del sistema. [Timeouts oficiales](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale), [contenedores de Functions](https://learn.microsoft.com/en-us/azure/azure-functions/functions-how-to-custom-container).

### Muestra sin Azure

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m recording_studio demo --output runtime_recordings/demo
```

La muestra es ficticia y no llama al modelo. `examples/aprobado/` conserva los archivos aprobados de v1 sin modificaciones.

### SDK para tu backend

```python
from recording_studio.application.service import RecordingService
from recording_studio.domain.models import Principal
from recording_studio.infrastructure.config import Settings

# El backend ya autenticó y autorizó esta identidad; no proviene del body del cliente.
result = RecordingService(Settings.from_env()).generate_url(
    Principal(tenant_id="organizacion", user_id="usuario-autenticado"),
    url="https://videos.example.com/capacitacion.mp4",
    brief="Explicar el proceso agrupando pasos por pantalla.",
    output_directory="runtime_recordings/resultado-nuevo",
    # source_token=token_del_origen,  # Independiente del JWT de la API.
    # subtitles=texto_vtt_o_srt,      # Alternativa al deployment de audio.
)
```

Se conserva `generate_file` y la CLI de archivos locales. La entrada por URL se ofrece en SDK y Function App. No se incluye una web nueva: la API está preparada para tu frontend existente.

## Contenido del paquete

`function_app.py`, `host.json`, `Dockerfile`, dependencias verificadas, plantilla de configuración sin secretos, pipeline completo, ejemplos aprobados, cliente de API, generador de JWT de desarrollo, pruebas y documentación. El proveedor JWT queda configurable; no hay un sistema productivo de login incluido.

[Autenticación y enlaces](docs/AUTENTICACION_Y_ENLACES.md) · [API](docs/API.md) · [Optimización](docs/OPTIMIZACION.md) · [Validación](docs/VALIDACION.md).

## Nuevo: probar vínculos públicos de SharePoint

Configura el dominio exacto en `RS_SHAREPOINT_PUBLIC_HOSTS` y también en `RS_VIDEO_ALLOWED_HOSTS`. La adaptación intenta preferir descarga, sigue redirecciones autorizadas y conserva únicamente cookies anónimas de esa descarga. No usa Graph, Entra ni sesiones de navegador. Si recibe login, contraseña, HTML o bloqueo de descarga, falla con un mensaje claro. Es un intento de compatibilidad pendiente de validarse con un enlace real de tu tenant.

Espera `source_link_can_be_revoked=true` en el estado antes de eliminar el enlace público. Esa señal exige que el video completo esté respaldado en Storage privado. El cliente incluido también muestra el aviso. Véase [prueba y revocación](docs/SHAREPOINT_PUBLICO.md).
