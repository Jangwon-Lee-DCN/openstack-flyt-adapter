from __future__ import annotations

import argparse
import logging
import time

from .config import ServiceConfig
from .http import serve
from .http import Application
from .messaging import run_notification_listener
from .kubernetes import GpuCellProfile, KubernetesGpuCellProvider, UrllibKubernetesTransport
from .openstack import (
    NovaInstanceStatusInventory, PlacementInventoryManager, UrllibJsonTransport,
)
from .reconcile import OrphanPortReconciler, SessionReconciler
from .gateway import relay


LOG = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenStack FLYT adapter")
    parser.add_argument("--config")
    parser.add_argument(
        "--mode", choices=("http", "notifications", "reconcile", "placement-sync", "gateway"), default="http"
    )
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int)
    parser.add_argument("--backend-host")
    parser.add_argument("--backend-port", type=int)
    args = parser.parse_args()
    try:
        if args.mode == "gateway":
            if not args.listen_port or not args.backend_host or not args.backend_port:
                raise RuntimeError("gateway listen and backend ports are required")
            relay(args.listen_host, args.listen_port, args.backend_host, args.backend_port)
            return
        if not args.config:
            raise RuntimeError("--config is required")
        config = ServiceConfig.load(args.config)
        if args.mode == "http":
            serve(config)
        elif args.mode == "notifications":
            if not config.notification_transport_url:
                raise RuntimeError("notification_transport_url is required")
            application = Application.build(config)
            run_notification_listener(
                config.notification_transport_url,
                config.notification_topics,
                application.events,
            )
        elif args.mode == "reconcile":
            application = Application.build(config)
            if not config.openstack or application.service_ports is None:
                raise RuntimeError("OpenStack mode is required for reconciliation")
            transport = UrllibJsonTransport(ca_file=config.openstack.ca_file)
            inventory = NovaInstanceStatusInventory(
                config.openstack.nova_endpoint,
                config.openstack.token,
                transport,
            )
            session_reconciler = SessionReconciler(application.lifecycle, inventory)
            port_reconciler = OrphanPortReconciler(
                application.lifecycle,
                inventory,
                application.service_ports,
                config.orphan_grace_seconds,
                application.catalog,
            )
            while True:
                try:
                    session_reconciler.run_once()
                    port_reconciler.run_once()
                except Exception:
                    # OpenStack endpoints and DNS can disappear briefly while
                    # their control plane rolls. Reconciliation is periodic;
                    # keep the sidecar (and therefore the Adapter Service)
                    # healthy and retry instead of crash-looping the Pod.
                    LOG.exception("FLYT reconciliation pass failed; retrying")
                time.sleep(config.reconcile_interval_seconds)
        else:
            if not config.openstack or not config.placement_sync:
                raise RuntimeError("OpenStack and placement_sync configuration are required")
            manager = PlacementInventoryManager(
                config.openstack.placement_endpoint,
                config.openstack.token,
                UrllibJsonTransport(ca_file=config.openstack.ca_file),
            )
            for profile, total in config.inventory.items():
                safe_total = total
                if config.kubernetes_gpu_cells:
                    cells = config.kubernetes_gpu_cells
                    cell_profile = cells.session_profiles[profile]
                    readiness = KubernetesGpuCellProvider(
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
                    ).readiness(cell_profile)
                    if not readiness.ready:
                        safe_total = 0
                manager.ensure_profile(
                    profile=profile,
                    total=safe_total,
                    aggregate_uuid=config.placement_sync.aggregate_uuid,
                    compute_provider_uuids=config.placement_sync.compute_provider_uuids,
                )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
