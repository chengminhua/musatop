"""Read the selected MUSA Toolkit's metadata without running its compiler.

An installation selection is authoritative even if its metadata is missing.
In particular, several installed releases do not tell us which one is active.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil


def _default_root() -> Path:
    return Path("/usr/local/musa")


def _installation_roots() -> list[Path]:
    """Discover versioned installations; resolving/deduplication happens later."""
    directory = Path("/usr/local")
    return sorted(
        path for path in directory.iterdir()
        if path.name.startswith("musa-") and (path.is_dir() or path.is_symlink())
    )


def _metadata(root: Path) -> tuple[str | None, str, str | None]:
    source = str(root / "version.json")
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, source, "Toolkit metadata file is missing"
    except OSError as error:
        return None, source, f"Toolkit metadata is unreadable: {error.strerror or error}"
    except (ValueError, UnicodeError):
        return None, source, "Toolkit metadata is not valid UTF-8 JSON"
    section = data.get("musa_toolkits") if isinstance(data, dict) else None
    version = section.get("version") if isinstance(section, dict) else None
    # A numeric JSON value loses version components (e.g. 4.10 becomes 4.1).
    # Do not stringify malformed values or use the installation directory name.
    if not isinstance(version, str) or not re.fullmatch(
        r"\d+(?:\.\d+)*(?:[-+._][0-9A-Za-z][0-9A-Za-z._+-]*)?", version.strip()
    ):
        return None, source, "Toolkit metadata has no valid musa_toolkits.version string"
    return version.strip(), source, None


def _resolved(root: Path) -> Path:
    # Non-strict resolution lets a missing, explicitly selected root retain its
    # identity instead of accidentally falling back to another installation.
    return root.expanduser().resolve()


def detect_toolkit() -> tuple[str | None, str | None, str | None]:
    """Return ``(version, metadata_file, unavailable_reason)``.

    Selection precedence is MUSA_HOME/MUSA_PATH, the real PATH mcc location,
    /usr/local/musa, then a unique /usr/local/musa-* installation. Conflicting
    explicit roots or multiple unselected installations are unknown. Symlinks
    to the same real installation count only once. No commands are executed.
    """
    explicit = [Path(value.strip()) for name in ("MUSA_HOME", "MUSA_PATH")
                if (value := os.environ.get(name, "")).strip()]
    try:
        roots = {_resolved(root) for root in explicit}
    except (OSError, RuntimeError) as error:
        return None, None, f"Cannot resolve explicitly selected Toolkit installation: {error}"
    if len(roots) > 1:
        return None, None, "MUSA_HOME and MUSA_PATH select different Toolkit installations"
    if roots:
        return _metadata(roots.pop())

    compiler = shutil.which("mcc")
    if compiler:
        try:
            root = _resolved(Path(compiler)).parent.parent
        except (OSError, RuntimeError) as error:
            return None, None, f"Cannot resolve PATH mcc installation: {error}"
        return _metadata(root)

    default = _default_root()
    try:
        # lstat notices a dangling default symlink: it is still an explicit
        # installation choice and must not silently select a different SDK.
        default.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        return None, str(default / "version.json"), f"Cannot inspect default Toolkit installation: {error}"
    else:
        try:
            return _metadata(_resolved(default))
        except (OSError, RuntimeError) as error:
            return None, str(default / "version.json"), f"Cannot resolve default Toolkit installation: {error}"

    try:
        roots = {_resolved(root) for root in _installation_roots()}
    except (OSError, RuntimeError) as error:
        return None, None, f"Cannot discover Toolkit installations: {error}"
    if len(roots) > 1:
        return None, None, "Multiple Toolkit installations found; select one with MUSA_HOME or MUSA_PATH"
    if roots:
        return _metadata(roots.pop())
    return None, None, "No MUSA Toolkit installation found"
