"""
Unit tests for knowledge_article._image_api.

We mock SFConnector entirely. Every test exercises the wrapper's URL
construction and response parsing — no live Salesforce calls.
"""

from __future__ import annotations

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from knowledge_article import _image_api as api
from knowledge_article._image_api import (
    ContentVersionMetadata,
    ImageApiError,
    download_content_version_bytes,
    download_image_bytes,
    fetch_content_version_metadata,
    get_content_document_id,
    link_content_to_record,
    reset_feoid_cache,
    resolve_target_feoid,
    upload_content_version,
    upload_image,
)


# ---------------------------------------------------------------- #
#  Test doubles                                                     #
# ---------------------------------------------------------------- #

class _FakeResponse:
    """Minimal stand-in for requests.Response."""
    def __init__(self, *, status_code=200, content=b"", json_body=None, text=""):
        self.status_code = status_code
        self.content = content
        self.text = text or (content.decode("utf-8", errors="replace") if content else "")
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("not json")
        return self._json_body


class _FakeConnector:
    """Captures calls so tests can assert on URL/method/payload."""
    def __init__(self, instance_url="https://target.my.salesforce.com", api_version="v60.0"):
        self.instance_url = instance_url
        self.api_version = api_version
        self.calls: list[dict] = []
        self.next_response: _FakeResponse | None = None
        self.responses_by_url: dict[str, _FakeResponse] = {}

    def _request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if url in self.responses_by_url:
            return self.responses_by_url[url]
        if self.next_response is None:
            return _FakeResponse(status_code=200, content=b"OK")
        return self.next_response


# ---------------------------------------------------------------- #
#  download_image_bytes                                             #
# ---------------------------------------------------------------- #

def test_download_returns_bytes_on_200():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, content=b"\x89PNGfake")
    out = download_image_bytes(conn, "https://src.file.force.com/sfc/servlet/rtaImage?refid=R1")
    assert out == b"\x89PNGfake"
    assert conn.calls[0]["method"] == "GET"
    assert conn.calls[0]["kwargs"]["headers"] == {"Accept": "*/*"}


def test_download_rejects_empty_url():
    conn = _FakeConnector()
    try:
        download_image_bytes(conn, "")
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


def test_download_raises_on_non_200():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=404, content=b"missing", text="missing")
    try:
        download_image_bytes(conn, "https://src/file")
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "404" in str(e)


def test_download_raises_on_empty_body():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, content=b"")
    try:
        download_image_bytes(conn, "https://src/file")
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "empty" in str(e)


def test_download_wraps_underlying_exception():
    conn = _FakeConnector()

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")
    conn._request = boom  # type: ignore

    try:
        download_image_bytes(conn, "https://src/file")
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "network down" in str(e)


# ---------------------------------------------------------------- #
#  upload_image                                                     #
# ---------------------------------------------------------------- #

def test_upload_posts_to_richtext_endpoint_and_returns_refid():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=200,
        json_body={"contentReferenceId": "0EM1T00000ABCDEFG"},
    )
    result = upload_image(
        conn,
        object_name="Knowledge__kav",
        target_kav_id="kA1T0000000ABCAA0",
        target_field="Article_Body__c",
        content=b"\x89PNGfake",
        filename="hero.png",
    )
    assert result.target_refid == "0EM1T00000ABCDEFG"
    assert result.target_field == "Article_Body__c"
    assert result.target_kav_id == "kA1T0000000ABCAA0"

    call = conn.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://target.my.salesforce.com/services/data/v60.0"
        "/sobjects/Knowledge__kav/kA1T0000000ABCAA0/richTextImageFields/Article_Body__c"
    )
    assert "files" in call["kwargs"]
    files = call["kwargs"]["files"]
    name, content, content_type = files["FileData"]
    assert name == "hero.png"
    assert content == b"\x89PNGfake"
    assert content_type == "image/png"
    # Multipart boundary must be set by requests, so Content-Type header
    # is overridden to None to let requests rebuild it.
    assert call["kwargs"]["headers"]["Content-Type"] is None


def test_upload_falls_back_to_contentDocumentId_key():
    """Some org versions return contentDocumentId instead — accept both."""
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=200,
        json_body={"contentDocumentId": "069T00000001abcdEAA"},
    )
    result = upload_image(
        conn,
        object_name="Knowledge__kav",
        target_kav_id="kA1T0000000ABCAA0",
        target_field="Article_Body__c",
        content=b"x",
    )
    assert result.target_refid == "069T00000001abcdEAA"


