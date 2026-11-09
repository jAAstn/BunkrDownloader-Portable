#!/usr/bin/env python3
"""Sync the BunkrDownloader backend with a newer upstream release.

The fork carries its own backend changes (``--min-image-height``, subdomain
retry handling, album-name fallback, ...). Copying upstream files over ``src/``
would throw those away, so this script performs a three-way merge per file:

    base   = the upstream release the fork was last synced with
    ours   = the backend files in this repository
    theirs = the new upstream release

``git merge-file`` does the merge. If anything conflicts, or the merged tree
does not compile, nothing is written and the current backend stays untouched.

The last synced release is tracked in ``.backend_base.json``. If the file does
not exist yet, the version in ``src/version.py`` is used as the baseline.

Usage:
    python update_backend.py     # check and update the backend
    build.bat                    # calls this automatically before packaging
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM_REPO = "Lysagxra/BunkrDownloader"
STATE_FILE_NAME = ".backend_base.json"
BACKUP_FOLDER = "Backups"

# Backend paths that come from upstream and are merged. The GUI files, build
# scripts and README are fork-owned and deliberately left alone.
ROOT_FILES = ("downloader.py", "main.py", "bunkr.toml.example")
TREE_DIRS = ("src", "tests")

_VERSION_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_VERSION_INFO_RE = re.compile(r"major=(\d+),\s*minor=(\d+),\s*micro=(\d+)")
_VERSION_LINE_RE = re.compile(r"version_info = VersionInfo\([^)]*\)")

Version = tuple[int, int, int]


class UpdateError(Exception):
    """The update could not be applied; the local backend was not modified."""


class NetworkError(UpdateError):
    """Upstream could not be reached; the build may continue unchanged."""


def format_version(version: Version) -> str:
    """Return ``1.2.3`` for a ``(1, 2, 3)`` tuple."""
    return ".".join(str(part) for part in version)


def parse_version(text: str) -> Version:
    """Parse ``v1.2.3`` / ``1.2.3`` into a comparable tuple."""
    match = _VERSION_TAG_RE.match(text.strip())
    if match is None:
        raise UpdateError(f"Unrecognised version: {text!r}")
    major, minor, micro = (int(part) for part in match.groups())
    return major, minor, micro


def read_local_version(root: Path) -> Version:
    """Read the backend version from ``src/version.py``."""
    match = _VERSION_INFO_RE.search((root / "src" / "version.py").read_text("utf-8"))
    if match is None:
        raise UpdateError("Could not read the backend version from src/version.py.")
    major, minor, micro = (int(part) for part in match.groups())
    return major, minor, micro


def state_file(root: Path) -> Path:
    """Return the sync-state path for the given repository root."""
    return root / STATE_FILE_NAME


def load_base_version(root: Path) -> Version:
    """Return the upstream release this fork is currently based on."""
    path = state_file(root)
    if not path.exists():
        base = read_local_version(root)
        save_state(root, base)
        print(
            f"[backend-update] No sync state found; baseline set to "
            f"v{format_version(base)}."
        )
        return base

    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise UpdateError(f"Could not read {STATE_FILE_NAME}: {err}") from err

    if data.get("repo") != UPSTREAM_REPO:
        raise UpdateError(
            f"{STATE_FILE_NAME} tracks {data.get('repo')!r}, expected "
            f"{UPSTREAM_REPO!r}."
        )
    return parse_version(str(data.get("base_version", "")))


def save_state(root: Path, version: Version) -> None:
    """Persist the upstream release the fork is now synced with."""
    state_file(root).write_text(
        json.dumps(
            {"repo": UPSTREAM_REPO, "base_version": format_version(version)},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def fetch(url: str) -> bytes:
    """Download ``url``, raising :class:`NetworkError` on transport problems."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BunkrDownloader-Portable-updater",
            "Accept": "application/vnd.github+json",
        },
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    except urllib.error.HTTPError as err:
        raise NetworkError(f"GitHub returned HTTP {err.code} for {url}.") from err
    except urllib.error.URLError as err:
        raise NetworkError(f"Could not reach GitHub: {err.reason}") from err
    except TimeoutError as err:
        raise NetworkError("The GitHub request timed out.") from err


