from __future__ import annotations

import base64
import hashlib
import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fleetctl.py"
SPEC = importlib.util.spec_from_file_location("fleetctl", MODULE_PATH)
assert SPEC and SPEC.loader
fleetctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleetctl)


class FleetCtlTests(unittest.TestCase):
    def test_normalize_platform(self) -> None:
        self.assertEqual(fleetctl.normalize_platform("Darwin", "x86_64"), ("darwin", "amd64"))
        self.assertEqual(fleetctl.normalize_platform("Darwin", "arm64"), ("darwin", "arm64"))
        self.assertEqual(fleetctl.normalize_platform("Linux", "aarch64"), ("linux", "arm64"))
        self.assertEqual(fleetctl.normalize_platform("Windows", "AMD64"), ("windows", "amd64"))
        with self.assertRaises(RuntimeError):
            fleetctl.normalize_platform("Plan9", "amd64")

    def test_parse_semver_from_binary_output(self) -> None:
        self.assertEqual(fleetctl.parse_semver("0.0.14 git sha: abc"), (0, 0, 14))
        self.assertEqual(fleetctl.parse_semver("v1.2.3"), (1, 2, 3))
        self.assertIsNone(fleetctl.parse_semver("unknown"))

    def test_expected_checksum(self) -> None:
        text = "abc123  first.zip\ndef456 *second.zip\n"
        self.assertEqual(fleetctl.expected_checksum(text, "first.zip"), "abc123")
        self.assertEqual(fleetctl.expected_checksum(text, "second.zip"), "def456")
        with self.assertRaises(RuntimeError):
            fleetctl.expected_checksum(text, "missing.zip")

    def test_safe_extract_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape", "bad")
            destination = root / "out"
            destination.mkdir()
            with self.assertRaises(RuntimeError):
                fleetctl.safe_extract_zip(archive, destination)

    def test_generated_runtime_configs_use_bin_paths(self) -> None:
        host = {
            "workspace_root": "/work",
            "source_root": "/work/mcp-server",
            "bin_root": "/work/mcp-server/bin",
            "runtime_root": "/work/mcp-server/runtime",
            "servers": {
                "filesystem": {},
                "git": {"extra_args": ["--allow-remote-read"]},
                "exec": {"extra_args": ["--default-timeout-ms", "120000"], "env": {"CI": "1"}},
            },
        }
        studio = fleetctl.studio_config_text(host)
        self.assertIn('/work/mcp-server/bin/rust-mcp-filesystem', studio)
        self.assertIn('/work/mcp-server/runtime/tunnel-client/current/tunnel-client-runtime-cloudflared', studio)
        self.assertNotIn('/target/release/', studio)
        tunnel = fleetctl.tunnel_config_text(host)
        self.assertIn('/work/mcp-server/bin/rust-mcp-gateway', tunnel)
        self.assertIn('/work/mcp-server/runtime/gateway/servers.d', tunnel)
        self.assertNotIn('/mcp-server/src/', tunnel)

    def test_render_plan_is_deterministic_pure_and_root_relative(self) -> None:
        host = {
            "host_id": "test-host",
            "workspace_root": "/work",
            "source_root": "/work/mcp-server",
            "bin_root": "/work/mcp-server/bin",
            "runtime_root": "/work/mcp-server/runtime",
            "gateway": {"server_dir": "gateway/servers.d"},
            "servers": {
                "filesystem": {},
                "git": {"extra_args": ["--allow-remote-read"]},
                "exec": {"extra_args": ["--default-timeout-ms", "120000"]},
            },
        }
        fleet = {
            "components": {
                "filesystem": {"binary": "rust-mcp-filesystem"},
                "git": {"binary": "rust-mcp-git"},
                "exec": {"binary": "rust-mcp-exec"},
            }
        }
        first = fleetctl.render_plan_data(host, fleet)
        second = fleetctl.render_plan_data(host, fleet)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["host_id"], "test-host")
        self.assertEqual(len(first["outputs"]), 5)
        paths = {item["relative_path"] for item in first["outputs"]}
        self.assertEqual(
            paths,
            {
                "gateway/servers.d/filesystem.yaml",
                "gateway/servers.d/git.yaml",
                "gateway/servers.d/exec.yaml",
                "studio/studio.toml",
                "tunnel-client/config.yaml",
            },
        )
        effects = {item["surface"]: item["effects"] for item in first["outputs"]}
        self.assertEqual(effects["gateway.git"], ["gateway_reload"])
        self.assertEqual(effects["studio.config"], ["studio_restart"])
        self.assertEqual(effects["tunnel.config"], ["tunnel_restart"])
        for item in first["outputs"]:
            payload = base64.b64decode(item["content_b64"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
            self.assertEqual(item["ownership"], "fleet_managed")
            self.assertNotIn("target/release", payload.decode())

    def test_component_path_uses_host_source_root(self) -> None:
        host = {"source_root": "/work/mcp-server"}
        component = {"source_dir": "gateway"}
        self.assertEqual(fleetctl.component_path(component, host), Path('/work/mcp-server/gateway'))

    def test_component_install_scope_separates_mcp_bin_and_services(self) -> None:
        host = {
            "bin_root": "/work/mcp-server/bin",
            "runtime_root": "/work/mcp-server/runtime",
        }
        mcp = {"kind": "git", "binary": "rust-mcp-git"}
        studio = {"kind": "git", "install_scope": "runtime", "install_dir": "studio", "binary": "mcp-studio"}
        self.assertEqual(fleetctl.component_install_dir("git", mcp, host), Path('/work/mcp-server/bin'))
        self.assertEqual(fleetctl.component_install_dir("studio", studio, host), Path('/work/mcp-server/runtime/studio'))
        with self.assertRaises(RuntimeError):
            fleetctl.component_install_dir("bad", {"kind": "git", "install_scope": "other"}, host)


if __name__ == "__main__":
    unittest.main()
