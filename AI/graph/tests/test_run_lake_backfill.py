"""Plan identity, safe checkpoint resume and sequential subprocess regressions."""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import shutil
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_lake_backfill import RunLock, digest_file, execute, launch, read_json, validate_plan


class BackfillRunnerTests(unittest.TestCase):
    def setUp(self):
        artifacts = Path(__file__).resolve().parents[1] / "artifacts"
        artifacts.mkdir(exist_ok=True)
        self.artifacts = artifacts.resolve()
        # mkdir's inherited permissions also work in Windows managed workspaces;
        # tempfile.mkdtemp's private 0700 ACL can reject the sandboxed child user.
        self.root = self.artifacts / ("runner-tests-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.code = self.root / "code"
        self.run = self.root / "run"
        self.code.mkdir()
        self.run.mkdir()
        self.script = self.code / "stage.py"
        self.script.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "root=Path(sys.argv[1]); name=sys.argv[2]\n"
            "with (root/'calls.jsonl').open('a') as f: f.write(json.dumps(name)+'\\n')\n"
            "(root/(name+'.json')).write_text(json.dumps({'name':name}))\n"
            "(root/(name+'.done')).write_text('complete')\n", encoding="utf-8")
        self.source = self.run / "source-manifest.json"
        self.source.write_text('{"source":"frozen"}', encoding="utf-8")
        self.plan = {
            "schema_version": "lake-backfill-plan-1", "run_id": "test-run",
            "allowed_code_dir": str(self.code), "allowed_executables": [sys.executable],
            "code_fingerprints": [{"path": str(self.script), "sha256": digest_file(self.script)}],
            "source_fingerprints": [{"path": str(self.source), "sha256": digest_file(self.source)}],
            "steps": [self.step("one"), self.step("two")],
        }

    def tearDown(self):
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self.artifacts) or not resolved.name.startswith("runner-tests-"):
            raise AssertionError("Refusing cleanup outside this test workspace")
        shutil.rmtree(resolved)

    def step(self, name):
        return {"id": name, "stage": "extract", "partition": name,
                "command": [sys.executable, str(self.script), str(self.run), name],
                "cwd": str(self.run), "completion_marker": name + ".done",
                "outputs": [{"path": name + ".json"}], "counts": {"partitions": 1}}

    def test_sequential_success_then_verified_skip(self):
        self.assertEqual(execute(self.plan, self.run), 0)
        status = read_json(self.run / "status.json")
        self.assertEqual(status["state"], "COMPLETED")
        self.assertEqual(status["counts"], {"partitions": 2})
        before = (self.run / "calls.jsonl").read_bytes()
        self.assertEqual(execute(self.plan, self.run), 0)
        self.assertEqual((self.run / "calls.jsonl").read_bytes(), before)
        self.assertEqual([json.loads(line) for line in before.splitlines()], ["one", "two"])

    def test_locked_plan_change_refused(self):
        self.assertEqual(execute(self.plan, self.run), 0)
        changed = copy.deepcopy(self.plan)
        changed["steps"][0]["counts"] = {"partitions": 99}
        self.assertEqual(execute(changed, self.run), 1)
        self.assertIn("plan hash", read_json(self.run / "status.json")["error"])

    def test_changed_checkpoint_output_refused_without_rerun(self):
        self.assertEqual(execute(self.plan, self.run), 0)
        before = (self.run / "calls.jsonl").read_bytes()
        (self.run / "one.json").write_text('tampered', encoding="utf-8")
        self.assertEqual(execute(self.plan, self.run), 1)
        self.assertEqual((self.run / "calls.jsonl").read_bytes(), before)
        self.assertIn("hash changed", read_json(self.run / "status.json")["error"])

    def test_code_and_source_fingerprint_changes_fail_before_child(self):
        for path in (self.script, self.source):
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            self.assertEqual(execute(self.plan, self.run), 1)
            self.assertFalse((self.run / "calls.jsonl").exists())
            path.write_bytes(original)

    def test_partial_outputs_are_not_overwritten(self):
        partial = self.run / "one.json"
        partial.write_text('partial', encoding="utf-8")
        self.assertEqual(execute(self.plan, self.run), 1)
        self.assertEqual(partial.read_text(), 'partial')
        self.assertFalse((self.run / "calls.jsonl").exists())

    def test_explicit_partial_resume_and_plan_environment(self):
        self.plan["steps"][0]["resume_partial"] = True
        self.plan["env"] = {"COSMOS_TEST_VALUE": "forwarded"}
        (self.run / "one.json").write_text("partial", encoding="utf-8")
        self.script.write_text(self.script.read_text() + "import os\nassert os.environ['COSMOS_TEST_VALUE']=='forwarded'\n", encoding="utf-8")
        self.plan["code_fingerprints"][0]["sha256"] = digest_file(self.script)
        self.assertEqual(execute(self.plan, self.run), 0)
        self.assertEqual(read_json(self.run / "one.json"), {"name": "one"})

    def test_detached_launch_completes_without_parent_stdin(self):
        receipt = launch(self.plan, self.run)
        self.assertTrue(receipt["detached"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = read_json(self.run / "status.json")
            if status["state"] in ("COMPLETED", "FAILED", "TERMINATED"):
                try:
                    with RunLock(self.run / ".runner.lock"):
                        time.sleep(.15)
                        break
                except RuntimeError:
                    pass
            time.sleep(.05)
        self.assertEqual(status["state"], "COMPLETED")
        self.assertEqual(status["runner_pid"], receipt["pid"])

    @unittest.skipIf(os.name == "nt", "SIGTERM process-group handling is a Linux production contract")
    def test_sigterm_records_terminated_and_no_checkpoint(self):
        self.script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        self.plan["code_fingerprints"][0]["sha256"] = digest_file(self.script)
        receipt = launch(self.plan, self.run)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if read_json(self.run / "status.json").get("child_pid"):
                break
            time.sleep(.05)
        os.kill(receipt["pid"], signal.SIGTERM)
        while time.monotonic() < deadline:
            status = read_json(self.run / "status.json")
            if status["state"] == "TERMINATED":
                time.sleep(.15)
                break
            time.sleep(.05)
        self.assertEqual(status["state"], "TERMINATED")
        self.assertFalse((self.run / "checkpoints" / "one.json").exists())

    def test_subprocess_failure_stops_later_steps(self):
        self.script.write_text("raise SystemExit(7)\n", encoding="utf-8")
        self.plan["code_fingerprints"][0]["sha256"] = digest_file(self.script)
        self.assertEqual(execute(self.plan, self.run), 1)
        self.assertEqual(read_json(self.run / "status.json")["state"], "FAILED")
        self.assertFalse((self.run / "checkpoints" / "one.json").exists())
        self.assertFalse((self.run / "two.done").exists())

    def test_escape_inline_shell_and_unfingerprinted_script_rejected(self):
        for mutation in (lambda p: p["steps"][0].update(completion_marker="../outside"),
                         lambda p: p["steps"][0].update(completion_marker="status.json"),
                         lambda p: p["steps"][0].update(command=[sys.executable, "-c", "print(1)"]),
                         lambda p: p["steps"][0].update(cwd=str(self.root.parent)),
                         lambda p: p["steps"][0].update(command=[sys.executable, str(self.code / "other.py")])):
            changed = copy.deepcopy(self.plan)
            mutation(changed)
            with self.assertRaises(ValueError):
                validate_plan(changed, self.run)

    def test_exclusive_runner_lock(self):
        with RunLock(self.run / ".runner.lock"):
            with self.assertRaises(RuntimeError):
                with RunLock(self.run / ".runner.lock"):
                    self.fail("second lock acquired")


if __name__ == "__main__":
    unittest.main()
