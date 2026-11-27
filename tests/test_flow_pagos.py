import copy
import hashlib
import hmac
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jwt
import requests
from bson import ObjectId
from flask import Flask
from pymongo.errors import DuplicateKeyError

os.environ["SECRET_KEY"] = "test-only-key-with-more-than-32-bytes"
os.environ["APP_ENV"] = "test"

import app as application
from backend.app.config import MAX_FLOW_HTTP_TIMEOUT_SECONDS, configure_flow_settings
from backend.app.integrations.flow_client import (
    FlowClient,
    FlowConfigurationError,
    FlowProviderError,
    FlowTimeoutError,
    sign_flow_params,
    verify_flow_signature,
)
from backend.app.pagos.catalog import PLAN_PASE_AVENTURERO_ID
from backend.app.pagos.service import calcular_nueva_vigencia


class InMemoryCollection:
    """Colección MongoDB standalone en memoria con semántica atómica por documento."""

    def __init__(self, initial_docs=None, unique_fields=None):
        self.docs = [copy.deepcopy(d) for d in (initial_docs or [])]
        self.unique_fields = set(unique_fields or [])
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))
        if isinstance(keys, str) and kwargs.get("unique"):
            self.unique_fields.add((keys, bool(kwargs.get("sparse", False))))

    def _matches(self, doc, query):
        if not query:
            return True
        for key, condition in query.items():
            if key == "$or":
                if not any(self._matches(doc, subq) for subq in condition):
                    return False
                continue

            has_key = key in doc
            val = doc.get(key)
            if isinstance(condition, dict):
                for op, expected in condition.items():
                    if op == "$ne":
                        if isinstance(val, list):
                            if expected in val:
                                return False
                        elif val == expected:
                            return False
                    elif op == "$in":
                        if isinstance(val, list):
                            if not any(item in expected for item in val):
                                return False
                        elif val not in expected:
                            return False
                    elif op == "$nin":
                        if isinstance(val, list):
                            if any(item in expected for item in val):
                                return False
                        elif val in expected:
                            return False
                    elif op == "$lte":
                        if val is None or val > expected:
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

    def _apply_update_ops(self, target, update, is_insert=False):
        if is_insert and "$setOnInsert" in update:
            target.update(copy.deepcopy(update["$setOnInsert"]))
        if "$inc" in update:
            for k, v in update["$inc"].items():
                target[k] = target.get(k, 0) + v
        if "$set" in update:
            target.update(copy.deepcopy(update["$set"]))
        if "$addToSet" in update:
            for k, v in update["$addToSet"].items():
                arr = list(target.get(k) or [])
                if v not in arr:
                    arr.append(copy.deepcopy(v))
                target[k] = arr

    def find(self, query=None, limit=0):
        results = [
            copy.deepcopy(doc)
            for doc in self.docs
            if self._matches(doc, query or {})
        ]
        if limit and limit > 0:
            return results[:limit]
        return results

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
                self._apply_update_ops(updated, update)
                self._check_unique(updated, ignore_id=doc.get("_id"))
                doc.clear()
                doc.update(updated)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def find_one_and_update(self, query, update, upsert=False, return_document=None):
        for doc in self.docs:
            if self._matches(doc, query):
                updated = copy.deepcopy(doc)
                self._apply_update_ops(updated, update)
                self._check_unique(updated, ignore_id=doc.get("_id"))
                doc.clear()
                doc.update(updated)
                return copy.deepcopy(doc)
        if upsert:
            created = copy.deepcopy(query)
            created.setdefault("_id", ObjectId())
            self._apply_update_ops(created, update, is_insert=True)
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
            FLOW_HTTP_TIMEOUT_SECONDS=8.0,
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
            flow_env="sandbox",
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

        status = flow.get_payment_status("TOK_SANDBOX_123")
        self.assertEqual(status["status"], 2)
        sent_get_params = fake_session.get.call_args.kwargs["params"]
        self.assertTrue(
            verify_flow_signature(sent_get_params, sent_get_params["s"], "test-secret")
        )

    def test_config_flow_api_url_allowlist_rechaza_http_y_hosts_no_oficiales(self):
        dummy_app = Flask("test_config_allowlist")

        casos_invalidos = [
            {"FLOW_ENV": "sandbox", "FLOW_API_URL": "http://sandbox.flow.cl/api"},
            {"FLOW_ENV": "sandbox", "FLOW_API_URL": "https://evil.example/api"},
            {"FLOW_ENV": "sandbox", "FLOW_API_URL": "https://www.flow.cl/api"},
            {"FLOW_ENV": "sandbox", "FLOW_API_URL": "https://sandbox.flow.cl:8443/api"},
            {"FLOW_ENV": "sandbox", "FLOW_API_URL": "https://user@sandbox.flow.cl/api"},
            {
                "FLOW_ENV": "production",
                "FLOW_API_URL": "https://sandbox.flow.cl/api",
                "FLOW_CONFIRMATION_URL": "https://hub.cl/api/pagos/flow/confirmacion",
                "FLOW_RETURN_URL": "https://hub.cl/api/pagos/flow/retorno",
                "FRONTEND_PUBLIC_URL": "https://hub.cl",
            },
        ]
        for env in casos_invalidos:
            with self.subTest(env=env):
                with self.assertRaises(RuntimeError):
                    configure_flow_settings(dummy_app, environ=env)

        configure_flow_settings(
            dummy_app,
            environ={"FLOW_ENV": "sandbox", "FLOW_API_URL": "https://sandbox.flow.cl/api"},
        )
        self.assertEqual(dummy_app.config["FLOW_API_URL"], "https://sandbox.flow.cl/api")

    def test_config_production_prohibe_localhost_http_y_variables_faltantes(self):
        dummy_app = Flask("test_config_prod")
        base_prod = {
            "FLOW_ENV": "production",
            "FLOW_API_URL": "https://www.flow.cl/api",
            "FLOW_CONFIRMATION_URL": "https://hub.aventureros.cl/api/pagos/flow/confirmacion",
            "FLOW_RETURN_URL": "https://hub.aventureros.cl/api/pagos/flow/retorno",
            "FRONTEND_PUBLIC_URL": "https://hub.aventureros.cl",
        }

        for clave in ("FLOW_CONFIRMATION_URL", "FLOW_RETURN_URL", "FRONTEND_PUBLIC_URL"):
            env_incompleto = dict(base_prod)
            env_incompleto.pop(clave)
            with self.subTest(missing=clave):
                with self.assertRaises(RuntimeError):
                    configure_flow_settings(dummy_app, environ=env_incompleto)

        urls_prohibidas = [
            "http://hub.aventureros.cl/ruta",
            "https://localhost:5000/ruta",
            "https://127.0.0.1:5000/ruta",
            "https://0.0.0.0:5000/ruta",
            "https://[::1]:5000/ruta",
        ]
        for clave in ("FLOW_CONFIRMATION_URL", "FLOW_RETURN_URL", "FRONTEND_PUBLIC_URL"):
            for url_invalida in urls_prohibidas:
                env_invalido = dict(base_prod)
                env_invalido[clave] = url_invalida
                with self.subTest(setting=clave, url=url_invalida):
                    with self.assertRaises(RuntimeError):
                        configure_flow_settings(dummy_app, environ=env_invalido)

        configure_flow_settings(dummy_app, environ=base_prod)
        self.assertEqual(dummy_app.config["FLOW_ENV"], "production")
        self.assertEqual(dummy_app.config["FLOW_API_URL"], "https://www.flow.cl/api")

    def test_config_production_rechaza_ips_privadas_literales_y_exige_is_global(self):
        dummy_app = Flask("test_config_private_ips")
        base_prod = {
            "FLOW_ENV": "production",
            "FLOW_API_URL": "https://www.flow.cl/api",
            "FLOW_CONFIRMATION_URL": "https://hub.aventureros.cl/api/pagos/flow/confirmacion",
            "FLOW_RETURN_URL": "https://hub.aventureros.cl/api/pagos/flow/retorno",
            "FRONTEND_PUBLIC_URL": "https://hub.aventureros.cl",
        }

        ips_no_globales = [
            "https://192.168.1.10/api/pagos/flow/confirmacion",
            "https://10.0.0.5/api/pagos/flow/confirmacion",
            "https://172.16.12.4/api/pagos/flow/confirmacion",
            "https://169.254.169.254/api/pagos/flow/confirmacion",
            "https://100.64.0.1/api/pagos/flow/confirmacion",
        ]
        for clave in ("FLOW_CONFIRMATION_URL", "FLOW_RETURN_URL", "FRONTEND_PUBLIC_URL"):
            for url_privada in ips_no_globales:
                env_invalido = dict(base_prod)
                env_invalido[clave] = url_privada
                with self.subTest(setting=clave, url=url_privada):
                    with self.assertRaises(RuntimeError):
                        configure_flow_settings(dummy_app, environ=env_invalido)

        # Una IP pública enrutable globalmente sí es aceptada si usa HTTPS
        env_ip_global = dict(base_prod)
        env_ip_global["FLOW_CONFIRMATION_URL"] = "https://190.196.10.20/api/pagos/flow/confirmacion"
        configure_flow_settings(dummy_app, environ=env_ip_global)
        self.assertEqual(
            dummy_app.config["FLOW_CONFIRMATION_URL"],
            "https://190.196.10.20/api/pagos/flow/confirmacion",
        )

    def test_flow_client_create_payment_rechaza_url_maliciosa_o_http(self):
        for url_maliciosa in (
            "https://phishing-flow.evil.example/pay.php",
            "http://sandbox.flow.cl/app/web/pay.php",
            "https://www.flow.cl/app/web/pay.php",
        ):
            fake_session = MagicMock()
            bad_resp = MagicMock(status_code=200)
            bad_resp.json.return_value = {
                "url": url_maliciosa,
                "token": "TOK_MALICIOSO",
                "flowOrder": 111,
            }
            fake_session.post.return_value = bad_resp

            flow = FlowClient(
                api_key="test-key",
                secret_key="test-secret",
                api_url="https://sandbox.flow.cl/api",
                flow_env="sandbox",
                session=fake_session,
            )
            with self.subTest(url=url_maliciosa):
                with self.assertRaises(FlowProviderError):
                    flow.create_payment(
                        commerce_order="AH-20261126150000-BAD00001",
                        subject="Pase Aventurero Total (30 días - Acceso Completo)",
                        amount=1490,
                        email="aventurero@gremio.cl",
                        url_confirmation="https://hub.example/confirmacion",
                        url_return="https://hub.example/retorno",
                    )

            with (
                patch.object(application.mongo, "db", self.fake_db),
                patch("backend.app.pagos.routes._get_flow_client", return_value=flow),
            ):
                res = self.client.post(
                    "/api/pagos/flow/iniciar",
                    json={},
                    headers=self.auth_headers(),
                )
                self.assertEqual(res.status_code, 502)

    def test_flow_client_timeout_acotado_bajo_15s_y_manejo_resiliente_en_confirmacion(self):
        # 1) Verifica cota estricta <= 10s (bajo los 15s de Flow)
        cliente_default = FlowClient(api_key="k", secret_key="s")
        self.assertLessEqual(
            cliente_default.total_timeout_seconds, MAX_FLOW_HTTP_TIMEOUT_SECONDS
        )
        self.assertLess(cliente_default.total_timeout_seconds, 15.0)

        with self.assertRaises(FlowConfigurationError):
            FlowClient(api_key="k", secret_key="s", timeout=15.0)
        with self.assertRaises(FlowConfigurationError):
            FlowClient(api_key="k", secret_key="s", timeout=(6.0, 6.0))

        dummy_app = Flask("test_timeout_cfg")
        with self.assertRaises(RuntimeError):
            configure_flow_settings(
                dummy_app, environ={"FLOW_HTTP_TIMEOUT_SECONDS": "15"}
            )

        # 2) Simula timeout transitorio de red en `get_payment_status` durante el webhook
        fake_session = MagicMock()
        create_ok = MagicMock(status_code=200)
        create_ok.json.return_value = {
            "url": "https://sandbox.flow.cl/app/web/pay.php",
            "token": "TOK_TIMEOUT_WEBHOOK",
            "flowOrder": 445566,
        }
        fake_session.post.return_value = create_ok
        fake_session.get.side_effect = requests.Timeout("Flow tardó demasiado")

        flow_real = FlowClient(
            api_key="flow-sandbox-api-key",
            secret_key="flow-sandbox-secret-key",
            api_url="https://sandbox.flow.cl/api",
            flow_env="sandbox",
            session=fake_session,
        )
        with self.assertRaises(FlowTimeoutError):
            flow_real.get_payment_status("TOK_TIMEOUT_WEBHOOK")

        with (
            patch.object(application.mongo, "db", self.fake_db),
            patch("backend.app.pagos.routes._get_flow_client", return_value=flow_real),
        ):
            inicio = self.client.post(
                "/api/pagos/flow/iniciar",
                json={},
                headers=self.auth_headers(),
            )
            self.assertEqual(inicio.status_code, 201)
            orden_comercio = inicio.json["orden_comercio"]

            # El webhook responde HTTP 200 controlado y persiste `requiere_reconciliacion=True`
            conf_timeout = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "TOK_TIMEOUT_WEBHOOK"},
            )
            self.assertEqual(conf_timeout.status_code, 200)
            self.assertTrue(conf_timeout.json["ok"])
            self.assertTrue(conf_timeout.json["reconciliacion_pendiente"])

            orden_pendiente = self.fake_db.ordenes.find_one(
                {"orden_comercio": orden_comercio}
            )
            self.assertTrue(orden_pendiente["requiere_reconciliacion"])
            self.assertEqual(
                orden_pendiente["ultimo_token_flow"], "TOK_TIMEOUT_WEBHOOK"
            )
            self.assertIsNotNone(orden_pendiente["confirmacion_recibida_en"])

            # Cuando Flow vuelve a responder normalmente, consultar suscripción/orden reconcilia automáticamente
            status_ok = MagicMock(status_code=200)
            status_ok.json.return_value = {
                "flowOrder": 445566,
                "commerceOrder": orden_comercio,
                "status": 2,
                "amount": 1490,
                "currency": "CLP",
            }
            fake_session.get.side_effect = None
            fake_session.get.return_value = status_ok

            sub_res = self.client.get(
                "/api/perfil/suscripcion",
                headers=self.auth_headers(),
            )
            self.assertEqual(sub_res.status_code, 200)
            self.assertTrue(sub_res.json["es_suscriptor_activo"])

            orden_reconciliada = self.fake_db.ordenes.find_one(
                {"orden_comercio": orden_comercio}
            )
            self.assertEqual(orden_reconciliada["estado"], "pagada")
            self.assertEqual(orden_reconciliada["activacion_estado"], "aplicada")
            self.assertFalse(orden_reconciliada["requiere_reconciliacion"])

    def test_confirmacion_concurrente_en_proceso_responde_200_y_reconcilia_automaticamente(self):
        mock_flow = MagicMock()
        mock_flow.create_payment.return_value = {
            "url": "https://sandbox.flow.cl/app/web/pay.php",
            "token": "TOK_CONCURRENTE_200",
            "flow_order": 909090,
            "redirect_url": "https://sandbox.flow.cl/app/web/pay.php?token=TOK_CONCURRENTE_200",
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
            doc_orden = self.fake_db.ordenes.find_one({"orden_comercio": orden_comercio})

            mock_flow.get_status.return_value = {
                "flowOrder": 909090,
                "commerceOrder": orden_comercio,
                "status": 2,
                "amount": 1490,
                "currency": "CLP",
            }

            # Simulamos que `/retorno` u otro worker tomó `en_proceso` hace 2 segundos (bloqueo vigente)
            self.fake_db.ordenes.update_one(
                {"_id": doc_orden["_id"]},
                {
                    "$set": {
                        "activacion_estado": "en_proceso",
                        "activacion_intento_en": datetime.now(timezone.utc),
                        "suscripcion_aplicada": False,
                    }
                },
            )

            # `urlConfirmation` concurrente NUNCA devuelve 409; responde HTTP 200 idempotente
            conf_concurrente = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "TOK_CONCURRENTE_200"},
            )
            self.assertEqual(conf_concurrente.status_code, 200)
            self.assertTrue(conf_concurrente.json["ok"])
            self.assertEqual(conf_concurrente.json["estado"], "en_proceso")
            self.assertTrue(conf_concurrente.json["idempotente"])

            # `/retorno` concurrente tampoco falla con 409; redirige con 303 See Other
            retorno_concurrente = self.client.post(
                "/api/pagos/flow/retorno",
                data={"token": "TOK_CONCURRENTE_200"},
            )
            self.assertEqual(retorno_concurrente.status_code, 303)

            orden_guardada = self.fake_db.ordenes.find_one({"_id": doc_orden["_id"]})
            self.assertTrue(orden_guardada["requiere_reconciliacion"])
            self.assertEqual(orden_guardada["ultimo_token_flow"], "TOK_CONCURRENTE_200")
            self.assertIsNotNone(orden_guardada["confirmacion_recibida_en"])

            # Si aquel primer worker murió sin terminar y el bloqueo vence, la consulta recupera la orden
            self.fake_db.ordenes.update_one(
                {"_id": doc_orden["_id"]},
                {
                    "$set": {
                        "activacion_intento_en": datetime.now(timezone.utc)
                        - timedelta(seconds=120),
                    }
                },
            )

            detalle_res = self.client.get(
                f"/api/ordenes/{orden_comercio}",
                headers=self.auth_headers(),
            )
            self.assertEqual(detalle_res.status_code, 200)
            self.assertEqual(detalle_res.json["orden"]["estado"], "pagada")
            self.assertEqual(detalle_res.json["orden"]["activacion_estado"], "aplicada")
            self.assertFalse(detalle_res.json["orden"]["requiere_reconciliacion"])
            self.assertTrue(detalle_res.json["orden"]["es_suscriptor_activo"])

    def test_calcular_nueva_vigencia_nueva_vencida_y_renovacion_anticipada(self):
        ahora = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

        primera = calcular_nueva_vigencia(None, duracion_dias=30, ahora=ahora)
        self.assertEqual(primera, ahora + timedelta(days=30))

        vencida = ahora - timedelta(days=7)
        post_vencimiento = calcular_nueva_vigencia(
            vencida, duracion_dias=30, ahora=ahora
        )
        self.assertEqual(post_vencimiento, ahora + timedelta(days=30))

        vigente = ahora + timedelta(days=12)
        renovada = calcular_nueva_vigencia(vigente, duracion_dias=30, ahora=ahora)
        self.assertEqual(renovada, ahora + timedelta(days=42))

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

            conflict_res = self.client.put(
                "/api/perfil/email",
                json={"email": "bardo@gremio.cl"},
                headers=self.auth_headers(),
            )
            self.assertEqual(conflict_res.status_code, 409)

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

            conf_1 = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "FLOW_TOKEN_UNICO_001"},
            )
            self.assertEqual(conf_1.status_code, 200)
            self.assertTrue(conf_1.json["ok"])
            self.assertEqual(conf_1.json["estado"], "pagada")
            self.assertFalse(conf_1.json["idempotente"])
            vigencia_inicial = conf_1.json["suscripcion_activa_hasta"]
            self.assertIsNotNone(vigencia_inicial)

            orden_aplicada = self.fake_db.ordenes.find_one({"orden_comercio": orden_comercio})
            self.assertEqual(orden_aplicada["activacion_estado"], "aplicada")
            self.assertTrue(orden_aplicada["suscripcion_aplicada"])
            self.assertFalse(orden_aplicada["requiere_reconciliacion"])

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

            retorno_res = self.client.post(
                "/api/pagos/flow/retorno",
                data={"token": "FLOW_TOKEN_UNICO_001"},
            )
            self.assertEqual(retorno_res.status_code, 303)
            self.assertEqual(
                retorno_res.headers["Location"],
                f"http://localhost:5173/?pago_orden={orden_comercio}&estado=pagada",
            )

            orden_res = self.client.get(
                f"/api/ordenes/{orden_comercio}",
                headers=self.auth_headers(),
            )
            self.assertEqual(orden_res.status_code, 200)
            self.assertEqual(orden_res.json["orden"]["estado"], "pagada")
            self.assertEqual(orden_res.json["orden"]["activacion_estado"], "aplicada")
            self.assertTrue(orden_res.json["orden"]["es_suscriptor_activo"])

            ajena_res = self.client.get(
                f"/api/ordenes/{orden_comercio}",
                headers=self.auth_headers(self.other_user_id),
            )
            self.assertEqual(ajena_res.status_code, 403)

    def test_recuperacion_activacion_interrumpida_en_proceso_sin_duplicar_30_dias(self):
        mock_flow = MagicMock()
        mock_flow.create_payment.side_effect = [
            {
                "url": "https://sandbox.flow.cl/app/web/pay.php",
                "token": "TOK_INTERRUPCION_1",
                "flow_order": 8001,
                "redirect_url": "https://sandbox.flow.cl/app/web/pay.php?token=TOK_INTERRUPCION_1",
            },
            {
                "url": "https://sandbox.flow.cl/app/web/pay.php",
                "token": "TOK_INTERRUPCION_2",
                "flow_order": 8002,
                "redirect_url": "https://sandbox.flow.cl/app/web/pay.php?token=TOK_INTERRUPCION_2",
            },
        ]

        with (
            patch.object(application.mongo, "db", self.fake_db),
            patch("backend.app.pagos.routes._get_flow_client", return_value=mock_flow),
        ):
            inicio_a = self.client.post(
                "/api/pagos/flow/iniciar",
                json={},
                headers=self.auth_headers(),
            )
            orden_a = inicio_a.json["orden_comercio"]
            mock_flow.get_status.return_value = {
                "flowOrder": 8001,
                "commerceOrder": orden_a,
                "status": 2,
                "amount": 1490,
                "currency": "CLP",
            }

            with patch(
                "backend.app.pagos.repository.aplicar_vigencia_idempotente_usuario",
                side_effect=RuntimeError("Corte abrupto simulando caída de red"),
            ):
                fallo_conf = self.client.post(
                    "/api/pagos/flow/confirmacion",
                    data={"token": "TOK_INTERRUPCION_1"},
                )
                self.assertEqual(fallo_conf.status_code, 500)

            orden_a_intermedia = self.fake_db.ordenes.find_one({"orden_comercio": orden_a})
            self.assertEqual(orden_a_intermedia["activacion_estado"], "en_proceso")
            self.assertFalse(orden_a_intermedia["suscripcion_aplicada"])
            self.assertTrue(orden_a_intermedia["requiere_reconciliacion"])
            self.assertIsNotNone(orden_a_intermedia["vigencia_otorgada_hasta"])
            vigencia_fijada_a = orden_a_intermedia["vigencia_otorgada_hasta"]

            reintento_a = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "TOK_INTERRUPCION_1"},
            )
            self.assertEqual(reintento_a.status_code, 200)
            self.assertFalse(reintento_a.json["idempotente"])

            orden_a_final = self.fake_db.ordenes.find_one({"orden_comercio": orden_a})
            self.assertEqual(orden_a_final["activacion_estado"], "aplicada")
            self.assertTrue(orden_a_final["suscripcion_aplicada"])
            self.assertFalse(orden_a_final["requiere_reconciliacion"])
            usuario_tras_a = self.fake_db.usuarios.find_one({"_id": self.user_id})
            self.assertEqual(usuario_tras_a["suscripcion_activa_hasta"], vigencia_fijada_a)

            inicio_b = self.client.post(
                "/api/pagos/flow/iniciar",
                json={},
                headers=self.auth_headers(),
            )
            orden_b = inicio_b.json["orden_comercio"]
            doc_orden_b = self.fake_db.ordenes.find_one({"orden_comercio": orden_b})
            vigencia_objetivo_b = vigencia_fijada_a + timedelta(days=30)

            self.fake_db.ordenes.update_one(
                {"_id": doc_orden_b["_id"]},
                {
                    "$set": {
                        "activacion_estado": "en_proceso",
                        "activacion_intento_en": datetime.now(timezone.utc)
                        - timedelta(seconds=120),
                        "suscripcion_aplicada": False,
                        "vigencia_base_previa": vigencia_fijada_a,
                        "vigencia_otorgada_hasta": vigencia_objetivo_b,
                    }
                },
            )
            self.fake_db.usuarios.update_one(
                {"_id": self.user_id},
                {
                    "$set": {"suscripcion_activa_hasta": vigencia_objetivo_b},
                    "$addToSet": {"ordenes_aplicadas": doc_orden_b["_id"]},
                },
            )

            mock_flow.get_status.return_value = {
                "flowOrder": 8002,
                "commerceOrder": orden_b,
                "status": 2,
                "amount": 1490,
                "currency": "CLP",
            }

            reintento_b = self.client.post(
                "/api/pagos/flow/confirmacion",
                data={"token": "TOK_INTERRUPCION_2"},
            )
            self.assertEqual(reintento_b.status_code, 200)

            orden_b_final = self.fake_db.ordenes.find_one({"orden_comercio": orden_b})
            self.assertEqual(orden_b_final["activacion_estado"], "aplicada")
            self.assertTrue(orden_b_final["suscripcion_aplicada"])

            usuario_tras_b = self.fake_db.usuarios.find_one({"_id": self.user_id})
            self.assertEqual(
                usuario_tras_b["suscripcion_activa_hasta"],
                vigencia_objetivo_b,
            )

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
