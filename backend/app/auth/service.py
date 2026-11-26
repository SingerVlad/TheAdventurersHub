import re
from datetime import datetime, timezone

from backend.app.auth import repository

EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}$"
)


class AuthValidationError(Exception):
    """Error de validación de datos del usuario."""


def normalize_utc_datetime(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalizar_validar_email(raw_email):
    if not isinstance(raw_email, str):
        raise AuthValidationError("El correo electrónico es obligatorio")
    email = raw_email.strip().lower()
    if (
        not (5 <= len(email) <= 254)
        or ".." in email
        or not EMAIL_PATTERN.fullmatch(email)
    ):
        raise AuthValidationError("El formato del correo electrónico no es válido")
    return email


def obtener_estado_suscripcion(usuario, ahora=None):
    referencia = normalize_utc_datetime(ahora) or datetime.now(timezone.utc)
    activa_hasta = normalize_utc_datetime(usuario.get("suscripcion_activa_hasta"))
    es_activo = bool(activa_hasta and activa_hasta > referencia)
    return {
        "suscripcion_activa_hasta": activa_hasta.isoformat() if activa_hasta else None,
        "es_suscriptor_activo": es_activo,
    }


def serializar_perfil_usuario(usuario, ahora=None):
    estado_suscripcion = obtener_estado_suscripcion(usuario, ahora=ahora)
    fecha_registro = normalize_utc_datetime(usuario.get("fecha_registro"))
    return {
        "id": str(usuario["_id"]),
        "username": usuario["username"],
        "email": usuario.get("email"),
        "fecha_registro": fecha_registro.isoformat() if fecha_registro else None,
        "suscripcion_activa_hasta": estado_suscripcion["suscripcion_activa_hasta"],
        "es_suscriptor_activo": estado_suscripcion["es_suscriptor_activo"],
    }


def guardar_email_usuario(usuario_actual, raw_email):
    email = normalizar_validar_email(raw_email)
    usuario_actualizado = repository.actualizar_email_usuario(
        usuario_actual["_id"], email
    )
    if not usuario_actualizado:
        raise LookupError("Usuario no encontrado")
    return usuario_actualizado
