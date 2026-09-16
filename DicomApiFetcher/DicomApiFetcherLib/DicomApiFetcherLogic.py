"""
DicomApiFetcherLogic
====================

Logic class for the DICOM API Fetcher module.

It connects the generic ``ApiClient`` layer with Slicer's DICOM infrastructure:

* download data for a selected item,
* import the DICOM files into Slicer's DICOM database,
* load the imported patient(s) into the scene.
"""

import os
import shutil
import tempfile

import slicer
from DICOMLib import DICOMUtils
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic


def _seriesInstanceUIDs(dicom_dir):
    """
    Return the unique SeriesInstanceUIDs found in the files under
    *dicom_dir*. Non-DICOM files are silently skipped.
    """
    import pydicom

    uids = []
    for root, _dirs, files in os.walk(dicom_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                dataset = pydicom.dcmread(path, stop_before_pixels=True)
            except Exception:
                continue
            uid = getattr(dataset, "SeriesInstanceUID", None)
            if uid and str(uid) not in uids:
                uids.append(str(uid))
    return uids


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
        Download DICOM data using *client*, import it into the Slicer DICOM
        database, and load the downloaded series into the scene.

        Two download modes:

        * ``archimed_nodes`` - a list of node descriptors
          ``{"level": "study"|"exam"|"serie", "studyID", "examID",
          "serieID"}`` downloaded through the ArchiMed hierarchy methods.
        * ``item_ids`` - a list of flat item IDs downloaded through the
          generic ``fetch_item`` interface (ZIP/manifest strategies).

        Loading targets exactly the downloaded series (by SeriesInstanceUID),
        so re-importing data that is already in the database still displays
        it. For non-DICOM downloads it falls back to loading only newly
        imported patients.

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

            if progress_callback:
                progress_callback("Importing into DICOM database...")
            # Remember which series we downloaded so we can load exactly
            # those, even if their patient was already in the database.
            series_uids = _seriesInstanceUIDs(dicom_dir)
            self._ensureDicomDatabase()
            patients_before = set(slicer.dicomDatabase.patients())
            DICOMUtils.importDicom(dicom_dir, slicer.dicomDatabase)

            if progress_callback:
                progress_callback("Loading into scene...")
            if series_uids:
                loaded_node_ids = DICOMUtils.loadSeriesByUID(series_uids)
                return loaded_node_ids or []

            # Fallback for non-DICOM downloads: load only newly imported
            # patients (never the whole database).
            new_patient_uids = [
                uid
                for uid in slicer.dicomDatabase.patients()
                if uid not in patients_before
            ]
            if not new_patient_uids:
                if progress_callback:
                    progress_callback(
                        "No new patient after import "
                        "(data may already be in the database)."
                    )
                return []

            loaded_node_ids = []
            for uid in new_patient_uids:
                loaded_node_ids.extend(DICOMUtils.loadPatientByUID(uid))

            return loaded_node_ids
        finally:
            # Imported files have been copied into the DICOM database.
            shutil.rmtree(temp_root, ignore_errors=True)

    def _ensureDicomDatabase(self):
        """Make sure Slicer's DICOM database is available."""
        if slicer.dicomDatabase is not None:
            return

        # Opening the DICOM module initializes the database.
        slicer.util.selectModule("DICOM")
        slicer.app.processEvents()

        if slicer.dicomDatabase is None:
            raise RuntimeError("Unable to initialize the Slicer DICOM database.")
