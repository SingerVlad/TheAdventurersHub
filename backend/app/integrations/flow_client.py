import hashlib
import hmac
from urllib.parse import urlencode, urlparse

import requests

from backend.app.config import (
    DEFAULT_FLOW_API_URLS,
    DEFAULT_FLOW_HTTP_TIMEOUT_SECONDS,
    MAX_FLOW_HTTP_TIMEOUT_SECONDS,
    OFFICIAL_FLOW_HOSTS,
)

DEFAULT_FLOW_TIMEOUT_TUPLE = (3.0, 5.0)


class FlowError(Exception):
    """Error base de la integración con Flow."""


class FlowConfigurationError(FlowError):
    """Falta configuración obligatoria de Flow."""


class FlowProviderError(FlowError):
    """Error de comunicación o respuesta inválida desde Flow."""


class FlowTimeoutError(FlowProviderError):
    """Tiempo de espera agotado al comunicarse con Flow."""


def _normalize_bounded_timeout(timeout):
    """
    Garantiza que el timeout HTTP hacia Flow nunca supere `MAX_FLOW_HTTP_TIMEOUT_SECONDS` (10s),
    dejando margen suficiente bajo el límite de 15 segundos exigido por el webhook de Flow.
    """
    if timeout is None:
        return DEFAULT_FLOW_TIMEOUT_TUPLE

    if isinstance(timeout, tuple) and len(timeout) == 2:
        connect_t, read_t = float(timeout[0]), float(timeout[1])
        total = connect_t + read_t
        if connect_t <= 0 or read_t <= 0 or total > MAX_FLOW_HTTP_TIMEOUT_SECONDS:
            raise FlowConfigurationError(
                f"El timeout total de Flow debe ser positivo y menor o igual a {MAX_FLOW_HTTP_TIMEOUT_SECONDS}s."
            )
        return (connect_t, read_t)

    if isinstance(timeout, (int, float)):
        total = float(timeout)
        if not (0 < total <= MAX_FLOW_HTTP_TIMEOUT_SECONDS):
            raise FlowConfigurationError(
                f"El timeout de Flow debe ser positivo y menor o igual a {MAX_FLOW_HTTP_TIMEOUT_SECONDS}s."
            )
        connect_t = round(min(3.0, total * 0.4), 2)
        read_t = round(max(0.1, total - connect_t), 2)
        return (connect_t, read_t)

    raise FlowConfigurationError("Formato de timeout inválido para FlowClient.")


