import copy
import hashlib
import hmac
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

os.environ["SECRET_KEY"] = "test-only-key-with-more-than-32-bytes"
os.environ["APP_ENV"] = "test"

import app as application
from backend.app.integrations.flow_client import (
    FlowClient,
    sign_flow_params,
    verify_flow_signature,
)
from backend.app.pagos.catalog import PLAN_PASE_AVENTURERO_ID
from backend.app.pagos.service import calcular_nueva_vigencia


class InMemoryCollection:
    """Colección MongoDB en memoria con semántica atómica para pruebas unitarias."""

    def __init__(self, initial_docs=None, unique_fields=None):
        self.docs = [copy.deepcopy(d) for d in (initial_docs or [])]
        self.unique_fields = set(unique_fields or [])
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))
        if isinstance(keys, str) and kwargs.get("unique"):
            self.unique_fields.add((keys, bool(kwargs.get("sparse", False))))

    def _matches(self, doc, query):
        for key, condition in query.items():
            has_key = key in doc
            val = doc.get(key)
            if isinstance(condition, dict):
                for op, expected in condition.items():
                    if op == "$ne" and val == expected:
                        return False
                    if op == "$in" and val not in expected:
                        return False
            else:
                if condition is None:
                    if has_key and val is not None:
                        return False
                elif val != condition:
                    return False
        return True

    def _check_unique(self, candidate, ignore_id=None):
        for entry in self.unique_fields:
            field, sparse = entry if isinstance(entry, tuple) else (entry, False)
            val = candidate.get(field)
            if sparse and val is None:
                continue
            for existing in self.docs:
                if ignore_id is not None and existing.get("_id") == ignore_id:
                    continue
                if existing.get(field) == val:
                    raise DuplicateKeyError(f"Duplicate key on {field}")

    def find_one(self, query):
        for doc in self.docs:
            if self._matches(doc, query):
                return copy.deepcopy(doc)
        return None

    def insert_one(self, doc):
        stored = copy.deepcopy(doc)
        stored.setdefault("_id", ObjectId())
        self._check_unique(stored)
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def update_one(self, query, update):
        for doc in self.docs:
            if self._matches(doc, query):
                updated = copy.deepcopy(doc)
                if "$set" in update:
                    updated.update(copy.deepcopy(update["$set"]))
                self._check_unique(updated, ignore_id=doc.get("_id"))
                doc.clear()
                doc.update(updated)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def find_one_and_update(self, query, update, upsert=False, return_document=None):
        for doc in self.docs:
            if self._matches(doc, query):
                updated = copy.deepcopy(doc)
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        updated[k] = updated.get(k, 0) + v
                if "$set" in update:
                    updated.update(copy.deepcopy(update["$set"]))
                self._check_unique(updated, ignore_id=doc.get("_id"))
                doc.clear()
                doc.update(updated)
                return copy.deepcopy(doc)
        if upsert:
            created = copy.deepcopy(query)
            created.setdefault("_id", ObjectId())
            if "$setOnInsert" in update:
                created.update(copy.deepcopy(update["$setOnInsert"]))
            if "$inc" in update:
                for k, v in update["$inc"].items():
                    created[k] = created.get(k, 0) + v
            if "$set" in update:
                created.update(copy.deepcopy(update["$set"]))
            self._check_unique(created)
            self.docs.append(created)
            return copy.deepcopy(created)
        return None


