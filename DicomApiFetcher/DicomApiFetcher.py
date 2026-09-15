"""
DicomApiFetcher
===============

A 3D Slicer scripted module that fetches DICOM images from a remote HTTP API.

Usage
-----

1. Open the module from the *DICOM* category.
2. Configure the API base URL (you will be prompted the first time you click
   **Fetch** if none is saved).
3. Choose the API strategy and endpoint templates that match your API.
4. Optionally paste an API token and select how it should be sent.
5. Click **Fetch** to list available studies/series.
6. Select one item and click **Import & Load** to import it into the Slicer
   DICOM database and load it into the scene.
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

from DicomApiFetcherLib.ApiClient import ManifestApiClient, ZipApiClient
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

        self.clientTypeComboBox = qt.QComboBox()
        self.clientTypeComboBox.addItem("ZIP archive", "zip")
        self.clientTypeComboBox.addItem("JSON manifest", "manifest")
        config_layout.addRow("API strategy:", self.clientTypeComboBox)

        self.listEndpointLineEdit = qt.QLineEdit()
        self.listEndpointLineEdit.setPlaceholderText("/api/db/final/studies")
        config_layout.addRow("List endpoint:", self.listEndpointLineEdit)

        self.fetchEndpointLineEdit = qt.QLineEdit()
        self.fetchEndpointLineEdit.setPlaceholderText("/studies/{id}/download")
        config_layout.addRow("Fetch endpoint:", self.fetchEndpointLineEdit)

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

        # --- Fetch / list section ---
        self.fetchButton = qt.QPushButton("Fetch studies / series")
        self.layout.addWidget(self.fetchButton)

        self.itemsListWidget = qt.QListWidget()
        self.itemsListWidget.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.layout.addWidget(self.itemsListWidget)

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
        self.itemsListWidget.connect(
            "currentItemChanged(QListWidgetItem*, QListWidgetItem*)",
            self.onItemSelectionChanged,
        )

        self._loadSettings()

    def _loadSettings(self):
        """Restore persisted settings."""
        settings = qt.QSettings()
        self.baseUrlLineEdit.text = settings.value(self.SETTINGS_BASE_URL, "")
        client_type = settings.value(self.SETTINGS_CLIENT_TYPE, "zip")
        index = 0 if client_type == "zip" else 1
        self.clientTypeComboBox.currentIndex = index
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

    def _tokenModeIndex(self, mode):
        """Return the combo-box index for *mode*."""
        for index in range(self.tokenModeComboBox.count):
            if self.tokenModeComboBox.itemData(index) == mode:
                return index
        return 0

    def _currentClient(self):
        """Return an ApiClient instance matching the selected strategy and token."""
        client_type = self.clientTypeComboBox.currentData
        if client_type == "manifest":
            client = ManifestApiClient()
        else:
            client = ZipApiClient()

        client.token = self.tokenLineEdit.text.strip()
        client.token_mode = self.tokenModeComboBox.currentData
        client.token_name = self.tokenNameLineEdit.text.strip()
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
            not busy and self.itemsListWidget.currentItem() is not None
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

    def onItemSelectionChanged(self, current, previous):
        """Enable the Import button only when an item is selected."""
        self.importButton.setEnabled(current is not None)

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
        self.itemsListWidget.clear()

        try:
            client = self._currentClient()
            list_endpoint = self.listEndpointLineEdit.text.strip() or None
            items = self.logic.listItems(client, base_url, list_endpoint)

            for item in items:
                display = f"{item['description']} ({item['id']})"
                list_item = qt.QListWidgetItem(display)
                list_item.setData(qt.Qt.UserRole, item["id"])
                self.itemsListWidget.addItem(list_item)

            self.statusLabel.text = f"Found {len(items)} item(s)."
        except Exception as e:
            self.statusLabel.text = f"Error: {e}"
            slicer.util.errorDisplay(f"Failed to fetch item list:\n{e}")
        finally:
            self._setBusy(False)

    def onImportButton(self):
        """Download, import, and load the selected item."""
        current_item = self.itemsListWidget.currentItem()
        if current_item is None:
            slicer.util.warningDisplay("Please select an item to import.")
            return

        base_url = self._ensureBaseUrl()
        if not base_url:
            return

        item_id = current_item.data(qt.Qt.UserRole)
        self._saveSettings()
        self._setBusy(True)

        try:
            client = self._currentClient()
            fetch_endpoint = self.fetchEndpointLineEdit.text.strip() or None
            loaded_node_ids = self.logic.fetchAndLoad(
                client,
                base_url,
                item_id,
                fetch_endpoint,
                progress_callback=self._updateProgress,
            )
            self.statusLabel.text = (
                f"Loaded {len(loaded_node_ids)} node(s) into the scene."
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
