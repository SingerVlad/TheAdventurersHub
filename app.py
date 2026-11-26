import os
import base64
import hmac
import re
import secrets
import binascii
from pathlib import Path
from groq import Groq
import uuid
import jwt
import json
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, g, jsonify, request, send_file
from flask_cors import CORS
from flask_pymongo import PyMongo
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv

import time
import requests

load_dotenv()

app = Flask(__name__)
secret_key = os.getenv("SECRET_KEY", "")
if len(secret_key.encode("utf-8")) < 32 or secret_key == "replace-with-a-long-random-secret":
    raise RuntimeError("SECRET_KEY debe configurarse con al menos 32 bytes aleatorios.")

app.config["SECRET_KEY"] = secret_key
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["MAX_IMAGE_BYTES"] = 5 * 1024 * 1024
app.config["JWT_ISSUER"] = "the-adventurers-hub"
app.config["APP_ENV"] = os.getenv("APP_ENV", "development").lower()
app.config["JWT_COOKIE_NAME"] = "access_token"
app.config["JWT_COOKIE_SECURE"] = (
    app.config["APP_ENV"] == "production"
    or os.getenv("COOKIE_SECURE", "false").lower() == "true"
)
app.config["JWT_COOKIE_SAMESITE"] = os.getenv("COOKIE_SAMESITE", "Lax")
if app.config["JWT_COOKIE_SAMESITE"] not in {"Strict", "Lax", "None"}:
    raise RuntimeError("COOKIE_SAMESITE debe ser Strict, Lax o None.")
if app.config["JWT_COOKIE_SAMESITE"] == "None" and not app.config["JWT_COOKIE_SECURE"]:
    raise RuntimeError("COOKIE_SECURE debe ser true cuando COOKIE_SAMESITE=None.")

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
]
CORS(
    app,
    resources={r"/api/*": {"origins": allowed_origins}},
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    supports_credentials=True,
)

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    if app.config["APP_ENV"] == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.name}), error.code
        return error
    app.logger.exception("Error no controlado en la solicitud")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Error interno del servidor"}), 500
    return "Error interno del servidor", 500


app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017/adventurers_hub")
if app.config["APP_ENV"] == "production":
    if not os.getenv("MONGO_URI"):
        raise RuntimeError("MONGO_URI debe configurarse en producción.")
    if not allowed_origins or any(not origin.startswith("https://") for origin in allowed_origins):
        raise RuntimeError("CORS_ORIGINS debe contener únicamente orígenes HTTPS en producción.")
FOUNDRY_DATA_PATH = Path(os.getenv("FOUNDRY_DATA_PATH", "./var/foundry/imports")).resolve()

mongo = PyMongo(app)
_rate_limit_index_ready = False

try:
    trusted_proxy_count = int(os.getenv("TRUST_PROXY_COUNT", "0"))
except ValueError as error:
    raise RuntimeError("TRUST_PROXY_COUNT debe ser un entero no negativo.") from error
if trusted_proxy_count < 0:
    raise RuntimeError("TRUST_PROXY_COUNT debe ser un entero no negativo.")
if trusted_proxy_count:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxy_count,
        x_proto=trusted_proxy_count,
        x_host=trusted_proxy_count,
    )

