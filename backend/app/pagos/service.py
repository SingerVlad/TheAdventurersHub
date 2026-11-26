import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from backend.app.auth.repository import obtener_usuario_por_id
from backend.app.auth.service import (
    AuthValidationError,
    guardar_email_usuario,
    normalize_utc_datetime,
    obtener_estado_suscripcion,
)
from backend.app.pagos import catalog, repository

ORDEN_COMERCIO_PATTERN = re.compile(r"^AH-\d{14}-[A-F0-9]{8}$")
FLOW_STATUS_MAP = {
    1: "pendiente",
    2: "pagada",
    3: "rechazada",
    4: "anulada",
}


class PagoValidationError(Exception):
    """Error de validación en una solicitud de pago."""


class OrdenNoEncontradaError(Exception):
    """La orden solicitada no existe."""


class AccesoOrdenDenegadoError(Exception):
    """El usuario no es propietario de la orden."""


class IntegridadPagoError(Exception):
    """Los datos devueltos por la pasarela no coinciden con la orden registrada."""


class ActivacionEnProcesoError(Exception):
    """Otra solicitud concurrente está procesando activamente la orden."""


def calcular_nueva_vigencia(vigencia_actual, duracion_dias=30, ahora=None):
    """
    Calcula la fecha exacta de expiración sumando `duracion_dias`.
    Si la suscripción actual aún no vence, acumula sobre `vigencia_actual`;
    si no existe o ya venció, calcula desde `ahora`.
    """
    if not isinstance(duracion_dias, int) or duracion_dias <= 0:
        raise ValueError("duracion_dias debe ser un entero positivo.")

    ahora_utc = normalize_utc_datetime(ahora) or datetime.now(timezone.utc)
    vigencia_utc = normalize_utc_datetime(vigencia_actual)

    base = vigencia_utc if (vigencia_utc and vigencia_utc > ahora_utc) else ahora_utc
    return base + timedelta(days=duracion_dias)


def generar_orden_comercio(ahora=None):
    marca = (normalize_utc_datetime(ahora) or datetime.now(timezone.utc)).strftime(
        "%Y%m%d%H%M%S"
    )
    return f"AH-{marca}-{secrets.token_hex(4).upper()}"


def validar_token_flow(raw_token):
    if not isinstance(raw_token, str):
        raise PagoValidationError("Falta el token de confirmación de Flow.")
    token = raw_token.strip()
    if not (1 <= len(token) <= 256):
        raise PagoValidationError("El token de Flow no es válido.")
    return token


def iniciar_pago_suscripcion(usuario_actual, datos, flow_client, config, ahora=None):
    if datos is None:
        datos = {}
    if not isinstance(datos, dict):
        raise PagoValidationError("El cuerpo debe ser un objeto JSON.")

    plan = catalog.obtener_plan(datos.get("plan_id"))
    if not plan:
        raise PagoValidationError("Plan de suscripción no válido.")

    email_enviado = datos.get("email")
    if email_enviado is not None and str(email_enviado).strip() != "":
        try:
            usuario_actual = guardar_email_usuario(usuario_actual, email_enviado)
        except AuthValidationError as error:
            raise PagoValidationError(str(error)) from error

    email_pagador = usuario_actual.get("email")
    if not email_pagador:
        raise PagoValidationError(
            "Debes registrar un correo electrónico antes de iniciar el pago."
        )

    ahora_utc = normalize_utc_datetime(ahora) or datetime.now(timezone.utc)
    orden_comercio = generar_orden_comercio(ahora_utc)

    documento_orden = {
        "orden_comercio": orden_comercio,
        "usuario_id": usuario_actual["_id"],
        "email_pagador": email_pagador,
        "plan_id": plan["id"],
        "items": [
            {
                "sku": plan["sku"],
                "nombre": plan["nombre"],
                "cantidad": 1,
                "precio_unitario": plan["monto"],
                "duracion_dias": plan["duracion_dias"],
            }
        ],
        "monto_total": plan["monto"],
        "moneda": plan["moneda"],
        "duracion_dias": plan["duracion_dias"],
        "estado": "pendiente",
        "activacion_estado": "pendiente",
        "activacion_intento_en": None,
        "suscripcion_aplicada": False,
        "vigencia_base_previa": None,
        "flow_token": None,
        "flow_orden": None,
        "flow_status": 1,
        "vigencia_otorgada_hasta": None,
        "fecha_creacion": ahora_utc,
        "fecha_actualizacion": ahora_utc,
        "fecha_pago": None,
    }
    orden = repository.crear_orden(documento_orden)

    try:
        respuesta_flow = flow_client.create_payment(
            commerce_order=orden_comercio,
            subject=plan["nombre"],
            amount=plan["monto"],
            currency=plan["moneda"],
            email=email_pagador,
            url_confirmation=config["FLOW_CONFIRMATION_URL"],
            url_return=config["FLOW_RETURN_URL"],
        )
    except Exception:
        repository.marcar_orden_fallida(orden["_id"], datetime.now(timezone.utc))
        raise

    repository.guardar_datos_flow_en_orden(
        orden["_id"],
        flow_token=respuesta_flow["token"],
        flow_orden=respuesta_flow["flow_order"],
        fecha_actualizacion=ahora_utc,
    )
    repository.registrar_transaccion_pago(
        {
            "orden_id": orden["_id"],
            "orden_comercio": orden_comercio,
            "flow_token": respuesta_flow["token"],
            "flow_orden": respuesta_flow["flow_order"],
            "tipo_evento": "creacion",
            "estado_flow": 1,
            "idempotente": False,
            "fecha_evento": ahora_utc,
        }
    )

    return {
        "orden_comercio": orden_comercio,
        "plan_id": plan["id"],
        "nombre_plan": plan["nombre"],
        "monto": plan["monto"],
        "moneda": plan["moneda"],
        "duracion_dias": plan["duracion_dias"],
        "flow_orden": respuesta_flow["flow_order"],
        "url_pago": respuesta_flow["redirect_url"],
    }


