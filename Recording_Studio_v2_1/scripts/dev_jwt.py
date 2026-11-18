"""Genera claves y tokens de desarrollo; no sustituye el inicio de sesión de producción."""
import argparse
import base64
import json
import os
from pathlib import Path
import time
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def save_private(path, content):
    """Crea archivos privados sin sobrescribir claves existentes y restringe permisos POSIX."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def main():
    """Inicializa un entorno local o emite un JWT corto con los scopes de prueba."""
    parser = argparse.ArgumentParser(description="JWT RS256 únicamente para desarrollo")
    parser.add_argument("command", choices=["init", "token"])
    parser.add_argument("--directory", default=".secrets")
    parser.add_argument("--issuer", default="https://auth.example.com/")
    parser.add_argument("--audience", default="recording-studio-api")
    parser.add_argument("--subject", default="usuario-de-prueba")
    parser.add_argument("--scope", default="recordings:write recordings:read")
    args = parser.parse_args()
    root = Path(args.directory)
    if args.command == "init":
        root.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        save_private(root / "jwt-private.pem", key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        save_private(root / "jwt-public.pem", key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode())
        save_private(root / "job-encryption-key.txt", base64.b64encode(os.urandom(32)).decode())
        print("Claves de desarrollo creadas en " + str(root) + ". Configura local.settings.json según docs/DEPLOY_FUNCTION_APP.md")
    else:
        now = int(time.time())
        claims = {"iss": args.issuer, "aud": args.audience, "sub": args.subject, "iat": now,
                  "exp": now + 3600, "scope": args.scope}
        print(jwt.encode(claims, (root / "jwt-private.pem").read_text(), algorithm="RS256"))


if __name__ == "__main__":
    main()