def test_upload_rejects_missing_args():
    conn = _FakeConnector()
    try:
        upload_image(
            conn,
            object_name="Knowledge__kav",
            target_kav_id="",
            target_field="Article_Body__c",
            content=b"x",
        )
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


def test_upload_raises_when_response_lacks_refid():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, json_body={"foo": "bar"})
    try:
        upload_image(
            conn,
            object_name="Knowledge__kav",
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content=b"x",
        )
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "contentReferenceId" in str(e)


def test_upload_raises_on_non_json_body():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, content=b"not json", text="not json")
    try:
        upload_image(
            conn,
            object_name="Knowledge__kav",
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content=b"x",
        )
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "non-JSON" in str(e) or "not json" in str(e).lower()


# ---------------------------------------------------------------- #
#  resolve_target_feoid                                             #
# ---------------------------------------------------------------- #

def test_resolve_feoid_for_custom_field_via_tooling():
    reset_feoid_cache()
    conn = _FakeConnector()
    expected_url = "https://target.my.salesforce.com/services/data/v60.0/tooling/query"
    conn.responses_by_url[expected_url] = _FakeResponse(
        status_code=200,
        json_body={"records": [{"Id": "00N1T00000ABCDEFG"}]},
    )
    feoid = resolve_target_feoid(
        conn,
        object_name="Knowledge__kav",
        field_name="Article_Body__c",
    )
    assert feoid == "00N1T00000ABCDEFG"

    # Subsequent call hits the cache: no extra request.
    conn.calls.clear()
    feoid2 = resolve_target_feoid(
        conn,
        object_name="Knowledge__kav",
        field_name="Article_Body__c",
    )
    assert feoid2 == "00N1T00000ABCDEFG"
    assert conn.calls == []


def test_resolve_feoid_returns_none_when_field_not_found():
    reset_feoid_cache()
    conn = _FakeConnector()
    expected_url = "https://target.my.salesforce.com/services/data/v60.0/tooling/query"
    conn.responses_by_url[expected_url] = _FakeResponse(
        status_code=200,
        json_body={"records": []},
    )
    feoid = resolve_target_feoid(
        conn,
        object_name="Knowledge__kav",
        field_name="Article_Body__c",
    )
    assert feoid is None


def test_resolve_feoid_for_standard_field_falls_back_to_field_definition():
    reset_feoid_cache()
    conn = _FakeConnector()

    captured_queries: list[str] = []
    expected_url = "https://target.my.salesforce.com/services/data/v60.0/tooling/query"

    # Standard field name (no __c) → CustomField query is skipped, so we
    # only need a single response keyed on the FieldDefinition query.
    def _request(method, url, **kwargs):
        params = kwargs.get("params") or {}
        captured_queries.append(params.get("q", ""))
        if "FieldDefinition" in params.get("q", ""):
            return _FakeResponse(
                status_code=200,
                json_body={"records": [{"DurableId": "01I.00N1T00000DEFGHI"}]},
            )
        return _FakeResponse(status_code=200, json_body={"records": []})

    conn._request = _request  # type: ignore

    feoid = resolve_target_feoid(
        conn,
        object_name="Knowledge__kav",
        field_name="Summary",
    )
    assert feoid == "00N1T00000DEFGHI"
    # Only one query (standard fields skip the CustomField path entirely).
    assert len(captured_queries) == 1
    assert "FieldDefinition" in captured_queries[0]


def test_resolve_feoid_handles_tooling_failure_gracefully():
    reset_feoid_cache()
    conn = _FakeConnector()

    def _request(*_args, **_kwargs):
        raise RuntimeError("tooling 500")

    conn._request = _request  # type: ignore
    feoid = resolve_target_feoid(
        conn,
        object_name="Knowledge__kav",
        field_name="Article_Body__c",
    )
    assert feoid is None


def test_resolve_feoid_returns_none_for_blank_inputs():
    assert resolve_target_feoid(_FakeConnector(), object_name="", field_name="x") is None
    assert resolve_target_feoid(_FakeConnector(), object_name="x", field_name="") is None


