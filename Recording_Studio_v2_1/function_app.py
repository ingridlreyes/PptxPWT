"""Azure Functions Python v2: JWT, entrada por URL y procesamiento Durable asíncrono."""
import asyncio
from functools import lru_cache
import json
import uuid
import azure.functions as func
import azure.durable_functions as df
from pydantic import ValidationError
from recording_studio.application.function_worker import process_job
from recording_studio.application.jobs import JobRequest
from recording_studio.infrastructure.auth import JwtVerifier, AuthenticationError, AuthorizationError
from recording_studio.infrastructure.config import Settings
from recording_studio.infrastructure.job_store import JobStore, JobNotFound
from recording_studio.integrations.video_url import DownloadPolicy, VideoUrlError


app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@lru_cache(maxsize=1)
def verifier():
    """Carga configuración confiable una vez por proceso de Functions."""
    return JwtVerifier.from_env()


def reply(payload, status=200, headers=None):
    """Responde sin cachear datos privados ni exponer tracebacks al cliente."""
    return func.HttpResponse(json.dumps(payload, ensure_ascii=False), status_code=status,
        mimetype="application/json", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **(headers or {})})


def error_response(exc):
    """Traduce errores conocidos y oculta detalles internos y credenciales de excepciones."""
    if isinstance(exc, AuthenticationError):
        return reply({"error": "unauthorized", "message": str(exc)}, 401, {"WWW-Authenticate": "Bearer"})
    if isinstance(exc, AuthorizationError):
        return reply({"error": "forbidden", "message": str(exc)}, 403)
    if isinstance(exc, JobNotFound):
        return reply({"error": "not_found", "message": "Trabajo no encontrado"}, 404)
    if isinstance(exc, VideoUrlError):
        return reply({"error": "unsupported_video_url", "message": str(exc)}, 400)
    if isinstance(exc, (ValidationError, ValueError, json.JSONDecodeError)):
        return reply({"error": "invalid_request", "message": "Solicitud inválida; revisa los campos y límites de docs/API.md"}, 400)
    return reply({"error": "service_unavailable", "message": "Revisa la configuración y disponibilidad del servicio"}, 503)


def authenticate(req, scope):
    """Todos los endpoints, incluso lectura y descarga, verifican firma y permisos."""
    identity = verifier().verify(req.headers.get("Authorization", ""))
    identity.require(scope)
    return identity


@app.route(route="recordings", methods=["POST"])
@app.durable_client_input(client_name="client")
async def submit_recording(req: func.HttpRequest, client):
    """Valida la solicitud antes de encolar y devuelve rutas JWT sin claves de gestión Durable."""
    try:
        identity = await asyncio.to_thread(authenticate, req, "recordings:write")
        if len(req.get_body()) > 2 * 1024**2:
            return reply({"error": "request_too_large", "message": "Envía la URL, no los bytes del video; máximo 2 MiB de JSON"}, 413)
        if req.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            return reply({"error": "unsupported_media_type", "message": "Usa Content-Type: application/json"}, 415)
        job = JobRequest.model_validate(req.get_json())
        policy = DownloadPolicy.from_env()
        source = policy.validate(job.video_url)
        payload = job.private_payload()
        policy.prepare(job.video_url, payload["source_token"])
        if payload["source_token"] and source.hostname.lower() not in policy.token_hosts:
            raise VideoUrlError("El dominio no está autorizado para recibir un token de origen")
        settings = Settings.from_env()
        if settings.mode != "azure" or (not job.subtitles and not settings.transcriber_model):
            return reply({"error": "configuration_required", "message": "Configura RS_MODE=azure, inferencia y transcriptor; o adjunta VTT/SRT como texto"}, 503)
        job_id = uuid.uuid4().hex
        store = JobStore.from_env()
        await asyncio.to_thread(store.create, job_id, identity.owner,
            {"request": payload, "principal": identity.principal.model_dump()})
        try:
            await client.start_new("recording_orchestrator", instance_id=job_id, client_input={"job_id": job_id})
        except Exception:
            # No se marca Failed: la respuesta del runtime puede haberse perdido DESPUÉS de encolar.
            # El cliente recibe el ID y puede consultar/reintentar el despacho del mismo trabajo.
            return reply({"job_id": job_id, "status": "DispatchUnknown", "status_url": f"/api/recordings/{job_id}",
                          "retry_url": f"/api/recordings/{job_id}/dispatch"}, 202)
        return reply({"job_id": job_id, "status": "Queued", "status_url": f"/api/recordings/{job_id}",
                      "result_url": f"/api/recordings/{job_id}/result"}, 202,
                     {"Location": f"/api/recordings/{job_id}", "Retry-After": "10"})
    except Exception as exc:
        return error_response(exc)


