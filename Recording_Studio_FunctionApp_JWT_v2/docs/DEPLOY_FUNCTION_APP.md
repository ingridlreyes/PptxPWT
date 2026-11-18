# Desplegar Recording Studio en Function App

## Destino de esta entrega

Function App **Linux, Python 3.12, runtime Functions v4, plan Premium o Dedicated con Always On**, mediante el Dockerfile incluido. La imagen incorpora FFmpeg y FFprobe. `functionTimeout` se fija en dos horas; es un límite por ejecución de la actividad, no una garantía de tiempo máximo de un trabajo con reintentos.

El endpoint HTTP responde 202 sin esperar al análisis. Durable mantiene la ejecución y su cola en Storage. El trabajo completo se ejecuta en una actividad con lotes paralelos internos; ante un reinicio puede repetirse desde la descarga en otro worker. El lease evita publicación simultánea; no se promete reanudación persistente de cada lote entre hosts. El timeout no elimina interrupciones por actualizaciones o escalado. [Límites oficiales](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale).

Esta imagen completa de host está orientada a Premium/Dedicated. No se entrega una ruta para Consumption clásica o un contenedor worker de Flex. [Soporte de contenedores](https://learn.microsoft.com/en-us/azure/azure-functions/functions-how-to-custom-container).

## Configuración requerida

| Setting | Valor / finalidad |
|---|---|
| `FUNCTIONS_WORKER_RUNTIME` | `python` |
| `FUNCTIONS_EXTENSION_VERSION` | `~4` |
| `AzureWebJobsStorage` | Connection string de tu cuenta Storage existente; Durable necesita blobs, colas y tablas |
| `RS_STORAGE_CONNECTION_STRING` | Opcional: otra cuenta existente para estado y entregables; si no, reutiliza la del runtime |
| `RS_JOBS_CONTAINER` | `recording-studio-jobs` por defecto, acceso privado obligatorio |
| `RS_MODE` | `azure` |
| `RS_MODEL_BASE_URL` | Endpoint HTTPS de inferencia compatible con Responses; por ejemplo `https://RECURSO.openai.azure.com/openai/v1/` |
| `RS_MODEL_API_KEY` | Clave del backend; no JWT del usuario |
| `RS_WRITER_MODEL` | Deployment de texto/visión compatible con el contrato existente |
| `RS_TRANSCRIBER_MODEL` | Deployment de audio si no se suministra VTT/SRT como texto |
| `RS_JWT_ISSUER` | Emisor exacto elegido para los JWT de esta API |
| `RS_JWT_AUDIENCE` | Audiencia de esta API, por ejemplo `recording-studio-api` |
| `RS_JWT_PUBLIC_KEY` o `RS_JWT_PUBLIC_KEY_FILE` | Clave pública PEM; alternativamente usa solo `RS_JWT_JWKS_URL` |
| `RS_JOB_ENCRYPTION_KEY` | 32 bytes aleatorios en base64; conservar durante toda la vida de trabajos pendientes |
| `RS_VIDEO_ALLOWED_HOSTS` | Dominios exactos separados por coma; incluir CDN de redirecciones |
| `RS_VIDEO_TOKEN_HOSTS` | Subconjunto autorizado a recibir `source_token`; vacío si no se usa |

No se requieren Graph, app registration de Microsoft, OBO, Entra ID ni AI Search. El emisor JWT productivo queda por elegir; sin configuración válida las rutas responden 503/401, nunca omiten la autenticación.

La cuenta Storage debe permitir acceso mediante connection string y operaciones de creación de contenedor, blobs y leases. El contenedor se crea privado al primer trabajo; si uno existente tiene acceso público, se rechaza. No uses `UseDevelopmentStorage=true` en Azure.

No guardes la clave privada del emisor en la Function App. La clave de cifrado de trabajos es un secreto distinto. Cambiar esta última con trabajos pendientes impide descifrar sus solicitudes: deja terminar la cola o implementa una migración de claves antes de rotarla.

## Ajustes de capacidad

En `host.json` se limita a una actividad de procesamiento por host. Cada actividad usa hasta tres lotes y tres llamadas al modelo. Varias instancias multiplican esa concurrencia; configura el máximo de instancias de la Function App según la cuota de tus deployments. Los endpoints HTTP usan threads separados y pueden consultar el estado mientras trabaja la actividad.

Defaults: video hasta 4 GiB, descarga de 30 minutos, duración multimedia hasta 24 horas, ZIP de respuesta hasta 100 MiB, máximo de 30 láminas. Los límites se rechazan, no se truncan. Reserva disco temporal para el video, capturas, audio y exportación; un video de 4 GiB requiere más de 4 GiB libres. Dimensiona RAM también para el ZIP en memoria durante la respuesta. Para videos que excedan esos recursos se necesita particionar el procesamiento en actividades adicionales y almacenamiento de intermedios; no basta aumentar un timeout.

El tráfico de descarga usa HTTPS directo hacia hosts públicos autorizados; esta versión no soporta destinos privados, proxies corporativos ni login interactivo. El modelo y Storage deben ser accesibles desde la Function App.

## Ejecución local

Instala Python 3.12, Azure Functions Core Tools v4, FFmpeg/FFprobe y Azurite. Desde la raíz del paquete:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/dev_jwt.py init
cp local.settings.example.json local.settings.json
```

Edita `local.settings.json` con tus endpoints/deployments, dominios y la clave que está en `.secrets/job-encryption-key.txt`. Para desarrollo, la plantilla usa `.secrets/jwt-public.pem` y el emisor/audiencia por defecto del script. No subas `.secrets`, `local.settings.json` ni `.env` al despliegue.

En otra terminal inicia Azurite y después Functions:

```bash
azurite --skipApiVersionCheck
func start
```

Genera un JWT local de una hora y utiliza el cliente:

```bash
export RS_API_TOKEN="$(python scripts/dev_jwt.py token)"
python scripts/submit_url.py --api http://localhost:7071 \
  --request examples/job-request.example.json --output capacitacion.zip
```

Primero cambia `video_url` por una descarga autorizada real y configura el mismo host. El modelo real se usará y consumirá su cuota. El modo demo solo se ejecuta mediante la CLI de muestra, no desde la API.

## Construir y desplegar en tu Function App existente

1. Configura las variables de la tabla en la Function App. `local.settings.json` no se usa automáticamente en Azure.
2. Construye la imagen y publícala en el registro autorizado de tu organización:

```bash
docker build --platform linux/amd64 -t TU_REGISTRO/recording-studio:2.0.0 .
docker push TU_REGISTRO/recording-studio:2.0.0
```

3. En Deployment Center de la Function App Linux Premium/Dedicated, selecciona esa imagen y configura el acceso al registro según sus políticas. En Dedicated activa Always On. Conserva la conexión Storage necesaria para el host.
4. Habilita HTTPS Only. Si tu frontend está en otro origen, configura CORS para ese origen y el header Authorization; no se habilitan orígenes globales en el código. Esta implementación aplica su propio JWT; si ya tienen Easy Auth, debe aceptar y dejar pasar los tokens de su proveedor elegido o desactivarse en esa ruta según su arquitectura.
5. Reinicia y verifica los endpoints con un JWT real. Una petición sin JWT debe devolver 401. Una solicitud a un enlace de reunión de Teams debe devolver 400 y explicar que se necesita el archivo descargable.
6. Prueba un video autorizado real, consulta progreso y descarga. Verifica contenido, marcas y diez preguntas con una persona del proceso.

El ZIP es una entrega de fuentes completa. **No se desplegó nada en tu cuenta de Azure desde este chat**. Si prefieres zip deploy en una imagen de plataforma, deberás aprovisionar FFmpeg/FFprobe y un plan compatible por tu cuenta; no se presupone que estén instalados.

## Retención y operación

Configura una política de lifecycle en el contenedor `RS_JOBS_CONTAINER` para borrar `jobs/` después del plazo aprobado (por ejemplo siete días, siempre mayor que la espera y ejecución máximas previstas). Cubre `request.bin`, estado, locks y paquetes. Si hay soft delete/versionado, configura también su retención: borrar el blob activo no elimina automáticamente versiones históricas.

Configura aparte la purga de historiales terminados de Durable; la política del contenedor de entregables no borra tablas ni colas del task hub. No se incluye un proceso automático de borrado porque el plazo depende de tu política. No cambies el nombre `RecordingStudioV2` del hub mientras haya trabajos pendientes.

No se deben registrar headers Authorization ni bodies privados en proxies, middleware o Application Insights personalizado. Los mensajes de progreso de este paquete no incluyen URLs o tokens. Los errores completos de terceros se omiten de las respuestas.
