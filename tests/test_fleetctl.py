from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import tempfile
import unittest
from unittest import mock
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

    def test_self_update_tree_fingerprint_is_deterministic_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            release = root / "release"
            (release / "web/dist").mkdir(parents=True)
            (release / "mcp-studio").write_bytes(b"binary")
            (release / "web/dist/index.html").write_text("index")
            first = fleetctl.self_update_tree_fingerprint(release)
            second = fleetctl.self_update_tree_fingerprint(release)
            self.assertEqual(first, second)

            link = release / "web/dist/link"
            try:
                link.symlink_to("index.html")
            except (OSError, NotImplementedError):
                return
            with self.assertRaises(RuntimeError):
                fleetctl.self_update_tree_fingerprint(release)

    def test_self_update_metadata_rejects_wrong_parent_or_identity(self) -> None:
        metadata = {
            "schema_version": 1,
            "transaction_id": "txn-studio-good",
            "component": "studio",
            "parent_pid": 123,
            "phase": "activation_pending",
        }
        fleetctl.validate_self_update_metadata(metadata, "txn-studio-good", 123)
        with self.assertRaises(RuntimeError):
            fleetctl.validate_self_update_metadata(metadata, "txn-studio-other", 123)
        with self.assertRaises(RuntimeError):
            fleetctl.validate_self_update_metadata(metadata, "txn-studio-good", 124)

    def test_studio_contract_requires_process_bound_protocol_v2(self) -> None:
        contract = fleetctl.studio_contract()
        self.assertEqual(contract["activation_protocol"], 2)
        self.assertIn(2, contract["schema_versions"])
        self.assertTrue(contract["process_bound_readiness"])
        self.assertTrue(contract["cross_process_lock"])

    def test_self_update_revision_rejects_stale_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "state.json"
            current = {
                "journal_revision": 1,
                "phase": "external_activating",
            }
            fleetctl.write_json_atomic(path, current)
            stale = dict(current)
            stale["journal_revision"] = 0
            with self.assertRaises(RuntimeError):
                fleetctl.update_self_update_metadata(
                    path, stale, "external_activated"
                )
            self.assertEqual(
                fleetctl.read_regular_json(path)["phase"],
                "external_activating",
            )

    def test_activation_lock_rejects_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            studio_root = Path(temp_name) / "studio"
            with fleetctl.studio_activation_lock(studio_root):
                with self.assertRaises(RuntimeError):
                    with fleetctl.studio_activation_lock(studio_root):
                        pass

    def test_readiness_rejects_unrelated_health_when_spawned_process_exited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            release = root / "release"
            (release / "web/dist").mkdir(parents=True)
            (release / "mcp-studio").write_bytes(b"binary")
            (release / "web/dist/index.html").write_text("web")
            fingerprint = fleetctl.self_update_tree_fingerprint(release)
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.pid = 5401
            with (
                mock.patch.object(fleetctl, "studio_health", return_value=True),
                mock.patch.object(fleetctl, "SELF_UPDATE_HEALTH_TIMEOUT_SECONDS", 0.01),
                mock.patch.object(fleetctl, "SELF_UPDATE_POLL_SECONDS", 0.001),
            ):
                self.assertFalse(
                    fleetctl.wait_for_spawned_studio_readiness(
                        root,
                        "0.5.0",
                        proc,
                        release,
                        fingerprint,
                        "release_tree",
                        "txn-studio-unrelated",
                        "a" * 64,
                    )
                )

    def test_readiness_requires_matching_spawned_process_nonce_and_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            studio_root = root / "studio"
            release = root / "release"
            (release / "web/dist").mkdir(parents=True)
            (release / "mcp-studio").write_bytes(b"binary")
            (release / "web/dist/index.html").write_text("web")
            (studio_root / "data/self-update").mkdir(parents=True)
            config = studio_root / "studio.toml"
            config.write_text('[server]\nlisten_addr = "127.0.0.1:18100"\n')
            fingerprint = fleetctl.self_update_tree_fingerprint(release)
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.pid = 5501
            tx = "txn-studio-proof"
            nonce = "b" * 64
            fleetctl.write_json_atomic(
                fleetctl.activation_proof_path(studio_root, tx),
                {
                    "schema_version": 1,
                    "transaction_id": tx,
                    "nonce": nonce,
                    "pid": proc.pid,
                    "config_path": str(config.resolve()),
                    "config_sha256": fleetctl.file_sha256(config),
                },
            )
            with (
                mock.patch.object(fleetctl, "studio_health", return_value=True),
                mock.patch.object(fleetctl, "SELF_UPDATE_READY_STABILITY_SECONDS", 0.0),
            ):
                self.assertTrue(
                    fleetctl.wait_for_spawned_studio_readiness(
                        studio_root,
                        "0.5.0",
                        proc,
                        release,
                        fingerprint,
                        "release_tree",
                        tx,
                        nonce,
                    )
                )
            wrong_pid = fleetctl.read_regular_json(
                fleetctl.activation_proof_path(studio_root, tx)
            )
            wrong_pid["pid"] = 9999
            fleetctl.write_json_atomic(
                fleetctl.activation_proof_path(studio_root, tx), wrong_pid
            )
            with (
                mock.patch.object(fleetctl, "studio_health", return_value=True),
                mock.patch.object(fleetctl, "SELF_UPDATE_HEALTH_TIMEOUT_SECONDS", 0.01),
                mock.patch.object(fleetctl, "SELF_UPDATE_POLL_SECONDS", 0.001),
            ):
                self.assertFalse(
                    fleetctl.wait_for_spawned_studio_readiness(
                        studio_root,
                        "0.5.0",
                        proc,
                        release,
                        fingerprint,
                        "release_tree",
                        tx,
                        nonce,
                    )
                )

    def _self_update_fixture(self, root: Path) -> tuple[Path, str]:
        fleet_root = root / "fleet"
        runtime_root = root / "runtime"
        studio_root = runtime_root / "studio"
        (fleet_root / "hosts").mkdir(parents=True)
        (studio_root / "data/self-update").mkdir(parents=True)
        (studio_root / "releases").mkdir(parents=True)
        (studio_root / "studio.toml").write_text(
            '[server]\nlisten_addr = "127.0.0.1:18100"\n'
        )
        (studio_root / "mcp-studio").write_bytes(b"old")

        host = "test"
        (fleet_root / "hosts" / f"{host}.toml").write_text(
            f'host_id = "{host}"\nruntime_root = "{runtime_root}"\n'
        )
        tx = "txn-studio-test"
        candidate = studio_root / "releases" / f".candidate-{tx}"
        (candidate / "web/dist").mkdir(parents=True)
        (candidate / "mcp-studio").write_bytes(b"new")
        (candidate / "web/dist/index.html").write_text("new web")
        fingerprint = fleetctl.self_update_tree_fingerprint(candidate)
        metadata = {
            "schema_version": 2,
            "launcher_protocol": 2,
            "journal_revision": 0,
            "transaction_id": tx,
            "component": "studio",
            "source_version": "0.4.0",
            "target_version": "0.5.0",
            "phase": "activation_pending",
            "candidate_dir": f".candidate-{tx}",
            "target_release": "v0.5.0",
            "candidate_fingerprint": fingerprint,
            "parent_pid": 999,
            "rollback_succeeded": None,
            "error": None,
            "updated_at_ms": 1,
        }
        fleetctl.write_json_atomic(
            studio_root / "data/self-update" / f"{tx}.json", metadata
        )
        return fleet_root, tx

    def test_studio_activate_switches_versioned_release_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fleet_root, tx = self._self_update_fixture(root)
            original_fleet_dir = fleetctl.FLEET_DIR
            original_fleet_config = fleetctl.FLEET_CONFIG
            fleetctl.FLEET_DIR = fleet_root
            fleetctl.FLEET_CONFIG = fleet_root / "fleet.toml"
            try:
                def fake_version(path: Path) -> str:
                    return "0.5.0" if "releases" in path.parts else "0.4.0"

                fake_process = mock.Mock()
                fake_process.poll.return_value = None
                fake_process.pid = 5001
                with (
                    mock.patch.object(fleetctl, "wait_for_pid_exit", return_value=True),
                    mock.patch.object(fleetctl, "binary_version", side_effect=fake_version),
                    mock.patch.object(fleetctl, "spawn_studio", return_value=fake_process),
                    mock.patch.object(fleetctl, "wait_for_spawned_studio_readiness", return_value=True),
                ):
                    self.assertEqual(fleetctl.studio_activate("test", tx, 999), 0)
                studio_root = root / "runtime/studio"
                self.assertEqual(
                    (studio_root / "current").readlink(),
                    Path("releases/v0.5.0"),
                )
                state = fleetctl.read_regular_json(
                    studio_root / "data/self-update" / f"{tx}.json"
                )
                self.assertEqual(state["phase"], "completed")
                self.assertTrue((studio_root / "releases/v0.5.0/web/dist/index.html").is_file())
            finally:
                fleetctl.FLEET_DIR = original_fleet_dir
                fleetctl.FLEET_CONFIG = original_fleet_config

    def test_studio_activate_health_failure_rolls_back_legacy_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fleet_root, tx = self._self_update_fixture(root)
            original_fleet_dir = fleetctl.FLEET_DIR
            original_fleet_config = fleetctl.FLEET_CONFIG
            fleetctl.FLEET_DIR = fleet_root
            fleetctl.FLEET_CONFIG = fleet_root / "fleet.toml"
            try:
                def fake_version(path: Path) -> str:
                    return "0.5.0" if "releases" in path.parts else "0.4.0"

                processes = [mock.Mock(), mock.Mock()]
                for index, proc in enumerate(processes, start=1):
                    proc.poll.return_value = None
                    proc.pid = 5100 + index
                with (
                    mock.patch.object(fleetctl, "wait_for_pid_exit", return_value=True),
                    mock.patch.object(fleetctl, "binary_version", side_effect=fake_version),
                    mock.patch.object(fleetctl, "spawn_studio", side_effect=processes),
                    mock.patch.object(
                        fleetctl,
                        "wait_for_spawned_studio_readiness",
                        side_effect=[False, True],
                    ),
                    mock.patch.object(fleetctl, "stop_spawned_process"),
                ):
                    self.assertEqual(fleetctl.studio_activate("test", tx, 999), 3)
                studio_root = root / "runtime/studio"
                self.assertFalse((studio_root / "current").exists())
                state = fleetctl.read_regular_json(
                    studio_root / "data/self-update" / f"{tx}.json"
                )
                self.assertEqual(state["phase"], "rolled_back")
                self.assertTrue(state["rollback_succeeded"])
            finally:
                fleetctl.FLEET_DIR = original_fleet_dir
                fleetctl.FLEET_CONFIG = original_fleet_config

    def test_studio_activate_exception_after_switch_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fleet_root, tx = self._self_update_fixture(root)
            original_fleet_dir = fleetctl.FLEET_DIR
            original_fleet_config = fleetctl.FLEET_CONFIG
            fleetctl.FLEET_DIR = fleet_root
            fleetctl.FLEET_CONFIG = fleet_root / "fleet.toml"
            try:
                def fake_version(path: Path) -> str:
                    return "0.5.0" if "releases" in path.parts else "0.4.0"

                rollback_proc = mock.Mock()
                rollback_proc.poll.return_value = None
                rollback_proc.pid = 5201
                with (
                    mock.patch.object(fleetctl, "wait_for_pid_exit", return_value=True),
                    mock.patch.object(fleetctl, "binary_version", side_effect=fake_version),
                    mock.patch.object(
                        fleetctl,
                        "spawn_studio",
                        side_effect=[OSError("forced spawn failure"), rollback_proc],
                    ),
                    mock.patch.object(fleetctl, "wait_for_spawned_studio_readiness", return_value=True),
                ):
                    self.assertEqual(fleetctl.studio_activate("test", tx, 999), 3)
                studio_root = root / "runtime/studio"
                self.assertFalse((studio_root / "current").exists())
                state = fleetctl.read_regular_json(
                    studio_root / "data/self-update" / f"{tx}.json"
                )
                self.assertEqual(state["phase"], "rolled_back")
                self.assertTrue(state["rollback_succeeded"])
                self.assertEqual(state["error"], "launcher_activation_failed")
            finally:
                fleetctl.FLEET_DIR = original_fleet_dir
                fleetctl.FLEET_CONFIG = original_fleet_config

    def test_studio_activate_resume_after_current_switch_uses_persisted_previous_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fleet_root, tx = self._self_update_fixture(root)
            studio_root = root / "runtime/studio"
            candidate = studio_root / "releases" / f".candidate-{tx}"
            target = studio_root / "releases/v0.5.0"
            os.replace(candidate, target)
            (studio_root / "current").symlink_to("releases/v0.5.0")

            metadata_path = studio_root / "data/self-update" / f"{tx}.json"
            metadata = fleetctl.read_regular_json(metadata_path)
            metadata["phase"] = "activation_pending"
            metadata["previous_layout"] = "legacy_flat"
            metadata["previous_release"] = None
            fleetctl.write_json_atomic(metadata_path, metadata)

            original_fleet_dir = fleetctl.FLEET_DIR
            original_fleet_config = fleetctl.FLEET_CONFIG
            fleetctl.FLEET_DIR = fleet_root
            fleetctl.FLEET_CONFIG = fleet_root / "fleet.toml"
            try:
                def fake_version(path: Path) -> str:
                    return "0.5.0" if "releases" in path.parts else "0.4.0"

                proc = mock.Mock()
                proc.poll.return_value = None
                proc.pid = 5301
                with (
                    mock.patch.object(fleetctl, "studio_health", return_value=False),
                    mock.patch.object(fleetctl, "wait_for_pid_exit", return_value=True),
                    mock.patch.object(fleetctl, "binary_version", side_effect=fake_version),
                    mock.patch.object(fleetctl, "spawn_studio", return_value=proc),
                    mock.patch.object(fleetctl, "wait_for_spawned_studio_readiness", return_value=True),
                ):
                    self.assertEqual(fleetctl.studio_activate("test", tx, 999), 0)
                state = fleetctl.read_regular_json(metadata_path)
                self.assertEqual(state["phase"], "completed")
                self.assertEqual(state["previous_layout"], "legacy_flat")
            finally:
                fleetctl.FLEET_DIR = original_fleet_dir
                fleetctl.FLEET_CONFIG = original_fleet_config

    def test_component_path_uses_host_source_root(self) -> None:
        host = {"source_root": "/work/mcp-server"}
        component = {"source_dir": "gateway"}
        self.assertEqual(fleetctl.component_path(component, host), Path('/work/mcp-server/gateway'))

    def test_studio_source_install_is_bootstrap_only_after_versioned_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fleet_root = root / "fleet"
            source_root = root / "source"
            runtime_root = root / "runtime"
            bin_root = root / "bin"
            (fleet_root / "hosts").mkdir(parents=True)
            (source_root / "studio/target/release").mkdir(parents=True)
            runtime_root.mkdir()
            bin_root.mkdir()
            source_binary = source_root / "studio/target/release/mcp-studio"
            source_binary.write_bytes(b"studio")
            current = runtime_root / "studio/current"
            current.parent.mkdir(parents=True)
            current.symlink_to("releases/v1.0.0")

            (fleet_root / "fleet.toml").write_text(
                """
[components.studio]
kind = "git"
install_scope = "runtime"
install_dir = "studio"
source_dir = "studio"
build_output = "target/release/mcp-studio"
binary = "mcp-studio"
"""
            )
            (fleet_root / "hosts/test.toml").write_text(
                f"""
source_root = "{source_root}"
runtime_root = "{runtime_root}"
bin_root = "{bin_root}"
"""
            )

            original_fleet_dir = fleetctl.FLEET_DIR
            original_fleet_config = fleetctl.FLEET_CONFIG
            fleetctl.FLEET_DIR = fleet_root
            fleetctl.FLEET_CONFIG = fleet_root / "fleet.toml"
            try:
                self.assertEqual(fleetctl.install_component("test", "studio"), 2)
                self.assertFalse((runtime_root / "studio/mcp-studio").exists())
            finally:
                fleetctl.FLEET_DIR = original_fleet_dir
                fleetctl.FLEET_CONFIG = original_fleet_config

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
