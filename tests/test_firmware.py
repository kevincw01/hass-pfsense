"""Exercise the exact patched methods, without requiring an HA installation."""

import ast
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import logging
from pathlib import Path
import socket
from socketserver import ThreadingMixIn
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "patched_client", ROOT / "custom_components/pfsense/pypfsense/__init__.py"
)
client_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_module)


def load_class(file, name, **namespace):
    tree = ast.parse(file.read_text())
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == name
    )
    code = compile(
        ast.Module(body=[node], type_ignores=[]),
        str(file),
        "exec",
        flags=__import__("__future__").annotations.compiler_flag,
    )
    scope = dict(
        copy=copy,
        datetime=datetime,
        timedelta=timedelta,
        timezone=timezone,
        threading=threading,
        time=time,
        _LOGGER=logging.getLogger("pfsense-test"),
    )
    scope.update(namespace)
    exec(code, scope)
    return scope[name]


Data = load_class(ROOT / "custom_components/pfsense/__init__.py", "PfSenseData")
Entity = load_class(
    ROOT / "custom_components/pfsense/update.py",
    "PfSenseFirmwareUpdatesAvailableUpdate",
    PfSenseUpdate=object,
    dict_get=client_module.dict_get,
)
GOOD = {"base": {"installed_version": "2.8.1", "version": "2.8.1"}, "packages": []}


class FirmwareTests(unittest.TestCase):
    def test_firmware_timeout_is_explicit_and_does_not_change_global_default(self):
        client = client_module.Client("http://localhost", "user", "password")
        old = socket.getdefaulttimeout()
        with patch.object(
            client, "_exec_php_no_timeout", return_value={"data": GOOD}
        ) as execute:
            self.assertEqual(client.get_firmware_update_info(), GOOD)
            self.assertEqual(execute.call_args.kwargs, {"timeout": 60})
            self.assertEqual(socket.getdefaulttimeout(), old)
        for url, verify in [
            ("http://localhost", True),
            ("https://localhost", True),
            ("https://localhost", False),
        ]:
            client = client_module.Client(
                url, "user", "password", {"verify_ssl": verify}
            )
            with client._get_proxy(timeout=60) as proxy:
                transport = proxy._ServerProxy__transport
                connection = transport.make_connection("localhost")
                self.assertEqual(connection.timeout, 60)
                if url.startswith("https:"):
                    self.assertEqual(connection._context.check_hostname, verify)
            self.assertEqual(socket.getdefaulttimeout(), old)

    def test_actual_slow_xmlrpc_response_succeeds_only_with_firmware_timeout(self):
        class Server(ThreadingMixIn, SimpleXMLRPCServer):
            daemon_threads = True

        class Handler(SimpleXMLRPCRequestHandler):
            rpc_paths = ("/xmlrpc.php",)

        server = Server(("127.0.0.1", 0), requestHandler=Handler, logRequests=False)

        def reply(script):
            time.sleep(0.15)
            return {"real": json.dumps({"data": GOOD})}

        server.register_function(reply, "pfsense.exec_php")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = client_module.Client(
            "http://127.0.0.1:" + str(server.server_address[1]), "user", "password"
        )
        try:
            with patch.object(client_module, "DEFAULT_TIMEOUT", 0.05), patch.object(
                client_module, "FIRMWARE_TIMEOUT", 0.5
            ):
                with self.assertRaises(TimeoutError):
                    client._exec_php("test")
                self.assertEqual(client.get_firmware_update_info(), GOOD)
                with patch.object(client_module, "FIRMWARE_TIMEOUT", 0.05):
                    with self.assertRaises(TimeoutError):
                        client.get_firmware_update_info()
        finally:
            server.shutdown()
            server.server_close()

    def test_no_overlapping_requests_and_health_reads_remain_nonblocking(self):
        entered, release = threading.Event(), threading.Event()

        def slow():
            entered.set()
            release.wait(3)
            return GOOD

        client = SimpleNamespace(get_firmware_update_info=Mock(side_effect=slow))
        data = Data(client, None, None)
        worker = threading.Thread(target=data._refresh_firmware_update_info)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            begin = time.monotonic()
            data._refresh_firmware_update_info()
            self.assertTrue(data._get_firmware_poll_health()["in_progress"])
            self.assertIsNone(data._get_firmware_update_info())
            self.assertLess(time.monotonic() - begin, 0.2)
            self.assertEqual(client.get_firmware_update_info.call_count, 1)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(data._get_firmware_poll_health()["in_progress"])
        self.assertIsNotNone(data._get_firmware_poll_health()["last_success_at"])

    def test_cache_and_timestamps_preserved_on_failure_then_one_recovery_log(self):
        client = SimpleNamespace(get_firmware_update_info=Mock(return_value=GOOD))
        data = Data(client, None, None)
        data._refresh_firmware_update_info()
        initial = data._get_firmware_poll_health()["last_success_at"]
        client.get_firmware_update_info.side_effect = TimeoutError("timed out")
        with self.assertLogs("pfsense-test", level="WARNING"):
            data._refresh_firmware_update_info()
        health = data._get_firmware_poll_health()
        self.assertEqual(health["last_success_at"], initial)
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertEqual(health["last_failure_type"], "TimeoutError")
        self.assertEqual(data._get_firmware_update_info(), GOOD)
        client.get_firmware_update_info.side_effect = None
        with self.assertLogs("pfsense-test", level="WARNING") as capture:
            data._refresh_firmware_update_info()
        self.assertEqual(
            sum("recovered after 1 failed checks" in line for line in capture.output), 1
        )
        self.assertEqual(data._get_firmware_poll_health()["consecutive_failures"], 0)
        with self.assertNoLogs("pfsense-test", level="INFO"):
            data._refresh_firmware_update_info()
        returned = data._get_firmware_update_info()
        returned["base"]["version"] = "changed"
        self.assertEqual(data._get_firmware_update_info(), GOOD)

    def test_invalid_results_do_not_establish_success_and_entity_exposes_freshness(
        self,
    ):
        for info in [None, {}, {"base": False}, {"base": None}, {"base": {}}]:
            data = Data(
                SimpleNamespace(get_firmware_update_info=lambda: info), None, None
            )
            with self.assertLogs("pfsense-test", level="WARNING"):
                data._refresh_firmware_update_info()
            health = data._get_firmware_poll_health()
            self.assertIsNone(health["last_success_at"])
            self.assertEqual(health["consecutive_failures"], 1)
        entity = Entity()
        entity.coordinator = SimpleNamespace(
            data={"firmware_update_info": GOOD, "firmware_poll_health": health}
        )
        attrs = entity.extra_state_attributes
        self.assertEqual(attrs["firmware_poll_consecutive_failures"], 1)
        self.assertEqual(attrs["pfsense_base_installed_version"], "2.8.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
