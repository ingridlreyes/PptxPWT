"""Intenta descargar vínculos anónimos de SharePoint sin obtener permisos ni ejecutar HTML."""
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import unquote_plus, urlsplit
from urllib.request import Request


def is_sharing_path(path):
    """Reconoce enlaces de video/archivo y guestaccess; no identifica si el permiso es público."""
    lower = path.lower()
    return lower.startswith(("/:v:/", "/:u:/")) or lower.endswith("/guestaccess.aspx")


def prefer_download(parts):
    """Conserva bytes de parámetros de acceso y añade la preferencia de descarga una sola vez.

    download=1 es un intento de compatibilidad, no una API contractual ni un permiso.
    No se usa para reescribir URLs directas firmadas o construir IDs de archivos.
    """
    kept = [item for item in parts.query.split("&") if item and
            unquote_plus(item.partition("=")[0]).lower() not in {"download", "web"}]
    kept.append("download=1")
    return parts._replace(query="&".join(kept)).geturl()


class ExactHostCookiePolicy(DefaultCookiePolicy):
    """Acepta cookies nuevas solo para el host exacto que respondió, nunca todo SharePoint."""
    def set_ok(self, cookie, request):
        """Restringe tamaño, host y transporte antes de aceptar cookies anónimas efímeras."""
        if cookie.domain.lstrip(".").lower() != urlsplit(request.full_url).hostname.lower() or len(cookie.name) + len(cookie.value or "") > 16000:
            return False
        cookie.secure = True
        return super().set_ok(cookie, request)

    def return_ok(self, cookie, request):
        """No envía la cookie a subdominios ni tenants vecinos aunque estén autorizados."""
        return cookie.domain.lstrip(".").lower() == urlsplit(request.full_url).hostname.lower() and super().return_ok(cookie, request)


class ResponseHeaders:
    """Adapta las cabeceras de http.client al contrato estándar de CookieJar."""
    def __init__(self, response):
        """Retiene únicamente headers; no lee ni interpreta el body del reproductor."""
        self.headers = response.headers

    def info(self):
        """Proporciona Set-Cookie al analizador estándar de Python."""
        return self.headers


class AnonymousCookies:
    """Cookies de una sola descarga; no importa sesiones de usuario ni persiste credenciales."""
    def __init__(self):
        """Inicia un jar vacío con política de host exacto."""
        self.jar = CookieJar(policy=ExactHostCookiePolicy())

    def headers(self, url):
        """Entrega solo cookies emitidas por ese host en esta misma descarga."""
        req = Request(url)
        self.jar.add_cookie_header(req)
        value = req.get_header("Cookie")
        if value and len(value) > 32000:
            raise ValueError("Demasiadas cookies de descarga")
        return {"Cookie": value} if value else {}

    def receive(self, url, response):
        """Acepta cookies de una respuesta HTTPS ya validada, sin ejecutar páginas ni scripts."""
        self.jar.extract_cookies(ResponseHeaders(response), Request(url))
        if len(list(self.jar)) > 64:
            raise ValueError("Demasiadas cookies de descarga")
