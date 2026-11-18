"""Pruebas con Azure SDK contra Azurite o una cuenta de pruebas expresamente configurada."""
import base64
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from recording_studio.infrastructure.job_store import JobStore, JobNotFound, JobBusy


@unittest.skipUnless(os.getenv("RS_TEST_STORAGE"), "Configura RS_TEST_STORAGE para probar Azure SDK con Azurite")
class StorageIntegrationTests(unittest.TestCase):
    """Valida cifrado, aislamiento, publicación y lease usando llamadas reales al emulador."""
    def setUp(self):
        """Usa un contenedor aleatorio que esta prueba puede borrar de forma aislada."""
        self.store = JobStore(os.environ["RS_TEST_STORAGE"], base64.b64encode(os.urandom(32)).decode(),
                              container="rs-test-" + uuid.uuid4().hex)
        self.job_id = uuid.uuid4().hex
        self.payload = {"video_url": "https://video.example/a?sig=PRIVATE", "source_token": "SECRET"}
        self.store.create(self.job_id, "owner", self.payload)

    def tearDown(self):
        """Borra únicamente el contenedor aleatorio creado por esta prueba."""
        self.store.container.delete_container()
        self.store.service.close()

    def test_encryption_ownership_and_publish_roundtrip(self):
        """Los bytes de Storage no incluyen secretos y el ZIP exige el dueño correcto."""
        raw = self.store.blob(self.job_id, "request.bin").download_blob().readall()
        self.assertNotIn(b"SECRET", raw)
        self.assertNotIn(b"PRIVATE", raw)
        self.assertEqual(self.store.request(self.job_id), self.payload)
        with self.assertRaises(JobNotFound):
            self.store.state(self.job_id, "other")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "paquete.zip"
            path.write_bytes(b"PK-test-result")
            with self.store.lock(self.job_id) as check:
                self.store.publish(self.job_id, path, check)
                self.store.update(self.job_id, status="Completed")
        self.assertEqual(self.store.result(self.job_id, "owner"), b"PK-test-result")
        with self.assertRaises(JobNotFound):
            self.store.result(self.job_id, "other")
        self.store.remove_request(self.job_id)
        self.assertFalse(self.store.blob(self.job_id, "request.bin").exists())

    def test_second_worker_cannot_take_active_lease_and_can_resume_after_release(self):
        """Previene dos publicaciones simultáneas y permite el reintento posterior."""
        with self.store.lock(self.job_id) as check:
            check()
            with self.assertRaises(JobBusy):
                with self.store.lock(self.job_id):
                    self.fail("No debe adquirir otro lease")
        with self.store.lock(self.job_id) as check:
            check()

    def test_source_video_roundtrip_without_public_origin(self):
        """Respalda/restaura bytes reales con Azure SDK para reintentar tras revocar el enlace."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"fixture-source-video")
            with self.store.lock(self.job_id) as check:
                self.store.save_source(self.job_id, source, check)
            self.assertTrue(self.store.has_source(self.job_id))
            restored = Path(temporary) / "restored.mp4"
            self.store.restore_source(self.job_id, restored)
            self.assertEqual(source.read_bytes(), restored.read_bytes())
            self.store.remove_source(self.job_id)
            self.assertFalse(self.store.has_source(self.job_id))