def sign_flow_params(params, secret_key):
    """Genera la firma HMAC-SHA256 oficial de Flow ordenando parámetros alfabéticamente."""
    if not isinstance(secret_key, str) or not secret_key.strip():
        raise FlowConfigurationError("FLOW_SECRET_KEY no está configurada.")

    pares_ordenados = sorted(
        (str(clave), str(valor))
        for clave, valor in params.items()
        if clave != "s" and valor is not None
    )
    mensaje = "".join(f"{clave}{valor}" for clave, valor in pares_ordenados)
    return hmac.new(
        secret_key.encode("utf-8"),
        mensaje.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_flow_signature(params, signature, secret_key):
    """Verifica de forma segura en tiempo constante una firma HMAC-SHA256 de Flow."""
    if not isinstance(signature, str) or not signature:
        return False
    expected = sign_flow_params(params, secret_key)
    return hmac.compare_digest(expected, signature)


def validate_flow_payment_url(raw_url, flow_env):
    """Valida que la URL de pago devuelta por Flow use HTTPS y el dominio oficial del ambiente."""
    expected_host = OFFICIAL_FLOW_HOSTS.get(flow_env)
    if not expected_host:
        raise FlowConfigurationError("FLOW_ENV no es válido para verificar URLs de Flow.")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise FlowProviderError("Flow no devolvió una URL de pago válida.")

    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or hostname != expected_host
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise FlowProviderError(
            "La URL de pago devuelta por Flow no pertenece al dominio oficial esperado."
        )
    return cleaned


class FlowClient:
    """Cliente HTTP aislado para la API REST de Flow (Sandbox / Producción)."""

    def __init__(
        self,
        api_key,
        secret_key,
        api_url=None,
        flow_env="sandbox",
        timeout=DEFAULT_FLOW_TIMEOUT_TUPLE,
        session=None,
    ):
        self.flow_env = (flow_env or "sandbox").strip().lower()
        if self.flow_env not in OFFICIAL_FLOW_HOSTS:
            raise FlowConfigurationError("FLOW_ENV debe ser 'sandbox' o 'production'.")

        default_url = DEFAULT_FLOW_API_URLS[self.flow_env]
        self.api_key = (api_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.api_url = (api_url or default_url).strip().rstrip("/")
        self.timeout = _normalize_bounded_timeout(timeout)
        self._http = session or requests

    @property
    def total_timeout_seconds(self):
        if isinstance(self.timeout, tuple):
            return sum(self.timeout)
        return float(self.timeout)

    @classmethod
    def from_app_config(cls, config):
        flow_env = config.get("FLOW_ENV", "sandbox")
        configured_timeout = config.get(
            "FLOW_HTTP_TIMEOUT_SECONDS", DEFAULT_FLOW_HTTP_TIMEOUT_SECONDS
        )
        return cls(
            api_key=config.get("FLOW_API_KEY", ""),
            secret_key=config.get("FLOW_SECRET_KEY", ""),
            api_url=config.get("FLOW_API_URL"),
            flow_env=flow_env,
            timeout=configured_timeout,
        )

    def _ensure_configured(self):
        if not self.api_key or not self.secret_key:
            raise FlowConfigurationError(
                "Las credenciales de Flow no están configuradas en el servidor."
            )
        validate_flow_payment_url(self.api_url, self.flow_env)

    def create_payment(
        self,
        *,
        commerce_order,
        subject,
        amount,
        email,
        url_confirmation,
        url_return,
        currency="CLP",
        optional=None,
    ):
        self._ensure_configured()
        params = {
            "apiKey": self.api_key,
            "commerceOrder": str(commerce_order),
            "subject": str(subject),
            "currency": str(currency),
            "amount": int(amount),
            "email": str(email),
            "urlConfirmation": str(url_confirmation),
            "urlReturn": str(url_return),
        }
        if optional is not None:
            params["optional"] = str(optional)

        params["s"] = sign_flow_params(params, self.secret_key)

        try:
            response = self._http.post(
                f"{self.api_url}/payment/create",
                data=params,
                timeout=self.timeout,
            )
        except requests.Timeout as error:
            raise FlowTimeoutError(
                "Tiempo de espera agotado al crear el pago en Flow."
            ) from error
        except requests.RequestException as error:
            raise FlowProviderError("No se pudo conectar con la pasarela Flow.") from error

        if response.status_code != 200:
            raise FlowProviderError(
                f"Flow rechazó la creación del pago con estado HTTP {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError as error:
            raise FlowProviderError("Respuesta inválida desde Flow en payment/create.") from error

        raw_url = data.get("url") if isinstance(data, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise FlowProviderError("Flow no devolvió un token válido.")

        base_url = validate_flow_payment_url(raw_url, self.flow_env)
        clean_token = token.strip()
        separador = "&" if "?" in base_url else "?"
        return {
            "url": base_url,
            "token": clean_token,
            "flow_order": data.get("flowOrder"),
            "redirect_url": f"{base_url}{separador}{urlencode({'token': clean_token})}",
        }

    def get_status(self, token):
        self._ensure_configured()
        if not isinstance(token, str) or not token.strip():
            raise FlowProviderError("El token de Flow es inválido.")

        params = {
            "apiKey": self.api_key,
            "token": token.strip(),
        }
        params["s"] = sign_flow_params(params, self.secret_key)

        try:
            response = self._http.get(
                f"{self.api_url}/payment/getStatus",
                params=params,
                timeout=self.timeout,
            )
        except requests.Timeout as error:
            raise FlowTimeoutError(
                "Tiempo de espera agotado al consultar el estado del pago en Flow."
            ) from error
        except requests.RequestException as error:
            raise FlowProviderError("No se pudo consultar el estado en Flow.") from error

        if response.status_code != 200:
            raise FlowProviderError(
                f"Flow rechazó la consulta de estado con HTTP {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError as error:
            raise FlowProviderError("Respuesta inválida desde Flow en payment/getStatus.") from error

        if not isinstance(data, dict) or "status" not in data or "commerceOrder" not in data:
            raise FlowProviderError("Respuesta incompleta desde Flow en payment/getStatus.")

        return data

    def get_payment_status(self, token):
        """Alias explícito usado en callbacks síncronos de confirmación."""
        return self.get_status(token)
