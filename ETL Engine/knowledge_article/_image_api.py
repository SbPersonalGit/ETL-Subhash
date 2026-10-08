"""
Thin Salesforce REST wrappers for inline rich-text image transfer.

Two embedding styles must be supported:

  * Legacy `rtaImage` (richTextImageFields)
      POST {instance}/services/data/{version}/sobjects/{Object}/{RecordId}/richTextImageFields/{FieldName}
      Returns `{"contentReferenceId": "<refid>"}`.
      NOTE: as of API v60+, Salesforce removed POST support on this
      endpoint in many orgs (HTTP 405). Kept here for legacy code paths
      and unit-test parity; the modern path is ContentVersion-based
      (below).

  * Modern `ContentVersion` (Salesforce Files)
      Lightning rich-text editors upload images as ContentVersion
      records and reference them with
        /sfc/servlet.shepherd/version/download/<ContentVersionId>
      To rehost we:
        1. Download the source's raw bytes from
             /services/data/{version}/sobjects/ContentVersion/{Id}/VersionData
        2. Fetch its metadata (Title, PathOnClient, FileType) so the
           target file looks identical.
        3. Multipart-POST a new ContentVersion to the target with
           `FirstPublishLocationId = <target_kav_id>` so Salesforce
           auto-creates a ContentDocumentLink (ShareType=V).
        4. Optionally re-link with `Visibility=AllUsers` if the
           caller wants community/portal visibility.

This module is intentionally small and stateless aside from the
in-memory feoid cache. Anything resembling business logic (manifest,
HTML scanning, IDMapper joins) lives in the rehoster.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connector import SFConnector

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadResult:
    """Result of a single richTextImageFields POST."""
    target_refid: str       # contentReferenceId returned by Salesforce
    target_field: str       # the rich-text field the image was uploaded into
    target_kav_id: str      # the target Kav (record) id we uploaded into


@dataclass(frozen=True)
class ContentVersionUploadResult:
    """Result of a successful ContentVersion upload to the target org."""
    target_content_version_id: str       # 068... new ContentVersion in target
    target_content_document_id: str | None  # 069... resolved post-insert
    target_kav_id: str                    # article we linked the file to
    title: str                            # title persisted in target
    path_on_client: str                   # filename persisted in target


@dataclass(frozen=True)
class ContentVersionMetadata:
    """Subset of source ContentVersion fields needed to recreate it."""
    title: str
    path_on_client: str
    file_type: str | None = None
    file_extension: str | None = None
    content_size: int | None = None


class ImageApiError(Exception):
    """Raised when an image API call fails for any reason."""


# ---------------------------------------------------------------- #
#  Download                                                         #
# ---------------------------------------------------------------- #

def download_image_bytes(connector: "SFConnector", url: str) -> bytes:
    """
    Fetch raw image bytes from a Salesforce-hosted URL using `connector`'s
    authenticated session. The URL is typically an rtaImage URL extracted
    from a source article's rich-text body.

    Raises `ImageApiError` if the response status isn't 200 or the body
    is empty (Salesforce returns 200 with empty body when the image was
    already deleted from the org).
    """
    if not url:
        raise ImageApiError("download_image_bytes() requires a non-empty url")

    try:
        resp = connector._request("GET", url, headers={"Accept": "*/*"})
    except Exception as e:  # noqa: BLE001 — wrap for caller clarity
        raise ImageApiError(f"GET {url} failed: {e}") from e

    if resp.status_code != 200:
        raise ImageApiError(
            f"GET {url} returned HTTP {resp.status_code}: {resp.text[:200]}"
        )

    if not resp.content:
        raise ImageApiError(f"GET {url} returned empty body")

    return resp.content


# ---------------------------------------------------------------- #
#  Upload                                                           #
# ---------------------------------------------------------------- #

# Salesforce expects multipart/form-data with a single file field whose
# name does not actually matter — anything works. We pick "FileData" to
# match the convention used by Salesforce's own rich-text editor.
_UPLOAD_FORM_FIELD = "FileData"


def upload_image(
    connector: "SFConnector",
    *,
    object_name: str,
    target_kav_id: str,
    target_field: str,
    content: bytes,
    filename: str = "image.png",
    content_type: str | None = None,
) -> UploadResult:
    """
    POST `content` to `<object>/<id>/richTextImageFields/<field>`.

    Returns the contentReferenceId (`target_refid`) Salesforce assigned
    to the new attachment. The same refid is then plugged into the
    rewritten <img src> URL.
    """
    if not (target_kav_id and target_field and content):
        raise ImageApiError(
            "upload_image() requires non-empty target_kav_id, target_field, and content"
        )

    if content_type is None:
        guessed, _ = mimetypes.guess_type(filename)
        content_type = guessed or "application/octet-stream"

    url = (
        f"{connector.instance_url}/services/data/{connector.api_version}"
        f"/sobjects/{object_name}/{target_kav_id}/richTextImageFields/{target_field}"
    )

    try:
        resp = connector._request(
            "POST",
            url,
            files={_UPLOAD_FORM_FIELD: (filename, content, content_type)},
            # Strip Content-Type so requests can set the multipart boundary itself.
            headers={"Content-Type": None},  # type: ignore[dict-item]
        )
    except Exception as e:  # noqa: BLE001
        raise ImageApiError(
            f"Upload to {object_name}/{target_kav_id}/richTextImageFields/{target_field} failed: {e}"
        ) from e

    try:
        body = resp.json()
    except ValueError as e:
        raise ImageApiError(
            f"Upload to {target_kav_id}/{target_field} returned non-JSON body: "
            f"{resp.text[:200]}"
        ) from e

    target_refid = body.get("contentReferenceId") or body.get("contentDocumentId")
    if not target_refid:
        raise ImageApiError(
            f"Upload response missing contentReferenceId | body={body}"
        )

    return UploadResult(
        target_refid=str(target_refid),
        target_field=target_field,
        target_kav_id=target_kav_id,
    )


# ---------------------------------------------------------------- #
#  feoid resolution                                                 #
# ---------------------------------------------------------------- #

# Salesforce's encoded-field-id ("feoid") for a custom rich-text field is
# simply the 18-char Id of the corresponding CustomField record. For
# standard rich-text fields the Id is queryable via FieldDefinition.
# We cache results in-process per (object, field) so only one Tooling
# round-trip happens regardless of how many articles need rehosting.

_FEOID_CACHE: dict[tuple[str, str, str], str | None] = {}  # (instance_url, object, field) -> feoid


def resolve_target_feoid(
    connector: "SFConnector",
    *,
    object_name: str,
    field_name: str,
) -> str | None:
    """
    Resolve the target org's encoded-field id (`feoid`) for the given
    (object, field). Returns None if the field cannot be located.

    Cached per (instance_url, object, field) so subsequent calls are free.
    """
    if not (connector.instance_url and object_name and field_name):
        return None

    cache_key = (connector.instance_url, object_name, field_name)
    if cache_key in _FEOID_CACHE:
        return _FEOID_CACHE[cache_key]

    feoid = _query_custom_field_id(connector, object_name, field_name)
    if feoid is None:
        feoid = _query_field_definition_id(connector, object_name, field_name)

    _FEOID_CACHE[cache_key] = feoid
    if feoid is None:
        logger.warning(
            "Could not resolve feoid for %s.%s on %s — image URLs may render incorrectly",
            object_name, field_name, connector.instance_url,
        )
    return feoid


def _developer_name(field_name: str) -> str:
    """Salesforce CustomField.DeveloperName drops the trailing __c (or __<ns>__c)."""
    return re.sub(r"__c$", "", field_name)


def _tooling_query(connector: "SFConnector", soql: str) -> list[dict]:
    """
    Run a Tooling API SOQL query. The Tooling API lives at
    `/services/data/{version}/tooling/query/?q=<soql>`.
    """
    url = (
        f"{connector.instance_url}/services/data/{connector.api_version}"
        f"/tooling/query"
    )
    try:
        resp = connector._request("GET", url, params={"q": soql})
    except Exception as e:  # noqa: BLE001
        logger.debug("Tooling query failed (%s): %s", soql, e)
        return []
    try:
        return resp.json().get("records", [])
    except ValueError:
        return []


def _query_custom_field_id(
    connector: "SFConnector", object_name: str, field_name: str
) -> str | None:
    """
    Look up the CustomField.Id for a custom rich-text field. Returns None
    for standard fields (handled by `_query_field_definition_id`).
    """
    if not field_name.endswith("__c"):
        return None

    soql = (
        "SELECT Id FROM CustomField WHERE TableEnumOrId = '"
        f"{object_name}' AND DeveloperName = '{_developer_name(field_name)}'"
        " LIMIT 1"
    )
    rows = _tooling_query(connector, soql)
    if not rows:
        return None
    return rows[0].get("Id")


def _query_field_definition_id(
    connector: "SFConnector", object_name: str, field_name: str
) -> str | None:
    """
    Fallback for standard fields: query FieldDefinition's DurableId.
    The DurableId encodes the entity field reference Salesforce uses
    in rtaImage URLs.
    """
    soql = (
        "SELECT DurableId FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = '"
        f"{object_name}' AND QualifiedApiName = '{field_name}' LIMIT 1"
    )
    rows = _tooling_query(connector, soql)
    if not rows:
        return None
    durable_id = rows[0].get("DurableId") or ""
    # DurableId for FieldDefinition is "<EntityKeyPrefix>.<FieldId>". The
    # right-hand side is the feoid Salesforce embeds in image URLs.
    if "." in durable_id:
        return durable_id.split(".", 1)[1]
    return durable_id or None


def reset_feoid_cache() -> None:
    """Test-only: drop the in-process feoid cache."""
    _FEOID_CACHE.clear()


# ---------------------------------------------------------------- #
#  ContentVersion (modern Files-based) helpers                      #
# ---------------------------------------------------------------- #
#
# Salesforce Files (ContentVersion) is the standard way Lightning rich
# text fields embed images. The lifecycle for cross-org rehosting is:
#
#     [SOURCE]                                 [TARGET]
#     download VersionData       ->  bytes ->  upload new ContentVersion
#     describe ContentVersion    ->  meta  ->  with FirstPublishLocationId
#                                                = target_kav_id
#                                              (auto creates ContentDocumentLink
#                                               ShareType=V, Visibility=AllUsers
#                                               by default)
#                                              -> capture new ContentVersionId
#                                                 + ContentDocumentId
#
# The new ContentVersionId is what the rewriter substitutes into the
# `servlet.shepherd/version/download/<id>` URL inside the article body.

def download_content_version_bytes(
    connector: "SFConnector", content_version_id: str
) -> bytes:
    """
    Stream the binary payload of a ContentVersion via the canonical
    `/sobjects/ContentVersion/{id}/VersionData` endpoint.

    We prefer this over GET-ing the rich-text URL because it sidesteps
    cookie-only servlets (some orgs require an active web session for
    `/sfc/servlet.shepherd/version/download/...`) and works uniformly
    against any Salesforce host, including community subdomains.
    """
    if not content_version_id:
        raise ImageApiError(
            "download_content_version_bytes() requires a non-empty content_version_id"
        )

    url = (
        f"{connector.instance_url}/services/data/{connector.api_version}"
        f"/sobjects/ContentVersion/{content_version_id}/VersionData"
    )
    try:
        resp = connector._request("GET", url, headers={"Accept": "*/*"})
    except Exception as e:  # noqa: BLE001
        raise ImageApiError(
            f"GET ContentVersion/{content_version_id}/VersionData failed: {e}"
        ) from e

    if resp.status_code != 200:
        raise ImageApiError(
            f"GET ContentVersion/{content_version_id}/VersionData returned "
            f"HTTP {resp.status_code}: {resp.text[:200]}"
        )
    if not resp.content:
        raise ImageApiError(
            f"GET ContentVersion/{content_version_id}/VersionData returned empty body"
        )
    return resp.content


def fetch_content_version_metadata(
    connector: "SFConnector", content_version_id: str
) -> ContentVersionMetadata | None:
    """
    Look up Title / PathOnClient / FileType / FileExtension / ContentSize
    for the source ContentVersion so we can recreate it faithfully on the
    target. Returns None if the record is not visible / does not exist.
    """
    if not content_version_id:
        return None
    soql = (
        "SELECT Title, PathOnClient, FileType, FileExtension, ContentSize "
        "FROM ContentVersion WHERE Id = '"
        f"{content_version_id}' LIMIT 1"
    )
    url = f"{connector.instance_url}/services/data/{connector.api_version}/query"
    try:
        resp = connector._request("GET", url, params={"q": soql})
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "ContentVersion metadata lookup failed for %s: %s",
            content_version_id, e,
        )
        return None
    try:
        rows = resp.json().get("records") or []
    except ValueError:
        return None
    if not rows:
        return None
    row = rows[0]
    return ContentVersionMetadata(
        title=str(row.get("Title") or content_version_id),
        path_on_client=str(row.get("PathOnClient") or f"{content_version_id}.bin"),
        file_type=row.get("FileType"),
        file_extension=row.get("FileExtension"),
        content_size=row.get("ContentSize"),
    )


def upload_content_version(
    connector: "SFConnector",
    *,
    content: bytes,
    title: str,
    path_on_client: str,
    first_publish_location_id: str | None = None,
    content_type: str | None = None,
) -> str:
    """
    Multipart-POST a new ContentVersion to the target org.

    When `first_publish_location_id` is supplied (typically the target
    Kav id), Salesforce automatically creates a ContentDocumentLink
    between the new file and that record with ShareType=V. This saves
    one round-trip per image.

    Returns the new ContentVersion's 18-char Id. Use
    `get_content_document_id()` to resolve its parent ContentDocumentId
    if you need it for an explicit re-link.
    """
    if not content:
        raise ImageApiError("upload_content_version() requires non-empty content")
    if not (title and path_on_client):
        raise ImageApiError(
            "upload_content_version() requires non-empty title and path_on_client"
        )

    if content_type is None:
        guessed, _ = mimetypes.guess_type(path_on_client)
        content_type = guessed or "application/octet-stream"

    metadata: dict = {"Title": title, "PathOnClient": path_on_client}
    if first_publish_location_id:
        metadata["FirstPublishLocationId"] = first_publish_location_id

    url = (
        f"{connector.instance_url}/services/data/{connector.api_version}"
        f"/sobjects/ContentVersion"
    )

    files = {
        # Salesforce expects a JSON metadata part literally named
        # "entity_content"; the binary part name is whatever the metadata
        # field is — `VersionData` for ContentVersion.
        "entity_content": (
            None,
            json.dumps(metadata),
            "application/json; charset=UTF-8",
        ),
        "VersionData": (path_on_client, content, content_type),
    }

    try:
        resp = connector._request(
            "POST",
            url,
            files=files,
            # Strip the connector's default JSON Content-Type so requests
            # can set its own multipart boundary header.
            headers={"Content-Type": None},  # type: ignore[dict-item]
        )
    except Exception as e:  # noqa: BLE001
        raise ImageApiError(
            f"POST ContentVersion failed (title={title}): {e}"
        ) from e

    try:
        body = resp.json()
    except ValueError as e:
        raise ImageApiError(
            f"ContentVersion upload returned non-JSON body: {resp.text[:200]}"
        ) from e

    if not body.get("success", True):
        raise ImageApiError(f"ContentVersion upload reported failure | body={body}")

    target_id = body.get("id")
    if not target_id:
        raise ImageApiError(
            f"ContentVersion upload response missing id | body={body}"
        )
    return str(target_id)


def get_content_document_id(
    connector: "SFConnector", content_version_id: str
) -> str | None:
    """
    Resolve `ContentVersion.ContentDocumentId` for the given version Id.
    Returns None if the record can't be queried.
    """
    if not content_version_id:
        return None
    soql = (
        "SELECT ContentDocumentId FROM ContentVersion WHERE Id = '"
        f"{content_version_id}' LIMIT 1"
    )
    url = f"{connector.instance_url}/services/data/{connector.api_version}/query"
    try:
        resp = connector._request("GET", url, params={"q": soql})
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "ContentDocumentId lookup failed for %s: %s",
            content_version_id, e,
        )
        return None
    try:
        rows = resp.json().get("records") or []
    except ValueError:
        return None
    if not rows:
        return None
    return rows[0].get("ContentDocumentId") or None


def link_content_to_record(
    connector: "SFConnector",
    *,
    content_document_id: str,
    linked_entity_id: str,
    share_type: str = "V",
    visibility: str = "AllUsers",
) -> str | None:
    """
    Insert a ContentDocumentLink so `linked_entity_id` (typically the
    target Kav id) can render `content_document_id`.

    Idempotent in practice: Salesforce silently no-ops a duplicate link
    request on the same (ContentDocumentId, LinkedEntityId) pair, but
    DUPLICATE_VALUE errors are caught and logged at debug level rather
    than raised, since the *outcome* (a link exists) is what matters.
    """
    if not (content_document_id and linked_entity_id):
        raise ImageApiError(
            "link_content_to_record() requires content_document_id + linked_entity_id"
        )

    url = (
        f"{connector.instance_url}/services/data/{connector.api_version}"
        f"/sobjects/ContentDocumentLink"
    )
    payload = {
        "ContentDocumentId": content_document_id,
        "LinkedEntityId": linked_entity_id,
        "ShareType": share_type,
        "Visibility": visibility,
    }
    try:
        resp = connector._request("POST", url, json=payload)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "DUPLICATE_VALUE" in msg or "duplicate value found" in msg.lower():
            logger.debug(
                "ContentDocumentLink already exists for %s -> %s",
                content_document_id, linked_entity_id,
            )
            return None
        raise ImageApiError(
            f"POST ContentDocumentLink ({content_document_id} -> {linked_entity_id}) "
            f"failed: {e}"
        ) from e

    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("id")
