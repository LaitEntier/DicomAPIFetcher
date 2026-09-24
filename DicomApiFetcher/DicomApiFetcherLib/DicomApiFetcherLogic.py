"""
DicomApiFetcherLogic
====================

Logic class for the DICOM API Fetcher module.

It connects the generic ``ApiClient`` layer with Slicer's DICOM loading
infrastructure:

* download data for the selected item(s), optionally through a persistent
  manifest-validated cache,
* examine the downloaded files with Slicer's DICOM plugins (series assembly,
  scalar volumes, segmentations, ...),
* load the series directly into the scene.

The Slicer DICOM database is intentionally NOT used: this module is a viewer
for remote data. When the optional cache is disabled, temporary files are
deleted once loading is done.
"""

import os
import shutil
import tempfile
import time

import slicer
from DICOMLib import DICOMUtils
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

from DicomApiFetcherLib.DownloadCache import DownloadCache


def _groupFilesBySeries(dicom_dirs):
    """
    Group the DICOM files under one or more directories by SeriesInstanceUID.

    Returns a dict ``{series_uid: [file paths]}``. Non-DICOM files are
    silently skipped.
    """
    import pydicom

    if isinstance(dicom_dirs, str):
        dicom_dirs = [dicom_dirs]

    files_by_series = {}
    for dicom_dir in dicom_dirs:
        for root, _dirs, files in os.walk(dicom_dir):
            for name in files:
                path = os.path.join(root, name)
                try:
                    dataset = pydicom.dcmread(
                        path, specific_tags=["SeriesInstanceUID"]
                    )
                except Exception:
                    continue
                uid = getattr(dataset, "SeriesInstanceUID", None)
                if uid:
                    files_by_series.setdefault(str(uid), []).append(path)
    return files_by_series


def _selectHighestConfidenceLoadables(loadables_by_plugin):
    """
    Keep only the highest-confidence selected loadable(s) per series.

    Same policy as Slicer's DICOM browser, but works without the DICOM
    database (the SeriesInstanceUID is read from the file itself).
    """
    import pydicom

    loadables_by_series = {}
    for loadables in loadables_by_plugin.values():
        for loadable in loadables:
            if not loadable.selected:
                continue
            try:
                dataset = pydicom.dcmread(
                    loadable.files[0], specific_tags=["SeriesInstanceUID"]
                )
                series_uid = str(getattr(dataset, "SeriesInstanceUID", ""))
            except Exception:
                series_uid = ""
            loadables_by_series.setdefault(series_uid, []).append(loadable)

    for loadables in loadables_by_series.values():
        highest = max(loadable.confidence for loadable in loadables)
        for loadable in loadables:
            loadable.selected = loadable.confidence == highest


