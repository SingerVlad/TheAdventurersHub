import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from bson import ObjectId
from werkzeug.security import generate_password_hash

os.environ["SECRET_KEY"] = "test-only-key-with-more-than-32-bytes"
os.environ["APP_ENV"] = "test"

import app as application


class SecurityBaselineTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()

    def make_token(self, **overrides):
        now = datetime.now(timezone.utc)
        payload = {
            "usuario_id": str(ObjectId()),
            "iat": now,
            "iss": application.app.config["JWT_ISSUER"],
            "csrf": "csrf-token-for-tests",
            "exp": now + timedelta(minutes=5),
        }
        payload.update(overrides)
        return jwt.encode(payload, application.app.config["SECRET_KEY"], algorithm="HS256")

    def test_private_diagnostics_require_authentication(self):
        response = self.client.get("/api/ia/diagnostico")
        self.assertEqual(response.status_code, 401)

    def test_invalid_token_is_rejected(self):
        response = self.client.get(
            "/api/perfil",
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_cookie_mutation_requires_csrf_header(self):
        self.client.set_cookie(
            application.app.config["JWT_COOKIE_NAME"],
            self.make_token(),
            path="/api",
        )
        response = self.client.post("/api/logout")
        self.assertEqual(response.status_code, 403)

    def test_login_uses_http_only_cookie_and_csrf_protection(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        user = {
            "_id": ObjectId(),
            "username": "adventurer",
            "password_hash": generate_password_hash("long-test-password-123"),
        }
        users = MagicMock()
        users.find_one.return_value = user
        rate_limits = MagicMock()
        rate_limits.find_one_and_update.return_value = {"count": 1}
        fake_db = SimpleNamespace(usuarios=users, rate_limits=rate_limits)

        with patch.object(application.mongo, "db", fake_db):
            response = self.client.post(
                "/api/login",
                json={"username": user["username"], "password": "long-test-password-123"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.json)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        csrf_token = response.json["csrf_token"]

        with patch.object(application.mongo, "db", fake_db):
            csrf_response = self.client.get("/api/csrf")
            self.assertEqual(csrf_response.json["csrf_token"], csrf_token)
            logout_response = self.client.post(
                "/api/logout",
                headers={"X-CSRF-Token": csrf_token},
            )
        self.assertEqual(logout_response.status_code, 200)
    def test_untrusted_origin_is_not_reflected_by_cors(self):
        response = self.client.options(
            "/api/login",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertNotEqual(
            response.headers.get("Access-Control-Allow-Origin"),
            "https://attacker.example",
        )

    def test_security_headers_are_set(self):
        response = self.client.get("/api/ia/diagnostico")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_asset_path_cannot_escape_storage_root(self):
        self.assertIsNone(application.resolve_asset_file("../../app.py"))

    def test_campaign_access_is_scoped_to_membership(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        campaign_id = ObjectId()
        dm_id = ObjectId()
        player_id = ObjectId()
        outsider_id = ObjectId()
        campaigns = MagicMock()
        campaigns.find_one.return_value = {
            "_id": campaign_id,
            "dm_id": dm_id,
            "jugadores": [player_id],
        }

        with patch.object(application.mongo, "db", SimpleNamespace(campanas=campaigns)):
            self.assertEqual(
                application.get_campaign_access(str(campaign_id), dm_id)[1:],
                (True, False),
            )
            self.assertEqual(
                application.get_campaign_access(str(campaign_id), player_id)[1:],
                (False, True),
            )
            self.assertEqual(
                application.get_campaign_access(str(campaign_id), outsider_id)[1:],
                (False, False),
            )
            self.assertEqual(
                application.get_campaign_access("invalid-id", outsider_id),
                (None, False, False),
            )


if __name__ == "__main__":
    unittest.main()