# GUARDIA DE SEGURIDAD (Decorador JWT)
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        scheme, separator, token = auth_header.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            token = None

        cookie_authenticated = token is None
        if cookie_authenticated:
            token = request.cookies.get(app.config["JWT_COOKIE_NAME"])

        if not token:
            return jsonify({"error": "Acceso denegado"}), 401
        
        try:
            data = jwt.decode(
                token,
                app.config["SECRET_KEY"],
                algorithms=["HS256"],
                issuer=app.config["JWT_ISSUER"],
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
            identity = str(g.get("usuario_id")) if authenticated else (request.remote_addr or "unknown")
            window = int(time.time() // window_seconds)
            digest = hmac.new(
                app.config["SECRET_KEY"].encode("utf-8"),
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
                        "$setOnInsert": {"expires_at": now + timedelta(seconds=window_seconds * 2)},
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                result = mongo.db.rate_limits.find_one_and_update(
                    {"_id": digest}, {"$inc": {"count": 1}}, return_document=ReturnDocument.AFTER
                )
            except Exception:
                app.logger.exception("No se pudo evaluar el límite de solicitudes")
                return jsonify({"error": "El servicio no está disponible temporalmente"}), 503

            if result["count"] > limit:
                retry_after = max(1, window_seconds - int(time.time() % window_seconds))
                response = jsonify({"error": "Demasiadas solicitudes. Intenta más tarde."})
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response
            return f(*args, **kwargs)
        return decorated
    return decorator


def parse_object_id(value):
    if not isinstance(value, str) or not ObjectId.is_valid(value):
        return None
    return ObjectId(value)


def get_campaign_access(campana_id, usuario_id):
    object_id = parse_object_id(campana_id)
    if object_id is None:
        return None, False, False
    campana = mongo.db.campanas.find_one({"_id": object_id})
    if not campana:
        return None, False, False
    es_dm = campana.get("dm_id") == usuario_id
    es_jugador = usuario_id in campana.get("jugadores", [])
    return campana, es_dm, es_jugador


def resolve_asset_file(file_path):
    if not isinstance(file_path, str) or not file_path:
        return None
    root = FOUNDRY_DATA_PATH.resolve()
    candidate = (root / file_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None



# 3. RUTAS DE AUTENTICACIÓN
@app.route('/api/registro', methods=['POST'])
@rate_limit("registro", limit=5, window_seconds=3600)
def registro():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400

    username = datos.get("username")
    password = datos.get("password")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", username):
        return jsonify({"error": "El nombre de usuario debe tener 3 a 32 caracteres alfanuméricos, guion o guion bajo"}), 400
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        return jsonify({"error": "La contraseña debe tener entre 12 y 128 caracteres"}), 400

    try:
        mongo.db.usuarios.create_index("username", unique=True)
    except Exception:
        app.logger.exception("No se pudo verificar el índice único de usuarios")
        return jsonify({"error": "El servicio de registro no está disponible"}), 503

    if mongo.db.usuarios.find_one({"username": username}):
        return jsonify({"error": "El usuario ya existe"}), 409
    
    nuevo_usuario = {
        "username": username,
        "password_hash": generate_password_hash(password),
        "fecha_registro": datetime.now(timezone.utc)
    }
    try:
        mongo.db.usuarios.insert_one(nuevo_usuario)
    except DuplicateKeyError:
        return jsonify({"error": "El usuario ya existe"}), 409
    return jsonify({"status": "success", "mensaje": "Usuario creado"}), 201

    

@app.route('/api/login', methods=['POST'])
@rate_limit("login", limit=10, window_seconds=900)
def login():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400

    username = datos.get("username")
    password = datos.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"error": "Credenciales inválidas"}), 401

    usuario = mongo.db.usuarios.find_one({"username": username})
    
    if not usuario or not check_password_hash(usuario['password_hash'], password):
        return jsonify({"error": "Credenciales inválidas"}), 401
        
    csrf_token = secrets.token_urlsafe(32)
    token = jwt.encode({
        'usuario_id': str(usuario['_id']),
        'iat': datetime.now(timezone.utc),
        'iss': app.config["JWT_ISSUER"],
        'csrf': csrf_token,
        'exp': datetime.now(timezone.utc) + timedelta(minutes=30)
    }, app.config['SECRET_KEY'], algorithm='HS256')

    response = jsonify({
        "status": "success",
        "authenticated": True,
        "csrf_token": csrf_token,
        "username": usuario['username'],
    })
    response.set_cookie(
        app.config["JWT_COOKIE_NAME"],
        token,
        max_age=1800,
        httponly=True,
        secure=app.config["JWT_COOKIE_SECURE"],
        samesite=app.config["JWT_COOKIE_SAMESITE"],
        path="/api",
    )
    return response


@app.route('/api/csrf', methods=['GET'])
@token_required
def obtener_token_csrf(usuario_actual):
    return jsonify({"csrf_token": g.csrf_token}), 200


@app.route('/api/logout', methods=['POST'])
@token_required
def cerrar_sesion(usuario_actual):
    response = jsonify({"status": "success"})
    response.delete_cookie(
        app.config["JWT_COOKIE_NAME"],
        path="/api",
        secure=app.config["JWT_COOKIE_SECURE"],
        httponly=True,
        samesite=app.config["JWT_COOKIE_SAMESITE"],
    )
    return response









@app.route('/api/perfil', methods=['GET'])
@token_required # <--- ¡Aquí está la magia! Solo los que tengan pase entran.
def obtener_perfil(usuario_actual):
    return jsonify({
        "status": "success",
        "mensaje": f"Hola {usuario_actual['username']}.",
        "datos_usuario": {
            "id": str(usuario_actual['_id']),
            "username": usuario_actual['username'],
            "fecha_registro": usuario_actual['fecha_registro'].isoformat()
        }
    }), 200

@app.route('/api/campanas', methods=['POST'])
@token_required
@rate_limit("crear-campana", limit=10, window_seconds=86400, authenticated=True)
def crear_campana(usuario_actual):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400

    nombre = datos.get("nombre")
    descripcion = datos.get("descripcion", "")
    if not isinstance(nombre, str) or not 1 <= len(nombre.strip()) <= 100:
        return jsonify({"error": "El nombre de la campaña debe tener entre 1 y 100 caracteres"}), 400
    if not isinstance(descripcion, str) or len(descripcion) > 2000:
        return jsonify({"error": "La descripción no puede superar 2000 caracteres"}), 400

    campanas = mongo.db.campanas
    try:
        campanas.create_index("codigo_invitacion", unique=True)
    except PyMongoError:
        app.logger.exception("No se pudo verificar el índice único de invitaciones")
        return jsonify({"error": "La creación de campañas no está disponible"}), 503

    nueva_campana = {
        "nombre": nombre.strip(),
        "descripcion": descripcion.strip(),
        "dm_id": usuario_actual["_id"],
        "jugadores": [],
    }
    for _ in range(3):
        codigo = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        nueva_campana["codigo_invitacion"] = codigo
        try:
            resultado = campanas.insert_one(nueva_campana.copy())
        except DuplicateKeyError:
            continue
        return jsonify({
            "status": "success",
            "id": str(resultado.inserted_id),
            "codigo": codigo,
        }), 201

    return jsonify({"error": "No se pudo generar un código de invitación. Intenta de nuevo."}), 503



@app.route('/api/campanas', methods=['GET'])
@token_required
def obtener_campanas(usuario_actual):
    # Buscamos campañas donde soy DM o donde estoy en la lista de jugadores
    query = {
        "$or": [
            {"dm_id": usuario_actual['_id']},
            {"jugadores": usuario_actual['_id']}
        ]
    }
    campanas = mongo.db.campanas.find(query)
    
    resultado = []
    for c in campanas:
        resultado.append({
            "id": str(c['_id']),
            "nombre": c['nombre'],
            "descripcion": c.get('descripcion', ''),
            "rol": "DM" if c['dm_id'] == usuario_actual['_id'] else "Jugador",
            "codigo_invitacion": c['codigo_invitacion'] if c['dm_id'] == usuario_actual['_id'] else "Oculto"
        })
    return jsonify({"status": "success", "mis_campanas": resultado})



@app.route('/api/ia/npc-rapido', methods=['POST'])
@token_required
@rate_limit("ia-npc", limit=10, window_seconds=3600, authenticated=True)
def generar_npc(usuario_actual):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400
    idea = datos.get('idea', 'Un NPC de taberna')
    if not isinstance(idea, str) or not 1 <= len(idea.strip()) <= 2000:
        return jsonify({"error": "La idea debe tener entre 1 y 2000 caracteres"}), 400
    
    api_key = os.getenv('GROQ_API_KEY')
    if not api_key:
        return jsonify({"error": "La generación de texto no está configurada"}), 503

    try:
        client_groq = Groq(api_key=api_key)

        # 1. CAMBIAMOS EL MODELO A 'llama-3.1-8b-instant'
        # Es la versión actual y recomendada por Groq
        chat_completion = client_groq.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": """Eres un generador de NPCs para D&D 5e. 
                    Responde ÚNICAMENTE con un JSON plano. 
                    IMPORTANTE: Todos los valores deben ser TEXTO (strings), NO objetos ni listas.
                    Usa estas llaves exactas:
                    {
                        "nombre": "Nombre",
                        "raza": "Raza",
                        "clase": "Clase",
                        "personalidad": "Descripción en una sola frase",
                        "gancho": "Misión o secreto"
                    }"""
                },
                {
                    "role": "user",
                    "content": f"Genera un NPC basado en: {idea}"
                }
            ],
            model="llama-3.1-8b-instant", # <--- MODELO ACTUALIZADO
            response_format={"type": "json_object"} # <--- Mantiene el JSON perfecto
        )

        # 2. Obtenemos el texto (JSON) de la respuesta
        resultado_texto = chat_completion.choices[0].message.content
        
        # 3. ¡IMPORTANTE PARA EVITAR EL ERROR DE REACT!
        # Convertimos el string a un objeto real de Python para que Flask 
        # lo envíe como un objeto JSON limpio a React.
        resultado_json = json.loads(resultado_texto)
        
        return jsonify({
            "status": "success",
            "resultado": resultado_json # <--- Enviamos el objeto, NO el string
        }), 200

    except Exception:
        app.logger.exception("Falló la generación de NPC")
        return jsonify({"error": "No se pudo generar el NPC"}), 502
    

@app.route('/api/ia/diagnostico', methods=['GET'])
@token_required
def listar_modelos():
    try:
        api_key = os.getenv('GOOGLE_API_KEY')
        if not api_key:
            return jsonify({"error": "El diagnóstico de modelos no está configurado"}), 503
        from google import genai
        client = genai.Client(api_key=api_key)
        
        # Le pedimos la lista oficial
        modelos_disponibles = []
        for m in client.models.list():
            # Filtramos solo los que sirven para generar contenido
            if 'generateContent' in m.supported_actions:
                modelos_disponibles.append(m.name)
                
        return jsonify({
            "status": "success",
            "modelos": modelos_disponibles
        })
    except Exception:
        app.logger.exception("Falló el diagnóstico de modelos")
        return jsonify({"error": "No se pudo completar el diagnóstico"}), 502
    

@app.route('/api/assets', methods=['POST'])
@token_required
@rate_limit("guardar-asset", limit=30, window_seconds=3600, authenticated=True)
def guardar_asset(usuario_actual):
    datos = request.get_json(silent=True)
    
    # 1. Validamos datos mínimos
    if not isinstance(datos, dict) or not datos.get('tipo') or datos.get('contenido') is None:
        return jsonify({"error": "Faltan datos del asset (tipo o contenido)"}), 400
        
    tipo = datos['tipo']
    contenido = datos['contenido']

    if tipo not in {"NPC", "Retrato"}:
        return jsonify({"error": "Tipo de asset no permitido"}), 400

    try:
        contenido_size = len(json.dumps(contenido, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return jsonify({"error": "El contenido del asset no es válido"}), 400
    if contenido_size > 100_000:
        return jsonify({"error": "El contenido del asset supera el límite permitido"}), 413

    campana_id = datos.get("campana_id")
    campana_object_id = None
    if campana_id:
        campana, es_dm, es_jugador = get_campaign_access(campana_id, usuario_actual["_id"])
        if not campana:
            return jsonify({"error": "Campaña no encontrada"}), 404
        if not (es_dm or es_jugador):
            return jsonify({"error": "No tienes permiso para usar esta campaña"}), 403
        campana_object_id = campana["_id"]

    # 2. Lógica inteligente para el contenido
    # Si es un NPC y viene como string, intentamos convertirlo a objeto 
    # para que en MongoDB se guarde de forma estructurada.
    if tipo == 'NPC' and isinstance(contenido, str):
        try:
            contenido = json.loads(contenido)
        except json.JSONDecodeError:
            pass # Si falla, lo dejamos como string (no pasa nada)

    # 3. Creamos el documento para MongoDB
    nuevo_asset = {
        "usuario_id": usuario_actual['_id'], # Usamos el ObjectId del usuario logueado
        "tipo": tipo,
        "prompt_original": contenido,
        "estado_aprobacion": 'Aprobado',
        "campana_id": campana_object_id,
        "fecha_creacion": datetime.now() # Siempre es bueno tener la fecha en Mongo
    }
    
    try:
        # 4. Insertamos en la colección 'assets'
        resultado = mongo.db.assets.insert_one(nuevo_asset)
        
        return jsonify({
            "status": "success", 
            "mensaje": "Asset guardado exitosamente en tus crónicas",
            "asset_id": str(resultado.inserted_id) # Convertimos el ID de Mongo a string
        }), 201
        
    except Exception:
        app.logger.exception("No se pudo guardar el asset")
        return jsonify({"error": "No se pudo guardar el asset"}), 500


@app.route('/api/assets', methods=['GET'])
@token_required
def obtener_assets(usuario_actual):
    try:
        # 1. En MongoDB buscamos en la colección 'assets' usando el _id del usuario
        # Recordatorio: usuario_actual ahora es un diccionario
        cursor_assets = mongo.db.assets.find({"usuario_id": usuario_actual['_id']})

        lista_assets = []
        
        for a in cursor_assets:
            info_npc = None
            tipo = a.get('tipo')
            prompt_orig = a.get('prompt_original', '')
            file_path = a.get('file_path')

            # 2. Lógica para NPCs (JSON)
            if tipo == 'NPC' and prompt_orig:
                # En MongoDB podrías haber guardado el JSON como string o como objeto
                if isinstance(prompt_orig, str):
                    try:
                        info_npc = json.loads(prompt_orig)
                    except json.JSONDecodeError:
                        info_npc = {"nombre": "Error", "personalidad": "Formato inválido"}
                else:
                    # Si ya es un diccionario (objeto nativo de Mongo), lo usamos directo
                    info_npc = prompt_orig

            # 3. Construcción de la URL de imagen (si tiene path)
            url_imagen = None
            if file_path:
                url_imagen = f"/assets/{a['_id']}/file"

            # 4. Formatear el nombre para mostrar
            # Si hay info_npc usamos su nombre, si no, el prompt original o el nombre del archivo
            nombre_display = "Sin nombre"
            if info_npc and isinstance(info_npc, dict):
                nombre_display = info_npc.get('nombre', 'NPC Desconocido')
            elif prompt_orig:
                nombre_display = prompt_orig
            elif file_path:
                nombre_display = file_path.split('/')[-1]

            # 5. Agregar a la lista convirtiendo el ID a string
            lista_assets.append({
                "id": str(a['_id']),  # <--- IMPORTANTE: Convertir ObjectId a string para JSON
                "tipo": tipo,
                "nombre": nombre_display,
                "url": url_imagen,
                "detalles": info_npc  
            })
            
        return jsonify({
            "status": "success", 
            "assets": lista_assets
        }), 200
        
    except Exception:
        app.logger.exception("No se pudo leer la galería")
        return jsonify({"error": "No se pudo leer la galería"}), 500


@app.route('/api/assets/<asset_id>', methods=['DELETE']) # Quitamos el <int:> porque en Mongo es un string
@token_required
def eliminar_asset(usuario_actual, asset_id):
    try:
        asset_object_id = parse_object_id(asset_id)
        if asset_object_id is None:
            return jsonify({"error": "Asset no encontrado"}), 404
        asset = mongo.db.assets.find_one({"_id": asset_object_id})
        
        # 2. Verificamos si existe
        if not asset:
            return jsonify({"error": "El registro no existe en las crónicas"}), 404
            
        # 3. SEGURIDAD: Verificamos que el asset pertenezca al usuario logueado
        # Comparamos el ID del dueño con el ID del usuario actual (ambos son ObjectIds)
        if asset['usuario_id'] != usuario_actual['_id']:
            return jsonify({"error": "No tienes permiso para borrar este tesoro"}), 403
            
        # 4. BORRADO FÍSICO (Opcional pero recomendado en tu LLD)
        # Si el asset tiene un archivo en el disco, lo eliminamos
        if asset.get('file_path'):
            ruta_fisica = resolve_asset_file(asset['file_path'])
            if ruta_fisica:
                ruta_fisica.unlink()

        # 5. BORRADO EN BASE DE DATOS
        mongo.db.assets.delete_one({"_id": asset_object_id})
        
        return jsonify({
            "status": "success",
            "mensaje": "Asset purgado permanentemente de la base de datos y el disco"
        }), 200
        
    except Exception:
        app.logger.exception("No se pudo eliminar el asset")
        return jsonify({"error": "No se pudo eliminar el asset"}), 500


@app.route('/api/assets/<asset_id>/file', methods=['GET'])
@token_required
def servir_asset(usuario_actual, asset_id):
    asset_object_id = parse_object_id(asset_id)
    if asset_object_id is None:
        return jsonify({"error": "Asset no encontrado"}), 404

    asset = mongo.db.assets.find_one({"_id": asset_object_id})
    if not asset or not asset.get("file_path"):
        return jsonify({"error": "Asset no encontrado"}), 404

    if asset.get("campana_id"):
        _, es_dm, es_jugador = get_campaign_access(str(asset["campana_id"]), usuario_actual["_id"])
        autorizado = es_dm or es_jugador
    else:
        autorizado = asset.get("usuario_id") == usuario_actual["_id"]
    if not autorizado:
        return jsonify({"error": "No tienes permiso para acceder a este asset"}), 403

    asset_file = resolve_asset_file(asset["file_path"])
    if not asset_file:
        return jsonify({"error": "Archivo no encontrado"}), 404
    return send_file(asset_file, mimetype="image/png", as_attachment=False, max_age=0)

@app.route('/api/ia/generar-imagen', methods=['POST'])
@token_required
@rate_limit("ia-imagen", limit=5, window_seconds=3600, authenticated=True)
def generar_imagen_preview(usuario_actual):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400
    npc_info = datos.get('prompt', 'A fantasy character')
    if not isinstance(npc_info, str) or not 1 <= len(npc_info.strip()) <= 2000:
        return jsonify({"error": "El prompt debe tener entre 1 y 2000 caracteres"}), 400
    
    # 1. PREPARAR LAS LLAVES
    GROQ_KEY = os.getenv('GROQ_API_KEY')
    HF_KEY = os.getenv('HUGGINGFACE_API_KEY')
    
    if not GROQ_KEY or not HF_KEY:
        return jsonify({"error": "La generación de imágenes no está configurada"}), 503

    art_prompt = npc_info # Valor por defecto

    try:
        # --- PASO 1: OPTIMIZAR PROMPT CON GROQ (LLAMA 3.1) ---
        try:
            client_groq = Groq(api_key=GROQ_KEY)
            chat_completion = client_groq.chat.completions.create(
                messages=[
                    {
                        "role": "system", 
                        "content": "You are a professional fantasy concept artist. Convert the user idea into a high-end image prompt for FLUX. Describe lighting, sharp focus, cinematic style and 8k resolution. Output ONLY the resulting English prompt."
                    },
                    {
                        "role": "user", 
                        "content": f"Create an art prompt for: {npc_info}"
                    }
                ],
                model="llama-3.1-8b-instant",
            )
            art_prompt = chat_completion.choices[0].message.content.strip()
        except Exception:
            app.logger.warning("Falló la optimización del prompt; se usará el original")

        # --- PASO 2: GENERAR IMAGEN CON FLUX (HUGGING FACE) ---
        IMAGE_MODEL_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
        headers = {"Authorization": f"Bearer {HF_KEY}"}
        
        image_base64 = None
        
        # Intentos por si el modelo está cargando
        for intento in range(3):
            img_res = requests.post(IMAGE_MODEL_URL, headers=headers, json={"inputs": art_prompt}, timeout=60)
            
            if img_res.status_code == 200:
                if len(img_res.content) > app.config["MAX_IMAGE_BYTES"]:
                    return jsonify({"error": "La imagen generada supera el límite permitido"}), 502
                image_base64 = base64.b64encode(img_res.content).decode('utf-8')
                break
            elif img_res.status_code in [503, 429]:
                time.sleep(10)
            else:
                app.logger.warning("El proveedor de imágenes respondió con estado %s", img_res.status_code)
                break

        if not image_base64:
            return jsonify({"error": "El proveedor de imágenes no está disponible"}), 502

        # --- PASO 3: GUARDAR LOG EN MONGODB ---
        mongo.db.logs_generacion.insert_one({
            "usuario_id": usuario_actual['_id'],
            "prompt_original": npc_info,
            "art_prompt": art_prompt,
            "fecha": datetime.now(timezone.utc),
            "engine_texto": "Groq-Llama-3.1",
            "engine_imagen": "HF-Flux-Schnell"
        })

        return jsonify({
            "status": "success",
            "image_b64": image_base64,
            "art_prompt": art_prompt
        }), 200

    except Exception:
        app.logger.exception("Falló la generación de imagen")
        return jsonify({"error": "No se pudo generar la imagen"}), 502

@app.route('/api/assets/guardar-imagen', methods=['POST'])
@token_required
@rate_limit("guardar-imagen", limit=10, window_seconds=3600, authenticated=True)
def confirmar_guardado_imagen(usuario_actual):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400

    image_b64 = datos.get("image_b64")
    if not isinstance(image_b64, str) or len(image_b64) > 7_000_000:
        return jsonify({"error": "La imagen no es válida o supera el límite permitido"}), 400
    if image_b64.startswith("data:"):
        prefix, separator, image_b64 = image_b64.partition(",")
        if not separator or prefix.lower() != "data:image/png;base64":
            return jsonify({"error": "Solo se permiten imágenes PNG"}), 400

    try:
        image_data = base64.b64decode(image_b64, validate=True)
    except (ValueError, binascii.Error):
        return jsonify({"error": "La imagen no está codificada correctamente"}), 400
    if len(image_data) > app.config["MAX_IMAGE_BYTES"] or not image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return jsonify({"error": "Solo se permiten imágenes PNG de hasta 5 MB"}), 400

    campana_id = datos.get("campana_id")
    campana_object_id = None
    if campana_id:
        campana, es_dm, es_jugador = get_campaign_access(campana_id, usuario_actual["_id"])
        if not campana:
            return jsonify({"error": "Campaña no encontrada"}), 404
        if not (es_dm or es_jugador):
            return jsonify({"error": "No tienes permiso para usar esta campaña"}), 403
        campana_object_id = campana["_id"]

    nombre_npc = datos.get("nombre_npc", "Personaje")
    if not isinstance(nombre_npc, str):
        nombre_npc = "Personaje"
    nombre_npc = re.sub(r"\s+", " ", nombre_npc).strip()[:100] or "Personaje"
    owner_dir = str(usuario_actual["_id"])
    campaign_dir = str(campana_object_id) if campana_object_id else "personal"
    relative_path = Path(campaign_dir) / owner_dir / f"{uuid.uuid4().hex}.png"
    target_path = (FOUNDRY_DATA_PATH / relative_path).resolve()
    try:
        target_path.relative_to(FOUNDRY_DATA_PATH)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("xb") as image_file:
            image_file.write(image_data)

        nuevo_asset = {
            "usuario_id": usuario_actual["_id"],
            "tipo": "Retrato",
            "file_path": relative_path.as_posix(),
            "nombre": nombre_npc,
            "prompt_original": str(datos.get("prompt_usado", ""))[:2000],
            "estado_aprobacion": "Aprobado",
            "fecha_creacion": datetime.now(timezone.utc),
            "campana_id": campana_object_id,
        }
        result = mongo.db.assets.insert_one(nuevo_asset)
        return jsonify({
            "status": "success",
            "asset_id": str(result.inserted_id),
            "url": f"/api/assets/{result.inserted_id}/file",
        }), 201
    except Exception:
        if target_path.exists():
            target_path.unlink()
        app.logger.exception("No se pudo guardar la imagen")
        return jsonify({"error": "No se pudo guardar la imagen"}), 500



@app.route('/api/campanas/unirse', methods=['POST'])
@token_required
@rate_limit("unirse-campana", limit=20, window_seconds=3600, authenticated=True)
def unirse_campana(usuario_actual):
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "El cuerpo debe ser un objeto JSON"}), 400
    codigo = datos.get('codigo')

    if not isinstance(codigo, str) or not re.fullmatch(r"[A-Za-z0-9]{6,8}", codigo):
        return jsonify({"error": "Falta el código de invitación"}), 400

    # 1. Buscamos la campaña en MongoDB por su código
    campana = mongo.db.campanas.find_one({"codigo_invitacion": codigo.upper()})

    if not campana:
        return jsonify({"error": "Código inválido o campaña inexistente"}), 404

    # 2. Verificamos si el usuario ya está dentro (para no duplicarlo)
    # Comparamos el ID del usuario actual con los IDs en la lista 'jugadores'
    if usuario_actual['_id'] in campana.get('jugadores', []):
        return jsonify({"error": "Ya eres parte de esta gesta"}), 400
        
    # 3. Verificamos que el DM no intente unirse a su propia campaña como jugador
    if campana['dm_id'] == usuario_actual['_id']:
        return jsonify({"error": "Eres el DM de esta campaña, no puedes unirte como jugador"}), 400

    # 4. ¡LA MAGIA DE MONGO! 
    # Usamos '$push' para agregar el ID del usuario al array de jugadores
    try:
        result = mongo.db.campanas.update_one(
            {
                "_id": campana['_id'],
                "dm_id": {"$ne": usuario_actual['_id']},
                "jugadores": {"$ne": usuario_actual['_id']},
            },
            {"$addToSet": {"jugadores": usuario_actual['_id']}}
        )
        if result.modified_count != 1:
            return jsonify({"error": "No se pudo agregar a la campaña"}), 409
        return jsonify({
            "status": "success",
            "mensaje": f"Te has unido a {campana['nombre']} con éxito."
        }), 200
    except Exception:
        app.logger.exception("No se pudo agregar al usuario a la campaña")
        return jsonify({"error": "No se pudo unir a la campaña"}), 500
    
    
    
@app.route('/api/campanas/<campana_id>', methods=['GET'])
@token_required
def obtener_detalle_campana(usuario_actual, campana_id):
    try:
        campana_object_id = parse_object_id(campana_id)
        if campana_object_id is None:
            return jsonify({"error": "Campaña no encontrada"}), 404
        campana = mongo.db.campanas.find_one({"_id": campana_object_id})
        if not campana:
            return jsonify({"error": "Campaña no encontrada"}), 404

        # 2. Seguridad: Verificar que el usuario pertenece a la campaña
        es_dm = campana['dm_id'] == usuario_actual['_id']
        es_jugador = usuario_actual['_id'] in campana.get('jugadores', [])

        if not es_dm and not es_jugador:
            return jsonify({"error": "No tienes permiso para ver esta campaña"}), 403

        # 3. Obtener nombres de los jugadores (NoSQL Lookup)
        ids_jugadores = campana.get('jugadores', [])
        lista_jugadores = []
        if ids_jugadores:
            usuarios_db = mongo.db.usuarios.find({"_id": {"$in": ids_jugadores}}, {"username": 1})
            lista_jugadores = [{"id": str(u['_id']), "username": u['username']} for u in usuarios_db]

        # 4. Obtener nombre del DM
        dm_db = mongo.db.usuarios.find_one({"_id": campana['dm_id']}, {"username": 1})

        # 5. Obtener Assets vinculados a esta campaña
        # (Filtramos por campana_id en la colección de assets)
        assets_cursor = mongo.db.assets.find({"campana_id": campana_object_id})
        lista_assets = []
        for a in assets_cursor:
            # Reutilizamos la lógica de limpieza de nombres que hicimos antes
            nombre_limpio = a.get('nombre', 'Asset sin nombre')
            lista_assets.append({
                "id": str(a['_id']),
                "tipo": a.get('tipo'),
                "nombre": nombre_limpio,
                "url": f"/assets/{a['_id']}/file" if a.get('file_path') else None
            })

        return jsonify({
            "status": "success",
            "campana": {
                "id": str(campana['_id']),
                "nombre": campana['nombre'],
                "descripcion": campana.get('descripcion', campana.get('description', '')),
                "codigo_invitacion": campana['codigo_invitacion'] if es_dm else "Oculto",
                "dm_username": dm_db['username'] if dm_db else "Desconocido",
                "jugadores": lista_jugadores,
                "assets": lista_assets,
                "soy_el_dm": es_dm
            }
        }), 200
    except Exception:
        app.logger.exception("No se pudo obtener el detalle de la campaña")
        return jsonify({"error": "No se pudo obtener la campaña"}), 500

if __name__ == '__main__':
    app.run(
        debug=False,
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
    )
