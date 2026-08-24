from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
import yaml


CHART = Path(__file__).resolve().parents[1] / "deploy/helm/flyt-adapter"
SHA = "sha256:" + "a" * 64


class HelmChartTests(unittest.TestCase):
    def render(self, *values: str) -> subprocess.CompletedProcess[str]:
        command = [
            "helm", "template", "flyt-adapter", str(CHART),
            "--namespace", "flyt-system",
            "--set", "image.repository=registry.invalid/flyt-adapter",
            "--set", f"image.digest={SHA}",
            "--set", "config.clientManagerEndpoint=unix:///run/flyt/manager.sock",
            "--set", "config.clientPackage.url=https://packages.invalid/flyt-client.tar.gz",
            "--set", f"config.clientPackage.digest={SHA}",
            "--set", "clientPackageServer.enabled=true",
            "--set", "clientPackageServer.image.repository=registry.invalid/flyt-client-package",
            "--set", f"clientPackageServer.image.digest={SHA}",
            "--set", "clientPackageServer.route.enabled=true",
            "--set", "clientPackageServer.route.hostname=cloud.example.invalid",
        ]
        for value in values:
            command.extend(("--set", value))
        return subprocess.run(command, text=True, capture_output=True)

    def test_gpu_cells_require_explicit_kubernetes_api_cidrs(self) -> None:
        result = self.render(
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
            "gpuCells.enabled=true",
            "gpuCells.image.repository=registry.invalid/flyt-gpu-cell",
            f"gpuCells.image.digest={SHA}",
            "gpuCells.clusterManagerAddress=10.80.0.10",
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("gpuCells.apiServerCidrs is required", result.stderr)

    def test_gpu_cell_network_policy_allows_only_declared_api_cidr(self) -> None:
        result = self.render(
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
            "gpuCells.enabled=true",
            "gpuCells.image.repository=registry.invalid/flyt-gpu-cell",
            f"gpuCells.image.digest={SHA}",
            "gpuCells.clusterManagerAddress=10.80.0.10",
            "gpuCells.apiServerCidrs[0]=10.96.0.1/32",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('cidr: "10.96.0.1/32"', result.stdout)
        self.assertIn("port: 443", result.stdout)
        self.assertIn("port: 6443", result.stdout)
        cilium_policy = next(
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "CiliumNetworkPolicy"
            and document["metadata"]["name"] == "flyt-adapter-kube-api"
        )
        self.assertEqual(
            ["kube-apiserver"],
            cilium_policy["spec"]["egress"][0]["toEntities"],
        )

    def test_cluster_manager_requires_gpu_node_cidrs(self) -> None:
        result = self.render(
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("clusterManager.nodeCidrs is required", result.stderr)

    def test_host_network_manager_can_reach_mongodb_from_declared_node(self) -> None:
        result = self.render(
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.mongodb.clientCidrs[0]=10.64.20.23/32",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        policies = [
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "NetworkPolicy"
            and document["metadata"]["name"] == "flyt-adapter-mongodb"
        ]
        self.assertEqual(1, len(policies))
        sources = policies[0]["spec"]["ingress"][0]["from"]
        self.assertIn({"ipBlock": {"cidr": "10.64.20.23/32"}}, sources)

    def test_cilium_policy_allows_host_identity_only_to_mongodb_port(self) -> None:
        result = self.render(
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.mongodb.allowCiliumHost=true",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        policy = next(
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "CiliumNetworkPolicy"
        )
        ingress = policy["spec"]["ingress"][0]
        self.assertEqual(["host"], ingress["fromEntities"])
        self.assertEqual("27017", ingress["toPorts"][0]["ports"][0]["port"])

    def test_cluster_manager_can_publish_ports_without_host_network(self) -> None:
        result = self.render(
            "podNetwork.hostNetwork=false",
            "clusterManager.enabled=true",
            "clusterManager.hostPorts=true",
            "clusterManager.hostIP=10.64.40.222",
            "clusterManager.service.externalIPs[0]=10.64.40.222",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        deployment = next(
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "Deployment"
            and document["metadata"]["name"] == "flyt-adapter"
        )
        pod = deployment["spec"]["template"]["spec"]
        self.assertNotIn("hostNetwork", pod)
        manager = next(item for item in pod["containers"] if item["name"] == "cluster-manager")
        self.assertEqual({12401, 12402}, {item["hostPort"] for item in manager["ports"]})
        self.assertEqual({"10.64.40.222"}, {item["hostIP"] for item in manager["ports"]})
        service = next(
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "Service"
            and document["metadata"]["name"] == "flyt-adapter-manager"
        )
        self.assertEqual(["10.64.40.222"], service["spec"]["externalIPs"])

    def test_reconciler_mounts_gpu_cell_kubernetes_credentials(self) -> None:
        result = self.render(
            "openstack.enabled=true",
            "openstack.novaEndpoint=https://nova.invalid",
            "openstack.glanceEndpoint=https://glance.invalid",
            "openstack.placementEndpoint=https://placement.invalid",
            "openstack.neutronEndpoint=https://neutron.invalid",
            "reconciler.enabled=true",
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
            "gpuCells.enabled=true",
            "gpuCells.image.repository=registry.invalid/flyt-gpu-cell",
            f"gpuCells.image.digest={SHA}",
            "gpuCells.clusterManagerAddress=10.80.0.10",
            "gpuCells.apiServerCidrs[0]=10.96.0.1/32",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        deployment = next(
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "Deployment"
            and document["metadata"]["name"] == "flyt-adapter"
        )
        reconciler = next(
            container for container in deployment["spec"]["template"]["spec"]["containers"]
            if container["name"] == "reconciler"
        )
        mounts = {mount["name"]: mount["mountPath"] for mount in reconciler["volumeMounts"]}
        self.assertEqual("/var/run/secrets/flyt-kubernetes", mounts["kubernetes-api"])
        self.assertEqual("/run/flyt", mounts["manager-socket"])

    def test_rack_gateway_policy_allows_host_nova_to_adapter_http(self) -> None:
        result = self.render(
            "rackGateway.enabled=true",
            "rackGateway.nodeSelector.kubernetes\\.io/hostname=gpu-node",
            "rackGateway.listenIP=10.81.0.10",
            "rackGateway.image.repository=registry.invalid/flyt-adapter",
            f"rackGateway.image.digest={SHA}",
            "clusterManager.enabled=true",
            "clusterManager.image.repository=registry.invalid/flyt-cluster-manager",
            f"clusterManager.image.digest={SHA}",
            "clusterManager.mongodb.image.repository=registry.invalid/mongodb",
            f"clusterManager.mongodb.image.digest={SHA}",
            "clusterManager.clientCidrs[0]=10.80.0.0/24",
            "clusterManager.nodeCidrs[0]=10.81.0.0/24",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        policy = next(
            document for document in yaml.safe_load_all(result.stdout)
            if document and document.get("kind") == "CiliumNetworkPolicy"
            and document["metadata"]["name"] == "flyt-adapter-rack-gateway"
        )
        self.assertEqual(["host", "remote-node"], policy["spec"]["ingress"][0]["fromEntities"])
        ports = {
            item["port"] for item in policy["spec"]["ingress"][0]["toPorts"][0]["ports"]
        }
        self.assertEqual({"8080", "12401", "12402"}, ports)


if __name__ == "__main__":
    unittest.main()
