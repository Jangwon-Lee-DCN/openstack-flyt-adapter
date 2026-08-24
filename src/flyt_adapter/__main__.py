from __future__ import annotations

import argparse
import time

from .config import ServiceConfig
from .http import serve
from .http import Application
from .messaging import run_notification_listener
from .openstack import (
    NovaInstanceStatusInventory, PlacementInventoryManager, UrllibJsonTransport,
)
from .reconcile import OrphanPortReconciler, SessionReconciler


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenStack FLYT adapter")
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--mode", choices=("http", "notifications", "reconcile", "placement-sync"), default="http"
    )
    args = parser.parse_args()
    try:
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
                session_reconciler.run_once()
                port_reconciler.run_once()
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
                manager.ensure_profile(
                    profile=profile,
                    total=total,
                    aggregate_uuid=config.placement_sync.aggregate_uuid,
                    compute_provider_uuids=config.placement_sync.compute_provider_uuids,
                )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
