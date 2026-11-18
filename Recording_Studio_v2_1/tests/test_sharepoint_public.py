"""Pruebas de enlaces públicos simulados y revocación segura después de persistir el video."""
from email.message import Message
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit
from recording_studio.application.function_worker import process_job
from recording_studio.application.service import RecordingService
from recording_studio.infrastructure.job_store import JobBusy
from recording_studio.integrations.video_url import DownloadPolicy, VideoDownloader, VideoUrlError
from recording_studio.integrations.sharepoint_public import prefer_download, AnonymousCookies
from test_auth_url import Response
from test_functions import MemoryStore
from test_service import FakeModel, FakeMedia


class SharePointResponse(Response):
    """Incluye headers HTTP multivalor para ejercitar cookies sin conectarse a Microsoft."""
    def __init__(self, body=b"binary-video", status=200, headers=None, cookies=()):
        """Construye una respuesta con Set-Cookie y MIME realista."""
        super().__init__(body, status, headers)
        raw = self.headers
        self.headers = Message()
        for key, value in raw.items():
            if value is not None:
                self.headers[key] = value
        for value in cookies:
            self.headers["Set-Cookie"] = value


class PublicLinkTests(unittest.TestCase):
    """No presume acceso anónimo: prueba el intento y su rechazo seguro cuando no entrega video."""
    def setUp(self):
        """Autoriza solo dos tenants ficticios para comprobar aislamiento entre hosts."""
        self.host = "tenant.sharepoint.com"
        self.url = "https://tenant.sharepoint.com/:v:/s/Training/OPAQUE?e=A%2BB%3D&web=1"
        self.policy = DownloadPolicy(frozenset({self.host, "other.sharepoint.com", "cdn.example"}),
            sharepoint_public_hosts=frozenset({self.host, "other.sharepoint.com"}))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "video.mp4"

    def download(self, responses):
        """Usa DNS y HTTP simulados manteniendo política, cookies y streaming reales."""
        connections = []
        for response in responses:
            connection = MagicMock()
            connection.getresponse.return_value = response
            connections.append(connection)
        with patch("recording_studio.integrations.video_url.public_addresses", return_value=["8.8.8.8"]), \
             patch("recording_studio.integrations.video_url.PinnedHTTPSConnection", side_effect=connections):
            result = VideoDownloader(self.policy).download(self.url, self.path)
        return result, connections

    def test_query_preserves_sharing_secrets_and_does_not_rewrite_direct_signed_url(self):
        """Conserva bytes del token, elimina preferencia web y no altera URLs directas firmadas."""
        value, public = self.policy.prepare(self.url)
        self.assertTrue(public)
        self.assertEqual(value, "https://tenant.sharepoint.com/:v:/s/Training/OPAQUE?e=A%2BB%3D&download=1")
        self.assertEqual(prefer_download(urlsplit(value)), value)
        direct = "https://tenant.sharepoint.com/direct.mp4?sig=A%2BB%3D&web=1"
        self.assertEqual(self.policy.prepare(direct), (direct, False))

    def test_redirects_anonymous_cookies_and_complete_video(self):
        """Sigue guestaccess/download solo con cookies recién emitidas, sin JWT ni Graph."""
        result, connections = self.download([
            SharePointResponse(status=302, headers={"Location": "/sites/Training/_layouts/15/guestaccess.aspx?share=OPAQUE"},
                cookies=("AnonymousSession=ephemeral; Path=/; Secure; HttpOnly",)),
            SharePointResponse(status=302, headers={"Location": "/sites/Training/_layouts/15/download.aspx?share=OPAQUE"}),
            SharePointResponse()])
        self.assertEqual(result["bytes"], 12)
        self.assertEqual(self.path.read_bytes(), b"binary-video")
        headers = [c.request.call_args.kwargs["headers"] for c in connections]
        self.assertNotIn("Cookie", headers[0])
        self.assertIn("AnonymousSession=ephemeral", headers[1]["Cookie"])
        self.assertIn("AnonymousSession=ephemeral", headers[2]["Cookie"])
        self.assertTrue(all("Authorization" not in h for h in headers))
        self.assertIn("download=1", connections[1].request.call_args.args[1])

    def test_cookies_never_cross_hosts_or_persist_across_downloads(self):
        """Rechaza cookies del dominio padre y no comparte sesiones entre tenants o descargas."""
        cookies = AnonymousCookies()
        response = SharePointResponse(cookies=("safe=value; Path=/; Secure", "broad=secret; Domain=.sharepoint.com; Path=/; Secure"))
        cookies.receive(self.url, response)
        self.assertIn("safe=value", cookies.headers(self.url)["Cookie"])
        self.assertNotIn("broad", cookies.headers(self.url)["Cookie"])
        self.assertEqual(cookies.headers("https://other.sharepoint.com/file.mp4"), {})
        self.assertEqual(cookies.headers("https://sub.tenant.sharepoint.com/file.mp4"), {})
        self.assertEqual(AnonymousCookies().headers(self.url), {})

    def test_login_html_blocked_download_and_revoked_link_never_publish_video(self):
        """La preferencia de descarga no evita login, contraseñas, revocación o bloqueo del origen."""
        variants = [SharePointResponse(status=302, headers={"Location": "https://login.microsoftonline.com/common/oauth2/authorize"}),
            SharePointResponse(b"<html>login or password</html>", headers={"Content-Type": "text/html"}),
            SharePointResponse(status=403), SharePointResponse(status=404), SharePointResponse(status=410)]
        for response in variants:
            with self.subTest(status=response.status), self.assertRaises(VideoUrlError) as raised:
                self.download([response])
            self.assertNotIn("OPAQUE", str(raised.exception))
            self.assertFalse(self.path.exists())
            self.assertFalse(self.path.with_name("video.mp4.partial").exists())

    def test_feature_requires_exact_enabled_sharepoint_host_and_no_source_bearer(self):
        """No se amplía el permiso a todos los tenants ni a hosts parecidos."""
        with self.assertRaises(VideoUrlError):
            DownloadPolicy(self.policy.allowed_hosts).prepare(self.url)
        with self.assertRaises(VideoUrlError):
            self.policy.prepare(self.url, "API.JWT")
        with self.assertRaises(RuntimeError):
            DownloadPolicy(frozenset({"evil.example"}), sharepoint_public_hosts=frozenset({"evil.example"}))
        with self.assertRaises(VideoUrlError):
            self.policy.prepare(self.url.replace("tenant.sharepoint.com", "tenant.sharepoint.com.evil.example"))


