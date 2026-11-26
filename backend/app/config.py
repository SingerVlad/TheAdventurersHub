import os
from urllib.parse import urlparse

ALLOWED_FLOW_ENVS = {"sandbox", "production"}


def configure_flow_settings(app):
    """Carga y valida la configuración de la pasarela Flow en la instancia Flask."""
    flow_env = os.getenv("FLOW_ENV", "sandbox").strip().lower() or "sandbox"
    if flow_env not in ALLOWED_FLOW_ENVS:
        raise RuntimeError("FLOW_ENV debe ser 'sandbox' o 'production'.")

    default_api_url = (
        "https://sandbox.flow.cl/api"
        if flow_env == "sandbox"
        else "https://www.flow.cl/api"
    )
    flow_api_url = os.getenv("FLOW_API_URL", default_api_url).strip().rstrip("/")
    parsed_api = urlparse(flow_api_url)
    if parsed_api.scheme not in {"http", "https"} or not parsed_api.netloc:
        raise RuntimeError("FLOW_API_URL debe ser una URL HTTP o HTTPS válida.")

    app.config["FLOW_ENV"] = flow_env
    app.config["FLOW_API_URL"] = flow_api_url
    app.config["FLOW_API_KEY"] = os.getenv("FLOW_API_KEY", "").strip()
    app.config["FLOW_SECRET_KEY"] = os.getenv("FLOW_SECRET_KEY", "").strip()
    app.config["FLOW_CONFIRMATION_URL"] = os.getenv(
        "FLOW_CONFIRMATION_URL",
        "http://localhost:5000/api/pagos/flow/confirmacion",
    ).strip()
    app.config["FLOW_RETURN_URL"] = os.getenv(
        "FLOW_RETURN_URL",
        "http://localhost:5000/api/pagos/flow/retorno",
    ).strip()
    app.config["FRONTEND_PUBLIC_URL"] = (
        os.getenv("FRONTEND_PUBLIC_URL", "http://localhost:5173").strip().rstrip("/")
    )
