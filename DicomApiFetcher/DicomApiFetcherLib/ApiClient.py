"""
ApiClient
=========

Small adapter layer for fetching DICOM data from a remote HTTP API.

The ``BaseApiClient`` defines two operations:

* ``list_items(base_url, list_endpoint)`` - returns a list of selectable items.
* ``fetch_item(base_url, item_id, output_dir, fetch_endpoint)`` - downloads the
  DICOM files for one item into ``output_dir`` and returns the folder that
  contains the DICOM files.

Two concrete clients are provided:

* ``ZipApiClient`` - the API returns a JSON list and each item is downloaded as
  a ZIP archive of DICOM files.
* ``ManifestApiClient`` - the API returns a JSON list and a detail endpoint that
  contains a JSON manifest with a list of file URLs to download individually.

Authentication
--------------

``BaseApiClient`` supports three token modes:

* ``none`` - no token is sent.
* ``bearer`` - the token is sent as ``Authorization: Bearer <token>``.
* ``header`` - the token is sent in a custom header (default ``X-API-Key``).
* ``query`` - the token is appended as a query parameter (default ``token``).

Default JSON schemas
--------------------

List endpoint (both clients) - a JSON array or an object with an ``items`` key::

    [
      {"id": "study_1", "description": "CT Chest"},
      {"id": "study_2", "description": "MR Brain"}
    ]

    # or

    {"items": [...]}

Zip fetch endpoint - must return a ZIP archive of DICOM files.

Manifest fetch endpoint::

    {
      "files": [
        {"url": "https://host/file1.dcm", "name": "file1.dcm"},
        {"url": "https://host/file2.dcm", "name": "file2.dcm"}
      ]
    }

    # or

    {"files": ["https://host/file1.dcm", "https://host/file2.dcm"]}
"""

import json
import os
import shutil
import ssl
import urllib.request
import zipfile
from abc import ABCMeta, abstractmethod
from urllib.parse import urlencode, urljoin