def test_resolve_feoid_cache_keyed_by_instance_url():
    """A second connector pointing at a different org must not see cached
    feoids from the first connector — feoids differ between orgs."""
    reset_feoid_cache()
    conn_a = _FakeConnector(instance_url="https://a.my.salesforce.com")
    conn_b = _FakeConnector(instance_url="https://b.my.salesforce.com")
    url_a = "https://a.my.salesforce.com/services/data/v60.0/tooling/query"
    url_b = "https://b.my.salesforce.com/services/data/v60.0/tooling/query"
    conn_a.responses_by_url[url_a] = _FakeResponse(
        status_code=200, json_body={"records": [{"Id": "00N_A"}]}
    )
    conn_b.responses_by_url[url_b] = _FakeResponse(
        status_code=200, json_body={"records": [{"Id": "00N_B"}]}
    )

    a = resolve_target_feoid(conn_a, object_name="Knowledge__kav", field_name="Article_Body__c")
    b = resolve_target_feoid(conn_b, object_name="Knowledge__kav", field_name="Article_Body__c")
    assert a == "00N_A"
    assert b == "00N_B"


# ---------------------------------------------------------------- #
#  ContentVersion: download                                         #
# ---------------------------------------------------------------- #

def test_download_content_version_uses_versiondata_endpoint():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, content=b"\x89PNG-cv-bytes")
    out = download_content_version_bytes(conn, "068SOURCE000000001")
    assert out == b"\x89PNG-cv-bytes"
    assert len(conn.calls) == 1
    call = conn.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == (
        "https://target.my.salesforce.com/services/data/v60.0"
        "/sobjects/ContentVersion/068SOURCE000000001/VersionData"
    )


def test_download_content_version_rejects_blank_id():
    try:
        download_content_version_bytes(_FakeConnector(), "")
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


def test_download_content_version_raises_on_non_200():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=404, content=b"missing", text="missing")
    try:
        download_content_version_bytes(conn, "068SOURCE000000001")
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "404" in str(e)


def test_download_content_version_raises_on_empty_body():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, content=b"")
    try:
        download_content_version_bytes(conn, "068SOURCE000000001")
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "empty" in str(e)


# ---------------------------------------------------------------- #
#  ContentVersion: metadata                                         #
# ---------------------------------------------------------------- #

def test_fetch_content_version_metadata_returns_dataclass():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=200,
        json_body={
            "records": [
                {
                    "Title": "diagram",
                    "PathOnClient": "diagram.png",
                    "FileType": "PNG",
                    "FileExtension": "png",
                    "ContentSize": 1234,
                }
            ]
        },
    )
    meta = fetch_content_version_metadata(conn, "068SOURCE000000001")
    assert isinstance(meta, ContentVersionMetadata)
    assert meta.title == "diagram"
    assert meta.path_on_client == "diagram.png"
    assert meta.file_type == "PNG"
    assert meta.file_extension == "png"
    assert meta.content_size == 1234


def test_fetch_content_version_metadata_handles_empty_records():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, json_body={"records": []})
    assert fetch_content_version_metadata(conn, "068SOURCE000000001") is None


def test_fetch_content_version_metadata_handles_request_error():
    conn = _FakeConnector()

    def boom(*_a, **_k):
        raise RuntimeError("query down")
    conn._request = boom  # type: ignore
    assert fetch_content_version_metadata(conn, "068SOURCE000000001") is None


def test_fetch_content_version_metadata_returns_none_on_blank_id():
    assert fetch_content_version_metadata(_FakeConnector(), "") is None


# ---------------------------------------------------------------- #
#  ContentVersion: upload                                           #
# ---------------------------------------------------------------- #

def test_upload_content_version_posts_multipart_with_metadata_and_bytes():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=201,
        json_body={"id": "068TARGET000000001", "success": True},
    )
    target_id = upload_content_version(
        conn,
        content=b"\x89PNGfake",
        title="diagram",
        path_on_client="diagram.png",
        first_publish_location_id="kA1T0000000ABCAA0",
    )
    assert target_id == "068TARGET000000001"

    call = conn.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://target.my.salesforce.com/services/data/v60.0"
        "/sobjects/ContentVersion"
    )
    files = call["kwargs"]["files"]
    # Metadata part is named "entity_content" with JSON body.
    assert "entity_content" in files
    name, body, ctype = files["entity_content"]
    assert name is None
    assert "diagram" in body
    assert "diagram.png" in body
    assert "FirstPublishLocationId" in body
    assert "kA1T0000000ABCAA0" in body
    assert "application/json" in ctype
    # Binary part is named "VersionData".
    name, content, content_type = files["VersionData"]
    assert name == "diagram.png"
    assert content == b"\x89PNGfake"
    assert content_type == "image/png"
    # Multipart boundary set by requests, so caller forces None.
    assert call["kwargs"]["headers"]["Content-Type"] is None


