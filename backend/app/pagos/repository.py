from datetime import timedelta

from pymongo import ReturnDocument

from backend.app.auth.service import normalize_utc_datetime
from backend.app.extensions import mongo

_pagos_indices_listos = False
ACTIVACION_LOCK_TTL_SECONDS = 60


def asegurar_indices_pagos():
    global _pagos_indices_listos
    if _pagos_indices_listos:
        return
    mongo.db.ordenes.create_index("orden_comercio", unique=True)
    mongo.db.ordenes.create_index("flow_token", unique=True, sparse=True)
    mongo.db.ordenes.create_index([("usuario_id", 1), ("fecha_creacion", -1)])
    mongo.db.ordenes.create_index(
        [("requiere_reconciliacion", 1), ("activacion_estado", 1)]
    )
    mongo.db.transacciones_pago.create_index([("orden_id", 1), ("fecha_evento", -1)])
    _pagos_indices_listos = True


def crear_orden(documento_orden):
    asegurar_indices_pagos()
    resultado = mongo.db.ordenes.insert_one(documento_orden)
    documento_orden["_id"] = resultado.inserted_id
    return documento_orden


def guardar_datos_flow_en_orden(orden_id, flow_token, flow_orden, fecha_actualizacion):
    return mongo.db.ordenes.find_one_and_update(
        {"_id": orden_id},
        {
            "$set": {
                "flow_token": flow_token,
                "ultimo_token_flow": flow_token,
                "flow_orden": flow_orden,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def marcar_orden_fallida(orden_id, fecha_actualizacion):
    mongo.db.ordenes.update_one(
        {
            "_id": orden_id,
            "suscripcion_aplicada": {"$ne": True},
            "activacion_estado": {"$nin": ["en_proceso", "aplicada"]},
        },
        {
            "$set": {
                "estado": "fallida",
                "requiere_reconciliacion": False,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
    )


def buscar_orden_por_token(flow_token):
    orden = mongo.db.ordenes.find_one({"flow_token": flow_token})
    if orden is not None:
        return orden
    return mongo.db.ordenes.find_one({"ultimo_token_flow": flow_token})


def buscar_orden_por_comercio(orden_comercio):
    return mongo.db.ordenes.find_one({"orden_comercio": orden_comercio})


def persistir_recepcion_confirmacion(orden_id, token, ahora):
    """
    Registra de forma duradera en la orden la llegada de una notificación/token de Flow
    antes de reclamar o ejecutar la activación, marcando `requiere_reconciliacion=True`
    únicamente mientras la orden no esté ya `"aplicada"`.
    """
    actualizada = mongo.db.ordenes.find_one_and_update(
        {
            "_id": orden_id,
            "suscripcion_aplicada": {"$ne": True},
            "activacion_estado": {"$ne": "aplicada"},
        },
        {
            "$set": {
                "confirmacion_recibida_en": ahora,
                "ultimo_token_flow": token,
                "requiere_reconciliacion": True,
                "fecha_actualizacion": ahora,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if actualizada is not None:
        return actualizada
    return mongo.db.ordenes.find_one({"_id": orden_id})


def buscar_ordenes_reconciliables(
    usuario_id=None,
    orden_comercio=None,
    ahora=None,
    lock_ttl_seconds=ACTIVACION_LOCK_TTL_SECONDS,
    limite=5,
):
    """
    Obtiene órdenes con `requiere_reconciliacion=True` o bloqueos `"en_proceso"` vencidos
    para completar automáticamente su activación cuando sea seguro hacerlo.
    """
    coleccion = getattr(mongo.db, "ordenes", None)
    if coleccion is None or not hasattr(coleccion, "find"):
        return []

    expiracion_bloqueo = ahora - timedelta(seconds=lock_ttl_seconds)
    filtro = {
        "suscripcion_aplicada": {"$ne": True},
        "activacion_estado": {"$ne": "aplicada"},
        "$or": [
            {
                "requiere_reconciliacion": True,
                "activacion_estado": {"$in": [None, "pendiente"]},
            },
            {
                "activacion_estado": "en_proceso",
                "activacion_intento_en": None,
            },
            {
                "activacion_estado": "en_proceso",
                "activacion_intento_en": {"$lte": expiracion_bloqueo},
            },
        ],
    }
    if usuario_id is not None:
        filtro["usuario_id"] = usuario_id
    if orden_comercio is not None:
        filtro["orden_comercio"] = orden_comercio

    return list(coleccion.find(filtro, limit=limite))


def adquirir_orden_para_activacion(
    orden_id,
    flow_orden,
    ahora,
    lock_ttl_seconds=ACTIVACION_LOCK_TTL_SECONDS,
):
    """
    Pasa la orden a `activacion_estado="en_proceso"` sin marcar `suscripcion_aplicada=True`.
    Permite retomar órdenes interrumpidas cuyo bloqueo `activacion_intento_en` esté vacío o vencido.
    """
    expiracion_bloqueo = ahora - timedelta(seconds=lock_ttl_seconds)
    return mongo.db.ordenes.find_one_and_update(
        {
            "_id": orden_id,
            "suscripcion_aplicada": {"$ne": True},
            "activacion_estado": {"$ne": "aplicada"},
            "$or": [
                {"activacion_estado": {"$in": [None, "pendiente"]}},
                {"activacion_estado": "en_proceso", "activacion_intento_en": None},
                {
                    "activacion_estado": "en_proceso",
                    "activacion_intento_en": {"$lte": expiracion_bloqueo},
                },
            ],
        },
        {
            "$set": {
                "activacion_estado": "en_proceso",
                "activacion_intento_en": ahora,
                "requiere_reconciliacion": True,
                "flow_orden": flow_orden,
                "flow_status": 2,
                "fecha_actualizacion": ahora,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def fijar_vigencia_objetivo_en_orden(
    orden_id,
    vigencia_objetivo,
    vigencia_base_previa,
    fecha_pago,
    ahora,
):
    """
    Registra de forma determinista `vigencia_otorgada_hasta` en la orden antes de mutar `usuarios`,
    conservando cualquier valor previo si la orden se está retomando tras una interrupción.
    """
    actualizada = mongo.db.ordenes.find_one_and_update(
        {
            "_id": orden_id,
            "vigencia_otorgada_hasta": None,
        },
        {
            "$set": {
                "vigencia_otorgada_hasta": vigencia_objetivo,
                "vigencia_base_previa": vigencia_base_previa,
                "fecha_pago": fecha_pago,
                "fecha_actualizacion": ahora,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if actualizada is not None:
        return actualizada
    return mongo.db.ordenes.find_one({"_id": orden_id})


def liberar_intento_activacion_en_proceso(orden_id, fecha_actualizacion):
    """
    Mantiene `activacion_estado="en_proceso"` y `requiere_reconciliacion=True`,
    liberando `activacion_intento_en` ante un fallo recuperable.
    """
    mongo.db.ordenes.update_one(
        {
            "_id": orden_id,
            "suscripcion_aplicada": {"$ne": True},
            "activacion_estado": "en_proceso",
        },
        {
            "$set": {
                "activacion_intento_en": None,
                "requiere_reconciliacion": True,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
    )


def finalizar_activacion_orden(
    orden_id,
    vigencia_hasta,
    flow_orden,
    fecha_pago,
    fecha_actualizacion,
):
    """Marca la orden como pagada, `suscripcion_aplicada=True` y limpia `requiere_reconciliacion`."""
    return mongo.db.ordenes.find_one_and_update(
        {"_id": orden_id},
        {
            "$set": {
                "estado": "pagada",
                "activacion_estado": "aplicada",
                "suscripcion_aplicada": True,
                "requiere_reconciliacion": False,
                "vigencia_otorgada_hasta": vigencia_hasta,
                "flow_orden": flow_orden,
                "flow_status": 2,
                "fecha_pago": fecha_pago,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def actualizar_estado_no_pagado(
    orden_id, nuevo_estado, flow_status, flow_orden, fecha_actualizacion
):
    requiere_reconciliacion = nuevo_estado == "pendiente"
    return mongo.db.ordenes.find_one_and_update(
        {
            "_id": orden_id,
            "estado": {"$ne": "pagada"},
            "suscripcion_aplicada": {"$ne": True},
            "activacion_estado": {"$nin": ["en_proceso", "aplicada"]},
        },
        {
            "$set": {
                "estado": nuevo_estado,
                "flow_status": flow_status,
                "flow_orden": flow_orden,
                "requiere_reconciliacion": requiere_reconciliacion,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def aplicar_vigencia_idempotente_usuario(
    usuario_id,
    orden_id,
    vigencia_objetivo,
    vigencia_base_previa,
    duracion_dias,
    ahora,
    calcular_vigencia_fn,
):
    """
    Aplica la vigencia de la orden en `usuarios` en MongoDB standalone de forma atómica e idempotente.
    Si una ejecución previa ya aplicó esta orden (registrada en `ordenes_aplicadas` o coincidente con
    `vigencia_objetivo`) antes de poder cerrar la orden, no vuelve a sumar 30 días.
    """
    vigencia_objetivo_utc = normalize_utc_datetime(vigencia_objetivo)
    vigencia_base_previa_utc = normalize_utc_datetime(vigencia_base_previa)

    for _ in range(5):
        usuario = mongo.db.usuarios.find_one({"_id": usuario_id})
        if not usuario:
            raise LookupError("El usuario asociado a la orden no existe.")

        ordenes_aplicadas = list(usuario.get("ordenes_aplicadas") or [])
        vigencia_actual = usuario.get("suscripcion_activa_hasta")
        vigencia_actual_utc = normalize_utc_datetime(vigencia_actual)

        ya_aplicada_con_registro = orden_id in ordenes_aplicadas
        ya_aplicada_sin_registro = (
            not ordenes_aplicadas
            and vigencia_objetivo_utc is not None
            and vigencia_actual_utc == vigencia_objetivo_utc
        )
        if ya_aplicada_con_registro or ya_aplicada_sin_registro:
            if not ya_aplicada_con_registro:
                mongo.db.usuarios.update_one(
                    {"_id": usuario_id},
                    {"$addToSet": {"ordenes_aplicadas": orden_id}},
                )
            return vigencia_actual_utc or vigencia_objetivo_utc

        if (
            vigencia_objetivo_utc is not None
            and vigencia_actual_utc == vigencia_base_previa_utc
        ):
            nueva_vigencia = vigencia_objetivo_utc
        else:
            nueva_vigencia = calcular_vigencia_fn(
                vigencia_actual_utc,
                duracion_dias=duracion_dias,
                ahora=ahora,
            )

        resultado = mongo.db.usuarios.update_one(
            {
                "_id": usuario_id,
                "suscripcion_activa_hasta": vigencia_actual,
                "ordenes_aplicadas": {"$ne": orden_id},
            },
            {
                "$set": {
                    "suscripcion_activa_hasta": nueva_vigencia,
                    "fecha_ultima_renovacion": ahora,
                },
                "$addToSet": {
                    "ordenes_aplicadas": orden_id,
                },
            },
        )
        if (
            getattr(resultado, "modified_count", 0) == 1
            or getattr(resultado, "matched_count", 0) == 1
        ):
            return nueva_vigencia

    raise RuntimeError("No se pudo actualizar la suscripción por contención concurrente.")


def registrar_transaccion_pago(evento):
    asegurar_indices_pagos()
    mongo.db.transacciones_pago.insert_one(evento)