def _extraer_monto_entero(valor):
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError) as error:
        raise IntegridadPagoError("El monto informado por Flow no es válido.") from error


def _ejecutar_activacion_recuperable(orden, flow_orden, ahora_utc):
    orden_en_proceso = repository.adquirir_orden_para_activacion(
        orden["_id"],
        flow_orden=flow_orden,
        ahora=ahora_utc,
    )
    if orden_en_proceso is None:
        orden_actual = repository.buscar_orden_por_comercio(orden["orden_comercio"]) or orden
        if (
            orden_actual.get("suscripcion_aplicada") is True
            or orden_actual.get("activacion_estado") == "aplicada"
        ):
            return (
                "pagada",
                True,
                normalize_utc_datetime(orden_actual.get("vigencia_otorgada_hasta")),
            )
        raise ActivacionEnProcesoError(
            "La activación de esta orden ya se encuentra en ejecución."
        )

    duracion_dias = int(orden_en_proceso.get("duracion_dias", 30))
    fecha_pago = normalize_utc_datetime(orden_en_proceso.get("fecha_pago")) or ahora_utc
    vigencia_objetivo = normalize_utc_datetime(
        orden_en_proceso.get("vigencia_otorgada_hasta")
    )
    vigencia_base_previa = normalize_utc_datetime(
        orden_en_proceso.get("vigencia_base_previa")
    )

    try:
        if vigencia_objetivo is None:
            usuario = obtener_usuario_por_id(orden_en_proceso["usuario_id"])
            if not usuario:
                raise LookupError("El usuario asociado a la orden no existe.")
            vigencia_base_previa = normalize_utc_datetime(
                usuario.get("suscripcion_activa_hasta")
            )
            vigencia_calculada = calcular_nueva_vigencia(
                vigencia_base_previa,
                duracion_dias=duracion_dias,
                ahora=fecha_pago,
            )
            orden_fijada = repository.fijar_vigencia_objetivo_en_orden(
                orden_en_proceso["_id"],
                vigencia_objetivo=vigencia_calculada,
                vigencia_base_previa=vigencia_base_previa,
                fecha_pago=fecha_pago,
                ahora=ahora_utc,
            )
            vigencia_objetivo = normalize_utc_datetime(
                orden_fijada.get("vigencia_otorgada_hasta")
            )
            vigencia_base_previa = normalize_utc_datetime(
                orden_fijada.get("vigencia_base_previa")
            )

        vigencia_aplicada = repository.aplicar_vigencia_idempotente_usuario(
            usuario_id=orden_en_proceso["usuario_id"],
            orden_id=orden_en_proceso["_id"],
            vigencia_objetivo=vigencia_objetivo,
            vigencia_base_previa=vigencia_base_previa,
            duracion_dias=duracion_dias,
            ahora=fecha_pago,
            calcular_vigencia_fn=calcular_nueva_vigencia,
        )
        repository.finalizar_activacion_orden(
            orden_en_proceso["_id"],
            vigencia_hasta=vigencia_aplicada,
            flow_orden=flow_orden,
            fecha_pago=fecha_pago,
            fecha_actualizacion=ahora_utc,
        )
        return "pagada", False, vigencia_aplicada
    except Exception:
        repository.liberar_intento_activacion_en_proceso(
            orden_en_proceso["_id"], ahora_utc
        )
        raise


