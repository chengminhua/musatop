"""Toolkit discovery tests isolated from the machine's SDK and environment."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from musatop.toolkit import detect_toolkit


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.default = self.base / "musa"
        self.candidates = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target, kwargs in (
            ("musatop.toolkit._default_root", {"return_value": self.default}),
            ("musatop.toolkit._installation_roots", {"side_effect": lambda: self.candidates}),
            ("musatop.toolkit.shutil.which", {"return_value": None}),
        ):
            mock = patch(target, **kwargs)
            value = mock.start()
            self.addCleanup(mock.stop)
            if target.endswith("which"):
                self.which = value

    def install(self, version, name=None):
        root = self.base / (name or f"musa-{version}")
        root.mkdir()
        (root / "version.json").write_text(
            json.dumps({"musa_toolkits": {"version": version}}), encoding="utf-8"
        )
        self.candidates.append(root)
        return root

    def assert_selected(self, root, version):
        self.assertEqual(detect_toolkit(), (version, str(root / "version.json"), None))

    def test_standard_symlink_selects_its_target_even_with_newer_version(self):
        old = self.install("4.3.2")
        self.install("5.2.1")
        self.default.symlink_to(old, target_is_directory=True)
        self.assert_selected(old, "4.3.2")

    def test_standard_directory(self):
        root = self.install("4.3.2", "musa")
        self.assert_selected(root, "4.3.2")

    def test_unique_versioned_install_without_standard_symlink(self):
        root = self.install("4.3.2")
        self.assert_selected(root, "4.3.2")

    def test_multiple_versions_are_unknown_not_highest_version(self):
        self.install("4.3.2")
        self.install("5.2.1")
        version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertIsNone(source)
        self.assertIn("Multiple Toolkit installations", reason)

    def test_explicit_old_version_takes_priority_over_path_and_default(self):
        old = self.install("4.3.2")
        new = self.install("5.2.1")
        self.default.symlink_to(new, target_is_directory=True)
        self.which.return_value = str(new / "bin" / "mcc")
        for variable in ("MUSA_HOME", "MUSA_PATH"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(old)}):
                self.assert_selected(old, "4.3.2")

    def test_path_old_version_takes_priority_over_default(self):
        old = self.install("4.3.2")
        new = self.install("5.2.1")
        self.default.symlink_to(new, target_is_directory=True)
        self.which.return_value = str(old / "bin" / "mcc")
        self.assert_selected(old, "4.3.2")

    def test_path_compiler_symlink_is_resolved(self):
        old = self.install("4.3.2")
        self.install("5.2.1")
        (old / "bin").mkdir()
        (old / "bin" / "mcc").touch()
        link = self.base / "mcc"
        link.symlink_to(old / "bin" / "mcc")
        self.which.return_value = str(link)
        self.assert_selected(old, "4.3.2")

    def test_environment_paths_resolving_to_same_root_do_not_conflict(self):
        root = self.install("4.3.2")
        alias = self.base / "alias"
        alias.symlink_to(root, target_is_directory=True)
        with patch.dict(os.environ, {"MUSA_HOME": str(root), "MUSA_PATH": str(alias)}):
            self.assert_selected(root, "4.3.2")

    def test_conflicting_environment_roots_are_unknown(self):
        old = self.install("4.3.2")
        new = self.install("5.2.1")
        with patch.dict(os.environ, {"MUSA_HOME": str(old), "MUSA_PATH": str(new)}):
            version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertIsNone(source)
        self.assertIn("select different", reason)

    def test_duplicate_versioned_symlinks_count_as_one_install(self):
        root = self.install("4.3.2")
        alias = self.base / "musa-current"
        alias.symlink_to(root, target_is_directory=True)
        self.candidates.append(alias)
        self.assert_selected(root, "4.3.2")

    def test_explicit_missing_install_does_not_fall_back(self):
        self.install("5.2.1")
        missing = self.base / "missing"
        with patch.dict(os.environ, {"MUSA_HOME": str(missing)}):
            version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertEqual(source, str(missing / "version.json"))
        self.assertIn("missing", reason)

    def test_path_missing_install_does_not_fall_back(self):
        self.install("5.2.1")
        missing = self.base / "missing"
        self.which.return_value = str(missing / "bin" / "mcc")
        version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertEqual(source, str(missing / "version.json"))
        self.assertIn("missing", reason)

    def test_dangling_standard_symlink_does_not_fall_back(self):
        self.install("5.2.1")
        missing = self.base / "missing"
        self.default.symlink_to(missing, target_is_directory=True)
        version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertEqual(source, str(missing / "version.json"))
        self.assertIn("missing", reason)

    def test_invalid_json_does_not_fall_back_to_newer_install(self):
        old = self.install("4.3.2")
        self.install("5.2.1")
        (old / "version.json").write_text("not json", encoding="utf-8")
        with patch.dict(os.environ, {"MUSA_HOME": str(old)}):
            version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertEqual(source, str(old / "version.json"))
        self.assertIn("not valid UTF-8 JSON", reason)

    def test_invalid_encoding_is_unknown(self):
        root = self.install("4.3.2")
        (root / "version.json").write_bytes(b"\xff")
        self.assertIn("not valid UTF-8 JSON", detect_toolkit()[2])

    def test_metadata_permission_failure_is_unknown(self):
        root = self.install("4.3.2")
        with patch("musatop.toolkit.Path.read_text", side_effect=PermissionError(13, "Permission denied")):
            version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertEqual(source, str(root / "version.json"))
        self.assertIn("unreadable", reason)

    def test_bad_version_structures_are_unknown(self):
        root = self.install("4.3.2")
        bad_values = ([], None, {}, {"musa_toolkits": []}, {"musa_toolkits": "4.3.2"})
        bad_values += tuple({"musa_toolkits": {"version": value}}
                            for value in (None, True, 4.3, [], {}, "", "N/A", "hello", "4.3\n2"))
        for data in bad_values:
            with self.subTest(data=data):
                (root / "version.json").write_text(json.dumps(data), encoding="utf-8")
                version, source, reason = detect_toolkit()
                self.assertIsNone(version)
                self.assertEqual(source, str(root / "version.json"))
                self.assertIn("no valid musa_toolkits.version", reason)

    def test_version_is_metadata_value_not_directory_name(self):
        root = self.install("4.3.2", "musa-99.0")
        self.assert_selected(root, "4.3.2")

    def test_whitespace_environment_is_unset(self):
        root = self.install("4.3.2")
        with patch.dict(os.environ, {"MUSA_HOME": "  ", "MUSA_PATH": ""}):
            self.assert_selected(root, "4.3.2")

    def test_absent_install_is_unknown(self):
        self.assertEqual(detect_toolkit(), (None, None, "No MUSA Toolkit installation found"))

    def test_discovery_failure_is_unknown(self):
        with patch("musatop.toolkit._installation_roots", side_effect=PermissionError("denied")):
            version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertIsNone(source)
        self.assertIn("Cannot discover", reason)

    def test_symlink_loop_is_unknown(self):
        loop = self.base / "loop"
        loop.symlink_to(loop)
        with patch.dict(os.environ, {"MUSA_HOME": str(loop)}):
            version, source, reason = detect_toolkit()
        self.assertIsNone(version)
        self.assertIsNone(source)
        self.assertIn("Cannot resolve explicitly selected", reason)


if __name__ == "__main__":
    unittest.main()
