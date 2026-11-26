from pymongo import ReturnDocument

from backend.app.extensions import mongo

_pagos_indices_listos = False


def asegurar_indices_pagos():
    global _pagos_indices_listos
    if _pagos_indices_listos:
        return
    mongo.db.ordenes.create_index("orden_comercio", unique=True)
    mongo.db.ordenes.create_index("flow_token", unique=True, sparse=True)
    mongo.db.ordenes.create_index([("usuario_id", 1), ("fecha_creacion", -1)])
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
                "flow_orden": flow_orden,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def marcar_orden_fallida(orden_id, fecha_actualizacion):
    mongo.db.ordenes.update_one(
        {"_id": orden_id, "suscripcion_aplicada": {"$ne": True}},
        {
            "$set": {
                "estado": "fallida",
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
    )


def buscar_orden_por_token(flow_token):
    return mongo.db.ordenes.find_one({"flow_token": flow_token})


def buscar_orden_por_comercio(orden_comercio):
    return mongo.db.ordenes.find_one({"orden_comercio": orden_comercio})


def reclamar_orden_para_activacion(orden_id, flow_orden, fecha_pago):
    """Transiciona atómicamente la orden a pagada una única vez para garantizar idempotencia."""
    return mongo.db.ordenes.find_one_and_update(
        {
            "_id": orden_id,
            "suscripcion_aplicada": {"$ne": True},
        },
        {
            "$set": {
                "estado": "pagada",
                "suscripcion_aplicada": True,
                "flow_orden": flow_orden,
                "flow_status": 2,
                "fecha_pago": fecha_pago,
                "fecha_actualizacion": fecha_pago,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def revertir_reclamo_activacion(orden_id, fecha_actualizacion):
    mongo.db.ordenes.update_one(
        {"_id": orden_id},
        {
            "$set": {
                "estado": "pendiente",
                "suscripcion_aplicada": False,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
    )


def guardar_vigencia_otorgada_en_orden(orden_id, vigencia_hasta, fecha_actualizacion):
    return mongo.db.ordenes.find_one_and_update(
        {"_id": orden_id},
        {
            "$set": {
                "vigencia_otorgada_hasta": vigencia_hasta,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def actualizar_estado_no_pagado(
    orden_id, nuevo_estado, flow_status, flow_orden, fecha_actualizacion
):
    return mongo.db.ordenes.find_one_and_update(
        {
            "_id": orden_id,
            "estado": {"$ne": "pagada"},
            "suscripcion_aplicada": {"$ne": True},
        },
        {
            "$set": {
                "estado": nuevo_estado,
                "flow_status": flow_status,
                "flow_orden": flow_orden,
                "fecha_actualizacion": fecha_actualizacion,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


def extender_suscripcion_usuario(usuario_id, duracion_dias, ahora, calcular_vigencia_fn):
    """Extiende la fecha `suscripcion_activa_hasta` mediante compare-and-swap atómico."""
    for _ in range(5):
        usuario = mongo.db.usuarios.find_one({"_id": usuario_id})
        if not usuario:
            raise LookupError("El usuario asociado a la orden no existe.")

        vigencia_previa = usuario.get("suscripcion_activa_hasta")
        nueva_vigencia = calcular_vigencia_fn(
            vigencia_previa,
            duracion_dias=duracion_dias,
            ahora=ahora,
        )
        resultado = mongo.db.usuarios.update_one(
            {
                "_id": usuario_id,
                "suscripcion_activa_hasta": vigencia_previa,
            },
            {
                "$set": {
                    "suscripcion_activa_hasta": nueva_vigencia,
                    "fecha_ultima_renovacion": ahora,
                }
            },
        )
        if getattr(resultado, "modified_count", 0) == 1 or getattr(resultado, "matched_count", 0) == 1:
            return nueva_vigencia

    raise RuntimeError("No se pudo actualizar la suscripción por contención concurrente.")


def registrar_transaccion_pago(evento):
    asegurar_indices_pagos()
    mongo.db.transacciones_pago.insert_one(evento)
