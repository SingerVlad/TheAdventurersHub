PLAN_PASE_AVENTURERO_ID = "pase-aventurero-30d"

PLANES_SUSCRIPCION = {
    PLAN_PASE_AVENTURERO_ID: {
        "id": PLAN_PASE_AVENTURERO_ID,
        "sku": PLAN_PASE_AVENTURERO_ID,
        "nombre": "Pase Aventurero Total (30 días - Acceso Completo)",
        "descripcion": (
            "Acceso total por 30 días exactos a herramientas del gremio e IA Lab. "
            "Pago manual sin cobros automáticos ni almacenamiento de tarjetas."
        ),
        "monto": 1490,
        "moneda": "CLP",
        "duracion_dias": 30,
        "activo": True,
    }
}


def listar_planes_activos():
    return [dict(plan) for plan in PLANES_SUSCRIPCION.values() if plan.get("activo")]


def obtener_plan(plan_id=None):
    identificador = PLAN_PASE_AVENTURERO_ID if plan_id is None else plan_id
    if not isinstance(identificador, str):
        return None
    plan = PLANES_SUSCRIPCION.get(identificador.strip())
    if not plan or not plan.get("activo"):
        return None
    return dict(plan)