def procesar_confirmacion_flow(raw_token, flow_client, origen="confirmacion", ahora=None):
    token = validar_token_flow(raw_token)
    ahora_utc = normalize_utc_datetime(ahora) or datetime.now(timezone.utc)

    status_data = flow_client.get_status(token)
    commerce_order = str(status_data.get("commerceOrder", "")).strip()

    orden = repository.buscar_orden_por_token(token)
    if not orden and commerce_order:
        candidata = repository.buscar_orden_por_comercio(commerce_order)
        if candidata and candidata.get("flow_token") in {None, token}:
            orden = candidata

    if not orden:
        raise OrdenNoEncontradaError("No existe una orden asociada al token recibido.")

    if orden["orden_comercio"] != commerce_order:
        raise IntegridadPagoError("El identificador de comercio no coincide con la orden.")

    monto_flow = _extraer_monto_entero(status_data.get("amount"))
    if monto_flow != int(orden["monto_total"]):
        raise IntegridadPagoError("El monto pagado en Flow no coincide con la orden.")

    moneda_flow = str(status_data.get("currency", "CLP")).strip().upper()
    if moneda_flow != str(orden["moneda"]).upper():
        raise IntegridadPagoError("La moneda informada por Flow no coincide con la orden.")

    try:
        flow_status = int(status_data.get("status"))
    except (TypeError, ValueError) as error:
        raise IntegridadPagoError("El estado devuelto por Flow no es válido.") from error

    estado_mapeado = FLOW_STATUS_MAP.get(flow_status)
    if not estado_mapeado:
        raise IntegridadPagoError("Código de estado Flow desconocido.")

    flow_orden = status_data.get("flowOrder", orden.get("flow_orden"))
    idempotente = False
    vigencia_hasta = normalize_utc_datetime(orden.get("vigencia_otorgada_hasta"))

    if flow_status == 2:
        estado_final, idempotente, vigencia_hasta = _ejecutar_activacion_recuperable(
            orden,
            flow_orden=flow_orden,
            ahora_utc=ahora_utc,
        )
    else:
        orden_actualizada = repository.actualizar_estado_no_pagado(
            orden["_id"],
            nuevo_estado=estado_mapeado,
            flow_status=flow_status,
            flow_orden=flow_orden,
            fecha_actualizacion=ahora_utc,
        )
        if orden_actualizada is None:
            idempotente = True
            orden_actual = repository.buscar_orden_por_comercio(orden["orden_comercio"]) or orden
            estado_final = orden_actual.get("estado", "pagada")
        else:
            estado_final = estado_mapeado

    repository.registrar_transaccion_pago(
        {
            "orden_id": orden["_id"],
            "orden_comercio": orden["orden_comercio"],
            "flow_token": token,
            "flow_orden": flow_orden,
            "tipo_evento": origen,
            "estado_flow": flow_status,
            "estado_orden": estado_final,
            "idempotente": idempotente,
            "payload_resumen": {
                "commerceOrder": commerce_order,
                "status": flow_status,
                "amount": monto_flow,
                "currency": moneda_flow,
            },
            "fecha_evento": ahora_utc,
        }
    )

    return {
        "orden_comercio": orden["orden_comercio"],
        "estado": estado_final,
        "flow_status": flow_status,
        "idempotente": idempotente,
        "suscripcion_activa_hasta": vigencia_hasta.isoformat() if vigencia_hasta else None,
    }


def construir_url_retorno_frontend(
    frontend_public_url, orden_comercio=None, estado=None, error=None
):
    base = (frontend_public_url or "http://localhost:5173").strip().rstrip("/")
    query = {}
    if orden_comercio:
        query["pago_orden"] = orden_comercio
    if estado:
        query["estado"] = estado
    if error:
        query["pago_error"] = error
    if not query:
        return f"{base}/"
    return f"{base}/?{urlencode(query)}"


def obtener_detalle_orden_usuario(usuario_actual, orden_comercio):
    if not isinstance(orden_comercio, str) or not ORDEN_COMERCIO_PATTERN.fullmatch(
        orden_comercio.strip()
    ):
        raise OrdenNoEncontradaError("Orden no encontrada.")

    orden = repository.buscar_orden_por_comercio(orden_comercio.strip())
    if not orden:
        raise OrdenNoEncontradaError("Orden no encontrada.")

    if orden.get("usuario_id") != usuario_actual["_id"]:
        raise AccesoOrdenDenegadoError("No tienes permiso para consultar esta orden.")

    usuario_fresco = obtener_usuario_por_id(usuario_actual["_id"]) or usuario_actual
    estado_suscripcion = obtener_estado_suscripcion(usuario_fresco)
    fecha_creacion = normalize_utc_datetime(orden.get("fecha_creacion"))
    fecha_pago = normalize_utc_datetime(orden.get("fecha_pago"))
    vigencia_orden = normalize_utc_datetime(orden.get("vigencia_otorgada_hasta"))

    return {
        "orden_comercio": orden["orden_comercio"],
        "plan_id": orden.get("plan_id"),
        "items": orden.get("items", []),
        "monto_total": orden["monto_total"],
        "moneda": orden["moneda"],
        "duracion_dias": orden.get("duracion_dias", 30),
        "estado": orden["estado"],
        "activacion_estado": orden.get("activacion_estado", "pendiente"),
        "flow_orden": orden.get("flow_orden"),
        "fecha_creacion": fecha_creacion.isoformat() if fecha_creacion else None,
        "fecha_pago": fecha_pago.isoformat() if fecha_pago else None,
        "vigencia_otorgada_hasta": vigencia_orden.isoformat() if vigencia_orden else None,
        **estado_suscripcion,
    }
