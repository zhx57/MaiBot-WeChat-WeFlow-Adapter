import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest import TestCase

from config_webui import ConfigWebUIServer, DEFAULTS, validate_updates


class ConfigWebUIServerTest(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temporary.name) / ".env"
        self.env_path.write_text("WEBUI_PASSWORD=test-password\n", encoding="utf-8")
        self.server = ConfigWebUIServer(
            self.env_path,
            "127.0.0.1",
            0,
            password="test-password",
            status_provider=lambda: {
                "state": "running",
                "label": "运行中",
                "detail": "测试状态",
            },
        )
        self.server.start()
        port = self.server._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.server.close()
        self.temporary.cleanup()

    def _request(self, path, *, authenticated=False):
        request = urllib.request.Request(self.base_url + path)
        if authenticated:
            self.server._sessions.add("test-session")
            request.add_header("Cookie", "wemai_session=test-session")
        return urllib.request.urlopen(request, timeout=3)

    def test_optimized_page_preserves_config_and_status_surfaces(self):
        with self._request("/", authenticated=True) as response:
            page = response.read().decode("utf-8")
        self.assertIn("控制面板", page)
        self.assertIn("运行日志", page)
        self.assertIn("status-card", page)
        self.assertIn("@media(max-width:780px)", page)
        self.assertIn('name="WEFLOW_DISPATCH_WORKERS"', page)
        self.assertIn('name="INBOUND_ROUTER_CONCURRENCY"', page)

        with self._request("/api/config", authenticated=True) as response:
            config = json.load(response)
        self.assertIn("WEFLOW_API_URL", config["values"])
        self.assertEqual("", config["values"]["WEBUI_PASSWORD"])

        with self._request("/api/status", authenticated=True) as response:
            status = json.load(response)
        self.assertEqual("running", status["state"])

    def test_inbound_concurrency_defaults_and_validation(self):
        self.assertEqual("8", DEFAULTS["WEFLOW_DISPATCH_WORKERS"])
        self.assertEqual("8", DEFAULTS["INBOUND_ROUTER_CONCURRENCY"])
        updates = validate_updates(
            {
                "WEFLOW_DISPATCH_WORKERS": "12",
                "INBOUND_ROUTER_CONCURRENCY": 16,
            },
            dict(DEFAULTS),
        )
        self.assertEqual("12", updates["WEFLOW_DISPATCH_WORKERS"])
        self.assertEqual("16", updates["INBOUND_ROUTER_CONCURRENCY"])
        for key, value in (
            ("WEFLOW_DISPATCH_WORKERS", "0"),
            ("WEFLOW_DISPATCH_WORKERS", "33"),
            ("INBOUND_ROUTER_CONCURRENCY", "not-a-number"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_updates({key: value}, dict(DEFAULTS))

    def test_favicon_is_public_but_logs_require_authentication(self):
        with self._request("/static/favicon.png") as response:
            self.assertEqual("image/png", response.headers.get_content_type())
            self.assertGreater(int(response.headers["Content-Length"]), 0)

        with self.assertRaises(urllib.error.HTTPError) as context:
            self._request("/api/logs?lines=2")
        self.assertEqual(401, context.exception.code)

    def test_log_endpoint_is_exact_and_bounded(self):
        with self._request("/api/logs?lines=2", authenticated=True) as response:
            payload = json.load(response)
        self.assertLessEqual(len(payload["log"]["lines"]), 2)
        self.assertLessEqual(payload["log"]["shown"], 2)

        with self._request("/api/logs?lines=100000", authenticated=True) as response:
            payload = json.load(response)
        self.assertLessEqual(len(payload["log"]["lines"]), 1000)

        with self.assertRaises(urllib.error.HTTPError) as context:
            self._request("/api/logs-extra", authenticated=True)
        self.assertEqual(404, context.exception.code)