class BaseApiClient(metaclass=ABCMeta):
    """Abstract base class for API clients."""

    DEFAULT_LIST_ENDPOINT = "/"
    DEFAULT_FETCH_ENDPOINT = "/{id}"

    def __init__(
        self,
        timeout=60,
        token=None,
        token_mode="none",
        token_name=None,
        verify_ssl=True,
    ):
        self.timeout = timeout
        self.token = token or ""
        self.token_mode = (token_mode or "none").lower()
        self.token_name = token_name or ""
        self.verify_ssl = verify_ssl

    def login(self, base_url, username, password, login_endpoint="/api/login"):
        """
        Log in to the API and store the returned token.

        The login request is sent as ``application/x-www-form-urlencoded``
        with ``login`` and ``password`` fields. Returns the token string.
        """
        url = self._get_full_url(base_url, login_endpoint)
        form_data = urlencode({"login": username, "password": password}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            url,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with self._urlopen(request) as response:
            body = response.read().decode("utf-8")

        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"token": body}

        token = payload.get("token") if isinstance(payload, dict) else payload
        if not token:
            raise RuntimeError("Login response did not contain a token.")

        self.token = str(token)
        return self.token

    def _ssl_context(self):
        """Return an unverified SSL context when certificate checks are off."""
        if self.verify_ssl:
            return None
        return ssl._create_unverified_context()

    def _urlopen(self, request):
        """Open *request* with the configured SSL verification behaviour."""
        return urllib.request.urlopen(
            request,
            timeout=self.timeout,
            context=self._ssl_context(),
        )

    def _get_full_url(self, base_url, endpoint):
        """Join a base URL and an endpoint path."""
        base = base_url.rstrip("/")
        path = endpoint.lstrip("/")
        return urljoin(base + "/", path)

    def _apply_token_to_url(self, url):
        """Append the token as a query parameter when token_mode is 'query'."""
        if self.token_mode != "query" or not self.token:
            return url
        name = self.token_name or "token"
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{name}={self.token}"

    def _build_request(self, url):
        """Create a Request for *url* with the configured token."""
        url = self._apply_token_to_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

        if not self.token or self.token_mode in ("none", "query"):
            return request

        if self.token_mode == "bearer":
            header_name = "Authorization"
            header_value = f"Bearer {self.token}"
        else:  # header
            header_name = self.token_name or "X-API-Key"
            header_value = self.token

        request.add_header(header_name, header_value)
        return request

    def _request_json(self, url):
        """Fetch JSON from *url* and return the parsed object."""
        request = self._build_request(url)
        with self._urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _download_file(self, url, dest_path):
        """Download *url* to *dest_path*."""
        request = self._build_request(url)
        with self._urlopen(request) as response:
            with open(dest_path, "wb") as out:
                shutil.copyfileobj(response, out)

    @abstractmethod
    def list_items(self, base_url, list_endpoint=None):
        """Return a list of ``{"id": ..., "description": ...}`` dicts."""
        raise NotImplementedError

    @abstractmethod
    def fetch_item(self, base_url, item_id, output_dir, fetch_endpoint=None):
        """Download item *item_id* and return the DICOM folder path."""
        raise NotImplementedError

    def _normalize_list(self, data):
        """Accept either a JSON list or ``{"items": [...]}``."""
        if isinstance(data, dict):
            for key in ("items", "studies", "series", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = []
        if not isinstance(data, list):
            data = [data]
        return data

    def _item_to_dict(self, item):
        """Normalize a list entry to ``{"id", "description"}``."""
        if isinstance(item, dict):
            item_id = str(item.get("id", ""))
            description = item.get("description", item_id)
        else:
            item_id = str(item)
            description = str(item)
        return {"id": item_id, "description": description}


class ZipApiClient(BaseApiClient):
    """API client for APIs that deliver DICOMs as ZIP archives."""

    DEFAULT_LIST_ENDPOINT = "/studies"
    DEFAULT_FETCH_ENDPOINT = "/studies/{id}/download"

    def list_items(self, base_url, list_endpoint=None):
        endpoint = list_endpoint or self.DEFAULT_LIST_ENDPOINT
        url = self._get_full_url(base_url, endpoint)
        data = self._request_json(url)
        return [self._item_to_dict(item) for item in self._normalize_list(data)]

    def fetch_item(self, base_url, item_id, output_dir, fetch_endpoint=None):
        endpoint = (fetch_endpoint or self.DEFAULT_FETCH_ENDPOINT).replace(
            "{id}", str(item_id)
        )
        url = self._get_full_url(base_url, endpoint)

        zip_path = os.path.join(output_dir, "download.zip")
        self._download_file(url, zip_path)

        dicom_dir = os.path.join(output_dir, "dicoms")
        os.makedirs(dicom_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dicom_dir)

        return dicom_dir


class ManifestApiClient(BaseApiClient):
    """API client for APIs that deliver DICOMs via a JSON file manifest."""

    DEFAULT_LIST_ENDPOINT = "/series"
    DEFAULT_FETCH_ENDPOINT = "/series/{id}"

    def list_items(self, base_url, list_endpoint=None):
        endpoint = list_endpoint or self.DEFAULT_LIST_ENDPOINT
        url = self._get_full_url(base_url, endpoint)
        data = self._request_json(url)
        return [self._item_to_dict(item) for item in self._normalize_list(data)]

    def fetch_item(self, base_url, item_id, output_dir, fetch_endpoint=None):
        endpoint = (fetch_endpoint or self.DEFAULT_FETCH_ENDPOINT).replace(
            "{id}", str(item_id)
        )
        url = self._get_full_url(base_url, endpoint)

        data = self._request_json(url)
        files = data.get("files", [])
        if not files:
            raise RuntimeError("Manifest did not contain a 'files' list.")

        dicom_dir = os.path.join(output_dir, "dicoms")
        os.makedirs(dicom_dir, exist_ok=True)

        for index, entry in enumerate(files):
            if isinstance(entry, dict):
                file_url = entry.get("url", "")
                file_name = entry.get("name", "")
            else:
                file_url = str(entry)
                file_name = ""

            if not file_url:
                raise RuntimeError(f"Manifest entry {index} has no URL.")

            if not file_name:
                file_name = os.path.basename(file_url) or f"image_{index:04d}.dcm"

            dest_path = os.path.join(dicom_dir, file_name)
            self._download_file(file_url, dest_path)

        return dicom_dir
