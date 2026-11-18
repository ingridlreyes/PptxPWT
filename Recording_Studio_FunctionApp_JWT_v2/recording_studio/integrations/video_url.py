"""Descarga HTTPS por streaming con destinos explícitos y sin Graph ni cookies de navegador."""
from dataclasses import dataclass
import http.client
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin, quote


class VideoUrlError(ValueError):
    """Error de entrada o descarga sin URL, query ni token en su mensaje."""


def host_list(value):
    """Interpreta una lista exacta de dominios; no admite comodines ni puertos."""
    hosts = frozenset(h.strip().lower() for h in value.split(",") if h.strip())
    for host in hosts:
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in host) or ".." in host or host.startswith(".") or host.endswith("."):
            raise RuntimeError("Configura dominios exactos, sin comodines, esquema ni puerto")
    return hosts


@dataclass(frozen=True)
class DownloadPolicy:
    """Controla destinos, credenciales, tamaño y tiempo de descarga desde el backend."""
    allowed_hosts: frozenset[str]
    token_hosts: frozenset[str] = frozenset()
    max_bytes: int = 4 * 1024**3
    timeout_seconds: int = 1800
    socket_timeout: int = 30
    max_redirects: int = 5

    def __post_init__(self):
        """Evita autorizaciones de tokens más amplias que los destinos de descarga."""
        if not self.token_hosts <= self.allowed_hosts:
            raise RuntimeError("RS_VIDEO_TOKEN_HOSTS debe ser un subconjunto de RS_VIDEO_ALLOWED_HOSTS")
        if not 1 <= self.max_bytes <= 4 * 1024**3 or not 10 <= self.timeout_seconds <= 7200:
            raise RuntimeError("Límite de descarga inválido")

    @classmethod
    def from_env(cls):
        """Por defecto no autoriza ningún destino; el operador debe configurar hosts."""
        return cls(host_list(os.getenv("RS_VIDEO_ALLOWED_HOSTS", "")),
                   host_list(os.getenv("RS_VIDEO_TOKEN_HOSTS", "")),
                   int(os.getenv("RS_VIDEO_MAX_BYTES", str(4 * 1024**3))),
                   int(os.getenv("RS_VIDEO_DOWNLOAD_TIMEOUT", "1800")))

    def validate(self, url):
        """Valida la URL sin consultar DNS ni intentar iniciar una sesión de Microsoft."""
        if not isinstance(url, str) or not url or len(url) > 16000 or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
            raise VideoUrlError("URL de video inválida")
        try:
            p = urlsplit(url)
            if p.scheme != "https" or not p.hostname or p.port not in (None, 443) or p.username or p.password or p.fragment:
                raise VideoUrlError("Usa una URL HTTPS sin usuario, contraseña, fragmento ni puerto alternativo")
        except ValueError:
            raise VideoUrlError("URL de video inválida") from None
        host = p.hostname.lower()
        if host in {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}:
            raise VideoUrlError("El enlace de reunión/chat de Teams no es un archivo. Proporciona una URL de descarga autorizada de la grabación")
        if (p.path.lower().endswith(".aspx") and not p.path.lower().endswith("/download.aspx")) or p.path.lower().startswith("/:v:/"):
            raise VideoUrlError("El enlace abre un reproductor de SharePoint/OneDrive. Se necesita una URL de descarga autorizada; un JWT de esta API no inicia sesión en Microsoft")
        if host not in self.allowed_hosts:
            raise VideoUrlError("El dominio del video no está autorizado por RS_VIDEO_ALLOWED_HOSTS")
        return p


