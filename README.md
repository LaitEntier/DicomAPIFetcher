# DICOM API Fetcher

A 3D Slicer extension that fetches DICOM images from a remote HTTP API and
loads the selected series directly into the scene (no DICOM database involved).

## Features

* Prompts for the API base URL on first use and remembers it across Slicer sessions.
* Three interchangeable API strategies:
  * **ZIP archive** - the API returns a JSON list; each item is downloaded as a ZIP of DICOM files.
  * **JSON manifest** - the API returns a JSON list; a detail endpoint returns a list of file URLs to download individually.
  * **ArchiMed3-web** - browses the ArchiMed hierarchy `study → exam → serie → file` and downloads each file as an individual stream.
* Hierarchical tree browser (for ArchiMed): fetch the studies, then lazily expand a study to see its exams and an exam to see its series.
* Select any mix of studies, exams, or series (extended selection) and import exactly that data.
* Adapts to your API by changing endpoint templates in the module UI.
* Downloads individual files in parallel (six connections by default), retries transient failures, and writes only completed files.
* Runs network downloads in a background thread while keeping DICOM examination and scene loading on Slicer's main thread.
* Optional persistent download cache avoids transferring unchanged studies, exams, series, or manifest/ZIP items again. Cached data can be cleared from the module UI.
* Loads the downloaded series directly into the scene through Slicer's DICOM
  plugins (the Slicer DICOM database is not used).
* Built-in login against `POST /api/login` (form-encoded credentials) with automatic token capture.
* Optional API token that can be sent as a Bearer header, a custom header, or a query parameter.

## Installation

1. Build or point Slicer to the extension source:
   * **For development:** open Slicer, go to *Edit / Application settings / Modules*,
     and add the full path to `DicomApiFetcher/DicomApiFetcher` under
     *Additional module paths*. Restart Slicer.
   * **For distribution:** build the extension with CMake against a Slicer installation
     and install the resulting package via the Extensions Manager.

2. Open the module under the *DICOM* category.

## Usage

1. Enter the API base URL, e.g. `https://recharme-pr01:8443/ArchiMed3-web`.
2. Select the **ArchiMed3-web** strategy and the **Zone** (`final` or `trash`).
3. Enter your username/password and click **Login**.
4. The returned token is stored automatically and sent as `Authorization: <token>`.
5. Click **Fetch studies / series** — the tree is populated with studies.
6. Expand a study to load its exams, then expand an exam to load its series
   (children are fetched on demand).
7. Select one or more studies / exams / series.
8. Click **Import & Load selected** — exactly the selected data is downloaded
   in the background and loaded into the scene.

## Performance and local cache

ArchiMed exposes one HTTP stream per DICOM file. The supplied OpenAPI
specification does not provide a study/exam/series ZIP or bulk HTTP download
endpoint, so the ArchiMed strategy parallelizes these per-file requests instead.
The generic ZIP strategy remains available for APIs that already support a bulk
archive.

Downloads use six concurrent connections by default. Each file is downloaded to
a `.part` file, retried after transient failures, and renamed only after it is
complete. If a file still cannot be downloaded, the import fails rather than
silently loading an incomplete series.

When **Cache downloaded DICOM files** is enabled (the default), completed
selections are stored in Slicer's user-specific data directory under
`DicomApiFetcherCache`. A cache entry is reused only if its completion manifest
and every listed file are present. Metadata browsing still queries the server,
but a repeated import of the same cached study, exam, series, or item does not
download its files again.

Use **Clear cache** to delete all cached DICOM files. The cache does not
currently expire entries automatically. Cached data is stored unencrypted on the
local machine; disable the cache or clear it when using a shared or otherwise
sensitive workstation. When the cache is disabled, downloaded files are
temporary and are deleted after loading.

For the **ZIP archive** and **JSON manifest** strategies, set the **List
endpoint** (e.g. `/api/db/final/studies`) and **Fetch endpoint** instead of a
zone; the tree then shows a flat list of items.

