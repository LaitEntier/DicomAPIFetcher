"""
ApiClient
=========

Small adapter layer for fetching DICOM data from a remote HTTP API.

The ``BaseApiClient`` defines two operations:

* ``list_items(base_url, list_endpoint)`` - returns a list of selectable items.
* ``fetch_item(base_url, item_id, output_dir, fetch_endpoint)`` - downloads the
  DICOM files for one item into ``output_dir`` and returns the folder that
  contains the DICOM files.

Three concrete clients are provided:

* ``ZipApiClient`` - the API returns a JSON list and each item is downloaded as
  a ZIP archive of DICOM files.
* ``ManifestApiClient`` - the API returns a JSON list and a detail endpoint that
  contains a JSON manifest with a list of file URLs to download individually.
* ``ArchiMedApiClient`` - client for ArchiMed3-web archives, where DICOM data
  is organised as study -> exam -> serie -> file and each file is downloaded
  as an individual stream.

Authentication
--------------

``BaseApiClient`` supports three token modes:

* ``none`` - no token is sent.
* ``bearer`` - the token is sent as ``Authorization: Bearer <token>``.
* ``header`` - the token is sent in a custom header (default ``X-API-Key``).
* ``query`` - the token is appended as a query parameter (default ``token``).

Default JSON schemas
--------------------

List endpoint (both clients) - a JSON array or an object with an ``items``
(also ``studies`` / ``series`` / ``results``) key::

    [
      {"id": "study_1", "description": "CT Chest"},
      {"id": "study_2", "description": "MR Brain"}
    ]

    # or

    {"items": [...]}

Entries are normalized with ``_item_to_dict``: besides ``id`` /
``description``, any key ending in ``ID`` (e.g. ``studyID``, ``examID``)
or ``Description`` (e.g. ``studyDescription``, ``examDescription``) is
recognized.

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

import concurrent.futures
import json
import os
import re
import shutil
import ssl
import time
import urllib.error
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
        max_workers=6,
        download_retries=2,
        retry_delay=0.5,
    ):
        self.timeout = timeout
        self.token = token or ""
        self.token_mode = (token_mode or "none").lower()
        self.token_name = token_name or ""
        self.verify_ssl = verify_ssl
        self.max_workers = max(1, int(max_workers or 1))
        self.download_retries = max(0, int(download_retries or 0))
        self.retry_delay = max(0.0, float(retry_delay or 0.0))

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
        try:
            with self._urlopen(request) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # Surface the server's explanation (e.g. reason for a 401).
            try:
                detail = e.read().decode("utf-8").strip()
            except Exception:
                detail = ""
            message = f"HTTP {e.code} {e.reason} (POST {url})"
            if detail:
                message += f": {detail}"
            raise RuntimeError(message) from e

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

    def _build_request(self, url, accept="application/json"):
        """Create a Request for *url* with the configured token."""
        url = self._apply_token_to_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
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
        """Download *url* to *dest_path* through a temporary ``.part`` file."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        temporary_path = dest_path + ".part"
        try:
            request = self._build_request(url, accept="*/*")
            with self._urlopen(request) as response:
                with open(temporary_path, "wb") as out:
                    shutil.copyfileobj(response, out)
            os.replace(temporary_path, dest_path)
        finally:
            if os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass

    @staticmethod
    def _isFatalDownloadError(error):
        """Return True for HTTP errors that should stop the batch immediately."""
        return isinstance(error, urllib.error.HTTPError) and error.code in (
            400,
            401,
            403,
            404,
        )

    def _download_file_with_retry(self, url, dest_path):
        """Download one file, retrying transient failures before giving up."""
        attempts = self.download_retries + 1
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                self._download_file(url, dest_path)
                return
            except urllib.error.HTTPError as error:
                # Authentication and missing-file failures will affect every
                # remaining request. Do not retry them hundreds of times.
                if self._isFatalDownloadError(error):
                    raise
                last_error = error
                if attempt < attempts and self.retry_delay:
                    time.sleep(self.retry_delay * attempt)
            except Exception as error:  # urllib raises several exception types
                last_error = error
                if attempt < attempts and self.retry_delay:
                    time.sleep(self.retry_delay * attempt)
        raise last_error

    def _download_files_parallel(
        self, downloads, progress_callback=None, progress_label="Downloading files"
    ):
        """
        Download ``[(url, destination_path), ...]`` with bounded concurrency.

        Returns destination paths in input order. All files are retried; if
        any permanent failures remain, the import fails instead of silently
        producing an incomplete DICOM series.
        """
        total = len(downloads)
        if total == 0:
            return []

        results = [None] * total
        failures = []
        completed = 0

        def report_progress():
            if progress_callback:
                progress_callback(f"{progress_label} {completed}/{total}...")

        def download_one(index, url, dest_path):
            self._download_file_with_retry(url, dest_path)
            return index, dest_path

        worker_count = min(self.max_workers, total)
        if worker_count == 1:
            for index, (url, dest_path) in enumerate(downloads):
                try:
                    _, results[index] = download_one(index, url, dest_path)
                except Exception as error:
                    if self._isFatalDownloadError(error):
                        raise RuntimeError(
                            f"Download failed for {url}: {error}"
                        ) from error
                    failures.append((url, error))
                completed += 1
                report_progress()
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=worker_count
            ) as executor:
                futures = {
                    executor.submit(download_one, index, url, dest_path): index
                    for index, (url, dest_path) in enumerate(downloads)
                }
                for future in concurrent.futures.as_completed(futures):
                    future_index = futures[future]
                    try:
                        index, dest_path = future.result()
                        results[index] = dest_path
                    except Exception as error:
                        if self._isFatalDownloadError(error):
                            for pending_future in futures:
                                pending_future.cancel()
                            raise RuntimeError(
                                f"Download failed for "
                                f"{downloads[future_index][0]}: {error}"
                            ) from error
                        failures.append((downloads[future_index][0], error))
                    completed += 1
                    report_progress()

        if failures:
            details = "; ".join(
                f"{url}: {error}" for url, error in failures[:3]
            )
            raise RuntimeError(
                f"Failed to download {len(failures)}/{total} file(s). {details}"
            )
        return results

    @abstractmethod
    def list_items(self, base_url, list_endpoint=None):
        """Return a list of ``{"id": ..., "description": ...}`` dicts."""
        raise NotImplementedError

    @abstractmethod
    def fetch_item(
        self,
        base_url,
        item_id,
        output_dir,
        fetch_endpoint=None,
        progress_callback=None,
    ):
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

    def _find_field(self, item, exact_keys, suffix):
        """
        Find a field value in *item* by trying *exact_keys* first, then any
        key ending with *suffix* (case-insensitive). Returns ``None`` when
        nothing matches.
        """
        for key in exact_keys:
            if key in item and item[key] is not None:
                return item[key]
        for key, value in item.items():
            if key.lower().endswith(suffix) and value is not None:
                return value
        return None

    def _item_to_dict(self, item):
        """Normalize a list entry to ``{"id", "description"}``."""
        if isinstance(item, dict):
            item_id = self._find_field(
                item, ("id", "studyID", "seriesID", "examID"), "id"
            )
            description = self._find_field(
                item,
                ("description", "studyDescription", "seriesDescription",
                 "examDescription"),
                "description",
            )
            item_id = "" if item_id is None else str(item_id)
            if description is None:
                description = item_id
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

    def fetch_item(
        self,
        base_url,
        item_id,
        output_dir,
        fetch_endpoint=None,
        progress_callback=None,
    ):
        endpoint = (fetch_endpoint or self.DEFAULT_FETCH_ENDPOINT).replace(
            "{id}", str(item_id)
        )
        url = self._get_full_url(base_url, endpoint)

        if progress_callback:
            progress_callback(f"Downloading ZIP archive for item {item_id}...")
        zip_path = os.path.join(output_dir, "download.zip")
        self._download_file_with_retry(url, zip_path)

        dicom_dir = os.path.join(output_dir, "dicoms")
        os.makedirs(dicom_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dicom_dir)
        os.remove(zip_path)

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

    def fetch_item(
        self,
        base_url,
        item_id,
        output_dir,
        fetch_endpoint=None,
        progress_callback=None,
    ):
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

        downloads = []
        used_names = set()
        for index, entry in enumerate(files):
            if isinstance(entry, dict):
                file_url = entry.get("url", "")
                file_name = entry.get("name", "")
            else:
                file_url = str(entry)
                file_name = ""

            if not file_url:
                raise RuntimeError(f"Manifest entry {index} has no URL.")

            file_name = os.path.basename(str(file_name))
            if not file_name:
                file_name = os.path.basename(file_url) or f"image_{index:04d}.dcm"
            if file_name in used_names:
                file_name = f"{index:04d}_{file_name}"
            used_names.add(file_name)

            downloads.append((file_url, os.path.join(dicom_dir, file_name)))

        self._download_files_parallel(
            downloads,
            progress_callback=progress_callback,
            progress_label=f"Downloading files for item {item_id}",
        )
        return dicom_dir


