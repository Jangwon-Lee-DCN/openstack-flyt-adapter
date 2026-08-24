"""Small TCP relay used at a rack FLYT service-network boundary."""

from __future__ import annotations

import socket
import threading


def relay(listen_host: str, listen_port: int, backend_host: str, backend_port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((listen_host, listen_port))
        listener.listen()
        while True:
            client, _ = listener.accept()
            threading.Thread(
                target=_connection,
                args=(client, backend_host, backend_port),
                daemon=True,
            ).start()


def _connection(client: socket.socket, backend_host: str, backend_port: int) -> None:
    with client:
        try:
            backend = socket.create_connection((backend_host, backend_port), timeout=10)
        except OSError:
            return
        with backend:
            # Both directions must periodically observe the shared stop event.
            # Otherwise an EOF from the backend leaves the client-to-backend
            # pump blocked in recv() forever, hiding a Cluster Manager restart
            # from a long-lived GPU node connection.
            client.settimeout(1)
            backend.settimeout(1)
            stopped = threading.Event()
            reverse = threading.Thread(target=_pump, args=(backend, client, stopped))
            reverse.start()
            _pump(client, backend, stopped)
            for stream in (client, backend):
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            reverse.join(2)


def _pump(source: socket.socket, destination: socket.socket, stopped: threading.Event) -> None:
    while not stopped.is_set():
        try:
            data = source.recv(65536)
            if not data:
                break
            destination.sendall(data)
        except socket.timeout:
            continue
        except OSError:
            break
    stopped.set()
