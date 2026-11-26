from flask import Blueprint, current_app, jsonify, request

from backend.app.auth.repository import EmailConflictError
from backend.app.auth.service import (
    AuthValidationError,
    guardar_email_usuario,
    obtener_estado_suscripcion,
    serializar_perfil_usuario,
)
from backend.app.extensions import rate_limit, token_required

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/api/perfil", methods=["GET"])
@token_required
def obtener_perfil(usuario_actual):
    return (
        jsonify(
            {
                "status": "success",
                "mensaje": f"Hola {usuario_actual['username']}.",
                "datos_usuario": serializar_perfil_usuario(usuario_actual),
            }
        ),
        200,
    )


@auth_bp.route("/api/perfil/suscripcion", methods=["GET"])
@token_required
def consultar_suscripcion(usuario_actual):
    estado = obtener_estado_suscripcion(usuario_actual)
    return (
        jsonify(
            {
                "status": "success",
                "email": usuario_actual.get("email"),
                **estado,
            }
        ),
        200,
    )


@auth_bp.route("/api/perfil/email", methods=["GET"])
@token_required
def consultar_email(usuario_actual):
    estado = obtener_estado_suscripcion(usuario_actual)
    return (
        jsonify(
            {
                "status": "success",
                "email": usuario_actual.get("email"),
                **estado,
            }
        ),
        200,
    )


@auth_bp.route("/api/perfil/email", methods=["POST", "PUT"])
@token_required
@rate_limit("actualizar-email", limit=10, window_seconds=3600, authenticated=True)
def actualizar_email(usuario_actual):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400

    try:
        usuario_actualizado = guardar_email_usuario(usuario_actual, datos.get("email"))
    except AuthValidationError as error:
        return jsonify({"error": str(error)}), 400
    except EmailConflictError as error:
        return jsonify({"error": str(error)}), 409
    except Exception:
        current_app.logger.exception("No se pudo actualizar el correo del usuario")
        return jsonify({"error": "No se pudo actualizar el correo electrónico"}), 500

    estado = obtener_estado_suscripcion(usuario_actualizado)
    return (
        jsonify(
            {
                "status": "success",
                "mensaje": "Correo electrónico actualizado correctamente",
                "email": usuario_actualizado.get("email"),
                **estado,
            }
        ),
        200,
    )
