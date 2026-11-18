"""Verifica endpoints Functions, aislamiento de usuarios y procesamiento completo por URL."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
import azure.functions as func
import function_app
from recording_studio.application.function_worker import process_job
from recording_studio.application.service import RecordingService
from recording_studio.infrastructure.auth import JwtVerifier
from recording_studio.infrastructure.config import Settings
from recording_studio.infrastructure.job_store import JobNotFound, JobBusy
from recording_studio.integrations.video_url import DownloadPolicy
from test_auth_url import keys, signed
from test_service import FakeModel, FakeMedia


FUNCTIONS = {f.get_function_name(): inspect.unwrap(f.get_user_function()) for f in function_app.app.get_functions()}


class MemoryStore:
    """Adaptador efímero para aislar el contrato de endpoints; Azure SDK se prueba aparte."""
    def __init__(self):
        """Inicializa trabajos, solicitudes privadas y resultados de prueba."""
        self.states, self.requests, self.results, self.sources = {}, {}, {}, {}

    def create(self, job_id, owner, payload):
        """Registra la identidad autorizada por el endpoint."""
        self.states[job_id] = {"job_id": job_id, "owner": owner, "status": "Queued"}
        self.requests[job_id] = deepcopy(payload)

    def state(self, job_id, owner=None):
        """Devuelve el mismo 404 ante ausencia y propiedad ajena."""
        state = self.states.get(job_id)
        if state is None or (owner is not None and state["owner"] != owner):
            raise JobNotFound("Trabajo no encontrado")
        return deepcopy(state)

    def update(self, job_id, **changes):
        """Aplica cambios sin reemplazar la identidad del trabajo."""
        self.states[job_id].update(changes)

    def request(self, job_id):
        """Devuelve únicamente los datos requeridos por el worker."""
        return deepcopy(self.requests[job_id])

    def remove_request(self, job_id):
        """Simula eliminación de URL y credencial al terminar."""
        self.requests.pop(job_id, None)

    def has_source(self, job_id):
        """Consulta la copia privada de prueba."""
        return job_id in self.sources

    def save_source(self, job_id, path, check):
        """Guarda todos los bytes antes de publicar el permiso para revocar."""
        check()
        self.sources[job_id] = Path(path).read_bytes()

    def restore_source(self, job_id, path):
        """Recupera bytes sin usar el enlace del origen."""
        Path(path).write_bytes(self.sources[job_id])

    def remove_source(self, job_id):
        """Elimina la copia temporal al terminar."""
        self.sources.pop(job_id, None)

    def publish(self, job_id, path, check):
        """Guarda bytes del ZIP antes del estado Completed."""
        check()
        self.results[job_id] = Path(path).read_bytes()

    def result(self, job_id, owner):
        """Aplica la pertenencia también al resultado binario."""
        self.state(job_id, owner)
        return self.results[job_id]

    @contextmanager
    def lock(self, job_id):
        """Proporciona una reserva sin concurrencia para pruebas del worker."""
        yield lambda: None


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    """Ejecuta las funciones registradas con JWT firmados de verdad y cliente Durable simulado."""
    @classmethod
    def setUpClass(cls):
        """Crea claves efímeras de API para todo el grupo."""
        cls.private, cls.public = keys()

    def setUp(self):
        """Inyecta almacenamiento y configuración de inferencia sin llamadas a Azure."""
        self.store = MemoryStore()
        self.jwt = JwtVerifier("https://issuer.example/", "recording-api", public_key=self.public)
        self.client = MagicMock()
        self.client.start_new = AsyncMock(return_value="started")
        self.client.get_status = AsyncMock(return_value=None)
        self.settings = Settings(mode="azure", model_base_url="https://model.example/v1/", model_api_key="fake", writer_model="vision", transcriber_model="audio")
        self.patches = [patch.object(function_app, "verifier", return_value=self.jwt),
            patch.object(function_app.JobStore, "from_env", return_value=self.store),
            patch.object(function_app.Settings, "from_env", return_value=self.settings),
            patch.object(function_app.DownloadPolicy, "from_env", return_value=DownloadPolicy(frozenset({"video.example"}), frozenset({"video.example"})))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def request(self, method="POST", body=None, job_id=None, subject="user-a", scopes="recordings:write recordings:read", auth=True):
        """Construye una solicitud Azure real con claims firmados y campos conocidos."""
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + signed(self.private, sub=subject, scope=scopes)
        return func.HttpRequest(method, "https://function.example/api/recordings", headers=headers,
            params={}, route_params={"job_id": job_id} if job_id else {},
            body=json.dumps(body if body is not None else {"video_url": "https://video.example/a.mp4?sig=PRIVATE",
                "brief": "Explicar el proceso", "source_token": "SOURCE.JWT"}).encode())

    async def submit(self):
        """Crea un trabajo válido y devuelve su ID generado por el servidor."""
        response = await FUNCTIONS["submit_recording"](self.request(), self.client)
        self.assertEqual(response.status_code, 202, response.get_body())
        return json.loads(response.get_body())["job_id"]

    async def test_submit_separates_secrets_from_durable_history(self):
        """Durable solo recibe el identificador, nunca URL, JWT ni tokens de origen."""
        job_id = await self.submit()
        call = self.client.start_new.call_args
        self.assertEqual(call.kwargs["client_input"], {"job_id": job_id})
        self.assertEqual(self.store.requests[job_id]["request"]["source_token"], "SOURCE.JWT")
        self.assertEqual(self.store.requests[job_id]["principal"]["user_id"], self.jwt.verify(self.request().headers["Authorization"]).principal.user_id)

    async def test_missing_token_and_wrong_scope_never_queue(self):
        """Autenticación y autorización ocurren antes de gastar un trabajo de procesamiento."""
        for request, code in [(self.request(auth=False), 401), (self.request(scopes="recordings:read"), 403)]:
            response = await FUNCTIONS["submit_recording"](request, self.client)
            self.assertEqual(response.status_code, code)
        self.assertEqual(self.store.states, {})
        self.client.start_new.assert_not_called()

    async def test_cannot_choose_identity_paths_or_unknown_fields(self):
        """El cliente no puede suplantar usuario o pasar una ruta del servidor."""
        for field in ["owner", "user_id", "output_directory", "graph_reference"]:
            body = {"video_url": "https://video.example/a.mp4", "brief": "Explicar", field: "x"}
            response = await FUNCTIONS["submit_recording"](self.request(body=body), self.client)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.states, {})

    async def test_teams_link_and_missing_transcription_fail_before_queue(self):
        """Las limitaciones del enlace y audio se indican antes de iniciar el análisis."""
        response = await FUNCTIONS["submit_recording"](self.request(body={"video_url": "https://teams.microsoft.com/l/meetup-join/a", "brief": "Explicar"}), self.client)
        self.assertEqual(response.status_code, 400)
        with patch.object(function_app.Settings, "from_env", return_value=replace(self.settings, transcriber_model="")):
            response = await FUNCTIONS["submit_recording"](self.request(), self.client)
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.states, {})

    async def test_public_sharepoint_creation_requires_enabled_host_and_no_source_token(self):
        """Acepta solo el intento anónimo habilitado y conserva el link original en el body privado."""
        url = "https://tenant.sharepoint.com/:v:/s/training/OPAQUE?e=PRIVATE"
        policy = DownloadPolicy(frozenset({"tenant.sharepoint.com"}),
            sharepoint_public_hosts=frozenset({"tenant.sharepoint.com"}))
        body = {"video_url": url, "brief": "Explicar el proceso"}
        with patch.object(function_app.DownloadPolicy, "from_env", return_value=policy):
            response = await FUNCTIONS["submit_recording"](self.request(body=body), self.client)
            self.assertEqual(response.status_code, 202)
            job_id = json.loads(response.get_body())["job_id"]
            self.assertEqual(self.store.requests[job_id]["request"]["video_url"], url)
            self.assertNotIn("PRIVATE", response.get_body().decode())
            response = await FUNCTIONS["submit_recording"](self.request(body={**body, "source_token": "WRONG.JWT"}), self.client)
            self.assertEqual(response.status_code, 400)

    async def test_other_user_cannot_read_or_download(self):
        """El mismo ID con un JWT de otro usuario devuelve 404 sin revelar propietario."""
        job_id = await self.submit()
        status = await FUNCTIONS["recording_status"](self.request("GET", job_id=job_id, subject="other"), self.client)
        result = await FUNCTIONS["recording_result"](self.request("GET", job_id=job_id, subject="other"))
        self.assertEqual(status.status_code, 404)
        self.assertEqual(result.status_code, 404)

    async def test_status_and_result_hide_secrets_and_protect_unfinished_results(self):
        """La lectura no devuelve inputs ni permite descargar antes de completar."""
        job_id = await self.submit()
        response = await FUNCTIONS["recording_result"](self.request("GET", job_id=job_id))
        self.assertEqual(response.status_code, 409)
        self.store.update(job_id, status="Completed", message="Disponible")
        self.store.results[job_id] = b"PK-result"
        response = await FUNCTIONS["recording_status"](self.request("GET", job_id=job_id), self.client)
        text = response.get_body().decode()
        for secret in ["owner", "PRIVATE", "SOURCE.JWT", "video.example"]:
            self.assertNotIn(secret, text)
        result = await FUNCTIONS["recording_result"](self.request("GET", job_id=job_id))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_body(), b"PK-result")

    async def test_dispatch_unknown_can_be_recovered_without_overwriting_existing_instance(self):
        """Un fallo de confirmación devuelve el ID; reintentar no reemplaza un Durable activo."""
        self.client.start_new.side_effect = RuntimeError("private-provider-error")
        response = await FUNCTIONS["submit_recording"](self.request(), self.client)
        body = json.loads(response.get_body())
        self.assertEqual(body["status"], "DispatchUnknown")
        self.assertNotIn("private-provider-error", response.get_body().decode())
        self.client.start_new.side_effect = None
        self.client.get_status.return_value = None
        recovered = await FUNCTIONS["dispatch_recording"](self.request(job_id=body["job_id"]), self.client)
        self.assertEqual(recovered.status_code, 202)
        count = self.client.start_new.call_count
        self.client.get_status.return_value = MagicMock()
        await FUNCTIONS["dispatch_recording"](self.request(job_id=body["job_id"]), self.client)
        self.assertEqual(self.client.start_new.call_count, count)


class WorkerTests(unittest.TestCase):
    """Recorre URL -> pipeline original -> ZIP -> estado persistente con proveedores simulados."""
    def test_full_url_flow_preserves_four_steps_quiz_and_cleans_secrets(self):
        """Un frame mantiene sus cuatro pasos y la entrega contiene preguntas y marcas editables."""
        store = MemoryStore()
        job_id = "a" * 32
        store.create(job_id, "owner", {"principal": {"tenant_id": "t", "user_id": "u"}, "request": {
            "video_url": "https://video.example/a.mp4", "brief": "Explicar el proceso",
            "subtitles": "WEBVTT\n\n00:00:00.000 --> 00:01:00.000\nRegistrar propósito y responsables.\n"}})

        class Downloader:
            """Produce un archivo de prueba con validación de URL real y sin tráfico externo."""
            policy = DownloadPolicy(frozenset({"video.example"}))

            def download(self, url, path, source_token):
                """Suministra la evidencia que FakeMedia sabe procesar."""
                Path(path).write_bytes(b"fixture-video")
                return {"bytes": 13, "download_seconds": 0}

        def factory(settings):
            """Ejecuta el procesador y exportadores reales con un modelo explícitamente simulado."""
            return RecordingService(settings, model=FakeModel(), media=FakeMedia(), downloader=Downloader())

        result = process_job({"job_id": job_id}, store=store, service_factory=factory)
        self.assertEqual(result["status"], "Completed")
        self.assertNotIn(job_id, store.requests)
        from io import BytesIO
        from zipfile import ZipFile
        from pptx import Presentation
        with ZipFile(BytesIO(store.results[job_id])) as archive:
            deck = Presentation(BytesIO(archive.read("entregable.pptx")))
            self.assertEqual(len(deck.slides), 2)
            self.assertEqual(len(json.loads(archive.read("evaluacion.json"))["questions"]), 10)
            self.assertEqual(len(json.loads(archive.read("plan.json"))["groups"][0]["steps"]), 4)
        # Entrega al menos una vez no vuelve a llamar el modelo ni exige la solicitud eliminada.
        self.assertEqual(process_job({"job_id": job_id}, store=store)["status"], "Completed")

    def test_failure_is_sanitized_and_never_publishes_partial_zip(self):
        """El error de terceros no puede aparecer en estado, respuestas ni historial Durable."""
        store = MemoryStore()
        job_id = "b" * 32
        store.create(job_id, "owner", {"principal": {"tenant_id": "t", "user_id": "u"}, "request": {
            "video_url": "https://video.example/a?sig=PRIVATE", "brief": "Explicar", "source_token": "SECRET"}})
        result = process_job({"job_id": job_id}, store=store, service_factory=MagicMock(side_effect=RuntimeError("PRIVATE SECRET")))
        self.assertEqual(result["status"], "Failed")
        self.assertNotIn("SECRET", json.dumps(store.states))
        self.assertNotIn(job_id, store.results)
        self.assertNotIn(job_id, store.requests)
