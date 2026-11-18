"""Pruebas de firma JWT, controles de red y streaming sin servicios externos."""
import base64
from io import BytesIO
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from recording_studio.infrastructure.auth import JwtVerifier, AuthenticationError, AuthorizationError
from recording_studio.infrastructure.job_store import RequestCipher
from recording_studio.integrations.video_url import DownloadPolicy, VideoDownloader, VideoUrlError, public_addresses, PinnedHTTPSConnection


def keys():
    """Genera claves efímeras de prueba, sin incluir claves privadas en el repositorio."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def signed(key, **claims):
    """Emite claims válidos salvo las variantes que cada prueba necesita invalidar."""
    now = int(time.time())
    return jwt.encode({"iss": "https://issuer.example/", "aud": "recording-api", "sub": "user-a",
        "iat": now, "exp": now + 3600, "scope": "recordings:write recordings:read", **claims}, key, algorithm="RS256")


class JwtTests(unittest.TestCase):
    """Comprueba la identidad real que se utiliza al autorizar trabajos."""
    @classmethod
    def setUpClass(cls):
        """Crea una pareja de claves para todas las variantes de validación."""
        cls.private, cls.public = keys()

    def setUp(self):
        """Fija emisor y audiencia desde el servidor."""
        self.verifier = JwtVerifier("https://issuer.example/", "recording-api", public_key=self.public)

    def test_valid_identity_scopes_and_stable_owner(self):
        """El mismo sujeto conserva su dueño; otro sujeto o emisor no lo comparte."""
        identity = self.verifier.verify("Bearer " + signed(self.private))
        identity.require("recordings:read")
        self.assertEqual(identity.owner, self.verifier.verify("Bearer " + signed(self.private)).owner)
        self.assertNotEqual(identity.owner, self.verifier.verify("Bearer " + signed(self.private, sub="user-b")).owner)
        self.assertEqual(len(identity.principal.tenant_id), 64)

    def test_rejects_wrong_issuer_audience_expiry_key_and_future_issue(self):
        """Cambiar datos de confianza o usar otra firma no permite entrar."""
        other, _ = keys()
        tokens = [signed(self.private, iss="attacker"), signed(self.private, aud="other"),
                  signed(self.private, exp=int(time.time()) - 100), signed(other),
                  signed(self.private, iat=int(time.time()) + 1000)]
        for token in tokens:
            with self.subTest(token=token[-8:]), self.assertRaises(AuthenticationError):
                self.verifier.verify("Bearer " + token)

    def test_rejects_unsigned_missing_claims_and_scope_escalation(self):
        """No acepta alg=none, JWT incompleto ni un token de lectura para crear trabajos."""
        for value in ["", "Basic abc", "Bearer " + jwt.encode({"sub": "x"}, key="", algorithm="none"),
                      "Bearer " + jwt.encode({"sub": "x"}, self.private, algorithm="RS256")]:
            with self.assertRaises(AuthenticationError):
                self.verifier.verify(value)
        identity = self.verifier.verify("Bearer " + signed(self.private, scope="recordings:read"))
        with self.assertRaises(AuthorizationError):
            identity.require("recordings:write")

    def test_jwks_uses_server_endpoint_and_still_verifies_signature(self):
        """El header jku no reemplaza la URL JWKS elegida por el administrador."""
        with patch("recording_studio.infrastructure.auth.jwks_client") as client:
            client.return_value.get_signing_key_from_jwt.return_value.key = self.public
            verifier = JwtVerifier("https://issuer.example/", "recording-api", jwks_url="https://issuer.example/keys")
            token = jwt.encode(jwt.decode(signed(self.private), options={"verify_signature": False}), self.private,
                               algorithm="RS256", headers={"jku": "https://attacker.example/keys"})
            verifier.verify("Bearer " + token)
            client.assert_called_once_with("https://issuer.example/keys")


class CipherTests(unittest.TestCase):
    """Los tokens de origen y URLs firmadas no quedan visibles en el almacenamiento."""
    def test_encrypts_authenticates_and_binds_to_job(self):
        """Otra clave, ciphertext alterado u otro job_id impiden descifrar la solicitud."""
        cipher = RequestCipher(base64.b64encode(b"k" * 32).decode())
        data = {"video_url": "https://video.example/movie?sig=PRIVATE", "source_token": "SECRET"}
        encrypted = cipher.encrypt("a" * 32, data)
        self.assertNotIn(b"SECRET", encrypted)
        self.assertNotIn(b"PRIVATE", encrypted)
        self.assertEqual(cipher.decrypt("a" * 32, encrypted), data)
        for job, payload in [("b" * 32, encrypted), ("a" * 32, encrypted[:-1] + bytes([encrypted[-1] ^ 1]))]:
            with self.assertRaises(InvalidTag):
                cipher.decrypt(job, payload)


class Response(BytesIO):
    """Respuesta HTTP pequeña con el mismo contrato de streaming que http.client."""
    def __init__(self, body=b"binary-video", status=200, headers=None):
        """Declara MIME y tamaño para que las pruebas puedan crear respuestas malformadas."""
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": "video/mp4", "Content-Length": str(len(body)), **(headers or {})}

    def getheader(self, name, default=None):
        """Obtiene un header con valor por defecto como HTTPResponse."""
        return self.headers.get(name, default)


class DownloadTests(unittest.TestCase):
    """Ejercita las fronteras entre enlace de usuario, DNS, credencial y bytes de video."""
    def setUp(self):
        """Crea un destino aislado y una política de dominios exactos."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = DownloadPolicy(frozenset({"video.example", "cdn.example"}), frozenset({"video.example"}))

    def tearDown(self):
        """Elimina solo el directorio temporal de esta prueba."""
        self.temp.cleanup()

    def download(self, responses, token=None, policy=None):
        """Sustituye el transporte manteniendo la validación y lógica de descarga reales."""
        connections = []
        for response in responses:
            connection = MagicMock()
            connection.getresponse.return_value = response
            connections.append(connection)
        with patch("recording_studio.integrations.video_url.public_addresses", return_value=["8.8.8.8"]), \
             patch("recording_studio.integrations.video_url.PinnedHTTPSConnection", side_effect=connections) as factory:
            result = VideoDownloader(policy or self.policy).download("https://video.example/a.mp4?sig=PRIVATE", self.root / "video.mp4", token)
        return result, connections, factory

    def test_streams_without_api_token_and_cleans_partial(self):
        """No crea Authorization cuando el cliente no suministra una credencial de origen."""
        result, connections, factory = self.download([Response()])
        self.assertEqual(result["bytes"], 12)
        self.assertNotIn("Authorization", connections[0].request.call_args.kwargs["headers"])
        self.assertEqual((self.root / "video.mp4").read_bytes(), b"binary-video")
        self.assertFalse((self.root / "video.mp4.partial").exists())
        self.assertEqual(factory.call_args.args[:2], ("video.example", "8.8.8.8"))

    def test_preserves_token_on_same_host_and_strips_it_permanently_across_hosts(self):
        """Un redirect al CDN y de regreso nunca vuelve a incorporar la credencial inicial."""
        responses = [Response(status=302, headers={"Location": "/next"}),
            Response(status=302, headers={"Location": "https://cdn.example/file"}),
            Response(status=302, headers={"Location": "https://video.example/last"}), Response()]
        _, connections, _ = self.download(responses, token="SOURCE.JWT")
        auth = [c.request.call_args.kwargs["headers"].get("Authorization") for c in connections]
        self.assertEqual(auth, ["Bearer SOURCE.JWT", "Bearer SOURCE.JWT", None, None])

    def test_rejects_html_oversize_truncation_and_auth_failure_without_publishing(self):
        """Nunca deja un video parcial ni muestra la query firmada en mensajes de error."""
        responses = [Response(b"<html>login</html>", headers={"Content-Type": "text/html"}),
            Response(b"<html>login</html>"), Response(headers={"Content-Length": str(5 * 1024**3)}),
            Response(headers={"Content-Length": "999"}), Response(status=401)]
        for response in responses:
            with self.subTest(status=response.status), self.assertRaises(VideoUrlError) as error:
                self.download([response])
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_length_is_limited_while_streaming(self):
        """El límite no depende del Content-Length proporcionado por un servidor remoto."""
        policy = DownloadPolicy(self.policy.allowed_hosts, max_bytes=5)
        with self.assertRaises(VideoUrlError):
            self.download([Response(headers={"Content-Length": None})], policy=policy)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_bad_urls_teams_player_and_unauthorized_redirect(self):
        """No interpreta un enlace de reunión como video ni amplía la lista de dominios."""
        urls = ["http://video.example/a", "https://video.example:8443/a", "https://user:pass@video.example/a",
            "https://evil.example/a", "https://teams.microsoft.com/l/meetup-join/a", "https://video.example/_layouts/15/stream.aspx?id=a",
            "https://video.example/a#fragment", "https://video.example/a\r\nHeader:x"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(VideoUrlError):
                self.policy.validate(url)
        with self.assertRaises(VideoUrlError):
            self.download([Response(status=302, headers={"Location": "https://evil.example/private"})])

    def test_blocks_private_and_mixed_dns_answers(self):
        """Incluso un host autorizado debe resolver exclusivamente a direcciones públicas."""
        for ip in ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fd00::1", "224.0.0.1"]:
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                       (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=records), self.assertRaises(VideoUrlError):
                public_addresses("video.example")

    def test_tls_connects_pinned_ip_with_original_hostname(self):
        """Verifica que el transporte no vuelva a resolver DNS y mantenga TLS/SNI correcto."""
        connection = PinnedHTTPSConnection("video.example", "8.8.8.8", 5)
        connection._context = MagicMock()
        with patch("socket.create_connection") as connect:
            connection.connect()
            connect.assert_called_once_with(("8.8.8.8", 443), timeout=5)
            self.assertEqual(connection._context.wrap_socket.call_args.kwargs["server_hostname"], "video.example")

    def test_source_token_requires_explicit_host_permission(self):
        """Una URL autorizada no basta para que su dominio pueda recibir tokens."""
        policy = DownloadPolicy(self.policy.allowed_hosts)
        with self.assertRaises(VideoUrlError):
            self.download([Response()], token="SOURCE.JWT", policy=policy)
