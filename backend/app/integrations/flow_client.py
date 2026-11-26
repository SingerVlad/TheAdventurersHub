import hashlib
import hmac
import requests


class FlowError(Exception):
    """Error base de la integración con Flow."""


class FlowConfigurationError(FlowError):
    """Falta configuración obligatoria de Flow."""


class FlowProviderError(FlowError):
    """Error de comunicación o respuesta inválida desde Flow."""


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


class FlowClient:
    """Cliente HTTP aislado para la API REST de Flow (Sandbox / Producción)."""

    def __init__(
        self,
        api_key,
        secret_key,
        api_url="https://sandbox.flow.cl/api",
        timeout=20,
        session=None,
    ):
        self.api_key = (api_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.api_url = (api_url or "https://sandbox.flow.cl/api").strip().rstrip("/")
        self.timeout = timeout
        self._http = session or requests

    @classmethod
    def from_app_config(cls, config):
        return cls(
            api_key=config.get("FLOW_API_KEY", ""),
            secret_key=config.get("FLOW_SECRET_KEY", ""),
            api_url=config.get("FLOW_API_URL", "https://sandbox.flow.cl/api"),
        )

    def _ensure_configured(self):
        if not self.api_key or not self.secret_key:
            raise FlowConfigurationError(
                "Las credenciales de Flow no están configuradas en el servidor."
            )

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

        base_url = data.get("url") if isinstance(data, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(base_url, str) or not base_url or not isinstance(token, str) or not token:
            raise FlowProviderError("Flow no devolvió url o token válidos.")

        separador = "&" if "?" in base_url else "?"
        return {
            "url": base_url,
            "token": token,
            "flow_order": data.get("flowOrder"),
            "redirect_url": f"{base_url}{separador}token={token}",
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