def fetch_latest_release() -> tuple[Version, str]:
    """Return ``(version, tag)`` of the newest stable upstream release."""
    payload = fetch(f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as err:
        raise UpdateError("GitHub returned an invalid release description.") from err

    tag = str(data.get("tag_name", ""))
    try:
        return parse_version(tag), tag
    except UpdateError as err:
        raise UpdateError(f"Latest release has an unexpected tag: {tag!r}") from err


def download_release(version: Version, destination: Path) -> Path:
    """Download and extract an upstream release; return its source tree."""
    tag = f"v{format_version(version)}"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{tag}.tar.gz"
    archive.write_bytes(
        fetch(f"https://api.github.com/repos/{UPSTREAM_REPO}/tarball/{tag}")
    )

    extract_root = destination / "source"
    extract_root.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(extract_root, filter="data")
        except TypeError:  # Python < 3.12 has no extraction filter argument.
            tar.extractall(extract_root)

    roots = [path for path in extract_root.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise UpdateError(f"Unexpected archive layout for {tag}.")
    return roots[0]


def collect_backend_files(tree: Path) -> dict[str, str]:
    """Return the updatable backend files of ``tree`` keyed by relative path."""
    files: dict[str, str] = {}

    for name in ROOT_FILES:
        path = tree / name
        if path.is_file():
            files[name] = path.read_text("utf-8")

    for directory in TREE_DIRS:
        root = tree / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files[path.relative_to(tree).as_posix()] = path.read_text("utf-8")

    return files


def merge_text(ours: str, base: str, theirs: str) -> tuple[str, bool]:
    """Three-way merge one file; the flag reports unresolved conflicts."""
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False

    if shutil.which("git") is None:
        raise UpdateError(
            "git is required to merge upstream changes but was not found on PATH."
        )

    with tempfile.TemporaryDirectory(prefix="bunkr-merge-") as tmp:
        holder = Path(tmp)
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            with (holder / name).open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)

        result = subprocess.run(  # noqa: S603 - fixed argv, no shell.
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                str(holder / "ours"),
                str(holder / "base"),
                str(holder / "theirs"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    if result.returncode == 0:
        return result.stdout, False
    if 0 < result.returncode < 128:
        return result.stdout, True
    raise UpdateError(f"git merge-file failed: {result.stderr.strip()}")


def with_target_version(text: str, version: Version) -> str:
    """Force ``src/version.py`` to report ``version``."""
    replacement = (
        f'version_info = VersionInfo(major={version[0]}, minor={version[1]}, '
        f'micro={version[2]}, release_level="final")'
    )
    updated, count = _VERSION_LINE_RE.subn(replacement, text, count=1)
    if count != 1:
        raise UpdateError("Could not update the version in src/version.py.")
    return updated


def build_updates(
    root: Path,
    base_tree: Path,
    target_tree: Path,
    target_version: Version,
) -> tuple[dict[str, str], list[str], list[str], list[str]]:
    """Merge the new release into the local backend.

    Returns the files to write, newly added files, conflicting files and files
    upstream dropped. Raises :class:`UpdateError` when the result is unusable.
    """
    base_files = collect_backend_files(base_tree)
    target_files = collect_backend_files(target_tree)
    local_files = collect_backend_files(root)

    updates: dict[str, str] = {}
    added: list[str] = []
    conflicts: list[str] = []
    removed: list[str] = []

    for rel, theirs in target_files.items():
        ours = local_files.get(rel)
        base = base_files.get(rel)

        if ours is None:
            updates[rel] = theirs
            added.append(rel)
            continue

        if base is None:
            # Upstream did not ship this path in the tracked release, so there
            # is no common ancestor to merge against.
            if ours != theirs:
                conflicts.append(rel)
            continue

        merged, conflicted = merge_text(ours, base, theirs)
        if conflicted:
            conflicts.append(rel)
        elif merged != ours:
            updates[rel] = merged

    for rel in base_files:
        if rel not in target_files and rel in local_files:
            removed.append(rel)

    if conflicts:
        listing = "\n".join(f"  - {rel}" for rel in sorted(conflicts))
        raise UpdateError(
            "The new upstream release conflicts with local backend changes:\n"
            f"{listing}\n"
            "Nothing was changed. Merge these files manually, remove the "
            "conflict markers, then run the updater again."
        )

    version_text = updates.get(
        "src/version.py", local_files.get("src/version.py", "")
    )
    updates["src/version.py"] = with_target_version(version_text, target_version)

    problem = compile_check(updates)
    if problem:
        raise UpdateError("The merged backend does not compile:\n" + problem)

    return updates, added, conflicts, removed


def compile_check(files: dict[str, str]) -> str | None:
    """Syntax-check the merged files in a throwaway tree."""
    with tempfile.TemporaryDirectory(prefix="bunkr-compile-") as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            if not rel.endswith(".py"):
                continue
            destination = root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)

        result = subprocess.run(  # noqa: S603 - fixed argv, no shell.
            [sys.executable, "-m", "compileall", "-q", str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    if result.returncode:
        return (result.stdout + result.stderr).strip()
    return None


def requirements_notice(base_tree: Path, target_tree: Path) -> str | None:
    """Warn when upstream changed dependencies the fork has to install."""

    def entries(tree: Path) -> set[str]:
        path = tree / "requirements.txt"
        if not path.is_file():
            return set()
        return {
            line.strip()
            for line in path.read_text("utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

    base = entries(base_tree)
    target = entries(target_tree)
    added = sorted(target - base)
    removed = sorted(base - target)
    if not added and not removed:
        return None

    lines = ["Upstream changed requirements.txt since the last sync:"]
    lines += [f"  + {entry}" for entry in added]
    lines += [f"  - {entry}" for entry in removed]
    lines.append(
        "Review requirements.txt and install the new packages before building."
    )
    return "\n".join(lines)


def apply_updates(
    root: Path,
    updates: dict[str, str],
    old_version: Version,
    new_version: Version,
) -> Path:
    """Write merged files, keeping each file's line endings and a backup."""
    backup_root = (
        root
        / BACKUP_FOLDER
        / f"backend_v{format_version(old_version)}_to_v{format_version(new_version)}"
    )

    for rel, text in updates.items():
        destination = root / rel
        if destination.exists():
            existing = destination.read_bytes()
            line_ending = "\r\n" if b"\r\n" in existing else "\n"
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        else:
            line_ending = "\r\n"

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(text.replace("\n", line_ending).encode("utf-8"))

    return backup_root


def run_update(root: Path = ROOT) -> int:
    """Check upstream and apply a newer backend release."""
    local_version = read_local_version(root)
    print(f"[backend-update] Local backend: v{format_version(local_version)}")

    base_version = load_base_version(root)
    if base_version != local_version:
        raise UpdateError(
            f"{STATE_FILE_NAME} says the fork is synced with "
            f"v{format_version(base_version)}, but src/version.py reports "
            f"v{format_version(local_version)}. Correct the baseline in "
            f"{STATE_FILE_NAME} first."
        )

    latest_version, tag = fetch_latest_release()
    print(f"[backend-update] Latest upstream release: {tag}")

    if latest_version <= local_version:
        print("[backend-update] Already up to date.")
        return 0

    print(
        f"[backend-update] Syncing backend v{format_version(local_version)} -> "
        f"v{format_version(latest_version)}"
    )

    with tempfile.TemporaryDirectory(prefix="bunkr-update-") as tmp:
        work = Path(tmp)
        print("[backend-update] Downloading upstream snapshots...")
        base_tree = download_release(base_version, work / "base")
        target_tree = download_release(latest_version, work / "target")

        print("[backend-update] Merging backend files (fork changes kept)...")
        updates, added, _conflicts, removed = build_updates(
            root, base_tree, target_tree, latest_version
        )

        notice = requirements_notice(base_tree, target_tree)
        backup_root = apply_updates(
            root, updates, local_version, latest_version
        )
        save_state(root, latest_version)

    print(f"[backend-update] Updated {len(updates)} file(s).")
    if added:
        print("[backend-update] New file(s): " + ", ".join(sorted(added)))
    if removed:
        print(
            "[backend-update] Upstream no longer ships: "
            + ", ".join(sorted(removed))
            + " (kept locally)"
        )
    print(
        "[backend-update] Backup: "
        + str(backup_root.relative_to(root))
    )
    if notice:
        print("[backend-update] " + notice)
    print("[backend-update] Done. Run build.bat to create the new executable.")
    return 0


def main() -> int:
    """Entry point used by the CLI and by build.bat."""
    try:
        return run_update()
    except NetworkError as err:
        print(f"[backend-update] {err}")
        print(
            "[backend-update] Skipping the update check; the build can continue "
            "with the current backend."
        )
        return 0
    except UpdateError as err:
        print(f"[backend-update] Update failed: {err}")
        print("[backend-update] The current backend was left unchanged.")
        return 1
    except KeyboardInterrupt:
        print("\n[backend-update] Cancelled.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
