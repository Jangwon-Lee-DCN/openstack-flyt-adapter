from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest

from flyt_adapter.flyt import FlytUnixBackend
from flyt_adapter.models import SessionRecord, SessionState


class FlytBackendTest(unittest.TestCase):
    def test_missing_managed_session_capability_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "manager.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)

            def respond():
                connection, _ = server.accept()
                with connection:
                    reader = connection.makefile("r")
                    self.assertEqual("GET_CAPABILITIES", reader.readline().strip())
                    reader.readline()
                    connection.sendall(b"200\nwhole-gpu-mps\n")
                    reader.close()

            thread = threading.Thread(target=respond)
            thread.start()
            backend = FlytUnixBackend(path, {"gpu-small": (16, 8192)})
            with self.assertRaisesRegex(RuntimeError, "lacks required capability"):
                backend.create_session(SessionRecord(
                    instance_uuid="vm-1", project_id="project-1", profile="gpu-small",
                    reservation_id="vm-1", port_id="port-1", service_ip="172.30.1.10",
                    bootstrap_token="bootstrap-secret",
                ))
            thread.join(timeout=2)
            server.close()

    def test_pending_capacity_contract(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "manager.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            request = []

            def respond():
                responses = (
                    b"200\nmanaged-session-v1,whole-gpu-mps,mig\n",
                    b"200\nPENDING_CAPACITY\n",
                )
                for response in responses:
                    connection, _ = server.accept()
                    with connection:
                        reader = connection.makefile("r")
                        request.extend(reader.readline().strip() for _ in range(2))
                        connection.sendall(response)
                        reader.close()

            thread = threading.Thread(target=respond)
            thread.start()
            backend = FlytUnixBackend(path, {"gpu-small": (16, 8192)})
            result = backend.create_session(SessionRecord(
                instance_uuid="vm-1", project_id="project-1", profile="gpu-small",
                reservation_id="vm-1", port_id="port-1", service_ip="172.30.1.10",
                bootstrap_token="bootstrap-secret",
            ))
            thread.join(timeout=2)
            server.close()
            self.assertEqual(SessionState.PENDING_CAPACITY, result.state)
            self.assertEqual("GET_CAPABILITIES", request[0])
            self.assertEqual("UPSERT_SESSION", request[2])
            self.assertIn("vm-1,project-1,port-1,172.30.1.10", request[3])
            self.assertTrue(request[3].endswith(",bootstrap-secret"))


if __name__ == "__main__":
    unittest.main()