class RevocationTests(unittest.TestCase):
    """Una copia persistente permite reintentar después de retirar el enlace público."""
    def setUp(self):
        """Prepara un trabajo con transcripción ficticia para evitar llamadas externas."""
        self.store = MemoryStore()
        self.job = "c" * 32
        self.store.create(self.job, "owner", {"principal": {"tenant_id": "t", "user_id": "u"}, "request": {
            "video_url": "https://tenant.sharepoint.com/:v:/s/site/OPAQUE", "brief": "Explicar el proceso",
            "subtitles": "WEBVTT\n\n00:00:00.000 --> 00:01:00.000\nRegistrar responsables.\n"}})
        self.downloads = 0
        parent = self

        class Downloader:
            """Simula el origen público; la segunda descarga equivale a un enlace ya revocado."""
            policy = DownloadPolicy(frozenset({"tenant.sharepoint.com"}),
                sharepoint_public_hosts=frozenset({"tenant.sharepoint.com"}))

            def download(self, url, path, source_token):
                """Entrega el archivo una sola vez y detecta reintentos incorrectos al origen."""
                parent.downloads += 1
                if parent.downloads > 1:
                    raise VideoUrlError("Enlace revocado")
                Path(path).write_bytes(b"fixture-video")
                return {"bytes": 13, "download_seconds": 0}

        self.downloader = Downloader()

    def test_source_signal_after_backup_and_resume_without_public_link(self):
        """Interrumpe el análisis, revoca el origen y completa usando solo la copia privada."""
        media = FakeMedia()
        media.duration = MagicMock(side_effect=JobBusy("Simula interrupción del worker"))

        def first(settings):
            """Falla después de la descarga y respaldo, antes del análisis."""
            return RecordingService(settings, model=FakeModel(), media=media, downloader=self.downloader)

        with self.assertRaises(JobBusy):
            process_job({"job_id": self.job}, self.store, first)
        self.assertTrue(self.store.state(self.job)["source_link_can_be_revoked"])
        self.assertEqual(self.store.sources[self.job], b"fixture-video")

        def resumed(settings):
            """El pipeline real debe completar sin volver a descargar desde SharePoint."""
            return RecordingService(settings, model=FakeModel(), media=FakeMedia(), downloader=self.downloader)

        result = process_job({"job_id": self.job}, self.store, resumed)
        self.assertEqual(result["status"], "Completed")
        self.assertEqual(self.downloads, 1)
        self.assertNotIn(self.job, self.store.sources)

    def test_backup_failure_never_claims_link_can_be_revoked(self):
        """Si Storage falla, una descarga local por sí sola no dispara la señal segura."""
        self.store.save_source = MagicMock(side_effect=RuntimeError("Storage unavailable"))

        def factory(settings):
            """Conserva pipeline real y sustituye únicamente proveedores externos."""
            return RecordingService(settings, model=FakeModel(), media=FakeMedia(), downloader=self.downloader)

        result = process_job({"job_id": self.job}, self.store, factory)
        self.assertEqual(result["status"], "Failed")
        self.assertFalse(self.store.state(self.job)["source_link_can_be_revoked"])
        self.assertFalse(self.store.state(self.job)["source_downloaded"])