class DicomApiFetcherLogic(ScriptedLoadableModuleLogic):
    """Logic for fetching DICOMs from an API and loading them in Slicer."""

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    @staticmethod
    def defaultCacheDirectory():
        """Return the user-specific default cache directory."""
        try:
            base_dir = str(slicer.app.slicerUserDBDirectory)
        except Exception:
            base_dir = os.path.join(os.path.expanduser("~"), ".slicer")
        return os.path.join(base_dir, DownloadCache.CACHE_DIR_NAME)

    def listItems(self, client, base_url, list_endpoint=None):
        """Return the list of items exposed by the API."""
        return client.list_items(base_url, list_endpoint)

    @staticmethod
    def _archimedCacheKey(client, base_url, node):
        """Build a stable cache key for an ArchiMed tree node."""
        return {
            "strategy": "archimed",
            "base_url": base_url.rstrip("/"),
            "zone": str(getattr(client, "zone", "final")),
            "level": str(node.get("level", "")),
            "studyID": str(node.get("studyID", "")),
            "examID": str(node.get("examID", "")),
            "serieID": str(node.get("serieID", "")),
        }

    @staticmethod
    def _itemCacheKey(client, base_url, item_id, fetch_endpoint):
        """Build a stable cache key for a generic ZIP/manifest item."""
        return {
            "strategy": client.__class__.__name__,
            "base_url": base_url.rstrip("/"),
            "fetch_endpoint": str(fetch_endpoint or ""),
            "item_id": str(item_id),
        }

    @staticmethod
    def _downloadArchimedNode(
        client, base_url, node, output_dir, progress_callback=None
    ):
        """Download one ArchiMed study/exam/serie into *output_dir*."""
        level = node.get("level")
        if level == "study":
            return client.download_study(
                base_url,
                node["studyID"],
                output_dir,
                progress_callback=progress_callback,
            )
        if level == "exam":
            return client.download_exam(
                base_url,
                node["studyID"],
                node["examID"],
                output_dir,
                progress_callback=progress_callback,
            )
        return client.download_serie(
            base_url,
            node["studyID"],
            node["examID"],
            node["serieID"],
            output_dir,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _describeArchimedNode(node):
        """Return a short human-readable ArchiMed node description."""
        level = node.get("level", "serie")
        if level == "study":
            return f"study {node.get('studyID')}"
        if level == "exam":
            return f"exam {node.get('examID')}"
        return f"serie {node.get('serieID')}"

    def _downloadWithCache(
        self,
        cache_manager,
        cache_key,
        metadata,
        download_function,
        progress_callback=None,
    ):
        """
        Return a cached DICOM directory when complete, otherwise download into
        staging and promote it to the cache only after success.
        """
        cached_entry = cache_manager.get_valid_entry(cache_key)
        if cached_entry:
            cached_dicom_dir = os.path.join(cached_entry, "dicoms")
            if os.path.isdir(cached_dicom_dir):
                if progress_callback:
                    progress_callback(
                        f"Using cached {metadata.get('description', 'DICOM data')}."
                    )
                return cached_dicom_dir

        staging_dir = cache_manager.create_staging_dir()
        try:
            dicom_dir = download_function(staging_dir)
            if not dicom_dir or not os.path.isdir(dicom_dir):
                raise RuntimeError(
                    f"Download did not produce a DICOM folder: {dicom_dir}"
                )
            cached_entry = cache_manager.promote_entry(
                cache_key, staging_dir, metadata=metadata
            )
            return os.path.join(cached_entry, "dicoms")
        except Exception:
            cache_manager.discard_staging_dir(staging_dir)
            raise

    def fetchDicomData(
        self,
        client,
        base_url,
        item_ids=None,
        fetch_endpoint=None,
        archimed_nodes=None,
        progress_callback=None,
        cache_manager=None,
    ):
        """
        Download the selected data and return ``(dicom_dirs, temporary_dir)``.

        ``temporary_dir`` is ``None`` when data comes from the persistent
        cache. Otherwise, the caller owns it and must delete it after loading.
        This method performs no Slicer scene operations and is safe to run in
        a worker thread.
        """
        start_time = time.monotonic()
        temp_root = None
        dicom_dirs = []

        if progress_callback:
            progress_callback("Preparing DICOM download...")

        try:
            if archimed_nodes:
                if not cache_manager:
                    temp_root = tempfile.mkdtemp(prefix="dicom_api_fetcher_")
                for node in archimed_nodes:
                    description = self._describeArchimedNode(node)
                    if cache_manager:
                        cache_key = self._archimedCacheKey(
                            client, base_url, node
                        )
                        dicom_dir = self._downloadWithCache(
                            cache_manager,
                            cache_key,
                            {"description": description},
                            lambda output_dir, current_node=node: (
                                self._downloadArchimedNode(
                                    client,
                                    base_url,
                                    current_node,
                                    output_dir,
                                    progress_callback=progress_callback,
                                )
                            ),
                            progress_callback=progress_callback,
                        )
                    else:
                        dicom_dir = self._downloadArchimedNode(
                            client,
                            base_url,
                            node,
                            temp_root,
                            progress_callback=progress_callback,
                        )
                    if dicom_dir not in dicom_dirs:
                        dicom_dirs.append(dicom_dir)
            else:
                item_ids = item_ids or []
                if not item_ids:
                    raise RuntimeError("No item was selected for download.")
                if not cache_manager:
                    temp_root = tempfile.mkdtemp(prefix="dicom_api_fetcher_")
                for item_id in item_ids:
                    if cache_manager:
                        cache_key = self._itemCacheKey(
                            client, base_url, item_id, fetch_endpoint
                        )
                        dicom_dir = self._downloadWithCache(
                            cache_manager,
                            cache_key,
                            {"description": f"item {item_id}"},
                            lambda output_dir, current_item=item_id: (
                                client.fetch_item(
                                    base_url,
                                    current_item,
                                    output_dir,
                                    fetch_endpoint,
                                    progress_callback=progress_callback,
                                )
                            ),
                            progress_callback=progress_callback,
                        )
                    else:
                        dicom_dir = client.fetch_item(
                            base_url,
                            item_id,
                            temp_root,
                            fetch_endpoint,
                            progress_callback=progress_callback,
                        )
                    if dicom_dir not in dicom_dirs:
                        dicom_dirs.append(dicom_dir)

            dicom_dirs = [
                dicom_dir
                for dicom_dir in dicom_dirs
                if dicom_dir and os.path.isdir(dicom_dir)
            ]
            if not dicom_dirs:
                raise RuntimeError("Download did not produce a DICOM folder.")

            file_count = sum(
                len(files)
                for dicom_dir in dicom_dirs
                for _root, _dirs, files in os.walk(dicom_dir)
            )
            elapsed = time.monotonic() - start_time
            if progress_callback:
                progress_callback(
                    f"DICOM data ready: {file_count} file(s) "
                    f"in {elapsed:.1f} s."
                )
            return dicom_dirs, temp_root
        except Exception:
            if temp_root:
                shutil.rmtree(temp_root, ignore_errors=True)
            raise

    def loadDicomData(self, dicom_dirs, progress_callback=None):
        """
        Examine and load one or more DICOM directories into the Slicer scene.

        This method must run on Slicer's main thread because it calls Slicer's
        DICOM plugins and modifies the scene.
        """
        if isinstance(dicom_dirs, str):
            dicom_dirs = [dicom_dirs]
        dicom_dirs = [
            dicom_dir
            for dicom_dir in dicom_dirs
            if dicom_dir and os.path.isdir(dicom_dir)
        ]
        if not dicom_dirs:
            raise RuntimeError("No downloaded DICOM folder is available.")

        grouping_start = time.monotonic()
        if progress_callback:
            progress_callback("Grouping DICOM files by series...")
        files_by_series = _groupFilesBySeries(dicom_dirs)
        if not files_by_series:
            raise RuntimeError(
                "No DICOM files found in downloaded data: "
                + ", ".join(dicom_dirs)
            )
        grouping_elapsed = time.monotonic() - grouping_start

        if progress_callback:
            progress_callback(
                f"Examining {len(files_by_series)} DICOM serie(s)..."
            )
        examination_start = time.monotonic()
        loadables_by_plugin, load_enabled = (
            DICOMUtils.getLoadablesFromFileLists(
                list(files_by_series.values())
            )
        )
        if not load_enabled:
            raise RuntimeError(
                "No Slicer DICOM plugin could load the downloaded data."
            )
        examination_elapsed = time.monotonic() - examination_start

        # Same default as the DICOM browser: one interpretation per series.
        _selectHighestConfidenceLoadables(loadables_by_plugin)

        if progress_callback:
            progress_callback(
                f"Loading into scene... "
                f"(grouping: {grouping_elapsed:.1f} s, "
                f"examination: {examination_elapsed:.1f} s)"
            )
        loading_start = time.monotonic()
        loaded_node_ids = DICOMUtils.loadLoadables(loadables_by_plugin)
        loading_elapsed = time.monotonic() - loading_start
        if progress_callback:
            progress_callback(
                f"Loaded {len(loaded_node_ids)} node(s) "
                f"in {loading_elapsed:.1f} s."
            )
        return loaded_node_ids

    def fetchAndLoad(
        self,
        client,
        base_url,
        item_ids=None,
        fetch_endpoint=None,
        archimed_nodes=None,
        progress_callback=None,
        cache_manager=None,
    ):
        """
        Download DICOM data using *client* and load it directly into the
        scene (no DICOM database involved).

        Two download modes:

        * ``archimed_nodes`` - a list of node descriptors
          ``{"level": "study"|"exam"|"serie", "studyID", "examID",
          "serieID"}`` downloaded through the ArchiMed hierarchy methods.
        * ``item_ids`` - a list of flat item IDs downloaded through the
          generic ``fetch_item`` interface (ZIP/manifest strategies).

        Returns the list of loaded node IDs.
        """
        dicom_dirs, temp_root = self.fetchDicomData(
            client,
            base_url,
            item_ids=item_ids,
            fetch_endpoint=fetch_endpoint,
            archimed_nodes=archimed_nodes,
            progress_callback=progress_callback,
            cache_manager=cache_manager,
        )
        try:
            return self.loadDicomData(
                dicom_dirs, progress_callback=progress_callback
            )
        finally:
            if temp_root:
                # Data is loaded into the scene; nothing references the files.
                shutil.rmtree(temp_root, ignore_errors=True)
