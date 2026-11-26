import hmac
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from bson.errors import InvalidId
from bson.objectid import ObjectId
from flask import current_app, g, jsonify, request
from flask_pymongo import PyMongo
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

mongo = PyMongo()
_rate_limit_index_ready = False


def parse_object_id(value):
    if not isinstance(value, str) or not ObjectId.is_valid(value):
        return None
    return ObjectId(value)


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        scheme, separator, token = auth_header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            token = None

        cookie_authenticated = token is None
        if cookie_authenticated:
            token = request.cookies.get(current_app.config["JWT_COOKIE_NAME"])

        if not token:
            return jsonify({"error": "Acceso denegado"}), 401

        try:
            data = jwt.decode(
                token,
                current_app.config["SECRET_KEY"],
                algorithms=["HS256"],
                issuer=current_app.config["JWT_ISSUER"],
                options={"require": ["exp", "iat", "iss", "usuario_id", "csrf"]},
            )
            usuario_id = ObjectId(data["usuario_id"])
        except (jwt.PyJWTError, InvalidId, KeyError, TypeError, ValueError):
            return jsonify({"error": "Token inválido"}), 401

        if cookie_authenticated and request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not csrf_header or not hmac.compare_digest(str(data["csrf"]), csrf_header):
                return jsonify({"error": "Validación CSRF fallida"}), 403

        usuario_actual = mongo.db.usuarios.find_one({"_id": usuario_id})
        if not usuario_actual:
            return jsonify({"error": "Token inválido"}), 401

        g.usuario_id = usuario_id
        g.csrf_token = data["csrf"]
        return f(usuario_actual, *args, **kwargs)

    return decorated


def rate_limit(scope, limit, window_seconds, authenticated=False):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            global _rate_limit_index_ready
            identity = (
                str(g.get("usuario_id"))
                if authenticated
                else (request.remote_addr or "unknown")
            )
            window = int(time.time() // window_seconds)
            digest = hmac.new(
                current_app.config["SECRET_KEY"].encode("utf-8"),
                f"{scope}:{identity}:{window}".encode("utf-8"),
                "sha256",
            ).hexdigest()
            now = datetime.now(timezone.utc)
            try:
                if not _rate_limit_index_ready:
                    mongo.db.rate_limits.create_index("expires_at", expireAfterSeconds=0)
                    _rate_limit_index_ready = True
                result = mongo.db.rate_limits.find_one_and_update(
                    {"_id": digest},
                    {
                        "$inc": {"count": 1},
                        "$setOnInsert": {
                            "expires_at": now + timedelta(seconds=window_seconds * 2)
                        },
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                result = mongo.db.rate_limits.find_one_and_update(
                    {"_id": digest},
                    {"$inc": {"count": 1}},
                    return_document=ReturnDocument.AFTER,
                )
            except Exception:
                current_app.logger.exception("No se pudo evaluar el límite de solicitudes")
                return jsonify({"error": "El servicio no está disponible temporalmente"}), 503

            if result["count"] > limit:
                retry_after = max(1, window_seconds - int(time.time() % window_seconds))
                response = jsonify(
                    {"error": "Demasiadas solicitudes. Intenta más tarde."}
                )
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response
            return f(*args, **kwargs)

        return decorated

    return decorator
