"""FLYT Cluster Manager Unix management protocol backend."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Mapping

from .models import BackendSession, GpuCellState, SessionRecord, SessionState
from .ports import FlytBackend, GpuCellProvider


@dataclass
class FlytUnixBackend:
    socket_path: str
    profiles: Mapping[str, tuple[int, int]]
    timeout: float = 5.0
    required_capability: str = "managed-session-v1"

    def create_session(self, record: SessionRecord) -> BackendSession:
        capabilities = set(self._command("GET_CAPABILITIES", "").split(","))
        if self.required_capability not in capabilities:
            raise RuntimeError(
                f"FLYT manager lacks required capability {self.required_capability}"
            )
        if not record.port_id or not record.service_ip or not record.bootstrap_token:
            raise RuntimeError("FLYT session requires a bound port, IP, and credential")
        try:
            compute_units, memory_mb = self.profiles[record.profile]
        except KeyError as exc:
            raise RuntimeError(f"unknown FLYT profile {record.profile}") from exc
        # OpenStack vocabulary ends at this adapter boundary. Field order is
        # workload, tenant, attachment, client address, preferred node,
        # profile, compute units, memory, generation, credential.
        response = self._command(
            "UPSERT_SESSION",
            ",".join((record.instance_uuid, record.project_id, record.port_id,
                      record.service_ip, "", record.profile, str(compute_units),
                      str(memory_mb), str(record.generation), record.bootstrap_token)),
        )
        state = SessionState.PENDING_CAPACITY if response == "PENDING_CAPACITY" else SessionState.READY
        return BackendSession(record.instance_uuid, state)

    def delete_session(self, backend_session_id: str) -> None:
        self._command("DELETE_SESSION", backend_session_id)

    def _command(self, command: str, payload: str) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(f"{command}\n{payload}\n".encode())
            reader = client.makefile("r", encoding="utf-8")
            status = reader.readline().strip()
            message = reader.readline().strip()
        if status != "200":
            raise RuntimeError(f"FLYT manager rejected {command}: {status} {message}")
        return message


@dataclass
class GpuCellFlytBackend:
    """Map logical sessions onto long-lived, whole-device GPU Cells."""

    manager: FlytBackend
    cells: GpuCellProvider
    profile_map: Mapping[str, str]

    def create_session(self, record: SessionRecord) -> BackendSession:
        cell_profile = self._cell_profile(record.profile)
        cell = self.cells.ensure(cell_profile=cell_profile)
        if cell.state == GpuCellState.FAILED:
            raise RuntimeError(f"GPU Cell {cell_profile} is failed")
        session = self.manager.create_session(record)
        if cell.state != GpuCellState.READY:
            return BackendSession(session.session_id, SessionState.PENDING_CAPACITY)
        return session

    def delete_session(self, backend_session_id: str) -> None:
        # A VM owns only its logical session. The physical GPU Cell remains
        # available for other MPS clients and is managed at pool scope.
        self.manager.delete_session(backend_session_id)

    def refresh_session(self, record: SessionRecord) -> BackendSession:
        cell_profile = self._cell_profile(record.profile)
        cell = self.cells.get(cell_profile)
        if cell is None:
            cell = self.cells.ensure(cell_profile=cell_profile)
        if cell.state == GpuCellState.FAILED:
            raise RuntimeError(f"GPU Cell {cell_profile} is failed")
        state = (SessionState.READY if cell.state == GpuCellState.READY
                 else SessionState.PENDING_CAPACITY)
        return BackendSession(record.backend_session_id or record.instance_uuid, state)

    def _cell_profile(self, session_profile: str) -> str:
        try:
            return self.profile_map[session_profile]
        except KeyError as exc:
            raise RuntimeError(
                f"logical FLYT profile {session_profile} has no GPU Cell mapping"
            ) from exc
