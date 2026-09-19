"""PID-reuse and strict disk-threshold regressions without signaling real processes."""
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guard_lake_backfill as guard


class BackfillGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "artifacts" / "guard-run"
        self.argv = ["/usr/bin/python3", "/code/run_lake_backfill.py", "run", "--run-dir", str(self.root),
                     "--plan", str(self.root / "plan.locked.json")]
        self.identity = {"pid": 12345, "starttime_ticks": 987654, "cmdline": self.argv, "boot_id": "boot-one"}

    def test_proc_stat_uses_field_22_after_complex_comm(self):
        suffix = ["S"] + ["0"] * 18 + ["987654"] + ["1", "2"]
        self.assertEqual(guard.parse_proc_stat("12345 (python ) worker) " + " ".join(suffix)),
                         {"pid": 12345, "state": "S", "starttime_ticks": 987654})
        with self.assertRaises(ValueError):
            guard.parse_proc_stat("12345 (python) S 0")

    def test_threshold_is_strictly_below_12_gib(self):
        self.assertFalse(guard.below_threshold(12 * guard.GIB, 12))
        self.assertTrue(guard.below_threshold(12 * guard.GIB - 1, 12))
        self.assertFalse(guard.below_threshold(20 * guard.GIB, 12))
        with self.assertRaises(ValueError):
            guard.below_threshold(0, 0)

    def test_exact_supervisor_run_directory_required(self):
        guard.validate_supervisor(self.argv, self.root)
        wrong = self.argv.copy()
        wrong[4] = str(self.root.parent / "other-run")
        with self.assertRaisesRegex(ValueError, "does not match"):
            guard.validate_supervisor(wrong, self.root)
        wrong = self.argv.copy()
        wrong[2] = "launch"
        with self.assertRaisesRegex(ValueError, "not the"):
            guard.validate_supervisor(wrong, self.root)

    def test_reused_pid_never_opens_pidfd_or_signals(self):
        reader = Mock(return_value={**self.identity, "starttime_ticks": 222222})
        with patch.object(guard.os, "pidfd_open", create=True) as open_fd, \
             patch.object(guard.signal, "pidfd_send_signal", create=True) as send:
            self.assertEqual(guard.terminate_supervisor(self.identity, process_reader=reader,
                                                      boot_reader=lambda: "boot-one"), "TARGET_CHANGED")
            open_fd.assert_not_called()
            send.assert_not_called()

    def test_pid_reused_between_checks_closes_fd_without_signaling(self):
        reader = Mock(side_effect=[self.identity, {**self.identity, "starttime_ticks": 222222}])
        with patch.object(guard.os, "pidfd_open", return_value=77, create=True), \
             patch.object(guard.os, "close") as close, \
             patch.object(guard.signal, "pidfd_send_signal", create=True) as send:
            self.assertEqual(guard.terminate_supervisor(self.identity, process_reader=reader,
                                                      boot_reader=lambda: "boot-one"), "TARGET_CHANGED")
            send.assert_not_called()
            close.assert_called_once_with(77)

    def test_matching_identity_signals_only_bound_pidfd(self):
        reader = Mock(return_value=self.identity)
        with patch.object(guard.os, "pidfd_open", return_value=77, create=True) as open_fd, \
             patch.object(guard.os, "close") as close, \
             patch.object(guard.signal, "pidfd_send_signal", create=True) as send:
            self.assertEqual(guard.terminate_supervisor(self.identity, process_reader=reader,
                                                      boot_reader=lambda: "boot-one"), "SIGTERM_SENT")
            open_fd.assert_called_once_with(12345, 0)
            send.assert_called_once_with(77, signal.SIGTERM, None, 0)
            close.assert_called_once_with(77)

    def test_changed_boot_or_exited_target_never_signals(self):
        with patch.object(guard.os, "pidfd_open", create=True) as open_fd:
            self.assertEqual(guard.terminate_supervisor(self.identity, process_reader=lambda unused: self.identity,
                                                      boot_reader=lambda: "new-boot"), "TARGET_CHANGED")
            self.assertEqual(guard.terminate_supervisor(self.identity, process_reader=lambda unused: None,
                                                      boot_reader=lambda: "boot-one"), "TARGET_EXITED")
            open_fd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
