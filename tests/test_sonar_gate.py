from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sonar_gate.py"
SPEC = importlib.util.spec_from_file_location("sonar_gate", MODULE_PATH)
assert SPEC and SPEC.loader
sonar_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sonar_gate)


class SonarGateTests(unittest.TestCase):
    def test_project_key_validation(self) -> None:
        sonar_gate.validate_project_key("riscvx:main")
        with self.assertRaises(sonar_gate.GateError):
            sonar_gate.validate_project_key("../../secret")

    def test_parse_report_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "report-task.txt"
            path.write_text(
                "projectKey=riscvx\n"
                "serverUrl=http://sonar.internal:9000\n"
                "ceTaskId=abc\n"
                "ceTaskUrl=http://sonar.internal:9000/api/ce/task?id=abc\n"
            )
            parsed = sonar_gate.parse_report_task(path)
            self.assertEqual(parsed["ceTaskId"], "abc")
            self.assertEqual(parsed["serverUrl"], "http://sonar.internal:9000")

    def test_evidence_path_is_project_and_sha_scoped(self) -> None:
        config = {"evidence_root": "~/state/sonar"}
        path = sonar_gate.evidence_path(config, "proj", "a" * 40)
        self.assertTrue(str(path).endswith("/proj/" + "a" * 40 + ".json"))

    def test_repo_state_requires_exact_clean_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            repo = Path(temp_name) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "a.txt").write_text("a\n")
            subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: initial"], cwd=repo, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            state = sonar_gate.repo_state(repo, head)
            self.assertEqual(state["head"], head)
            (repo / "dirty.txt").write_text("dirty\n")
            with self.assertRaises(sonar_gate.GateError):
                sonar_gate.repo_state(repo, head)

    def test_verify_requires_pass_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = {
                "server_url": "http://example.invalid",
                "scanner": "/bin/false",
                "token_dir": str(root / "tokens"),
                "evidence_root": str(root / "evidence"),
            }
            path = sonar_gate.evidence_path(config, "proj", "b" * 40)
            payload = {
                "schema_version": 1,
                "gate": "sonarqube-main",
                "status": "PASS",
                "repo": "/repo",
                "project_key": "proj",
                "commit": "b" * 40,
                "branch": "main",
                "analysis_id": "analysis-1",
            }
            sonar_gate.write_json_atomic(path, payload)
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["status"], "PASS")
            self.assertEqual(loaded["analysis_id"], "analysis-1")


if __name__ == "__main__":
    unittest.main()
