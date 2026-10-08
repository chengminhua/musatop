import contextlib
import io
import json
import unittest
from unittest.mock import patch

from musatop.cli import main
from musatop.models import Device, Process, Snapshot


class CliTests(unittest.TestCase):
    def invoke(self, args, snapshot):
        stdout = io.StringIO()
        with patch("musatop.monitor.Monitor.sample", return_value=snapshot), contextlib.redirect_stdout(stdout):
            code = main(args)
        return code, stdout.getvalue()

    def test_json_schema_units_null_and_filter(self):
        snapshot = Snapshot(devices=[Device(0), Device(1)], processes=[
            Process(0, 100, gpu_memory_bytes=1024, username="alice"),
            Process(1, 101, gpu_memory_bytes=2048, username="bob"),
        ])
        code, output = self.invoke(["--json", "--gpu", "1", "--pid", "101", "--user", "bob"], snapshot)
        data = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual([d["index"] for d in data["devices"]], [1])
        self.assertEqual(data["processes"][0]["gpu_memory_bytes"], 2048)
        self.assertIsNone(data["devices"][0]["temperature_c"])

    def test_backend_failure_is_valid_json_and_nonzero(self):
        code, output = self.invoke(["--json"], Snapshot(devices_stale=True, errors=["missing GMI"]))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["errors"], ["missing GMI"])

    def test_non_tty_automatically_prints_once(self):
        code, output = self.invoke([], Snapshot())
        self.assertEqual(code, 0)
        self.assertIn("No GPUs found", output)

    def test_host_sampling_failure_also_sets_failure_status(self):
        code, output = self.invoke(["--json"], Snapshot(errors=["Host sampling failed: denied"]))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["errors"], ["Host sampling failed: denied"])

    def test_text_sanitizes_terminal_escape_in_command(self):
        _, output = self.invoke(["--once"], Snapshot(processes=[Process(0, 100, command="test\x1b[2J\nnext")]))
        self.assertNotIn("\x1b", output)
        self.assertIn("test [2J next", output)

    def test_bad_arguments_exit_two_without_sampling(self):
        for args in (["--interval", "nan"], ["--interval", "inf"], ["--interval", "0"], ["--interval", "1e300"],
                     ["--gpu", "0,-1"], ["--pid", "10,"], ["--sort", "invalid"]):
            with self.subTest(args=args), patch("musatop.monitor.Monitor.sample") as sample, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(args)
                self.assertEqual(exc.exception.code, 2)
                sample.assert_not_called()

    def test_version_without_hardware(self):
        with patch("musatop.monitor.Monitor.sample") as sample, contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as exc:
                main(["--version"])
            self.assertEqual(exc.exception.code, 0)
            self.assertIn("0.1.1", out.getvalue())
            sample.assert_not_called()
