# Local Setup Guide

This guide assumes that 64-bit Python is already installed on Windows.

## 1. Open the project folder

```powershell
cd "C:\Users\puneetmalhotra\OneDrive - Nagarro\Desktop\VSTS\ETL\ETL_For_Local_Only"
```

Python 3.11 or newer is recommended. Confirm the active installation:

```powershell
py --version
py -0p
```

## 2. Create a virtual environment

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in the current terminal and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Install Python dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The requirements install:

- Flask for the local web application
- Requests and PyJWT for Salesforce authentication and API calls
- DuckDB, Polars, and PyArrow for staging and large-file processing
- PyYAML for migration configuration
- Rich and Click for CLI output
- OpenPyXL for Excel-based hierarchy files
- python-dotenv for `.env` configuration

DuckDB resources:

- Official website: [https://duckdb.org/](https://duckdb.org/)
- Python API documentation: [https://duckdb.org/docs/stable/clients/python/overview](https://duckdb.org/docs/stable/clients/python/overview)
- Python package: [https://pypi.org/project/duckdb/](https://pypi.org/project/duckdb/)
- Source repository: [https://github.com/duckdb/duckdb](https://github.com/duckdb/duckdb)

The project currently declares `duckdb>=0.10.0` in `requirements.txt`. The version installed in the development environment at the time this guide was updated is `1.5.4`.

No Node.js packages are required. The browser JavaScript and CSS are served directly by Flask.

Salesforce CLI is not required by the application. Authentication and migration calls use the Salesforce REST, Bulk, Tooling, and rich-text APIs directly.

## 4. Create the environment file

Create `.env` from the example if `.env` does not already exist:

```powershell
Copy-Item .env.example .env
```

Do not commit `.env`, session IDs, client secrets, or JWT private keys.

## 5. Configure Salesforce authentication

Configure both sections for SF-to-SF migration. File-to-SF requires only the target section.

### Client credentials

Example:

```dotenv
SOURCE_SF_LOGIN_URL=https://source-domain.my.salesforce.com
SOURCE_SF_CLIENT_ID=source-connected-app-consumer-key
SOURCE_SF_CLIENT_SECRET=source-connected-app-consumer-secret
SOURCE_SF_GRANT_TYPE=client_credentials
SOURCE_SF_API_VERSION=v60.0

TARGET_SF_LOGIN_URL=https://target-domain.my.salesforce.com
TARGET_SF_CLIENT_ID=target-connected-app-consumer-key
TARGET_SF_CLIENT_SECRET=target-connected-app-consumer-secret
TARGET_SF_GRANT_TYPE=client_credentials
TARGET_SF_API_VERSION=v60.0
```

The Salesforce Connected App must have client-credentials flow enabled and an integration user assigned.

### JWT bearer authentication

Example:

```dotenv
SOURCE_SF_LOGIN_URL=https://login.salesforce.com
SOURCE_SF_CLIENT_ID=source-connected-app-consumer-key
SOURCE_SF_GRANT_TYPE=auth:jwt:grant
SOURCE_SF_USERNAME=source.integration@example.com
SOURCE_SF_JWT_KEY_FILE=config/source_jwt.key

TARGET_SF_LOGIN_URL=https://login.salesforce.com
TARGET_SF_CLIENT_ID=target-connected-app-consumer-key
TARGET_SF_GRANT_TYPE=auth:jwt:grant
TARGET_SF_USERNAME=target.integration@example.com
TARGET_SF_JWT_KEY_FILE=config/target_jwt.key
```

Use `https://test.salesforce.com` for a sandbox unless the org-specific My Domain URL is required by your Connected App configuration.

### Existing session authentication

For a temporary local test, an existing session can be supplied:

```dotenv
TARGET_SF_LOGIN_URL=https://target-domain.my.salesforce.com
TARGET_SF_SESSION_ID=your-session-id
TARGET_SF_API_VERSION=v60.0
```

Session authentication expires and is not recommended for a durable setup.

## 6. Multiple org profiles

The `default` UI profile uses variables such as:

```dotenv
SOURCE_SF_LOGIN_URL=...
TARGET_SF_LOGIN_URL=...
```

Additional named profiles use the profile name in the variable prefix. For a profile named `uat`:

```dotenv
SOURCE_UAT_SF_LOGIN_URL=...
SOURCE_UAT_SF_CLIENT_ID=...
SOURCE_UAT_SF_CLIENT_SECRET=...
SOURCE_UAT_SF_GRANT_TYPE=client_credentials

TARGET_UAT_SF_LOGIN_URL=...
TARGET_UAT_SF_CLIENT_ID=...
TARGET_UAT_SF_CLIENT_SECRET=...
TARGET_UAT_SF_GRANT_TYPE=client_credentials
```

The profile will appear as `Uat` in the UI after the application restarts.

## 7. Required Salesforce access

The source integration user normally needs:

- API Enabled
- Read access to every selected source object and field
- Read access to Record Type metadata
- Read access to referenced objects used for lookup resolution
- Access to Knowledge and Salesforce Files when migrating Knowledge content

The target integration user normally needs:

- API Enabled
- Create, Read, Edit, and Delete permissions appropriate to the selected operations
- Field-level write access for mapped fields
- Read access to target External ID and lookup-match fields
- Access to Record Types and their picklist values
- Bulk API access
- Knowledge authoring/publishing permission for Knowledge migrations
- Apex/Tooling API permission required by the Knowledge publishing step

Salesforce-managed fields, formulas, roll-up summaries, compound address/location fields, and auto-number fields are excluded automatically.

## 8. Prepare local directories

The application creates most state and log directories automatically. The Flat File drop folder can be prepared explicitly:

```powershell
New-Item -ItemType Directory -Force state\flat_file_drop
```

Example layout:

```text
state/flat_file_drop/
└── 01_insert_files/
    ├── Account.csv
    ├── Contact.csv
    ├── Opportunity.csv
    └── Knowledge__kav.csv
```

Subfolders are supported. The Flat Files page reads only CSV headers during template creation. Prevalidation and staging stream the complete files later.

Do not rename, move, replace, or edit a registered drop-folder file. Re-import it in the UI after any change.

For multi-gigabyte files, keep enough free disk space for DuckDB staging, logs, and optional clean/rejected outputs. Full prevalidation can create a clean file approximately as large as the input.

## 9. Start the application

From the activated virtual environment:

```powershell
python ui\app.py
```

Open:

```text
http://127.0.0.1:5000/
```

Main pages:

```text
http://127.0.0.1:5000/source-org
http://127.0.0.1:5000/flat-files
http://127.0.0.1:5000/pipeline
http://127.0.0.1:5000/pipeline-runs
```

The Flask development server uses debug mode. Use the local application on a trusted machine and do not expose port 5000 publicly.

## 10. Verify the installation

Check that Python modules compile:

```powershell
python -m py_compile ui\app.py cli.py core\connector.py
```

Check the Salesforce connections from the UI header, or call:

```text
http://127.0.0.1:5000/api/auth/check?org=source&profile=default
http://127.0.0.1:5000/api/auth/check?org=target&profile=default
```

The backend health endpoint is:

```text
http://127.0.0.1:5000/sf-api/v1/health
```

Run core regression tests with the repository test runner:

```powershell
python tests\test_flat_file.py
python tests\test_flat_file_schema_validator.py
python tests\test_flat_file_pipeline.py
python tests\test_source_template_mode.py
python tests\test_identity_compiler.py
```

`pytest` is not required by `requirements.txt`. Install it separately only if you want to use pytest:

```powershell
python -m pip install pytest
python -m pytest tests -q
```

## 11. Basic user flow

### SF-to-SF

1. Open Source Org.
2. Select source and target profiles.
3. Create object/field/identity mappings.
4. Save templates and add them to Pipeline Designer.
5. Run pipeline prevalidation.
6. Start the migration from Pipeline Run.

### File-to-SF

1. Copy CSV files into `state\flat_file_drop`.
2. Open Flat Files and refresh the drop-folder tree.
3. Select files and create mappings.
4. Save the draft template.
5. Run full or sampled prevalidation.
6. Add the template to Pipeline Designer.
7. Stage and run the pipeline.

## 12. Troubleshooting

### PowerShell cannot activate the virtual environment

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### Proxy error mentioning `127.0.0.1`

Add this to `.env` and restart:

```dotenv
SF_ETL_IGNORE_SYSTEM_PROXY=1
```

### Browser/DLP blocks file selection

Use the local drop-folder workflow instead of browser upload:

```text
state/flat_file_drop/
```

### A drop-folder file changed

Refresh the Flat Files page, select the file again, and recreate or resave the mapping template. This deliberately prevents a template from processing different file contents silently.

### Port 5000 is already in use

Find the owning process:

```powershell
Get-NetTCPConnection -LocalPort 5000 | Select-Object LocalAddress,LocalPort,State,OwningProcess
```

Then inspect it:

```powershell
Get-Process -Id <OwningProcess>
```

### Generic server failure

Inspect:

```text
logs/sf_etl.log
```

Restart the Flask application after Python changes. Use a hard browser refresh after JavaScript or CSS changes.
