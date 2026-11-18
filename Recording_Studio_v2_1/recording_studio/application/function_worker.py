"""Ejecuta el pipeline aprobado en una actividad Durable con estado persistente."""
from dataclasses import replace
from pathlib import Path
import tempfile
from recording_studio.application.jobs import JobRequest
from recording_studio.application.service import RecordingService
from recording_studio.domain.models import Principal
from recording_studio.infrastructure.config import Settings
from recording_studio.infrastructure.job_store import JobStore, JobBusy, timestamp
from recording_studio.infrastructure.providers import ProviderError
from recording_studio.integrations.video_url import VideoUrlError


def process_job(reference, store=None, service_factory=RecordingService):
    """El historial recibe solo job_id; URLs y tokens permanecen en la solicitud cifrada."""
    store = store or JobStore.from_env()
    job_id = reference["job_id"]
    with store.lock(job_id) as check:
        state = store.state(job_id)
        if state["status"] in {"Completed", "Failed"}:
            return {"job_id": job_id, "status": state["status"]}
        private = store.request(job_id)
        request = JobRequest.model_validate(private["request"])
        principal = Principal.model_validate(private["principal"])
        check()
        store.update(job_id, status="Running", message="Iniciando procesamiento", source_downloaded=False,
                     source_link_can_be_revoked=False)

        def progress(message):
            """Persiste mensajes del pipeline sin URLs, transcripciones ni credenciales."""
            check()
            store.update(job_id, status="Running", message=message)

        def downloaded(path, metadata):
            """La señal de revocación se emite solo después de persistir la copia completa."""
            progress("Descarga completa; guardando copia privada para reintentos")
            store.save_source(job_id, path, check)
            check()
            store.update(job_id, source_downloaded=True, source_link_can_be_revoked=True,
                source_ready_at=timestamp(), source_bytes=metadata["bytes"],
                message="Video guardado en copia privada. Ya puedes revocar el enlace público; el análisis continúa")

        try:
            # Nunca se espera que el siguiente worker encuentre archivos locales de este host.
            with tempfile.TemporaryDirectory(prefix="recording-job-") as temporary:
                root = Path(temporary)
                settings = replace(Settings.from_env(), data_dir=root / "runtime")
                service = service_factory(settings)
                token = request.source_token.get_secret_value() if request.source_token else None
                if store.has_source(job_id):
                    source = root / "recording.mp4"
                    store.restore_source(job_id, source)
                    check()
                    store.update(job_id, source_downloaded=True, source_link_can_be_revoked=True,
                        message="Reintentando con la copia privada; no se necesita el enlace original")
                    result = service.generate_file(principal, source, request.brief, root / "result",
                        subtitles=request.subtitles, theme=request.theme, workspace_id=job_id,
                        progress=progress, include_word=request.include_word)
                    result["elapsed_with_download_seconds"] = result["elapsed_seconds"]
                else:
                    result = service.generate_url(principal, request.video_url, request.brief, root / "result",
                        source_token=token, subtitles=request.subtitles, theme=request.theme,
                        workspace_id=job_id, progress=progress, include_word=request.include_word,
                        on_downloaded=downloaded)
                check()
                store.publish(job_id, root / "result/paquete.zip", check)
                check()
                store.update(job_id, status="Completed", message="Presentación y evaluación disponibles",
                    files=result["files"], elapsed_seconds=result["elapsed_with_download_seconds"])
        except JobBusy:
            raise
        except Exception as exc:
            # No registrar str(exc): errores de terceros pueden incluir URL firmada o contenido.
            if isinstance(exc, VideoUrlError):
                code, message = "video_download_failed", str(exc)
            elif isinstance(exc, ProviderError):
                code, message = "model_failed", str(exc)
            else:
                code, message = "processing_failed", "No se completó la generación. Revisa configuración del modelo, audio, límites y FFmpeg; crea un nuevo trabajo al corregirlos"
            check()
            store.update(job_id, status="Failed", error_code=code, message=message)
        # Un trabajo finalizado no necesita conservar credenciales ni URL firmada.
        try:
            store.remove_request(job_id)
        except Exception:
            pass  # La política de retención de Storage actúa también como respaldo.
        try:
            store.remove_source(job_id)
        except Exception:
            pass  # Lifecycle debe cubrir fuentes que queden después de una interrupción.
        return {"job_id": job_id, "status": store.state(job_id)["status"]}
