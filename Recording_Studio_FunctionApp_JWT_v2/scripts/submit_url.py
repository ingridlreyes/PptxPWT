"""Cliente de ejemplo: envía un enlace, consulta estado y descarga el paquete con JWT."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib import request, error
from urllib.parse import urlsplit, urljoin


def same_origin(base, path):
    """No sigue rutas de estado/resultados a un servidor distinto con el JWT de la API."""
    target = urljoin(base, path)
    a, b = urlsplit(base), urlsplit(target)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        raise RuntimeError("La API devolvió una ruta de otro origen")
    return target


class NoRedirect(request.HTTPRedirectHandler):
    """Evita que urllib reenvíe Authorization a redirecciones inesperadas de la API."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """La API no necesita redirects para entregar estado o resultado."""
        return None


def main():
    """Mantiene tokens fuera de argumentos, archivos de salida y mensajes de progreso."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True, help="Host HTTPS de la Function App, sin /api")
    parser.add_argument("--request", required=True, help="Archivo JSON de solicitud")
    parser.add_argument("--output", default="capacitacion.zip")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    base = args.api.rstrip("/")
    parsed = urlsplit(base)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path or \
       (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})):
        parser.error("--api requiere un host HTTPS, o HTTP localhost para desarrollo")
    token = os.getenv("RS_API_TOKEN", "")
    if not token:
        parser.error("Configura RS_API_TOKEN con el JWT de Recording Studio")
    target = Path(args.output)
    if target.exists() or target.with_suffix(target.suffix + ".partial").exists():
        parser.error("El archivo de salida o su descarga parcial ya existen")
    payload = json.loads(Path(args.request).read_text())
    if os.getenv("RS_SOURCE_TOKEN"):
        payload["source_token"] = os.environ["RS_SOURCE_TOKEN"]
    opener = request.build_opener(NoRedirect)

    def call(path, body=None):
        """Envía JSON al mismo host sin imprimir headers ni la solicitud privada."""
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(same_origin(base, path), data=data,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=60) as response:
                return json.loads(response.read(2 * 1024**2))
        except error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("JWT inválido/expirado. Obtén uno nuevo para el mismo usuario y consulta el job_id guardado") from None
            raise RuntimeError(f"La API devolvió HTTP {exc.code}; revisa la configuración y el contrato de entrada") from None

    created = call("/api/recordings", payload)
    print("Trabajo: " + created["job_id"], flush=True)
    deadline = time.monotonic() + args.timeout
    previous = None
    while time.monotonic() < deadline:
        state = call(created["status_url"])
        message = state.get("message", state["status"])
        if message != previous:
            print(state["status"] + ": " + message, flush=True)
            previous = message
        if state["status"] == "Completed":
            partial = target.with_suffix(target.suffix + ".partial")
            req = request.Request(same_origin(base, state["result_url"]), headers={"Authorization": "Bearer " + token})
            try:
                with opener.open(req, timeout=180) as response, partial.open("xb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                partial.rename(target)
            finally:
                partial.unlink(missing_ok=True)
            print("Paquete guardado en " + str(target))
            return
        if state["status"] in {"Failed", "Interrupted"}:
            raise RuntimeError("El trabajo no se completó: " + message)
        if state["status"] == "DispatchPending":
            call(state["retry_url"], {})
        time.sleep(10)
    raise RuntimeError("Se agotó la espera del cliente; el trabajo puede continuar. Conserva su ID y consulta el estado")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError):
        # Las excepciones de transporte pueden contener URL; no imprimir tracebacks ni bodies.
        raise SystemExit("No se completó la operación. Revisa el estado del trabajo y la configuración; no repitas el envío si ya recibiste un ID") from None