@app.route(route="recordings/{job_id}/dispatch", methods=["POST"])
@app.durable_client_input(client_name="client")
async def dispatch_recording(req: func.HttpRequest, client):
    """Recupera un despacho incierto; nunca reemplaza una instancia Durable existente."""
    try:
        identity = await asyncio.to_thread(authenticate, req, "recordings:write")
        job_id = req.route_params["job_id"]
        store = JobStore.from_env()
        state = await asyncio.to_thread(store.state, job_id, identity.owner)
        existing = await client.get_status(job_id)
        if existing is None and state["status"] == "Queued":
            # Un lease impide que dos clientes creen simultáneamente el mismo instance_id.
            def reserve():
                """Adquiere el lease fuera del loop asíncrono."""
                guard = store.lock(job_id)
                guard.__enter__()
                return guard
            guard = await asyncio.to_thread(reserve)
            try:
                existing = await client.get_status(job_id)
                if existing is None:
                    await client.start_new("recording_orchestrator", instance_id=job_id, client_input={"job_id": job_id})
            finally:
                await asyncio.to_thread(guard.__exit__, None, None, None)
        return reply({"job_id": job_id, "status_url": f"/api/recordings/{job_id}"}, 202)
    except Exception as exc:
        return error_response(exc)


@app.route(route="recordings/{job_id}", methods=["GET"])
@app.durable_client_input(client_name="client")
async def recording_status(req: func.HttpRequest, client):
    """Consulta el estado propio; no devuelve inputs ni management URLs de Durable."""
    try:
        identity = await asyncio.to_thread(authenticate, req, "recordings:read")
        job_id = req.route_params["job_id"]
        state = await asyncio.to_thread(JobStore.from_env().state, job_id, identity.owner)
        public = {k: v for k, v in state.items() if k != "owner"}
        if state["status"] not in {"Completed", "Failed"}:
            durable = await client.get_status(job_id)
            if durable is None:
                public["status"] = "DispatchPending"
                public["retry_url"] = f"/api/recordings/{job_id}/dispatch"
            elif str(durable.runtime_status.value) in {"Failed", "Terminated", "Canceled"}:
                public["status"] = "Interrupted"
                public["message"] = "El runtime interrumpió el trabajo; revisa el despliegue antes de enviar uno nuevo"
        if state["status"] == "Completed":
            public["result_url"] = f"/api/recordings/{job_id}/result"
        return reply(public)
    except Exception as exc:
        return error_response(exc)


@app.route(route="recordings/{job_id}/result", methods=["GET"])
async def recording_result(req: func.HttpRequest):
    """Devuelve el paquete completo por una ruta autenticada, sin hacer públicos los blobs."""
    try:
        identity = await asyncio.to_thread(authenticate, req, "recordings:read")
        job_id = req.route_params["job_id"]
        store = JobStore.from_env()
        state = await asyncio.to_thread(store.state, job_id, identity.owner)
        if state["status"] != "Completed":
            return reply({"error": "result_not_ready", "status": state["status"]}, 409)
        data = await asyncio.to_thread(store.result, job_id, identity.owner)
        return func.HttpResponse(data, mimetype="application/zip", headers={"Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="capacitacion.zip"', "X-Content-Type-Options": "nosniff"})
    except Exception as exc:
        return error_response(exc)


@app.orchestration_trigger(context_name="context")
def recording_orchestrator(context):
    """Orquestación determinista: no realiza red, descarga ni maneja tokens."""
    options = df.RetryOptions(30000, 120)
    result = yield context.call_activity_with_retry("process_recording", options, context.get_input())
    return result


@app.activity_trigger(input_name="reference")
def process_recording(reference: dict):
    """Una actividad ejecuta descarga y pipeline completo; requiere plan con ejecuciones largas."""
    return process_job(reference)
