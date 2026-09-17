"""
DicomApiFetcherLogic
====================

Logic class for the DICOM API Fetcher module.

It connects the generic ``ApiClient`` layer with Slicer's DICOM loading
infrastructure:

* download data for the selected item(s),
* examine the downloaded files with Slicer's DICOM plugins (series assembly,
  scalar volumes, segmentations, ...),
* load the series directly into the scene.

The Slicer DICOM database is intentionally NOT used: this module is a viewer
for remote data, so files are loaded straight from the temporary download
folder, which is deleted once loading is done.
"""

import os
import shutil
import tempfile

import slicer
from DICOMLib import DICOMUtils
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic


def _groupFilesBySeries(dicom_dir):
    """
    Group the DICOM files under *dicom_dir* by SeriesInstanceUID.

    Returns a dict ``{series_uid: [file paths]}``. Non-DICOM files are
    silently skipped.
    """
    import pydicom

    files_by_series = {}
    for root, _dirs, files in os.walk(dicom_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                dataset = pydicom.dcmread(path, stop_before_pixels=True)
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
                    loadable.files[0], stop_before_pixels=True
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

    def listItems(self, client, base_url, list_endpoint=None):
        """Return the list of items exposed by the API."""
        return client.list_items(base_url, list_endpoint)

    def fetchAndLoad(
        self,
        client,
        base_url,
        item_ids=None,
        fetch_endpoint=None,
        archimed_nodes=None,
        progress_callback=None,
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
        temp_root = tempfile.mkdtemp(prefix="dicom_api_fetcher_")
        try:
            if progress_callback:
                progress_callback("Downloading DICOM data...")

            if archimed_nodes:
                dicom_dir = os.path.join(temp_root, "dicoms")
                for node in archimed_nodes:
                    level = node.get("level")
                    if level == "study":
                        client.download_study(
                            base_url,
                            node["studyID"],
                            temp_root,
                            progress_callback=progress_callback,
                        )
                    elif level == "exam":
                        client.download_exam(
                            base_url,
                            node["studyID"],
                            node["examID"],
                            temp_root,
                            progress_callback=progress_callback,
                        )
                    else:  # serie
                        client.download_serie(
                            base_url,
                            node["studyID"],
                            node["examID"],
                            node["serieID"],
                            temp_root,
                            progress_callback=progress_callback,
                        )
            else:
                dicom_dir = None
                for item_id in item_ids or []:
                    dicom_dir = client.fetch_item(
                        base_url, item_id, temp_root, fetch_endpoint
                    )

            if not dicom_dir or not os.path.isdir(dicom_dir):
                raise RuntimeError(
                    f"Download did not produce a DICOM folder: {dicom_dir}"
                )

            files_by_series = _groupFilesBySeries(dicom_dir)
            if not files_by_series:
                raise RuntimeError(
                    f"No DICOM files found in downloaded data: {dicom_dir}"
                )

            if progress_callback:
                progress_callback("Examining DICOM data...")
            loadables_by_plugin, load_enabled = (
                DICOMUtils.getLoadablesFromFileLists(
                    list(files_by_series.values())
                )
            )
            if not load_enabled:
                raise RuntimeError(
                    "No Slicer DICOM plugin could load the downloaded data."
                )

            # Same default as the DICOM browser: one interpretation per series.
            _selectHighestConfidenceLoadables(loadables_by_plugin)

            if progress_callback:
                progress_callback("Loading into scene...")
            return DICOMUtils.loadLoadables(loadables_by_plugin)
        finally:
            # Data is loaded into the scene; nothing references the files.
            shutil.rmtree(temp_root, ignore_errors=True)