class ArchiMedApiClient(BaseApiClient):
    """
    API client for ArchiMed3-web archives.

    ArchiMed organises DICOM data as ``study -> exam -> serie -> file`` and
    exposes each file as an individual stream::

        GET /api/db/<zone>/studies                                          -> [study]
        GET /api/db/<zone>/studies/<studyID>/exams                          -> [exam]
        GET /api/db/<zone>/studies/<studyID>/exams/<examID>/series          -> [serie]
        GET .../exams/<examID>/series/<serieID>/files                       -> [file]
        GET /api/db/<zone>/files/<fileID>/stream                            -> raw bytes

    ``zone`` is ``"final"`` (default) or ``"trash"``.

    The ``list_studies`` / ``list_exams`` / ``list_series`` / ``list_files``
    methods expose each level of the hierarchy, and ``download_study`` /
    ``download_exam`` / ``download_serie`` download the corresponding subtree.

    The legacy ``list_items`` / ``fetch_item`` interface is kept: selecting an
    item whose ID is a plain study ID downloads all its exams; the composite
    ID ``<studyID>/<examID>`` downloads a single exam.
    """

    DEFAULT_LIST_ENDPOINT = "/api/db/final/studies"
    DEFAULT_FETCH_ENDPOINT = "/api/db/final/studies/{id}"

    def __init__(self, *args, zone="final", **kwargs):
        super().__init__(*args, **kwargs)
        self.zone = zone or "final"

    # ------------------------------------------------------------------
    # Hierarchy browsing
    # ------------------------------------------------------------------
    def _studies_url(self, base_url):
        """URL of the studies collection for the configured zone."""
        return self._get_full_url(base_url, f"/api/db/{self.zone}/studies")

    def list_studies(self, base_url):
        """Return the raw study dicts of the configured zone."""
        data = self._request_json(self._studies_url(base_url))
        return self._normalize_list(data)

    def list_exams(self, base_url, study_id):
        """Return the raw exam dicts of study *study_id*."""
        url = f"{self._studies_url(base_url)}/{study_id}/exams"
        return self._normalize_list(self._request_json(url))

    def list_series(self, base_url, study_id, exam_id):
        """Return the raw serie dicts of exam *exam_id* in study *study_id*."""
        url = (
            f"{self._studies_url(base_url)}/{study_id}"
            f"/exams/{exam_id}/series"
        )
        return self._normalize_list(self._request_json(url))

    def list_files(self, base_url, study_id, exam_id, serie_id):
        """Return the raw file dicts of serie *serie_id*."""
        url = (
            f"{self._studies_url(base_url)}/{study_id}"
            f"/exams/{exam_id}/series/{serie_id}/files"
        )
        return self._normalize_list(self._request_json(url))

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------
    def _file_stream_url(self, base_url, file_id):
        """URL of the raw stream of file *file_id*."""
        return self._get_full_url(
            base_url, f"/api/db/{self.zone}/files/{file_id}/stream"
        )

    def download_serie(
        self,
        base_url,
        study_id,
        exam_id,
        serie_id,
        output_dir,
        progress_callback=None,
    ):
        """
        Download every file of serie *serie_id* into ``<output_dir>/dicoms``
        and return that folder.
        """
        files = self.list_files(base_url, study_id, exam_id, serie_id)
        if not files:
            raise RuntimeError(
                f"Serie {serie_id} (exam {exam_id}) contains no files."
            )

        dicom_dir = os.path.join(output_dir, "dicoms")
        os.makedirs(dicom_dir, exist_ok=True)

        downloads = []
        used_names = set()
        for index, entry in enumerate(files):
            if isinstance(entry, dict):
                file_id = entry.get("fileID")
                file_name = entry.get("fileName") or ""
            else:
                file_id = entry
                file_name = ""
            file_name = os.path.basename(str(file_name))
            dest_name = (
                f"{file_id}_{file_name}" if file_name else f"file_{file_id}.dcm"
            )
            if dest_name in used_names:
                dest_name = f"{index:04d}_{dest_name}"
            used_names.add(dest_name)
            downloads.append(
                (
                    self._file_stream_url(base_url, file_id),
                    os.path.join(dicom_dir, dest_name),
                )
            )

        self._download_files_parallel(
            downloads,
            progress_callback=progress_callback,
            progress_label=f"Downloading files for serie {serie_id}",
        )
        return dicom_dir

    def download_exam(
        self, base_url, study_id, exam_id, output_dir, progress_callback=None
    ):
        """Download every serie of exam *exam_id* into ``<output_dir>/dicoms``."""
        series = self.list_series(base_url, study_id, exam_id)
        if not series:
            raise RuntimeError(
                f"Exam {exam_id} (study {study_id}) contains no series."
            )
        for serie in series:
            serie_id = (
                serie.get("serieID") if isinstance(serie, dict) else serie
            )
            self.download_serie(
                base_url,
                study_id,
                exam_id,
                serie_id,
                output_dir,
                progress_callback=progress_callback,
            )
        return os.path.join(output_dir, "dicoms")

    def download_study(
        self, base_url, study_id, output_dir, progress_callback=None
    ):
        """Download every exam of study *study_id* into ``<output_dir>/dicoms``."""
        exams = self.list_exams(base_url, study_id)
        if not exams:
            raise RuntimeError(f"Study {study_id} contains no exams.")
        for exam in exams:
            exam_id = exam.get("examID") if isinstance(exam, dict) else exam
            self.download_exam(
                base_url,
                study_id,
                exam_id,
                output_dir,
                progress_callback=progress_callback,
            )
        return os.path.join(output_dir, "dicoms")

    # ------------------------------------------------------------------
    # Legacy flat interface
    # ------------------------------------------------------------------
    def list_items(self, base_url, list_endpoint=None):
        endpoint = list_endpoint or self.DEFAULT_LIST_ENDPOINT
        url = self._get_full_url(base_url, endpoint)
        data = self._request_json(url)
        items = [self._item_to_dict(item) for item in self._normalize_list(data)]

        # Listing the exams of one study: keep the study ID in the item ID
        # so fetch_item can rebuild the full path for a single exam.
        match = re.search(r"/studies/([^/]+)/exams/?$", endpoint)
        if match:
            study_id = match.group(1)
            for item in items:
                item["id"] = f"{study_id}/{item['id']}"
        return items

    def fetch_item(
        self,
        base_url,
        item_id,
        output_dir,
        fetch_endpoint=None,
        progress_callback=None,
    ):
        template = fetch_endpoint or self.DEFAULT_FETCH_ENDPOINT

        # Honour a custom zone if the template addresses one.
        match = re.match(r"/api/db/([^/]+)/studies", template)
        if match:
            self.zone = match.group(1)

        item_id = str(item_id)
        if "/" in item_id:
            study_id, only_exam_id = item_id.split("/", 1)
        else:
            study_id, only_exam_id = item_id, None

        if only_exam_id is not None:
            return self.download_exam(
                base_url,
                study_id,
                only_exam_id,
                output_dir,
                progress_callback=progress_callback,
            )
        return self.download_study(
            base_url,
            study_id,
            output_dir,
            progress_callback=progress_callback,
        )
