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
        item_id,
        fetch_endpoint=None,
        progress_callback=None,
    ):
        """
        Download the DICOM files for *item_id* using *client*, import them into
        the Slicer DICOM database, and load the patient(s) into the scene.

        Returns the list of loaded node IDs.
        """
        temp_root = tempfile.mkdtemp(prefix="dicom_api_fetcher_")
        try:
            if progress_callback:
                progress_callback("Downloading DICOM data...")
            dicom_dir = client.fetch_item(
                base_url, item_id, temp_root, fetch_endpoint
            )

            if not os.path.isdir(dicom_dir):
                raise RuntimeError(
                    f"Download did not produce a DICOM folder: {dicom_dir}"
                )

            if progress_callback:
                progress_callback("Importing into DICOM database...")
            self._ensureDicomDatabase()
            DICOMUtils.importDicom(dicom_dir, slicer.dicomDatabase)

            if progress_callback:
                progress_callback("Loading into scene...")
            patient_uids = slicer.dicomDatabase.patients()
            if not patient_uids:
                raise RuntimeError("No DICOM patients found after import.")

            loaded_node_ids = []
            for uid in patient_uids:
                loaded_node_ids.extend(DICOMUtils.loadPatientByUID(uid))

            return loaded_node_ids
        except Exception:
            # Clean up the temporary download folder on any error.
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

    def _ensureDicomDatabase(self):
        """Make sure Slicer's DICOM database is available."""
        if slicer.dicomDatabase is not None:
            return

        # Opening the DICOM module initializes the database.
        slicer.util.selectModule("DICOM")
        slicer.app.processEvents()

        if slicer.dicomDatabase is None:
            raise RuntimeError("Unable to initialize the Slicer DICOM database.")
