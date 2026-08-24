"""Small authenticated HTTP boundary for Nova and operator integrations."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .config import ServiceConfig
from .events import InvalidNotification, NovaEventConsumer
from .fakes import (
    FakeCapacityProvider,
    FakeCredentialIssuer,
    FakeFlytBackend,
    FakeQuotaProvider,
    InMemoryApprovalRegistry,
)
from .injection import render_cloud_config
from .flyt import FlytUnixBackend, GpuCellFlytBackend
from .kubernetes import GpuCellProfile, KubernetesGpuCellProvider, UrllibKubernetesTransport
from .models import InstanceRequest, SessionState
from .network import FakeServicePortProvider, NeutronServicePortProvider
from .openstack import (
    OpenStackCatalog,
    OpenStackError,
    PlacementCapacityProvider,
    UrllibJsonTransport,
)
from .serialization import InvalidPayload, instance_request, session
from .service import AdmissionError, FlytLifecycleService
from .storage import SQLiteSessionStore
from .ports import ServicePortProvider


MAX_BODY = 1024 * 1024


@dataclass
class Application:
    config: ServiceConfig
    lifecycle: FlytLifecycleService
    events: NovaEventConsumer
    catalog: OpenStackCatalog | None = None
    service_ports: ServicePortProvider | None = None

    def _materialize_for_vendor_data(self, instance_uuid: str):
        """Create the credential/session before Nova assembles guest metadata.

        Dynamic vendordata is fetched while the server is still building, so
        waiting for instance.create.end creates an impossible dependency: the
        guest cannot boot without data that is produced only after it boots.
        The later notification remains an idempotent reconciliation signal.
        """
        record = self.lifecycle.sessions.get(instance_uuid)
        if record is not None and record.state in {
            SessionState.RESERVED, SessionState.STARTING
        }:
            record = self.lifecycle.finalize_scheduled_build(instance_uuid)
            record = self.lifecycle.instance_active(instance_uuid)
        return record

    def _injection_target(self, instance_uuid: str):
        package = self.config.client_package
        manager_endpoint = self.config.client_manager_endpoint
        port = self.service_ports.get(instance_uuid) if self.service_ports else None
        if port is not None:
            endpoint = self.config.service_network.endpoints.get(port.availability_zone)
            if endpoint:
                package = replace(package, resolve_address=endpoint)
                manager_endpoint = f"tcp://{endpoint}:12402"
        return package, manager_endpoint

    @classmethod
    def build(cls, config: ServiceConfig) -> "Application":
        sessions = SQLiteSessionStore(config.database)
        catalog = None
        if config.openstack:
            transport = UrllibJsonTransport(ca_file=config.openstack.ca_file)
            capacity = PlacementCapacityProvider(
                config.openstack.placement_endpoint,
                config.openstack.token,
                transport,
            )
            catalog = OpenStackCatalog(
                config.openstack.nova_endpoint,
                config.openstack.glance_endpoint,
                config.openstack.token,
                transport,
            )
            service_ports: ServicePortProvider = NeutronServicePortProvider(
                config.openstack.neutron_endpoint,
                config.openstack.token,
                transport,
                config.service_network.networks,
                config.service_network.subnets,
                config.service_network.security_group_ids,
            )
        else:
            capacity = FakeCapacityProvider(dict(config.inventory))
            service_ports = FakeServicePortProvider(config.service_network.networks)
        quotas = FakeQuotaProvider(dict(config.quotas))
        backend = (
            FlytUnixBackend(
                config.flyt_manager.socket_path, config.flyt_manager.profiles
            ) if config.flyt_manager else FakeFlytBackend()
        )
        if config.kubernetes_gpu_cells:
            cells = config.kubernetes_gpu_cells
            backend = GpuCellFlytBackend(
                backend,
                KubernetesGpuCellProvider(
                    namespace=cells.namespace,
                    image=cells.image,
                    cluster_manager_address=cells.cluster_manager_address,
                    profiles={
                        name: GpuCellProfile(
                            device_class=value["device_class"],
                            selectors=value["selectors"],
                            expected_product_name=value["expected_product_name"],
                        ) for name, value in cells.profiles.items()
                    },
                    transport=UrllibKubernetesTransport(
                        cells.endpoint, cells.token_file, cells.ca_file
                    ),
                    runtime_class_name=cells.runtime_class_name,
                    host_network=cells.host_network,
                ),
                cells.session_profiles,
            )
        credentials = FakeCredentialIssuer()
        for record in sessions.values():
            if record.state.value not in {"DELETED", "FAILED"}:
                if isinstance(capacity, FakeCapacityProvider):
                    capacity.reservations[record.reservation_id] = (
                        record.profile, record.instance_uuid, record.project_id, "restored"
                    )
                quotas.reservations[record.instance_uuid] = (
                    record.project_id, record.profile
                )
                if record.backend_session_id and isinstance(backend, FakeFlytBackend):
                    backend.sessions[record.backend_session_id] = record.profile
                if record.bootstrap_token:
                    credentials.active.add(record.bootstrap_token)
        lifecycle = FlytLifecycleService(
            approvals=InMemoryApprovalRegistry({item.image_id: item for item in config.approvals}),
            quotas=quotas,
            capacity=capacity,
            backend=backend,
            credentials=credentials,
            sessions=sessions,
        )
        return cls(
            config=config,
            lifecycle=lifecycle,
            events=NovaEventConsumer(
                lifecycle,
                request_from_notification=(
                    (lambda payload: _request_from_notification(
                        payload, catalog, service_ports
                    ))
                    if catalog else None
                ),
            ),
            catalog=catalog,
            service_ports=service_ports,
        )

    def handle(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any, str]:
        if method == "GET" and path == "/healthz":
            return HTTPStatus.OK, {"status": "ok"}, "application/json"
        if method == "GET" and path == "/metrics":
            counts: dict[str, int] = {}
            for record in self.lifecycle.sessions.values():
                counts[record.state.value] = counts.get(record.state.value, 0) + 1
            lines = [
                "# HELP flyt_adapter_sessions Current sessions by lifecycle state.",
                "# TYPE flyt_adapter_sessions gauge",
            ]
            for state, count in sorted(counts.items()):
                lines.append(f'flyt_adapter_sessions{{state="{state}"}} {count}')
            lines.append("")
            return HTTPStatus.OK, "\n".join(lines), "text/plain; version=0.0.4"
        if method == "POST" and path == "/v1/admissions":
            request = instance_request(body or {})
            if self.catalog:
                request = InstanceRequest(
                    instance_uuid=request.instance_uuid,
                    project_id=request.project_id,
                    user_id=request.user_id,
                    flavor=self.catalog.flavor(request.flavor.id),
                    image=self.catalog.image(request.image.id),
                )
            if self.service_ports:
                managed_port = self.service_ports.get(request.instance_uuid)
                if managed_port is None:
                    raise AdmissionError("managed FLYT service port is missing")
                request = replace(request, service_port=managed_port)
            record = self.lifecycle.admit_build(request)
            return HTTPStatus.CREATED, session(record), "application/json"
        if method == "POST" and path == "/v1/preflight":
            request = instance_request(body or {})
            if self.catalog:
                request = InstanceRequest(
                    instance_uuid=request.instance_uuid,
                    project_id=request.project_id,
                    user_id=request.user_id,
                    flavor=self.catalog.flavor(request.flavor.id),
                    image=self.catalog.image(request.image.id),
                )
            profile = self.lifecycle.preflight(request)
            return HTTPStatus.OK, {"eligible": True, "profile": profile}, "application/json"
        if method == "POST" and path == "/v1/prebuild":
            if not isinstance(body, dict):
                raise InvalidPayload("prebuild body must be an object")
            request = instance_request(body)
            # Nova already resolved the Flavor and Glance image before this
            # authenticated pre-scheduling hook.  Re-entering Nova here can
            # deadlock or time out its API workers under load.  Validate the
            # signed service-to-service payload against the local approval
            # policy instead; notification/reconciliation paths still refresh
            # authoritative catalog state independently.
            profile = self.lifecycle.preflight(request)
            availability_zone = _required_string(body, "availability_zone")
            if self.service_ports is None:
                raise RuntimeError("FLYT service-port provider is unavailable")
            port = self.service_ports.ensure(
                instance_uuid=request.instance_uuid,
                project_id=request.project_id,
                availability_zone=availability_zone,
            )
            request = replace(request, service_port=port)
            # Reserve the logical FLYT session synchronously with Nova's
            # pre-scheduling hook. Config-drive vendordata is fetched before
            # create.start notifications are guaranteed to arrive, so the
            # asynchronous consumer cannot be the first session creator.
            record = self.lifecycle.prepare_build(request)
            return HTTPStatus.CREATED, {
                "eligible": True,
                "profile": profile,
                "session": session(record),
                "port": {
                    "id": port.port_id,
                    "network_id": port.network_id,
                    "mac_address": port.mac_address,
                    "service_ip": port.service_ip,
                    "availability_zone": port.availability_zone,
                },
            }, "application/json"
        if method == "DELETE" and path.startswith("/v1/prebuild/"):
            instance_uuid = _last_path(path)
            self.lifecycle.abort_build(instance_uuid, "Nova prebuild rollback")
            if self.service_ports:
                self.service_ports.delete(instance_uuid)
            return HTTPStatus.NO_CONTENT, "", "text/plain"
        if method == "POST" and path == "/v1/events":
            if not isinstance(body, dict):
                raise InvalidPayload("event body must be an object")
            event_type = body.get("event_type")
            payload = body.get("payload")
            if not isinstance(event_type, str) or not isinstance(payload, dict):
                raise InvalidPayload("event_type and payload are required")
            value = self.events.handle(event_type, payload)
            if event_type in {"instance.create.error", "instance.delete.end"}:
                instance_uuid = payload.get("instance_uuid") or payload.get("uuid")
                nested = payload.get("nova_object.data")
                if isinstance(nested, dict):
                    instance_uuid = nested.get("uuid") or instance_uuid
                if isinstance(instance_uuid, str) and self.service_ports:
                    self.service_ports.delete(instance_uuid)
            return HTTPStatus.OK, session(value), "application/json"
        if method == "GET" and path.startswith("/v1/sessions/"):
            instance_uuid = _last_path(path)
            record = self.lifecycle.sessions.get(instance_uuid)
            if record is None:
                return HTTPStatus.NOT_FOUND, {"error": "session not found"}, "application/json"
            return HTTPStatus.OK, session(record), "application/json"
        if method == "POST" and path == "/v1/nova/vendor-data":
            if not isinstance(body, dict):
                raise InvalidPayload("Nova vendor-data body must be an object")
            instance_uuid = _required_string(body, "instance-id")
            project_id = _required_string(body, "project-id")
            image_id = _required_string(body, "image-id")
            record = self._materialize_for_vendor_data(instance_uuid)
            if record is None:
                # A non-FLYT VM is not an error.  With the Nova target named
                # `cloud-init`, JSON null causes cloud-init to ignore it.
                return HTTPStatus.OK, None, "application/json"
            if record.project_id != project_id:
                raise InvalidPayload("vendor-data project does not match session")
            approval = self.lifecycle.approvals.find(image_id)
            if approval is None:
                raise InvalidPayload("vendor-data image is not approved")
            package, manager_endpoint = self._injection_target(instance_uuid)
            value = render_cloud_config(
                package=package,
                bootstrap_token=self.lifecycle.bootstrap_token(instance_uuid),
                instance_uuid=instance_uuid,
                generation=record.generation,
                manager_endpoint=manager_endpoint,
            )
            # DynamicJSON wraps this value using the configured target name.
            # Configure the target as `cloud-init@...`; cloud-init extracts the
            # value from vendordata2.json and executes this vendor shell script
            # independently from the tenant's cloud-config.
            return HTTPStatus.OK, value, "application/json"
        if method == "POST" and path.startswith("/v1/vendor-data/"):
            instance_uuid = _last_path(path)
            self._materialize_for_vendor_data(instance_uuid)
            token = self.lifecycle.bootstrap_token(instance_uuid)
            package, manager_endpoint = self._injection_target(instance_uuid)
            value = render_cloud_config(
                package=package,
                bootstrap_token=token,
                instance_uuid=instance_uuid,
                generation=self.lifecycle.sessions[instance_uuid].generation,
                manager_endpoint=manager_endpoint,
            )
            return HTTPStatus.OK, value, "text/cloud-config"
        return HTTPStatus.NOT_FOUND, {"error": "not found"}, "application/json"


def make_handler(application: Application) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "flyt-adapter/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _dispatch(self) -> None:
            path = urlparse(self.path).path
            if path not in {"/healthz", "/metrics"} and not self._authenticated():
                self._write(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, "application/json")
                return
            try:
                body = self._read_json() if self.command == "POST" and not path.startswith(
                    "/v1/vendor-data/"
                ) else None
                status, value, content_type = application.handle(self.command, path, body)
            except OpenStackError as exc:
                self._write(HTTPStatus.BAD_GATEWAY, {"error": str(exc)}, "application/json")
                return
            except (AdmissionError, InvalidPayload, InvalidNotification, RuntimeError) as exc:
                self._write(HTTPStatus.CONFLICT, {"error": str(exc)}, "application/json")
                return
            except ValueError as exc:
                self._write(HTTPStatus.BAD_REQUEST, {"error": str(exc)}, "application/json")
                return
            self._write(status, value, content_type)

        def _authenticated(self) -> bool:
            expected = f"Bearer {application.config.api_token}"
            bearer_ok = hmac.compare_digest(
                self.headers.get("Authorization", ""), expected
            )
            # keystoneauth's token plugin used by Nova DynamicJSON sends the
            # configured token in X-Auth-Token, not as a Bearer credential.
            service_token_ok = hmac.compare_digest(
                self.headers.get("X-Auth-Token", ""), application.config.api_token
            )
            return bearer_ok or service_token_ok

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise InvalidPayload("invalid Content-Length") from exc
            if length <= 0 or length > MAX_BODY:
                raise InvalidPayload("request body size is invalid")
            try:
                value = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise InvalidPayload("request body is not valid JSON") from exc
            if not isinstance(value, dict):
                raise InvalidPayload("request body must be an object")
            return value

        def _write(self, status: int, value: Any, content_type: str) -> None:
            if content_type == "application/json":
                payload = json.dumps(value, separators=(",", ":")).encode()
            else:
                payload = str(value).encode()
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def serve(config: ServiceConfig) -> None:
    application = Application.build(config)
    server = ThreadingHTTPServer(
        (config.listen_host, config.listen_port), make_handler(application)
    )
    server.serve_forever()


def _last_path(path: str) -> str:
    value = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if not value or "/" in value or ".." in value:
        raise InvalidPayload("invalid instance UUID path")
    return value


def _required_string(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidPayload(f"{name} must be a non-empty string")
    return value


def _request_from_notification(
    payload: Any, catalog: OpenStackCatalog, service_ports: ServicePortProvider
) -> InstanceRequest:
    if not isinstance(payload, dict):
        payload = dict(payload)
    flavor_value = payload.get("flavor")
    if not isinstance(flavor_value, dict):
        raise InvalidNotification("Nova notification has no flavor")
    flavor_data = flavor_value.get("nova_object.data", flavor_value)
    if not isinstance(flavor_data, dict):
        raise InvalidNotification("Nova notification flavor is malformed")
    values = {
        "flavor ID": flavor_data.get("flavorid") or flavor_data.get("id"),
        "image ID": payload.get("image_uuid"),
        "instance UUID": payload.get("uuid"),
        "project ID": payload.get("tenant_id") or payload.get("project_id"),
        "user ID": payload.get("user_id"),
    }
    for name, value in values.items():
        if not isinstance(value, str) or not value:
            raise InvalidNotification(f"Nova notification has no {name}")
    managed_port = service_ports.get(values["instance UUID"])
    if managed_port is None:
        raise InvalidNotification("managed FLYT service port is missing")
    return InstanceRequest(
        instance_uuid=values["instance UUID"],
        project_id=values["project ID"],
        user_id=values["user ID"],
        flavor=catalog.flavor(values["flavor ID"]),
        image=catalog.image(values["image ID"]),
        service_port=managed_port,
    )
