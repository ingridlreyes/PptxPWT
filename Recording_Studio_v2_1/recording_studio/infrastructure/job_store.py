"""Estado y entregables privados en el Storage de la Function App; solicitudes cifradas."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Event, Thread
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError, HttpResponseError
from azure.storage.blob import BlobServiceClient, ContentSettings
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class JobNotFound(ValueError):
    """Trabajo inexistente o no perteneciente al usuario; no permite enumerar propietarios."""


class JobBusy(RuntimeError):
    """Otra ejecución mantiene el lease; Durable puede reintentar sin duplicar la publicación."""


def timestamp():
    """Fecha UTC estable para estado y diagnóstico del trabajo."""
    return datetime.now(timezone.utc).isoformat()


class RequestCipher:
    """AES-256-GCM con ID de trabajo como datos autenticados, clave fuera de Storage."""
    def __init__(self, key):
        """Requiere 32 bytes aleatorios codificados en base64, sin valor por defecto."""
        try:
            raw = base64.b64decode(key, validate=True)
            if len(raw) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise RuntimeError("RS_JOB_ENCRYPTION_KEY requiere 32 bytes aleatorios en base64") from None
        self.cipher = AESGCM(raw)

    def encrypt(self, job_id, payload):
        """Cifra también las URLs firmadas y el objetivo, además del token de origen."""
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(payload, ensure_ascii=False).encode(), job_id.encode())

    def decrypt(self, job_id, payload):
        """Autentica ciphertext y trabajo antes de devolver la solicitud al worker."""
        return json.loads(self.cipher.decrypt(payload[:12], payload[12:], job_id.encode()))


class JobStore:
    """Guarda datos grandes fuera del historial Durable y autoriza por propietario firmado."""
    def __init__(self, connection_string, encryption_key, container="recording-studio-jobs"):
        """Usa connection string; no necesita Entra ID, Graph ni una nueva cuenta de video."""
        if not connection_string:
            raise RuntimeError("Configura AzureWebJobsStorage o RS_STORAGE_CONNECTION_STRING")
        self.service = BlobServiceClient.from_connection_string(connection_string, logging_enable=False)
        self.container = self.service.get_container_client(container)
        self.cipher = RequestCipher(encryption_key)

    @classmethod
    def from_env(cls):
        """Reutiliza el Storage del runtime salvo que el operador seleccione otro existente."""
        return cls(os.getenv("RS_STORAGE_CONNECTION_STRING") or os.getenv("AzureWebJobsStorage", ""),
                   os.getenv("RS_JOB_ENCRYPTION_KEY", ""), os.getenv("RS_JOBS_CONTAINER", "recording-studio-jobs"))

    def ensure_container(self):
        """Crea un contenedor privado; falla si uno existente permite acceso público."""
        try:
            self.container.create_container()
        except ResourceExistsError:
            pass
        if self.container.get_container_properties().get("public_access"):
            raise RuntimeError("El contenedor de trabajos debe ser privado")

    def blob(self, job_id, name):
        """Solo admite IDs generados por el servidor y nombres internos conocidos."""
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or name not in {"request.bin", "state.json", "paquete.zip", "lock", "source.mp4"}:
            raise JobNotFound("Trabajo no encontrado")
        return self.container.get_blob_client(f"jobs/{job_id}/{name}")

    def create(self, job_id, owner, payload):
        """Publica la solicitud cifrada antes de permitir que el worker tome el trabajo."""
        self.ensure_container()
        self.blob(job_id, "request.bin").upload_blob(self.cipher.encrypt(job_id, payload), overwrite=False)
        state = {"job_id": job_id, "owner": owner, "status": "Queued", "message": "Trabajo recibido", "created_at": timestamp(),
                 "source_downloaded": False, "source_link_can_be_revoked": False}
        self.blob(job_id, "state.json").upload_blob(json.dumps(state), overwrite=False)

    def state(self, job_id, owner=None):
        """Verifica pertenencia en el mismo método que recupera el estado."""
        try:
            state = json.loads(self.blob(job_id, "state.json").download_blob().readall())
        except ResourceNotFoundError:
            raise JobNotFound("Trabajo no encontrado") from None
        if owner is not None and state["owner"] != owner:
            raise JobNotFound("Trabajo no encontrado")
        return state

    def update(self, job_id, **changes):
        """Actualiza solo desde el backend, preservando propietario y fecha de creación."""
        state = self.state(job_id)
        if {"owner", "job_id", "created_at"} & changes.keys():
            raise ValueError("Los campos de identidad del trabajo son inmutables")
        state.update(changes, updated_at=timestamp())
        self.blob(job_id, "state.json").upload_blob(json.dumps(state, ensure_ascii=False), overwrite=True)
        return state

    def request(self, job_id):
        """Recupera secretos únicamente dentro de la actividad de procesamiento."""
        return self.cipher.decrypt(job_id, self.blob(job_id, "request.bin").download_blob().readall())

    def remove_request(self, job_id):
        """Elimina URL y credencial cifradas al finalizar definitivamente el procesamiento."""
        self.blob(job_id, "request.bin").delete_blob()

    def has_source(self, job_id):
        """Comprueba la existencia de una copia fuente comprometida por completo en Storage."""
        return self.blob(job_id, "source.mp4").exists()

    def save_source(self, job_id, path, check_lease):
        """Persiste el video por streaming antes de permitir revocar el enlace del origen."""
        size = Path(path).stat().st_size
        if not 0 < size <= 4 * 1024**3:
            raise ValueError("Tamaño de copia fuente inválido")
        check_lease()
        with Path(path).open("rb") as stream:
            self.blob(job_id, "source.mp4").upload_blob(stream, overwrite=True, max_concurrency=1,
                content_settings=ContentSettings(content_type="application/octet-stream"))
        check_lease()

    def restore_source(self, job_id, path):
        """Restaura la copia privada para un reintento sin volver a acceder al enlace público."""
        blob = self.blob(job_id, "source.mp4")
        size = blob.get_blob_properties().size
        if not 0 < size <= 4 * 1024**3:
            raise ValueError("Tamaño de copia fuente inválido")
        with Path(path).open("xb") as stream:
            count = blob.download_blob(max_concurrency=1).readinto(stream)
        if count != size:
            Path(path).unlink(missing_ok=True)
            raise ValueError("La copia fuente de Storage quedó incompleta")

    def remove_source(self, job_id):
        """Retira la copia temporal después de un estado final; retención cubre interrupciones."""
        if self.has_source(job_id):
            self.blob(job_id, "source.mp4").delete_blob()

    def publish(self, job_id, path, check_lease):
        """Sube el ZIP completo antes de que el estado pueda indicar Completed."""
        if Path(path).stat().st_size > self.result_limit():
            raise ValueError("El paquete supera RS_MAX_RESULT_BYTES; reduce el presupuesto de láminas o capturas")
        check_lease()
        with Path(path).open("rb") as stream:
            self.blob(job_id, "paquete.zip").upload_blob(stream, overwrite=True, max_concurrency=1,
                content_settings=ContentSettings(content_type="application/zip", content_disposition='attachment; filename="capacitacion.zip"'))
        check_lease()

    @staticmethod
    def result_limit():
        """Limita la respuesta HTTP a un máximo de 100 MiB para este adaptador."""
        value = int(os.getenv("RS_MAX_RESULT_BYTES", str(100 * 1024**2)))
        if not 1024 <= value <= 100 * 1024**2:
            raise RuntimeError("RS_MAX_RESULT_BYTES debe estar entre 1024 y 104857600")
        return value

    def result(self, job_id, owner):
        """Entrega un ZIP solo al propietario y después de confirmar publicación completa."""
        if self.state(job_id, owner)["status"] != "Completed":
            raise ValueError("El trabajo todavía no tiene resultado")
        blob = self.blob(job_id, "paquete.zip")
        if blob.get_blob_properties().size > self.result_limit():
            raise ValueError("El paquete supera el límite de respuesta configurado")
        return blob.download_blob().readall()

    @contextmanager
    def lock(self, job_id):
        """Lease renovable para soportar entrega al menos una vez de las actividades Durable."""
        blob = self.blob(job_id, "lock")
        try:
            blob.upload_blob(b"lock", overwrite=False)
        except ResourceExistsError:
            pass
        try:
            lease = blob.acquire_lease(lease_duration=60)
        except HttpResponseError as exc:
            if exc.status_code == 409:
                raise JobBusy("El trabajo sigue en procesamiento") from None
            raise
        stopped, lost = Event(), Event()

        def renew():
            """Renueva mientras el worker esté vivo; una pérdida impide publicar resultados."""
            while not stopped.wait(20):
                try:
                    lease.renew()
                except Exception:
                    lost.set()
                    return

        def check():
            """Prueba el lease en el servidor antes de escribir cambios de estado definitivos."""
            if lost.is_set():
                raise JobBusy("Se perdió la reserva del trabajo; el runtime reintentará")
            try:
                lease.renew()
            except Exception:
                lost.set()
                raise JobBusy("Se perdió la reserva del trabajo; el runtime reintentará") from None

        worker = Thread(target=renew, daemon=True)
        worker.start()
        try:
            yield check
        finally:
            stopped.set()
            worker.join(timeout=5)
            try:
                lease.release()
            except HttpResponseError:
                pass
