from flask import Blueprint, current_app, jsonify, redirect, request

from backend.app.auth.repository import EmailConflictError
from backend.app.extensions import rate_limit, token_required
from backend.app.integrations.flow_client import (
    FlowClient,
    FlowConfigurationError,
    FlowProviderError,
)
from backend.app.pagos import catalog
from backend.app.pagos.repository import buscar_orden_por_token
from backend.app.pagos.service import (
    AccesoOrdenDenegadoError,
    IntegridadPagoError,
    OrdenNoEncontradaError,
    PagoValidationError,
    construir_url_retorno_frontend,
    iniciar_pago_suscripcion,
    obtener_detalle_orden_usuario,
    procesar_confirmacion_flow,
)

pagos_bp = Blueprint("pagos", __name__)


def _get_flow_client():
    return FlowClient.from_app_config(current_app.config)


@pagos_bp.route("/api/planes", methods=["GET"])
def listar_planes():
    return (
        jsonify(
            {
                "status": "success",
                "planes": catalog.listar_planes_activos(),
            }
        ),
        200,
    )


@pagos_bp.route("/api/pagos/flow/iniciar", methods=["POST"])
@token_required
@rate_limit("pagos-iniciar", limit=15, window_seconds=3600, authenticated=True)
def iniciar_pago(usuario_actual):
    datos = request.get_json(silent=True)
    if datos is None and request.data:
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400

    try:
        resultado = iniciar_pago_suscripcion(
            usuario_actual=usuario_actual,
            datos=datos or {},
            flow_client=_get_flow_client(),
            config=current_app.config,
        )
        return jsonify({"status": "success", **resultado}), 201
    except PagoValidationError as error:
        return jsonify({"error": str(error)}), 400
    except EmailConflictError as error:
        return jsonify({"error": str(error)}), 409
    except FlowConfigurationError:
        return jsonify({"error": "La pasarela de pagos no está configurada"}), 503
    except FlowProviderError:
        current_app.logger.warning("Fallo de comunicación con Flow al crear pago")
        return jsonify({"error": "No se pudo iniciar el pago con el proveedor"}), 502
    except Exception:
        current_app.logger.exception("Error inesperado al iniciar el pago en Flow")
        return jsonify({"error": "No se pudo procesar la solicitud de pago"}), 500


@pagos_bp.route("/api/pagos/flow/confirmacion", methods=["POST"])
@rate_limit("flow-confirmacion", limit=120, window_seconds=60, authenticated=False)
def confirmar_pago_flow():
    token = request.form.get("token")
    if token is None:
        datos = request.get_json(silent=True)
        if isinstance(datos, dict):
            token = datos.get("token")

    try:
        resultado = procesar_confirmacion_flow(
            raw_token=token,
            flow_client=_get_flow_client(),
            origen="confirmacion",
        )
        return jsonify({"status": "success", **resultado}), 200
    except PagoValidationError as error:
        return jsonify({"error": str(error)}), 400
    except OrdenNoEncontradaError:
        return jsonify({"error": "Orden no encontrada"}), 404
    except IntegridadPagoError:
        current_app.logger.warning("Discrepancia de integridad en confirmación Flow")
        return jsonify({"error": "Validación de pago rechazada"}), 400
    except FlowConfigurationError:
        return jsonify({"error": "La pasarela de pagos no está configurada"}), 503
    except FlowProviderError:
        current_app.logger.warning("No se pudo verificar estado del pago en Flow")
        return jsonify({"error": "No se pudo verificar el estado con Flow"}), 502
    except Exception:
        current_app.logger.exception("Error inesperado en confirmación de Flow")
        return jsonify({"error": "Error al confirmar el pago"}), 500


@pagos_bp.route("/api/pagos/flow/retorno", methods=["POST", "GET"])
@rate_limit("flow-retorno", limit=60, window_seconds=60, authenticated=False)
def retorno_pago_flow():
    frontend_url = current_app.config.get("FRONTEND_PUBLIC_URL", "http://localhost:5173")
    token = request.form.get("token") or request.args.get("token")

    if not isinstance(token, str) or not (1 <= len(token.strip()) <= 256):
        destino = construir_url_retorno_frontend(frontend_url, error="token_invalido")
        return redirect(destino, code=303)

    token_limpio = token.strip()
    try:
        resultado = procesar_confirmacion_flow(
            raw_token=token_limpio,
            flow_client=_get_flow_client(),
            origen="retorno",
        )
        destino = construir_url_retorno_frontend(
            frontend_url,
            orden_comercio=resultado["orden_comercio"],
            estado=resultado["estado"],
        )
        return redirect(destino, code=303)
    except (FlowConfigurationError, FlowProviderError):
        orden = buscar_orden_por_token(token_limpio)
        if orden:
            estado_actual = (
                "pagada"
                if orden.get("suscripcion_aplicada")
                else orden.get("estado", "pendiente")
            )
            destino = construir_url_retorno_frontend(
                frontend_url,
                orden_comercio=orden["orden_comercio"],
                estado=estado_actual,
            )
            return redirect(destino, code=303)
        destino = construir_url_retorno_frontend(
            frontend_url, error="verificacion_pendiente"
        )
        return redirect(destino, code=303)
    except OrdenNoEncontradaError:
        destino = construir_url_retorno_frontend(
            frontend_url, error="orden_no_encontrada"
        )
        return redirect(destino, code=303)
    except Exception:
        current_app.logger.exception("Error al procesar retorno desde Flow")
        destino = construir_url_retorno_frontend(frontend_url, error="error_retorno")
        return redirect(destino, code=303)


@pagos_bp.route("/api/ordenes/<orden_comercio>", methods=["GET"])
@token_required
def consultar_orden(usuario_actual, orden_comercio):
    try:
        detalle = obtener_detalle_orden_usuario(
            usuario_actual,
            orden_comercio,
            flow_client=_get_flow_client(),
        )
        return jsonify({"status": "success", "orden": detalle}), 200
    except OrdenNoEncontradaError:
        return jsonify({"error": "Orden no encontrada"}), 404
    except AccesoOrdenDenegadoError as error:
        return jsonify({"error": str(error)}), 403
    except Exception:
        current_app.logger.exception("Error al consultar orden de pago")
        return jsonify({"error": "No se pudo consultar la orden"}), 500
