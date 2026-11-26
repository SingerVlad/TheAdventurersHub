from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from backend.app.extensions import mongo


class EmailConflictError(Exception):
    """El correo electrónico ya pertenece a otro usuario."""


def asegurar_indices_usuarios():
    mongo.db.usuarios.create_index("email", unique=True, sparse=True)


def obtener_usuario_por_id(usuario_id):
    return mongo.db.usuarios.find_one({"_id": usuario_id})


def actualizar_email_usuario(usuario_id, email):
    asegurar_indices_usuarios()
    try:
        return mongo.db.usuarios.find_one_and_update(
            {"_id": usuario_id},
            {"$set": {"email": email}},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as error:
        raise EmailConflictError(
            "El correo electrónico ya está registrado en otra cuenta"
        ) from error
