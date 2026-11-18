"""Valida JWT de la API con emisor y audiencia fijados por el administrador."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import jwt
from recording_studio.domain.models import Principal


class AuthenticationError(ValueError):
    """Token ausente o inválido; nunca incluye el token en el mensaje."""


class AuthorizationError(ValueError):
    """Identidad válida sin el permiso requerido."""


@dataclass(frozen=True)
class Identity:
    """Identidad derivada exclusivamente de claims firmados y verificados."""
    issuer: str
    subject: str
    scopes: frozenset[str]

    @property
    def owner(self):
        """Identificador estable sin revelar emisor ni usuario en nombres de blobs."""
        return hashlib.sha256(json.dumps([self.issuer, self.subject]).encode()).hexdigest()

    @property
    def principal(self):
        """Adapta identidades largas a los contratos del procesador original."""
        return Principal(tenant_id=hashlib.sha256(self.issuer.encode()).hexdigest(),
                         user_id=hashlib.sha256(self.subject.encode()).hexdigest())

    def require(self, scope):
        """Exige permisos explícitos; no convierte autenticación en autorización global."""
        if scope not in self.scopes:
            raise AuthorizationError("El JWT no incluye el permiso requerido")


@lru_cache(maxsize=4)
def jwks_client(url):
    """Reutiliza claves públicas de un JWKS HTTPS elegido en configuración, nunca en claims."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise RuntimeError("RS_JWT_JWKS_URL debe ser un endpoint HTTPS del proveedor")
    return jwt.PyJWKClient(url, timeout=5, lifespan=300)


class JwtVerifier:
    """Acepta solamente RS256, con firma, exp, iat, sub, iss y aud obligatorios."""
    def __init__(self, issuer, audience, public_key="", jwks_url=""):
        """Falla cerrado si no se ha elegido exactamente una fuente de claves públicas."""
        if not issuer or not audience or bool(public_key) == bool(jwks_url):
            raise RuntimeError("Configura issuer, audience y una clave pública PEM o JWKS")
        self.issuer, self.audience, self.public_key = issuer, audience, public_key
        self.jwks = jwks_client(jwks_url) if jwks_url else None

    @classmethod
    def from_env(cls):
        """Lee configuración del servidor; no acepta emisor, audiencia o claves del cliente."""
        key = os.getenv("RS_JWT_PUBLIC_KEY", "").replace("\\n", "\n")
        key_file = os.getenv("RS_JWT_PUBLIC_KEY_FILE", "")
        if key_file:
            if key:
                raise RuntimeError("Elige RS_JWT_PUBLIC_KEY o RS_JWT_PUBLIC_KEY_FILE")
            key = Path(key_file).read_text()
        return cls(os.getenv("RS_JWT_ISSUER", ""), os.getenv("RS_JWT_AUDIENCE", ""),
                   key, os.getenv("RS_JWT_JWKS_URL", ""))

    def verify(self, authorization):
        """Devuelve claims verificados sin reenviar este JWT al servidor del video."""
        if not authorization or len(authorization) > 20000:
            raise AuthenticationError("Se requiere Authorization: Bearer <JWT>")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthenticationError("Se requiere Authorization: Bearer <JWT>")
        token = parts[1]
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or header.get("crit"):
                raise AuthenticationError("JWT inválido o expirado")
            key = self.jwks.get_signing_key_from_jwt(token).key if self.jwks else self.public_key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.audience, leeway=30,
                                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
            scope = claims.get("scope", "")
            if not isinstance(scope, str) or not isinstance(claims["sub"], str) or not claims["sub"]:
                raise AuthenticationError("JWT inválido o expirado")
            return Identity(claims["iss"], claims["sub"], frozenset(scope.split()))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthenticationError("JWT inválido o expirado") from None
