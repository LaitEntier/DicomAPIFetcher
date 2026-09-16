"""
DicomApiFetcher
===============

A 3D Slicer scripted module that fetches DICOM images from a remote HTTP API.

Usage
-----

1. Open the module from the *DICOM* category.
2. Configure the API base URL (you will be prompted the first time you click
   **Fetch** if none is saved).
3. Choose the API strategy (and the zone for ArchiMed3-web).
4. Optionally paste an API token and select how it should be sent.
5. Click **Fetch** to list the available studies.
6. Expand a study to browse its exams, and an exam to browse its series.
7. Select one or more studies / exams / series and click **Import & Load**
   to import them into the Slicer DICOM database and load them into the
   scene.
"""

import os
import sys

import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)

# Make sibling modules importable when this file is loaded by Slicer.
module_dir = os.path.dirname(os.path.abspath(__file__))
if module_dir not in sys.path:
    sys.path.insert(0, module_dir)

from DicomApiFetcherLib.ApiClient import (
    ArchiMedApiClient,
    ManifestApiClient,
    ZipApiClient,
)
from DicomApiFetcherLib.DicomApiFetcherLogic import DicomApiFetcherLogic


# -----------------------------------------------------------------------------
# Module
# -----------------------------------------------------------------------------
class DicomApiFetcher(ScriptedLoadableModule):
    """Module class registered by Slicer."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = "DICOM API Fetcher"
        self.parent.categories = ["DICOM"]
        self.parent.dependencies = []
        self.parent.contributors = [ '<a href="https://github.com/LaitEntier">Lait Entier</a>']
        self.parent.helpText = (
            "Fetch DICOM images from a remote HTTP API and load them into Slicer."
        )
        self.parent.acknowledgementText = (
            "This module was developed as a custom Slicer extension."
        )


# -----------------------------------------------------------------------------
# Widget
# -----------------------------------------------------------------------------
class DicomApiFetcherWidget(ScriptedLoadableModuleWidget):
    """Widget shown in the Slicer module panel."""

    SETTINGS_BASE_URL = "DicomApiFetcher/baseUrl"
    SETTINGS_CLIENT_TYPE = "DicomApiFetcher/clientType"
    SETTINGS_LIST_ENDPOINT = "DicomApiFetcher/listEndpoint"
    SETTINGS_FETCH_ENDPOINT = "DicomApiFetcher/fetchEndpoint"
    SETTINGS_TOKEN = "DicomApiFetcher/token"
    SETTINGS_TOKEN_MODE = "DicomApiFetcher/tokenMode"
    SETTINGS_TOKEN_NAME = "DicomApiFetcher/tokenName"
    SETTINGS_IGNORE_SSL = "DicomApiFetcher/ignoreSslErrors"
    SETTINGS_ZONE = "DicomApiFetcher/zone"

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.logic = DicomApiFetcherLogic()

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        # Main layout
        self.layout.setContentsMargins(8, 8, 8, 8)
        self.layout.setSpacing(6)

        # --- Configuration group ---
        config_group = qt.QGroupBox("API configuration")
        config_layout = qt.QFormLayout(config_group)

        self.baseUrlLineEdit = qt.QLineEdit()
        self.baseUrlLineEdit.setPlaceholderText(
            "https://recharme-pr01:8443/ArchiMed3-web"
        )
        config_layout.addRow("API base URL:", self.baseUrlLineEdit)

        self.ignoreSslCheckBox = qt.QCheckBox(
            "Ignore SSL certificate errors (self-signed certificate)"
        )
        self.ignoreSslCheckBox.setChecked(True)
        config_layout.addRow(self.ignoreSslCheckBox)

        self.clientTypeComboBox = qt.QComboBox()
        self.clientTypeComboBox.addItem("ZIP archive", "zip")
        self.clientTypeComboBox.addItem("JSON manifest", "manifest")
        self.clientTypeComboBox.addItem("ArchiMed3-web", "archimed")
        config_layout.addRow("API strategy:", self.clientTypeComboBox)

        self.zoneLabel = qt.QLabel("Zone:")
        self.zoneComboBox = qt.QComboBox()
        self.zoneComboBox.addItem("final", "final")
        self.zoneComboBox.addItem("trash", "trash")
        config_layout.addRow(self.zoneLabel, self.zoneComboBox)

        self.listEndpointLabel = qt.QLabel("List endpoint:")
        self.listEndpointLineEdit = qt.QLineEdit()
        self.listEndpointLineEdit.setPlaceholderText("/api/db/final/studies")
        config_layout.addRow(self.listEndpointLabel, self.listEndpointLineEdit)

        self.fetchEndpointLabel = qt.QLabel("Fetch endpoint:")
        self.fetchEndpointLineEdit = qt.QLineEdit()
        self.fetchEndpointLineEdit.setPlaceholderText(
            "/studies/{id}/download or /api/db/final/studies/{id}"
        )
        config_layout.addRow(self.fetchEndpointLabel, self.fetchEndpointLineEdit)

        self.layout.addWidget(config_group)

        # --- Login group ---
        login_group = qt.QGroupBox("Login")
        login_layout = qt.QFormLayout(login_group)

        self.usernameLineEdit = qt.QLineEdit()
        self.usernameLineEdit.setPlaceholderText("Username")
        login_layout.addRow("Username:", self.usernameLineEdit)

        self.passwordLineEdit = qt.QLineEdit()
        self.passwordLineEdit.setPlaceholderText("Password")
        self.passwordLineEdit.setEchoMode(qt.QLineEdit.Password)
        login_layout.addRow("Password:", self.passwordLineEdit)

        self.loginButton = qt.QPushButton("Login")
        login_layout.addRow(self.loginButton)

        self.layout.addWidget(login_group)

        # --- Token group ---
        token_group = qt.QGroupBox("Authentication (optional)")
        token_layout = qt.QFormLayout(token_group)

        self.tokenLineEdit = qt.QLineEdit()
        self.tokenLineEdit.setPlaceholderText("Paste your API token here")
        self.tokenLineEdit.setEchoMode(qt.QLineEdit.Password)
        token_layout.addRow("API token:", self.tokenLineEdit)

        self.tokenModeComboBox = qt.QComboBox()
        self.tokenModeComboBox.addItem("No token", "none")
        self.tokenModeComboBox.addItem("Authorization: Bearer header", "bearer")
        self.tokenModeComboBox.addItem("Custom header", "header")
        self.tokenModeComboBox.addItem("Query parameter", "query")
        token_layout.addRow("Send token as:", self.tokenModeComboBox)

        self.tokenNameLineEdit = qt.QLineEdit()
        self.tokenNameLineEdit.setPlaceholderText(
            "Authorization (raw token) / X-API-Key / token"
        )
        token_layout.addRow("Header/query name:", self.tokenNameLineEdit)

        self.showTokenCheckBox = qt.QCheckBox("Show token")
        self.showTokenCheckBox.connect("stateChanged(int)", self.onShowTokenChanged)
        token_layout.addRow(self.showTokenCheckBox)

        self.layout.addWidget(token_group)

        # --- Fetch / browse section ---
        self.fetchButton = qt.QPushButton("Fetch studies / series")
        self.layout.addWidget(self.fetchButton)

        self.itemsTreeWidget = qt.QTreeWidget()
        self.itemsTreeWidget.setHeaderLabel("Studies / Exams / Series")
        self.itemsTreeWidget.setSelectionMode(
            qt.QAbstractItemView.ExtendedSelection
        )
        self.layout.addWidget(self.itemsTreeWidget)

        self.importButton = qt.QPushButton("Import & Load selected")
        self.importButton.setEnabled(False)
        self.layout.addWidget(self.importButton)

        # --- Progress / status ---
        self.progressBar = qt.QProgressBar()
        self.progressBar.setRange(0, 0)
        self.progressBar.setVisible(False)
        self.layout.addWidget(self.progressBar)

        self.statusLabel = qt.QLabel("Ready")
        self.statusLabel.wordWrap = True
        self.layout.addWidget(self.statusLabel)

        self.layout.addStretch(1)

        # --- Connections ---
        self.loginButton.connect("clicked(bool)", self.onLoginButton)
        self.fetchButton.connect("clicked(bool)", self.onFetchButton)
        self.importButton.connect("clicked(bool)", self.onImportButton)
        self.clientTypeComboBox.connect(
            "currentIndexChanged(int)", self.onStrategyChanged
        )
        self.itemsTreeWidget.connect(
            "itemExpanded(QTreeWidgetItem*)", self.onItemExpanded
        )
        self.itemsTreeWidget.connect(
            "itemSelectionChanged()", self.onItemSelectionChanged
        )

        self._loadSettings()
        self._updateStrategyVisibility()

    def _loadSettings(self):
        """Restore persisted settings."""
        settings = qt.QSettings()
        self.baseUrlLineEdit.text = settings.value(self.SETTINGS_BASE_URL, "")
        client_type = settings.value(self.SETTINGS_CLIENT_TYPE, "zip")
        for index in range(self.clientTypeComboBox.count):
            if self.clientTypeComboBox.itemData(index) == client_type:
                self.clientTypeComboBox.currentIndex = index
                break
        self.listEndpointLineEdit.text = settings.value(
            self.SETTINGS_LIST_ENDPOINT, ""
        )
        self.fetchEndpointLineEdit.text = settings.value(
            self.SETTINGS_FETCH_ENDPOINT, ""
        )
        self.tokenLineEdit.text = settings.value(self.SETTINGS_TOKEN, "")
        token_mode = settings.value(self.SETTINGS_TOKEN_MODE, "none")
        token_mode_index = self._tokenModeIndex(token_mode)
        self.tokenModeComboBox.currentIndex = token_mode_index
        self.tokenNameLineEdit.text = settings.value(self.SETTINGS_TOKEN_NAME, "")
        ignore_ssl = settings.value(self.SETTINGS_IGNORE_SSL, "true")
        self.ignoreSslCheckBox.setChecked(str(ignore_ssl).lower() != "false")
        zone = settings.value(self.SETTINGS_ZONE, "final")
        zone_index = self.zoneComboBox.findData(zone)
        if zone_index >= 0:
            self.zoneComboBox.currentIndex = zone_index

    def _saveSettings(self):
        """Persist current settings."""
        settings = qt.QSettings()
        settings.setValue(self.SETTINGS_BASE_URL, self.baseUrlLineEdit.text)
        settings.setValue(
            self.SETTINGS_CLIENT_TYPE,
            self.clientTypeComboBox.currentData,
        )
        settings.setValue(
            self.SETTINGS_LIST_ENDPOINT, self.listEndpointLineEdit.text
        )
        settings.setValue(
            self.SETTINGS_FETCH_ENDPOINT, self.fetchEndpointLineEdit.text
        )
        settings.setValue(self.SETTINGS_TOKEN, self.tokenLineEdit.text)
        settings.setValue(
            self.SETTINGS_TOKEN_MODE, self.tokenModeComboBox.currentData
        )
        settings.setValue(self.SETTINGS_TOKEN_NAME, self.tokenNameLineEdit.text)
        settings.setValue(
            self.SETTINGS_IGNORE_SSL,
            "true" if self.ignoreSslCheckBox.isChecked() else "false",
        )
        settings.setValue(self.SETTINGS_ZONE, self.zoneComboBox.currentData)

    def _tokenModeIndex(self, mode):
        """Return the combo-box index for *mode*."""
        for index in range(self.tokenModeComboBox.count):
            if self.tokenModeComboBox.itemData(index) == mode:
                return index
        return 0

    def _currentClient(self):
        """Return an ApiClient instance matching the selected strategy and token."""
        client_type = self.clientTypeComboBox.currentData
        if client_type == "archimed":
            client = ArchiMedApiClient()
        elif client_type == "manifest":
            client = ManifestApiClient()
        else:
            client = ZipApiClient()

        client.token = self.tokenLineEdit.text.strip()
        client.token_mode = self.tokenModeComboBox.currentData
        client.token_name = self.tokenNameLineEdit.text.strip()
        client.verify_ssl = not self.ignoreSslCheckBox.isChecked()
        if isinstance(client, ArchiMedApiClient):
            client.zone = self.zoneComboBox.currentData
        return client

    def _ensureBaseUrl(self):
        """
        Return the configured base URL, prompting the user if it is empty.
        Returns ``None`` if the user cancels the dialog.
        """
        url = self.baseUrlLineEdit.text.strip()
        if url:
            return url.rstrip("/")

        text, ok = qt.QInputDialog.getText(
            self.parent,
            "API base URL",
            "Enter the API base URL:",
            qt.QLineEdit.Normal,
            "",
        )
        if not ok or not text.strip():
            return None

        url = text.strip().rstrip("/")
        self.baseUrlLineEdit.text = url
        self._saveSettings()
        return url

    def _setBusy(self, busy):
        """Enable/disable interactive controls during operations."""
        self.loginButton.setEnabled(not busy)
        self.fetchButton.setEnabled(not busy)
        self.importButton.setEnabled(
            not busy and len(self._selectedImportableItems()) > 0
        )
        self.progressBar.setVisible(busy)

    def _updateProgress(self, message):
        """Update the status label and process pending UI events."""
        self.statusLabel.text = message
        slicer.app.processEvents()

    def onShowTokenChanged(self, state):
        """Toggle token visibility."""
        if state == qt.Qt.Checked:
            self.tokenLineEdit.setEchoMode(qt.QLineEdit.Normal)
        else:
            self.tokenLineEdit.setEchoMode(qt.QLineEdit.Password)

    def onStrategyChanged(self, index):
        """Show/hide strategy-specific configuration rows."""
        self._updateStrategyVisibility()

    def _updateStrategyVisibility(self):
        """Show the zone row for ArchiMed, endpoint rows otherwise."""
        is_archimed = self.clientTypeComboBox.currentData == "archimed"
        self.zoneLabel.setVisible(is_archimed)
        self.zoneComboBox.setVisible(is_archimed)
        self.listEndpointLabel.setVisible(not is_archimed)
        self.listEndpointLineEdit.setVisible(not is_archimed)
        self.fetchEndpointLabel.setVisible(not is_archimed)
        self.fetchEndpointLineEdit.setVisible(not is_archimed)

    def onItemSelectionChanged(self):
        """Enable the Import button only when an item is selected."""
        self.importButton.setEnabled(len(self._selectedImportableItems()) > 0)

    # ------------------------------------------------------------------
    # Tree helpers
    # ------------------------------------------------------------------
    def _selectedImportableItems(self):
        """Selected tree items that represent downloadable data."""
        result = []
        for item in self.itemsTreeWidget.selectedItems():
            data = item.data(0, qt.Qt.UserRole)
            if (
                isinstance(data, dict)
                and data.get("level") not in (None, "loading", "empty")
            ):
                result.append(item)
        return result

    @staticmethod
    def _filterNestedSelection(items):
        """Drop selected items whose ancestor is also selected."""
        selected_ids = {id(item) for item in items}

        def has_selected_ancestor(item):
            parent = item.parent()
            while parent is not None:
                if id(parent) in selected_ids:
                    return True
                parent = parent.parent()
            return False

        return [item for item in items if not has_selected_ancestor(item)]

    @staticmethod
    def _addLoadingDummy(parent_item):
        """Add a placeholder child so the node shows an expand arrow."""
        dummy = qt.QTreeWidgetItem(parent_item)
        dummy.setText(0, "Expand to load...")
        dummy.setData(0, qt.Qt.UserRole, {"level": "loading"})
        return dummy

    @staticmethod
    def _formatStudyText(study):
        study_id = study.get("studyID", "?")
        description = study.get("studyDescription") or study.get("studyCode")
        text = f"{description} (ID {study_id})" if description else f"Study {study_id}"
        nb_exams = study.get("nbExams")
        if nb_exams is not None:
            text += f" - {nb_exams} exam(s)"
        return text

    @staticmethod
    def _formatExamText(exam):
        exam_id = exam.get("examID", "?")
        parts = [p for p in (exam.get("examCode"), exam.get("examDescription")) if p]
        text = " - ".join(parts) if parts else f"Exam {exam_id}"
        date = exam.get("examDate")
        if date:
            text += f" ({date})"
        return text

    @staticmethod
    def _formatSerieText(serie):
        serie_id = serie.get("serieID", "?")
        number = serie.get("serieNumber")
        text = f"S{number}" if number is not None else f"Serie {serie_id}"
        description = serie.get("serieDescription")
        if description:
            text += f" - {description}"
        details = []
        nb_files = serie.get("nbFiles")
        if nb_files is not None:
            details.append(f"{nb_files} file(s)")
        types = serie.get("distinctsFileTypeAcronyms") or []
        if types:
            details.append(", ".join(str(t) for t in types))
        if details:
            text += f" [{'; '.join(details)}]"
        return text

    def _addStudyNode(self, study):
        """Add a lazily-loaded study node at the top level of the tree."""
        if not isinstance(study, dict):
            study = {"studyID": study}
        item = qt.QTreeWidgetItem(self.itemsTreeWidget)
        item.setText(0, self._formatStudyText(study))
        item.setData(
            0,
            qt.Qt.UserRole,
            {
                "level": "study",
                "strategy": "archimed",
                "studyID": study.get("studyID"),
            },
        )
        self._addLoadingDummy(item)
        return item

    def _addExamNode(self, parent_item, exam):
        """Add a lazily-loaded exam node under a study node."""
        if not isinstance(exam, dict):
            exam = {"examID": exam}
        study_id = parent_item.data(0, qt.Qt.UserRole).get("studyID")
        item = qt.QTreeWidgetItem(parent_item)
        item.setText(0, self._formatExamText(exam))
        item.setData(
            0,
            qt.Qt.UserRole,
            {
                "level": "exam",
                "strategy": "archimed",
                "studyID": study_id,
                "examID": exam.get("examID"),
            },
        )
        self._addLoadingDummy(item)
        return item

    def _addSerieNode(self, parent_item, serie):
        """Add a serie (leaf) node under an exam node."""
        if not isinstance(serie, dict):
            serie = {"serieID": serie}
        parent_data = parent_item.data(0, qt.Qt.UserRole)
        item = qt.QTreeWidgetItem(parent_item)
        item.setText(0, self._formatSerieText(serie))
        item.setData(
            0,
            qt.Qt.UserRole,
            {
                "level": "serie",
                "strategy": "archimed",
                "studyID": parent_data.get("studyID"),
                "examID": parent_data.get("examID"),
                "serieID": serie.get("serieID"),
            },
        )
        return item

    def onItemExpanded(self, item):
        """Lazily load the children of an expanded study/exam node."""
        data = item.data(0, qt.Qt.UserRole)
        if not isinstance(data, dict):
            return
        level = data.get("level")
        if level not in ("study", "exam") or item.childCount() != 1:
            return

        child = item.child(0)
        child_data = child.data(0, qt.Qt.UserRole)
        if not (
            isinstance(child_data, dict) and child_data.get("level") == "loading"
        ):
            return

        base_url = self._ensureBaseUrl()
        if not base_url:
            return

        self._setBusy(True)
        try:
            client = self._currentClient()
            item.removeChild(child)
            if level == "study":
                self.statusLabel.text = "Fetching exams..."
                slicer.app.processEvents()
                exams = client.list_exams(base_url, data["studyID"])
                for exam in exams:
                    self._addExamNode(item, exam)
                self.statusLabel.text = f"Found {len(exams)} exam(s)."
            else:
                self.statusLabel.text = "Fetching series..."
                slicer.app.processEvents()
                series = client.list_series(
                    base_url, data["studyID"], data["examID"]
                )
                for serie in series:
                    self._addSerieNode(item, serie)
                self.statusLabel.text = f"Found {len(series)} serie(s)."

            if item.childCount() == 0:
                empty = qt.QTreeWidgetItem(item)
                empty.setText(0, "(empty)")
                empty.setData(0, qt.Qt.UserRole, {"level": "empty"})
        except Exception as e:
            if item.childCount() == 0:
                self._addLoadingDummy(item)  # allow the user to retry
            self.statusLabel.text = f"Error: {e}"
            slicer.util.errorDisplay(f"Failed to expand item:\n{e}")
        finally:
            self._setBusy(False)

    def onLoginButton(self):
        """Log in to the API and store the returned token."""
        base_url = self._ensureBaseUrl()
        if not base_url:
            return

        username = self.usernameLineEdit.text.strip()
        password = self.passwordLineEdit.text
        if not username or not password:
            slicer.util.warningDisplay("Please enter a username and password.")
            return

        self._setBusy(True)
        self.statusLabel.text = "Logging in..."

        try:
            client = self._currentClient()
            token = client.login(base_url, username, password)

            # ArchiMed expects the raw token in the Authorization header.
            self.tokenLineEdit.text = token
            self.tokenModeComboBox.currentIndex = self._tokenModeIndex("header")
            self.tokenNameLineEdit.text = "Authorization"
            self._saveSettings()

            self.statusLabel.text = "Login successful."
        except Exception as e:
            self.statusLabel.text = f"Login failed: {e}"
            slicer.util.errorDisplay(f"Login failed:\n{e}")
        finally:
            self._setBusy(False)

    def onFetchButton(self):
        """List available items from the API."""
        base_url = self._ensureBaseUrl()
        if not base_url:
            return

        self._saveSettings()
        self._setBusy(True)
        self.statusLabel.text = "Fetching list..."
        self.itemsTreeWidget.clear()

        try:
            client = self._currentClient()
            if self.clientTypeComboBox.currentData == "archimed":
                studies = client.list_studies(base_url)
                for study in studies:
                    self._addStudyNode(study)
                self.statusLabel.text = (
                    f"Found {len(studies)} study/studies. "
                    "Expand a study to browse its exams."
                )
            else:
                list_endpoint = self.listEndpointLineEdit.text.strip() or None
                items = self.logic.listItems(client, base_url, list_endpoint)
                for item in items:
                    display = f"{item['description']} ({item['id']})"
                    node = qt.QTreeWidgetItem(self.itemsTreeWidget)
                    node.setText(0, display)
                    node.setData(
                        0,
                        qt.Qt.UserRole,
                        {
                            "level": "item",
                            "strategy": self.clientTypeComboBox.currentData,
                            "id": item["id"],
                        },
                    )
                self.statusLabel.text = f"Found {len(items)} item(s)."
        except Exception as e:
            self.statusLabel.text = f"Error: {e}"
            slicer.util.errorDisplay(f"Failed to fetch item list:\n{e}")
        finally:
            self._setBusy(False)

    def onImportButton(self):
        """Download, import, and load the selected item(s)."""
        selected = self._filterNestedSelection(self._selectedImportableItems())
        if not selected:
            slicer.util.warningDisplay("Please select an item to import.")
            return

        base_url = self._ensureBaseUrl()
        if not base_url:
            return

        self._saveSettings()
        self._setBusy(True)

        try:
            client = self._currentClient()
            node_data = [item.data(0, qt.Qt.UserRole) for item in selected]
            if node_data[0].get("strategy") == "archimed":
                loaded_node_ids = self.logic.fetchAndLoad(
                    client,
                    base_url,
                    archimed_nodes=node_data,
                    progress_callback=self._updateProgress,
                )
            else:
                item_ids = [data["id"] for data in node_data]
                fetch_endpoint = self.fetchEndpointLineEdit.text.strip() or None
                loaded_node_ids = self.logic.fetchAndLoad(
                    client,
                    base_url,
                    item_ids=item_ids,
                    fetch_endpoint=fetch_endpoint,
                    progress_callback=self._updateProgress,
                )
            if loaded_node_ids:
                self.statusLabel.text = (
                    f"Loaded {len(loaded_node_ids)} node(s) into the scene."
                )
            else:
                self.statusLabel.text = (
                    "Import done. No new patient to load "
                    "(data may already be in the DICOM database)."
                )
        except Exception as e:
            self.statusLabel.text = f"Error: {e}"
            slicer.util.errorDisplay(f"Failed to import/load DICOM data:\n{e}")
        finally:
            self._setBusy(False)


# -----------------------------------------------------------------------------
# Test
# -----------------------------------------------------------------------------
class DicomApiFetcherTest(ScriptedLoadableModuleTest):
    """Basic self-test for the module."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def runTest(self):
        self.setUp()
        self.delayDisplay("DicomApiFetcher test passed (placeholder).")
