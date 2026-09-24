"""
DicomApiFetcher
===============

A 3D Slicer scripted module that fetches DICOM images from a remote HTTP API.

Usage
-----

1. Open the module from the *DICOM* category.
2. Enter the API base URL, your username and password, then click **Login**
   (the returned token is stored and reused automatically).
3. Click **Fetch** to list the available studies.
4. Expand a study to browse its exams, and an exam to browse its series.
5. Select one or more studies / exams / series and click **Import & Load**
   to load them directly into the scene (nothing is stored in the Slicer
   DICOM database).

Advanced settings (API strategy, zone, endpoints, SSL, token handling) are
hidden behind the collapsed **Developer mode** section and default to the
ArchiMed3-web configuration.
"""

import os
import queue
import shutil
import sys
import threading
import time
import traceback

import qt
import ctk
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
from DicomApiFetcherLib.DownloadCache import DownloadCache


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
    SETTINGS_CACHE_ENABLED = "DicomApiFetcher/cacheEnabled"

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        self.logic = DicomApiFetcherLogic()
        self._lastProgressEventTime = 0.0
        self._downloadWorker = None
        self._downloadQueue = None
        self._downloadTimer = None

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        # Main layout
        self.layout.setContentsMargins(8, 8, 8, 8)
        self.layout.setSpacing(6)

        # --- Connection group ---
        connection_group = qt.QGroupBox("Connection")
        connection_layout = qt.QFormLayout(connection_group)

        self.baseUrlLineEdit = qt.QLineEdit()
        self.baseUrlLineEdit.setPlaceholderText(
            "https://recharme-pr01:8443/ArchiMed3-web"
        )
        connection_layout.addRow("API base URL:", self.baseUrlLineEdit)

        self.usernameLineEdit = qt.QLineEdit()
        self.usernameLineEdit.setPlaceholderText("Username")
        connection_layout.addRow("Username:", self.usernameLineEdit)

        self.passwordLineEdit = qt.QLineEdit()
        self.passwordLineEdit.setPlaceholderText("Password")
        self.passwordLineEdit.setEchoMode(qt.QLineEdit.Password)
        connection_layout.addRow("Password:", self.passwordLineEdit)

        self.loginButton = qt.QPushButton("Login")
        connection_layout.addRow(self.loginButton)

        self.layout.addWidget(connection_group)

        # --- Developer mode (collapsible, hidden by default) ---
        self.developerCollapsibleButton = ctk.ctkCollapsibleButton()
        self.developerCollapsibleButton.text = "Developer mode"
        self.developerCollapsibleButton.collapsed = True
        developer_layout = qt.QFormLayout(self.developerCollapsibleButton)

        # Subtle warning banner shown whenever the section is expanded
        warning_widget = qt.QWidget()
        warning_layout = qt.QHBoxLayout(warning_widget)
        warning_layout.setContentsMargins(0, 0, 0, 0)
        warning_icon = qt.QLabel()
        try:
            warning_icon.setPixmap(
                slicer.app.style()
                .standardIcon(qt.QStyle.SP_MessageBoxWarning)
                .pixmap(16, 16)
            )
        except Exception:
            warning_icon.setText("(!)")
        warning_label = qt.QLabel(
            "Advanced settings - only change these if you know what "
            "you are doing."
        )
        warning_label.setStyleSheet("font-style: italic;")
        warning_label.wordWrap = True
        warning_layout.addWidget(warning_icon)
        warning_layout.addWidget(warning_label, 1)
        developer_layout.addRow(warning_widget)

        self.ignoreSslCheckBox = qt.QCheckBox(
            "Ignore SSL certificate errors (self-signed certificate)"
        )
        self.ignoreSslCheckBox.setChecked(True)
        developer_layout.addRow(self.ignoreSslCheckBox)

        self.clientTypeComboBox = qt.QComboBox()
        self.clientTypeComboBox.addItem("ZIP archive", "zip")
        self.clientTypeComboBox.addItem("JSON manifest", "manifest")
        self.clientTypeComboBox.addItem("ArchiMed3-web", "archimed")
        developer_layout.addRow("API strategy:", self.clientTypeComboBox)

        self.zoneLabel = qt.QLabel("Zone:")
        self.zoneComboBox = qt.QComboBox()
        self.zoneComboBox.addItem("final", "final")
        self.zoneComboBox.addItem("trash", "trash")
        developer_layout.addRow(self.zoneLabel, self.zoneComboBox)

        self.listEndpointLabel = qt.QLabel("List endpoint:")
        self.listEndpointLineEdit = qt.QLineEdit()
        self.listEndpointLineEdit.setPlaceholderText("/api/db/final/studies")
        developer_layout.addRow(self.listEndpointLabel, self.listEndpointLineEdit)

        self.fetchEndpointLabel = qt.QLabel("Fetch endpoint:")
        self.fetchEndpointLineEdit = qt.QLineEdit()
        self.fetchEndpointLineEdit.setPlaceholderText(
            "/studies/{id}/download or /api/db/final/studies/{id}"
        )
        developer_layout.addRow(self.fetchEndpointLabel, self.fetchEndpointLineEdit)

        self.tokenLineEdit = qt.QLineEdit()
        self.tokenLineEdit.setPlaceholderText("Filled automatically after login")
        self.tokenLineEdit.setEchoMode(qt.QLineEdit.Password)
        developer_layout.addRow("API token:", self.tokenLineEdit)

        self.tokenModeComboBox = qt.QComboBox()
        self.tokenModeComboBox.addItem("No token", "none")
        self.tokenModeComboBox.addItem("Authorization: Bearer header", "bearer")
        self.tokenModeComboBox.addItem("Custom header", "header")
        self.tokenModeComboBox.addItem("Query parameter", "query")
        developer_layout.addRow("Send token as:", self.tokenModeComboBox)

        self.tokenNameLineEdit = qt.QLineEdit()
        self.tokenNameLineEdit.setPlaceholderText(
            "Authorization (raw token) / X-API-Key / token"
        )
        developer_layout.addRow("Header/query name:", self.tokenNameLineEdit)

        self.showTokenCheckBox = qt.QCheckBox("Show token")
        self.showTokenCheckBox.connect("stateChanged(int)", self.onShowTokenChanged)
        developer_layout.addRow(self.showTokenCheckBox)

        self.layout.addWidget(self.developerCollapsibleButton)

        # --- Local cache ---
        cache_group = qt.QGroupBox("Local cache")
        cache_layout = qt.QHBoxLayout(cache_group)
        self.cacheEnabledCheckBox = qt.QCheckBox("Cache downloaded DICOM files")
        self.cacheEnabledCheckBox.setChecked(True)
        self.cacheEnabledCheckBox.setToolTip(
            "Reuse completed downloads in Slicer's user data directory. "
            "Cached DICOM files are stored locally and are not encrypted."
        )
        cache_layout.addWidget(self.cacheEnabledCheckBox, 1)

        self.clearCacheButton = qt.QPushButton("Clear cache")
        self.clearCacheButton.setToolTip(
            "Delete all DICOM files cached by this module on this machine."
        )
        cache_layout.addWidget(self.clearCacheButton)
        self.layout.addWidget(cache_group)

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
        self.clearCacheButton.connect("clicked(bool)", self.onClearCacheButton)
        self.cacheEnabledCheckBox.connect(
            "stateChanged(int)", self.onCacheEnabledChanged
        )
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

    def cleanup(self):
        """Stop UI polling when the module widget is destroyed/reloaded."""
        if self._downloadTimer:
            self._downloadTimer.stop()
            self._downloadTimer = None
        ScriptedLoadableModuleWidget.cleanup(self)

    def _loadSettings(self):
        """Restore persisted settings."""
        settings = qt.QSettings()
        self.baseUrlLineEdit.text = settings.value(self.SETTINGS_BASE_URL, "")
        client_type = settings.value(self.SETTINGS_CLIENT_TYPE, "archimed")
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
        token_mode = settings.value(self.SETTINGS_TOKEN_MODE, "header")
        token_mode_index = self._tokenModeIndex(token_mode)
        self.tokenModeComboBox.currentIndex = token_mode_index
        self.tokenNameLineEdit.text = settings.value(
            self.SETTINGS_TOKEN_NAME, "Authorization"
        )
        ignore_ssl = settings.value(self.SETTINGS_IGNORE_SSL, "true")
        self.ignoreSslCheckBox.setChecked(str(ignore_ssl).lower() != "false")
        cache_enabled = settings.value(self.SETTINGS_CACHE_ENABLED, "true")
        self.cacheEnabledCheckBox.blockSignals(True)
        self.cacheEnabledCheckBox.setChecked(
            str(cache_enabled).lower() != "false"
        )
        self.cacheEnabledCheckBox.blockSignals(False)
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
        settings.setValue(
            self.SETTINGS_CACHE_ENABLED,
            "true" if self.cacheEnabledCheckBox.isChecked() else "false",
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
        self.itemsTreeWidget.setEnabled(not busy)
        self.cacheEnabledCheckBox.setEnabled(not busy)
        self.clearCacheButton.setEnabled(not busy)

    def _updateProgress(self, message, force=False):
        """Update status while limiting expensive UI event processing."""
        self.statusLabel.text = message
        now = time.monotonic()
        if force or now - self._lastProgressEventTime >= 0.15:
            self._lastProgressEventTime = now
            slicer.app.processEvents()

    def onCacheEnabledChanged(self, _state):
        """Persist the cache preference immediately."""
        self._saveSettings()

    def onClearCacheButton(self):
        """Delete all cached downloads after user confirmation."""
        if self._downloadWorker and self._downloadWorker.is_alive():
            slicer.util.warningDisplay(
                "Please wait for the current download to finish."
            )
            return

        cache_dir = self.logic.defaultCacheDirectory()
        confirmed = slicer.util.confirmYesNoDisplay(
            "Delete all DICOM files cached by this module?\n\n"
            f"Cache directory:\n{cache_dir}",
            "Clear DICOM API Fetcher cache",
        )
        if not confirmed:
            return

        try:
            DownloadCache(cache_dir).clear()
            self.statusLabel.text = "Download cache cleared."
        except Exception as e:
            self.statusLabel.text = f"Error clearing cache: {e}"
            slicer.util.errorDisplay(f"Failed to clear cache:\n{e}")

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
        """Download selected data in the background, then load it in Slicer."""
        selected = self._filterNestedSelection(self._selectedImportableItems())
        if not selected:
            slicer.util.warningDisplay("Please select an item to import.")
            return

        base_url = self._ensureBaseUrl()
        if not base_url:
            return

        self._saveSettings()
        self._setBusy(True)

        client = self._currentClient()
        node_data = [item.data(0, qt.Qt.UserRole) for item in selected]
        if node_data[0].get("strategy") == "archimed":
            archimed_nodes = node_data
            item_ids = None
            fetch_endpoint = None
        else:
            archimed_nodes = None
            item_ids = [data["id"] for data in node_data]
            fetch_endpoint = self.fetchEndpointLineEdit.text.strip() or None

        cache_dir = (
            self.logic.defaultCacheDirectory()
            if self.cacheEnabledCheckBox.isChecked()
            else None
        )
        self._downloadQueue = queue.Queue()
        self._downloadWorker = threading.Thread(
            target=self._downloadWorkerMain,
            args=(
                self.logic,
                self._downloadQueue,
                client,
                base_url,
                item_ids,
                fetch_endpoint,
                archimed_nodes,
                cache_dir,
            ),
            daemon=True,
        )
        self._downloadTimer = qt.QTimer()
        self._downloadTimer.setInterval(100)
        self._downloadTimer.connect("timeout()", self._pollDownloadQueue)
        self._downloadTimer.start()
        self._updateProgress("Starting DICOM download...", force=True)
        self._downloadWorker.start()

    @staticmethod
    def _downloadWorkerMain(
        logic,
        download_queue,
        client,
        base_url,
        item_ids,
        fetch_endpoint,
        archimed_nodes,
        cache_dir,
    ):
        """Run network/cache work without touching Qt or the Slicer scene."""
        def report_progress(message):
            download_queue.put(("progress", message))

        try:
            cache_manager = DownloadCache(cache_dir) if cache_dir else None
            result = logic.fetchDicomData(
                client,
                base_url,
                item_ids=item_ids,
                fetch_endpoint=fetch_endpoint,
                archimed_nodes=archimed_nodes,
                progress_callback=report_progress,
                cache_manager=cache_manager,
            )
            download_queue.put(("success", result))
        except Exception as e:
            download_queue.put(
                ("error", (str(e), traceback.format_exc()))
            )

    def _pollDownloadQueue(self):
        """Apply worker progress and completion on Slicer's main thread."""
        download_queue = self._downloadQueue
        if download_queue is None:
            return

        latest_progress = None
        final_result = None
        while True:
            try:
                message_type, payload = download_queue.get_nowait()
            except queue.Empty:
                break
            if message_type == "progress":
                latest_progress = payload
            else:
                final_result = (message_type, payload)

        if latest_progress and final_result is None:
            # The timer is already running on the UI thread; avoid re-entering
            # the event loop for every background progress message.
            self.statusLabel.text = latest_progress

        if final_result is None:
            return

        if self._downloadTimer:
            self._downloadTimer.stop()

        message_type, payload = final_result
        if message_type == "error":
            message, details = payload
            print(details)
            self.statusLabel.text = f"Error: {message}"
            slicer.util.errorDisplay(
                f"Failed to download DICOM data:\n{message}"
            )
            self._resetDownloadState()
            self._setBusy(False)
            return

        self._finishImport(payload)

    def _finishImport(self, download_result):
        """Load downloaded data on the main thread and clean temporary data."""
        dicom_dirs, temp_root = download_result
        try:
            loaded_node_ids = self.logic.loadDicomData(
                dicom_dirs, progress_callback=self._updateProgress
            )
            if loaded_node_ids:
                self.statusLabel.text = (
                    f"Loaded {len(loaded_node_ids)} node(s) into the scene."
                )
            else:
                self.statusLabel.text = (
                    "Loading finished but produced no displayable data."
                )
        except Exception as e:
            self.statusLabel.text = f"Error: {e}"
            slicer.util.errorDisplay(f"Failed to import/load DICOM data:\n{e}")
        finally:
            if temp_root:
                shutil.rmtree(temp_root, ignore_errors=True)
            self._resetDownloadState()
            self._setBusy(False)

    def _resetDownloadState(self):
        """Release completed download worker state."""
        self._downloadWorker = None
        self._downloadQueue = None
        self._downloadTimer = None


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
