"""
DownloadCache
=============

Small manifest-based cache for downloaded DICOM files.

A cache entry is considered valid only when its manifest exists and every
file listed in the manifest is present. Downloads are first written to a
staging directory and promoted to the cache only after the complete item has
been downloaded successfully.
"""

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone


class DownloadCache:
    """Manage persistent, manifest-validated download cache entries."""

    MANIFEST_NAME = ".dicom_api_fetcher_manifest.json"
    CACHE_DIR_NAME = "DicomApiFetcherCache"

    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)

    def _entry_dir(self, key):
        """Return the deterministic cache directory for *key*."""
        encoded_key = json.dumps(
            key,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded_key).hexdigest()
        strategy = self._safe_name(str(key.get("strategy", "download")))
        return os.path.join(self.root_dir, strategy or "download", digest)

    @staticmethod
    def _safe_name(value):
        """Return a conservative directory-name fragment."""
        return "".join(
            character if character.isalnum() or character in ("-", "_") else "_"
            for character in value
        )[:64]

    def create_staging_dir(self):
        """Create and return a temporary staging directory inside the cache."""
        staging_root = os.path.join(self.root_dir, ".staging")
        os.makedirs(staging_root, exist_ok=True)
        return tempfile.mkdtemp(prefix="download_", dir=staging_root)

    def get_valid_entry(self, key):
        """
        Return the cache entry directory for *key*, or ``None`` if absent or
        incomplete. A valid entry has a manifest and all listed files.
        """
        entry_dir = self._entry_dir(key)
        manifest_path = os.path.join(entry_dir, self.MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            return None

        try:
            with open(manifest_path, "r", encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
        except (OSError, ValueError):
            return None

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            return None

        entry_root = os.path.abspath(entry_dir)
        for relative_path in files:
            if not isinstance(relative_path, str) or not relative_path:
                return None
            file_path = os.path.abspath(
                os.path.join(entry_root, relative_path.replace("/", os.sep))
            )
            if os.path.commonpath([entry_root, file_path]) != entry_root:
                return None
            if not os.path.isfile(file_path):
                return None
        return entry_dir

    def promote_entry(self, key, staging_dir, metadata=None):
        """
        Validate *staging_dir*, add its manifest, and atomically move it into
        the cache. Returns the final cache entry directory.
        """
        staging_dir = os.path.abspath(staging_dir)
        files = []
        for root, _dirs, filenames in os.walk(staging_dir):
            for filename in filenames:
                if filename.endswith(".part") or filename == self.MANIFEST_NAME:
                    raise RuntimeError(
                        f"Download staging contains an incomplete file: {filename}"
                    )
                path = os.path.join(root, filename)
                files.append(
                    os.path.relpath(path, staging_dir).replace(os.sep, "/")
                )

        if not files:
            raise RuntimeError("Download did not produce any files to cache.")

        manifest = {
            "version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "key": key,
            "metadata": metadata or {},
            "files": sorted(files),
        }
        manifest_path = os.path.join(staging_dir, self.MANIFEST_NAME)
        temporary_manifest_path = manifest_path + ".tmp"
        with open(temporary_manifest_path, "w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
        os.replace(temporary_manifest_path, manifest_path)

        entry_dir = self._entry_dir(key)
        os.makedirs(os.path.dirname(entry_dir), exist_ok=True)
        if os.path.isdir(entry_dir):
            shutil.rmtree(entry_dir)
        os.replace(staging_dir, entry_dir)
        return entry_dir

    def discard_staging_dir(self, staging_dir):
        """Remove an incomplete staging directory."""
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def clear(self):
        """Delete all cache entries."""
        root_dir = os.path.abspath(self.root_dir)
        if os.path.basename(root_dir) != self.CACHE_DIR_NAME:
            raise RuntimeError(f"Refusing to clear unexpected directory: {root_dir}")
        shutil.rmtree(root_dir, ignore_errors=True)