The API base URL, chosen strategy, zone, cache preference, and token are saved
in Slicer's settings.

## Authentication

The module includes a built-in login flow matching the ArchiMed API:

* `POST <base_url>/api/login`
* Content-Type: `application/x-www-form-urlencoded`
* Body: `login=<username>&password=<password>`
* Response: JSON containing a `token` field.

After a successful login the token is stored in the **API token** field and sent
on subsequent requests as `Authorization: <token>` (raw token, no `Bearer`
prefix), exactly as expected by the ArchiMed API.

If you need to call a different API, the token can alternatively be sent as a
`Bearer` header, a custom header, or a query parameter — configure this in the
**Authentication** section.

### Self-signed certificates

The ArchiMed server uses a self-signed certificate on port 8443, so the module
ships with **Ignore SSL certificate errors** enabled by default. This disables
certificate verification for API calls — acceptable on a trusted private
network, but do not enable it for untrusted hosts. Uncheck the box if you
install a proper CA-signed certificate.

The token is stored in Slicer's settings in plain text. On shared machines,
consider whether this is acceptable for your security model.

## API contract

The default clients expect the following JSON schemas.

### ArchiMed3-web hierarchy endpoints

The ArchiMed strategy walks the `study → exam → serie → file` hierarchy
(`zone` is `final` or `trash`):

```
GET /api/db/{zone}/studies                                                   -> [study]   (studyID, studyDescription, nbExams, ...)
GET /api/db/{zone}/studies/{studyID}/exams                                   -> [exam]    (examID, examCode, examDescription, examDate, ...)
GET /api/db/{zone}/studies/{studyID}/exams/{examID}/series                   -> [serie]   (serieID, serieNumber, serieDescription, nbFiles, ...)
GET /api/db/{zone}/studies/{studyID}/exams/{examID}/series/{serieID}/files   -> [file]    (fileID, fileName, fileSize, ...)
GET /api/db/{zone}/files/{fileID}/stream                                     -> raw DICOM bytes
```

All requests send the token as `Authorization: <token>` (raw token).

### List endpoint

`GET <base_url>/<list_endpoint>`

Returns either a JSON array or an object with an `items` key:

```json
[
  {"id": "study_1", "description": "CT Chest"},
  {"id": "study_2", "description": "MR Brain"}
]
```

```json
{"items": [{"id": "study_1", "description": "CT Chest"}]}
```

### ZIP fetch endpoint

`GET <base_url>/<fetch_endpoint>` where `{id}` is replaced by the selected item ID.

The response body must be a ZIP archive containing DICOM files.

### Manifest fetch endpoint

`GET <base_url>/<fetch_endpoint>`

Returns a JSON object with a `files` array:

```json
{
  "files": [
    {"url": "https://host/file1.dcm", "name": "file1.dcm"},
    {"url": "https://host/file2.dcm", "name": "file2.dcm"}
  ]
}
```

or simply:

```json
{
  "files": ["https://host/file1.dcm", "https://host/file2.dcm"]
}
```

## Adapting to another API format

If your API returns a different shape, edit `DicomApiFetcher/DicomApiFetcherLib/ApiClient.py` and
add a new `BaseApiClient` subclass, then register it in
`DicomApiFetcher/DicomApiFetcher.py` by adding a corresponding entry to the
*API strategy* combo box.

## File structure

```
DicomApiFetcher/
├── CMakeLists.txt
├── DicomApiFetcher/
│   ├── CMakeLists.txt
│   ├── DicomApiFetcher.py              # Module and UI
│   ├── DicomApiFetcherLib/
│   │   ├── ApiClient.py                # Parallel HTTP API adapters (incl. login)
│   │   ├── DownloadCache.py            # Manifest-validated persistent download cache
│   │   └── DicomApiFetcherLogic.py     # Download / import / load logic
│   └── Resources/
│       └── Icons/
│           └── DicomApiFetcher.png
└── README.md
```