def test_upload_content_version_omits_first_publish_when_not_provided():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=201,
        json_body={"id": "068TARGET000000002", "success": True},
    )
    upload_content_version(
        conn,
        content=b"x",
        title="t",
        path_on_client="t.png",
    )
    body = conn.calls[0]["kwargs"]["files"]["entity_content"][1]
    assert "FirstPublishLocationId" not in body


def test_upload_content_version_rejects_empty_content():
    try:
        upload_content_version(
            _FakeConnector(),
            content=b"",
            title="t",
            path_on_client="t.png",
        )
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


def test_upload_content_version_rejects_blank_metadata():
    try:
        upload_content_version(
            _FakeConnector(),
            content=b"x",
            title="",
            path_on_client="t.png",
        )
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


def test_upload_content_version_raises_when_response_lacks_id():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=200, json_body={"success": True}
    )
    try:
        upload_content_version(
            conn,
            content=b"x",
            title="t",
            path_on_client="t.png",
        )
        assert False, "expected ImageApiError"
    except ImageApiError as e:
        assert "id" in str(e)


def test_upload_content_version_raises_on_non_json_body():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=200, content=b"oops", text="oops"
    )
    try:
        upload_content_version(
            conn,
            content=b"x",
            title="t",
            path_on_client="t.png",
        )
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


# ---------------------------------------------------------------- #
#  ContentVersion: ContentDocumentId lookup + linking               #
# ---------------------------------------------------------------- #

def test_get_content_document_id_returns_value_from_query():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=200,
        json_body={"records": [{"ContentDocumentId": "069T0000000abcdEAA"}]},
    )
    cdid = get_content_document_id(conn, "068TARGET000000001")
    assert cdid == "069T0000000abcdEAA"


def test_get_content_document_id_returns_none_on_empty_records():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(status_code=200, json_body={"records": []})
    assert get_content_document_id(conn, "068TARGET000000001") is None


def test_get_content_document_id_returns_none_on_blank_input():
    assert get_content_document_id(_FakeConnector(), "") is None


def test_link_content_to_record_posts_expected_payload():
    conn = _FakeConnector()
    conn.next_response = _FakeResponse(
        status_code=201,
        json_body={"id": "06A0000000abcde", "success": True},
    )
    out = link_content_to_record(
        conn,
        content_document_id="069T0000000abcdEAA",
        linked_entity_id="kA1T0000000ABCAA0",
    )
    assert out == "06A0000000abcde"
    call = conn.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == (
        "https://target.my.salesforce.com/services/data/v60.0"
        "/sobjects/ContentDocumentLink"
    )
    payload = call["kwargs"]["json"]
    assert payload["ContentDocumentId"] == "069T0000000abcdEAA"
    assert payload["LinkedEntityId"] == "kA1T0000000ABCAA0"
    assert payload["ShareType"] == "V"
    assert payload["Visibility"] == "AllUsers"


def test_link_content_to_record_swallows_duplicate_value_error():
    conn = _FakeConnector()

    def boom(*_a, **_k):
        raise RuntimeError(
            "400 Client Error | DUPLICATE_VALUE: a record with this id already exists"
        )
    conn._request = boom  # type: ignore
    assert link_content_to_record(
        conn,
        content_document_id="069T",
        linked_entity_id="kA1T",
    ) is None


def test_link_content_to_record_raises_on_other_errors():
    conn = _FakeConnector()

    def boom(*_a, **_k):
        raise RuntimeError("500 Internal Server Error")
    conn._request = boom  # type: ignore
    try:
        link_content_to_record(
            conn,
            content_document_id="069T",
            linked_entity_id="kA1T",
        )
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


def test_link_content_to_record_rejects_blank_inputs():
    try:
        link_content_to_record(
            _FakeConnector(),
            content_document_id="",
            linked_entity_id="kA1T",
        )
        assert False, "expected ImageApiError"
    except ImageApiError:
        pass


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