def public_addresses(host):
    """Resuelve una vez y rechaza redes privadas, loopback, link-local y metadatos."""
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
            raise VideoUrlError("El destino resuelve a una red no permitida")
        return addresses
    except OSError:
        raise VideoUrlError("No se pudo resolver el dominio del video") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Conecta a la IP ya validada y mantiene verificación TLS del nombre original."""
    def __init__(self, host, address, timeout):
        """Fija la dirección validada para evitar una segunda resolución DNS."""
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        """No usa proxy ni túnel; SNI y certificado siguen correspondiendo al host."""
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class VideoDownloader:
    """Separa autorización del video y JWT de la API; nunca reenvía cookies o identidad."""
    def __init__(self, policy=None):
        """Utiliza una política del servidor o una política explícita de pruebas."""
        self.policy = policy or DownloadPolicy.from_env()

    def download(self, url, destination, source_token=None):
        """Guarda bytes progresivamente; valida cada redirección y elimina descargas incompletas."""
        first = self.policy.validate(url)
        if source_token and (first.hostname.lower() not in self.policy.token_hosts or
                             len(source_token) > 20000 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in source_token)):
            raise VideoUrlError("Token de origen inválido o dominio no autorizado para recibirlo")
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError("El destino de descarga ya existe")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        if partial.exists():
            raise FileExistsError("Ya existe una descarga parcial en este destino")
        started = time.monotonic()
        token = source_token
        current = url
        try:
            for hop in range(self.policy.max_redirects + 1):
                p = self.policy.validate(current)
                remaining = self.policy.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise VideoUrlError("La descarga excedió el tiempo permitido")
                addresses = public_addresses(p.hostname)
                connection = PinnedHTTPSConnection(p.hostname, addresses[0], min(self.policy.socket_timeout, remaining))
                try:
                    headers = {"Accept": "video/*, application/octet-stream", "Accept-Encoding": "identity", "User-Agent": "RecordingStudio/2.0"}
                    if token:
                        headers["Authorization"] = "Bearer " + token
                    target = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~") + (("?" + p.query) if p.query else "")
                    connection.request("GET", target, headers=headers)
                    response = connection.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.getheader("Location")
                        if not location or hop == self.policy.max_redirects:
                            raise VideoUrlError("Redirecciones de descarga inválidas o excesivas")
                        following = urljoin(current, location)
                        next_parts = self.policy.validate(following)
                        if next_parts.hostname.lower() != p.hostname.lower():
                            token = None  # No recuperarlo aunque la cadena vuelva al dominio original.
                        current = following
                        continue
                    if response.status in (401, 403):
                        raise VideoUrlError("El servidor no autorizó descargar el video. Revisa permisos y token de origen; el JWT de la API no concede acceso a Teams")
                    if response.status != 200:
                        raise VideoUrlError(f"El servidor del video devolvió HTTP {response.status}")
                    mime = (response.getheader("Content-Type") or "").split(";", 1)[0].lower().strip()
                    if mime not in ("application/octet-stream", "application/mp4", "application/x-matroska") and not mime.startswith("video/"):
                        raise VideoUrlError("El enlace no entrega un archivo de video; podría ser una página de inicio de sesión o reproductor")
                    encoding = response.getheader("Content-Encoding", "identity").lower()
                    if encoding != "identity":
                        raise VideoUrlError("La descarga debe entregar bytes de video sin compresión HTTP")
                    length = response.getheader("Content-Length")
                    expected = int(length) if length is not None else None
                    if expected is not None and (expected < 1 or expected > self.policy.max_bytes):
                        raise VideoUrlError("El video está vacío o supera el límite de descarga")
                    total = 0
                    with partial.open("xb") as stream:
                        while chunk := response.read1(1024 * 1024):
                            total += len(chunk)
                            if total > self.policy.max_bytes or time.monotonic() - started > self.policy.timeout_seconds:
                                raise VideoUrlError("El video supera el límite de tamaño o tiempo de descarga")
                            if total == len(chunk) and chunk.lstrip()[:20].lower().startswith((b"<!doctype html", b"<html")):
                                raise VideoUrlError("El enlace devolvió HTML en lugar del video")
                            stream.write(chunk)
                    if total == 0 or (expected is not None and total != expected):
                        raise VideoUrlError("La descarga del video quedó incompleta")
                    partial.rename(destination)
                    return {"bytes": total, "download_seconds": round(time.monotonic() - started, 2)}
                finally:
                    connection.close()
            raise VideoUrlError("No se completó la descarga")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if isinstance(exc, VideoUrlError):
                raise
            raise VideoUrlError("No se pudo descargar el video por HTTPS") from None
        finally:
            partial.unlink(missing_ok=True)
