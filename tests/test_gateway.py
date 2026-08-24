from __future__ import annotations

import socket
import threading
import unittest

from flyt_adapter.gateway import _connection


class GatewayTests(unittest.TestCase):
    def test_relays_bidirectionally(self) -> None:
        backend_listener = socket.socket()
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        client_side, gateway_side = socket.socketpair()
        thread = threading.Thread(
            target=_connection,
            args=(gateway_side, "127.0.0.1", backend_listener.getsockname()[1]),
        )
        thread.start()
        backend, _ = backend_listener.accept()
        client_side.sendall(b"request")
        self.assertEqual(b"request", backend.recv(7))
        backend.sendall(b"reply")
        self.assertEqual(b"reply", client_side.recv(5))
        client_side.close()
        thread.join(2)
        backend.close()
        backend_listener.close()
        self.assertFalse(thread.is_alive())

    def test_backend_close_is_propagated_to_idle_client(self) -> None:
        backend_listener = socket.socket()
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        client_side, gateway_side = socket.socketpair()
        client_side.settimeout(3)
        thread = threading.Thread(
            target=_connection,
            args=(gateway_side, "127.0.0.1", backend_listener.getsockname()[1]),
        )
        thread.start()
        backend, _ = backend_listener.accept()
        backend.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(b"", client_side.recv(1))
        client_side.close()
        backend_listener.close()
