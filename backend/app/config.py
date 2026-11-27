import ipaddress
import os
from urllib.parse import urlparse

OFFICIAL_FLOW_HOSTS = {
    "sandbox": "sandbox.flow.cl",
    "production": "www.flow.cl",
}
DEFAULT_FLOW_API_URLS = {
    "sandbox": "https://sandbox.flow.cl/api",
    "production": "https://www.flow.cl/api",
}
ALLOWED_FLOW_ENVS = set(OFFICIAL_FLOW_HOSTS.keys())
FORBIDDEN_PRODUCTION_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

DEFAULT_FLOW_HTTP_TIMEOUT_SECONDS = 8.0
MAX_FLOW_HTTP_TIMEOUT_SECONDS = 10.0


def _is_forbidden_local_host(hostname):
    """
    Verifica si un host es inválido para URLs públicas en producción.
    Si es una dirección IP literal, exige estrictamente `ip.is_global == True`
    (rechazando privadas RFC1918, loopback, link-local, CGNAT y reservadas).
    """
    host = (hostname or "").strip().lower()
    if not host or host in FORBIDDEN_PRODUCTION_HOSTS or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        return False


def validate_official_flow_url(raw_url, flow_env, setting_name="FLOW_API_URL"):
    """Valida que una URL de Flow use HTTPS y pertenezca estrictamente al host oficial del ambiente."""
    if flow_env not in OFFICIAL_FLOW_HOSTS:
        raise ValueError("FLOW_ENV debe ser 'sandbox' o 'production'.")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError(f"{setting_name} no puede estar vacía.")

    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    expected_host = OFFICIAL_FLOW_HOSTS[flow_env]
    hostname = (parsed.hostname or "").lower()

    if (
        parsed.scheme != "https"
        or hostname != expected_host
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            f"{setting_name} debe usar https:// y apuntar exclusivamente a '{expected_host}'."
        )
    return cleaned


def _validate_production_public_url(setting_name, raw_url):
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise RuntimeError(
            f"{setting_name} debe definirse explícitamente cuando FLOW_ENV='production'."
        )
    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or _is_forbidden_local_host(hostname)
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError(
            f"{setting_name} debe usar https:// y un host público global válido en producción."
        )
    return cleaned


def _parse_flow_timeout(raw_value):
    if raw_value is None or str(raw_value).strip() == "":
        return DEFAULT_FLOW_HTTP_TIMEOUT_SECONDS
    try:
        timeout = float(str(raw_value).strip())
    except ValueError as error:
        raise RuntimeError("FLOW_HTTP_TIMEOUT_SECONDS debe ser un número válido.") from error
    if not (0.5 <= timeout <= MAX_FLOW_HTTP_TIMEOUT_SECONDS):
        raise RuntimeError(
            f"FLOW_HTTP_TIMEOUT_SECONDS debe estar entre 0.5 y {MAX_FLOW_HTTP_TIMEOUT_SECONDS} segundos."
        )
    return timeout


def configure_flow_settings(app, environ=None):
    """Carga y valida la configuración de la pasarela Flow en la instancia Flask."""
    env = os.environ if environ is None else environ

    flow_env = str(env.get("FLOW_ENV", "sandbox")).strip().lower() or "sandbox"
    if flow_env not in ALLOWED_FLOW_ENVS:
        raise RuntimeError("FLOW_ENV debe ser 'sandbox' o 'production'.")

    raw_flow_api_url = str(
        env.get("FLOW_API_URL") or DEFAULT_FLOW_API_URLS[flow_env]
    ).strip().rstrip("/")
    try:
        flow_api_url = validate_official_flow_url(
            raw_flow_api_url, flow_env, setting_name="FLOW_API_URL"
        ).rstrip("/")
    except ValueError as error:
        raise RuntimeError(str(error)) from error

    flow_timeout = _parse_flow_timeout(env.get("FLOW_HTTP_TIMEOUT_SECONDS"))

    if flow_env == "production":
        confirmation_url = _validate_production_public_url(
            "FLOW_CONFIRMATION_URL", env.get("FLOW_CONFIRMATION_URL", "")
        )
        return_url = _validate_production_public_url(
            "FLOW_RETURN_URL", env.get("FLOW_RETURN_URL", "")
        )
        frontend_public_url = _validate_production_public_url(
            "FRONTEND_PUBLIC_URL", env.get("FRONTEND_PUBLIC_URL", "")
        ).rstrip("/")
    else:
        confirmation_url = (
            str(
                env.get(
                    "FLOW_CONFIRMATION_URL",
                    "http://localhost:5000/api/pagos/flow/confirmacion",
                )
            ).strip()
            or "http://localhost:5000/api/pagos/flow/confirmacion"
        )
        return_url = (
            str(
                env.get(
                    "FLOW_RETURN_URL",
                    "http://localhost:5000/api/pagos/flow/retorno",
                )
            ).strip()
            or "http://localhost:5000/api/pagos/flow/retorno"
        )
        frontend_public_url = (
            str(env.get("FRONTEND_PUBLIC_URL", "http://localhost:5173")).strip().rstrip("/")
            or "http://localhost:5173"
        )

    app.config["FLOW_ENV"] = flow_env
    app.config["FLOW_API_URL"] = flow_api_url
    app.config["FLOW_API_KEY"] = str(env.get("FLOW_API_KEY", "")).strip()
    app.config["FLOW_SECRET_KEY"] = str(env.get("FLOW_SECRET_KEY", "")).strip()
    app.config["FLOW_HTTP_TIMEOUT_SECONDS"] = flow_timeout
    app.config["FLOW_CONFIRMATION_URL"] = confirmation_url
    app.config["FLOW_RETURN_URL"] = return_url
    app.config["FRONTEND_PUBLIC_URL"] = frontend_public_url