class FlowPagosTests(unittest.TestCase):
    def setUp(self):
        application.app.config.update(
            TESTING=True,
            FLOW_ENV="sandbox",
            FLOW_API_URL="https://sandbox.flow.cl/api",
            FLOW_API_KEY="flow-sandbox-api-key",
            FLOW_SECRET_KEY="flow-sandbox-secret-key",
            FLOW_CONFIRMATION_URL="https://hub.example/api/pagos/flow/confirmacion",
            FLOW_RETURN_URL="https://hub.example/api/pagos/flow/retorno",
            FRONTEND_PUBLIC_URL="http://localhost:5173",
        )
        self.client = application.app.test_client()

        self.user_id = ObjectId()
        self.other_user_id = ObjectId()
        self.now = datetime(2026, 11, 26, 15, 0, 0, tzinfo=timezone.utc)

        self.fake_db = SimpleNamespace(
            usuarios=InMemoryCollection(
                [
                    {
                        "_id": self.user_id,
                        "username": "aventurero",
                        "email": "aventurero@gremio.cl",
                        "fecha_registro": self.now,
                    },
                    {
                        "_id": self.other_user_id,
                        "username": "bardo",
                        "email": "bardo@gremio.cl",
                        "fecha_registro": self.now,
                    },
                ]
            ),
            ordenes=InMemoryCollection(),
            transacciones_pago=InMemoryCollection(),
            rate_limits=InMemoryCollection(),
        )

    def auth_headers(self, usuario_id=None):
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "usuario_id": str(usuario_id or self.user_id),
                "iat": now,
                "iss": application.app.config["JWT_ISSUER"],
                "csrf": "csrf-test",
                "exp": now + timedelta(minutes=10),
            },
            application.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def test_hmac_sha256_signature_sorts_keys_alphabetically_and_ignores_s(self):
        params = {
            "urlReturn": "https://hub.example/retorno",
            "amount": 1490,
            "apiKey": "api-key-123",
            "currency": "CLP",
            "commerceOrder": "AH-20261126150000-ABCDEF12",
            "email": "aventurero@gremio.cl",
            "subject": "Pase Aventurero Total (30 días - Acceso Completo)",
            "urlConfirmation": "https://hub.example/confirmacion",
            "s": "firma-anterior-a-ignorar",
            "opcionalNulo": None,
        }
        secret = "mi-secreto-sandbox"
        expected_chain = (
            "amount1490"
            "apiKeyapi-key-123"
            "commerceOrderAH-20261126150000-ABCDEF12"
            "currencyCLP"
            "emailaventurero@gremio.cl"
            "subjectPase Aventurero Total (30 días - Acceso Completo)"
            "urlConfirmationhttps://hub.example/confirmacion"
            "urlReturnhttps://hub.example/retorno"
        )
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            expected_chain.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        computed = sign_flow_params(params, secret)
        self.assertEqual(computed, expected_sig)
        self.assertTrue(verify_flow_signature(params, expected_sig, secret))
        self.assertFalse(verify_flow_signature(params, "firma-invalida", secret))

    def test_flow_client_signs_create_payment_and_get_status(self):
        fake_session = MagicMock()
        create_resp = MagicMock(status_code=200)
        create_resp.json.return_value = {
            "url": "https://sandbox.flow.cl/app/web/pay.php",
            "token": "TOK_SANDBOX_123",
            "flowOrder": 998877,
        }
        status_resp = MagicMock(status_code=200)
        status_resp.json.return_value = {
            "flowOrder": 998877,
            "commerceOrder": "AH-20261126150000-11223344",
            "status": 2,
            "amount": 1490,
            "currency": "CLP",
        }
        fake_session.post.return_value = create_resp
        fake_session.get.return_value = status_resp

        flow = FlowClient(
            api_key="test-key",
            secret_key="test-secret",
            api_url="https://sandbox.flow.cl/api",
            session=fake_session,
        )
        payment = flow.create_payment(
            commerce_order="AH-20261126150000-11223344",
            subject="Pase Aventurero Total (30 días - Acceso Completo)",
            amount=1490,
            email="aventurero@gremio.cl",
            url_confirmation="https://hub.example/confirmacion",
            url_return="https://hub.example/retorno",
        )
        self.assertEqual(
            payment["redirect_url"],
            "https://sandbox.flow.cl/app/web/pay.php?token=TOK_SANDBOX_123",
        )
        sent_post_data = fake_session.post.call_args.kwargs["data"]
        self.assertTrue(
            verify_flow_signature(sent_post_data, sent_post_data["s"], "test-secret")
        )

        status = flow.get_status("TOK_SANDBOX_123")
        self.assertEqual(status["status"], 2)
        sent_get_params = fake_session.get.call_args.kwargs["params"]
        self.assertTrue(
            verify_flow_signature(sent_get_params, sent_get_params["s"], "test-secret")
        )

    def test_calcular_nueva_vigencia_nueva_vencida_y_renovacion_anticipada(self):
        ahora = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

        # 1) Primera suscripción: 30 días exactos desde ahora
        primera = calcular_nueva_vigencia(None, duracion_dias=30, ahora=ahora)
        self.assertEqual(primera, ahora + timedelta(days=30))

        # 2) Suscripción ya vencida: reinicia desde ahora + 30 días
        vencida = ahora - timedelta(days=7)
        post_vencimiento = calcular_nueva_vigencia(
            vencida, duracion_dias=30, ahora=ahora
        )
        self.assertEqual(post_vencimiento, ahora + timedelta(days=30))

        # 3) Renovación anticipada (quedan 12 días): suma 30 días sobre la fecha vigente (42 días totales)
        vigente = ahora + timedelta(days=12)
        renovada = calcular_nueva_vigencia(vigente, duracion_dias=30, ahora=ahora)
        self.assertEqual(renovada, ahora + timedelta(days=42))

        # 4) Fecha naive proveniente de PyMongo (UTC implícito)
        vigente_naive = datetime(2026, 12, 6, 12, 0, 0)
        renovada_naive = calcular_nueva_vigencia(
            vigente_naive, duracion_dias=30, ahora=ahora
        )
        self.assertEqual(renovada_naive, ahora + timedelta(days=40))

    def test_catalogo_plan_unico_y_actualizacion_email_sparse(self):
        with patch.object(application.mongo, "db", self.fake_db):
            planes_res = self.client.get("/api/planes")
            self.assertEqual(planes_res.status_code, 200)
            planes = planes_res.json["planes"]
            self.assertEqual(len(planes), 1)
            self.assertEqual(planes[0]["id"], PLAN_PASE_AVENTURERO_ID)
            self.assertEqual(planes[0]["monto"], 1490)
            self.assertEqual(planes[0]["duracion_dias"], 30)

            # Conflicto si intenta usar el email de otro usuario
            conflict_res = self.client.put(
                "/api/perfil/email",
                json={"email": "bardo@gremio.cl"},
                headers=self.auth_headers(),
            )
            self.assertEqual(conflict_res.status_code, 409)

            # Actualización exitosa y verificación de índice sparse=True
            ok_res = self.client.put(
                "/api/perfil/email",
                json={"email": "NuevoCorreo@Gremio.cl"},
                headers=self.auth_headers(),
            )
            self.assertEqual(ok_res.status_code, 200)
            self.assertEqual(ok_res.json["email"], "nuevocorreo@gremio.cl")
            self.assertFalse(ok_res.json["es_suscriptor_activo"])
            self.assertIn(
                ("email", {"unique": True, "sparse": True}),
                self.fake_db.usuarios.indexes,
            )

    def test_flujo_completo_confirmacion_idempotencia_y_retorno_303(self):
        mock_flow = MagicMock()
        mock_flow.create_payment.return_value = {
            "url": "https://sandbox.flow.cl/app/web/pay.php",
            "token": "FLOW_TOKEN_UNICO_001",
            "flow_order": 554433,
            "redirect_url": "https://sandbox.flow.cl/app/web/pay.php?token=FLOW_TOKEN_UNICO_001",
        }

        with (
            patch.object(application.mongo, "db", self.fake_db),
            patch("backend.app.pagos.routes._get_flow_client", return_value=mock_flow),
        ):
            # 1) Iniciar pago (ignora cualquier monto manipulado por el cliente)
            inicio_res = self.client.post(
                "/api/pagos/flow/iniciar",
                json={"plan_id": PLAN_PASE_AVENTURERO_ID, "monto": 1},
                headers=self.auth_headers(),
            )
            self.assertEqual(inicio_res.status_code, 201)
            orden_comercio = inicio_res.json["orden_comercio"]
            self.assertEqual(inicio_res.json["monto"], 1490)

            mock_flow.get_status.return_value = {
                "flowOrder": 554433,
                "commerceOrder": orden_comercio,
                "status": 2,
                "amount": 1490,
                "currency": "CLP",
            }

            # 2) Primera confirmación del webhook de Flow -> activa 30 días
            conf_1 = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "FLOW_TOKEN_UNICO_001"},
            )
            self.assertEqual(conf_1.status_code, 200)
            self.assertEqual(conf_1.json["estado"], "pagada")
            self.assertFalse(conf_1.json["idempotente"])
            vigencia_inicial = conf_1.json["suscripcion_activa_hasta"]
            self.assertIsNotNone(vigencia_inicial)

            # 3) Webhook duplicado / reintento de Flow -> idempotente, NO suma otros 30 días
            conf_2 = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "FLOW_TOKEN_UNICO_001"},
            )
            self.assertEqual(conf_2.status_code, 200)
            self.assertTrue(conf_2.json["idempotente"])
            self.assertEqual(conf_2.json["suscripcion_activa_hasta"], vigencia_inicial)

            usuario_db = self.fake_db.usuarios.find_one({"_id": self.user_id})
            self.assertEqual(
                usuario_db["suscripcion_activa_hasta"].isoformat(),
                vigencia_inicial,
            )

            # 4) Retorno POST del navegador desde Flow -> 303 See Other e idempotente
            retorno_res = self.client.post(
                "/api/pagos/flow/retorno",
                data={"token": "FLOW_TOKEN_UNICO_001"},
            )
            self.assertEqual(retorno_res.status_code, 303)
            self.assertEqual(
                retorno_res.headers["Location"],
                f"http://localhost:5173/?pago_orden={orden_comercio}&estado=pagada",
            )
            usuario_post_retorno = self.fake_db.usuarios.find_one({"_id": self.user_id})
            self.assertEqual(
                usuario_post_retorno["suscripcion_activa_hasta"].isoformat(),
                vigencia_inicial,
            )

            # 5) Consulta de orden por propietario (200) y bloqueo a tercero (403)
            orden_res = self.client.get(
                f"/api/ordenes/{orden_comercio}",
                headers=self.auth_headers(),
            )
            self.assertEqual(orden_res.status_code, 200)
            self.assertEqual(orden_res.json["orden"]["estado"], "pagada")
            self.assertTrue(orden_res.json["orden"]["es_suscriptor_activo"])

            ajena_res = self.client.get(
                f"/api/ordenes/{orden_comercio}",
                headers=self.auth_headers(self.other_user_id),
            )
            self.assertEqual(ajena_res.status_code, 403)

    def test_renovacion_anticipada_acumula_30_dias_en_confirmacion(self):
        base_futura = datetime.now(timezone.utc) + timedelta(days=10)
        self.fake_db.usuarios.update_one(
            {"_id": self.user_id},
            {"$set": {"suscripcion_activa_hasta": base_futura}},
        )

        mock_flow = MagicMock()
        mock_flow.create_payment.return_value = {
            "url": "https://sandbox.flow.cl/app/web/pay.php",
            "token": "FLOW_TOKEN_RENOVACION",
            "flow_order": 777888,
            "redirect_url": "https://sandbox.flow.cl/app/web/pay.php?token=FLOW_TOKEN_RENOVACION",
        }

        with (
            patch.object(application.mongo, "db", self.fake_db),
            patch("backend.app.pagos.routes._get_flow_client", return_value=mock_flow),
        ):
            inicio = self.client.post(
                "/api/pagos/flow/iniciar",
                json={},
                headers=self.auth_headers(),
            )
            orden_comercio = inicio.json["orden_comercio"]
            mock_flow.get_status.return_value = {
                "flowOrder": 777888,
                "commerceOrder": orden_comercio,
                "status": 2,
                "amount": 1490,
                "currency": "CLP",
            }
            conf = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "FLOW_TOKEN_RENOVACION"},
            )
            self.assertEqual(conf.status_code, 200)

            usuario_db = self.fake_db.usuarios.find_one({"_id": self.user_id})
            self.assertEqual(
                usuario_db["suscripcion_activa_hasta"],
                base_futura + timedelta(days=30),
            )


if __name__ == "__main__":
    unittest.main()
