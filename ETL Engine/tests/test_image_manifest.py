"""
Unit tests for knowledge_article.image_manifest.ImageManifest.

Run any of:
    python tests/test_image_manifest.py
    python -m pytest tests/test_image_manifest.py
"""

from __future__ import annotations

import os
import tempfile

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from knowledge_article.image_manifest import ImageManifest, ImageManifestEntry


def _temp_path() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return tmp.name


def test_lookup_returns_none_when_empty():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        result = manifest.lookup(
            target_kav_id="kA1T0000000ABC",
            target_field="Article_Body__c",
            content_hash="sha256:" + "0" * 64,
        )
        assert result is None
        assert manifest.total_count() == 0
    finally:
        manifest.close()
        os.unlink(path)


def test_record_then_lookup_round_trip():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        entry = manifest.record(
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "a" * 64,
            target_eid="kA1T0000000ABCAA0",
            target_feoid="00N1T00000DEFGHI",
            target_refid="0EM1T0000004567XYZ",
            source_kav_id="kA00X0000004ABCAA0",
            source_refid="0EM5400000ABCDEF",
        )
        assert isinstance(entry, ImageManifestEntry)
        assert entry.target_refid == "0EM1T0000004567XYZ"

        round_trip = manifest.lookup(
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "a" * 64,
        )
        assert round_trip is not None
        assert round_trip.target_refid == "0EM1T0000004567XYZ"
        assert round_trip.target_feoid == "00N1T00000DEFGHI"
        assert round_trip.source_refid == "0EM5400000ABCDEF"
        assert manifest.total_count() == 1
    finally:
        manifest.close()
        os.unlink(path)


def test_record_is_idempotent_on_same_key():
    """Re-record on same (kav, field, hash) should update the row, not duplicate."""
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        manifest.record(
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "b" * 64,
            target_eid="kA1T0000000ABCAA0",
            target_feoid="00N1T00000ORIGINAL",
            target_refid="REF_ORIGINAL",
        )
        manifest.record(
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "b" * 64,
            target_eid="kA1T0000000ABCAA0",
            target_feoid="00N1T00000UPDATED",
            target_refid="REF_UPDATED",
        )
        assert manifest.total_count() == 1

        result = manifest.lookup(
            target_kav_id="kA1T0000000ABCAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "b" * 64,
        )
        assert result.target_feoid == "00N1T00000UPDATED"
        assert result.target_refid == "REF_UPDATED"
    finally:
        manifest.close()
        os.unlink(path)


def test_record_separate_kavs_create_separate_rows():
    """The same content hash uploaded to two different target articles
    must result in two manifest rows — Salesforce scopes images per record."""
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        digest = "sha256:" + "c" * 64
        manifest.record(
            target_kav_id="kA1T0000000ABCAAA",
            target_field="Article_Body__c",
            content_hash=digest,
            target_eid="kA1T0000000ABCAAA",
            target_feoid="00N",
            target_refid="REFA",
        )
        manifest.record(
            target_kav_id="kA1T0000000DEFAAA",
            target_field="Article_Body__c",
            content_hash=digest,
            target_eid="kA1T0000000DEFAAA",
            target_feoid="00N",
            target_refid="REFB",
        )
        assert manifest.total_count() == 2

        rows = manifest.lookup_by_hash(digest)
        assert len(rows) == 2
        refids = {r.target_refid for r in rows}
        assert refids == {"REFA", "REFB"}
    finally:
        manifest.close()
        os.unlink(path)


def test_record_separate_fields_create_separate_rows():
    """Different rich-text fields on the same target article are uploaded
    independently and so get their own manifest rows."""
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        digest = "sha256:" + "d" * 64
        manifest.record(
            target_kav_id="kA1T0000000XYZAAA",
            target_field="Article_Body__c",
            content_hash=digest,
            target_eid="kA1T0000000XYZAAA",
            target_feoid="00N",
            target_refid="REF_BODY",
        )
        manifest.record(
            target_kav_id="kA1T0000000XYZAAA",
            target_field="Summary",
            content_hash=digest,
            target_eid="kA1T0000000XYZAAA",
            target_feoid="00N",
            target_refid="REF_SUMMARY",
        )
        assert manifest.total_count() == 2

        body = manifest.lookup(
            target_kav_id="kA1T0000000XYZAAA",
            target_field="Article_Body__c",
            content_hash=digest,
        )
        summary = manifest.lookup(
            target_kav_id="kA1T0000000XYZAAA",
            target_field="Summary",
            content_hash=digest,
        )
        assert body.target_refid == "REF_BODY"
        assert summary.target_refid == "REF_SUMMARY"
    finally:
        manifest.close()
        os.unlink(path)


def test_record_rejects_empty_required_fields():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        try:
            manifest.record(
                target_kav_id="",
                target_field="Article_Body__c",
                content_hash="sha256:" + "0" * 64,
                target_eid="kA1",
                target_feoid="00N",
                target_refid="REF",
            )
            assert False, "expected ValueError on empty target_kav_id"
        except ValueError:
            pass

        try:
            manifest.record(
                target_kav_id="kA1",
                target_field="Article_Body__c",
                content_hash="sha256:" + "0" * 64,
                target_eid="",
                target_feoid="00N",
                target_refid="REF",
            )
            assert False, "expected ValueError on empty target_eid"
        except ValueError:
            pass
    finally:
        manifest.close()
        os.unlink(path)


def test_reset_clears_all_rows():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        manifest.record(
            target_kav_id="kA1T0000000RESET",
            target_field="Article_Body__c",
            content_hash="sha256:" + "e" * 64,
            target_eid="kA1T0000000RESET",
            target_feoid="00N",
            target_refid="R",
        )
        assert manifest.total_count() == 1
        manifest.reset()
        assert manifest.total_count() == 0
    finally:
        manifest.close()
        os.unlink(path)


def test_transaction_rolls_back_on_exception():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        try:
            with manifest.transaction():
                manifest.record(
                    target_kav_id="kA1T0000000TXN",
                    target_field="Article_Body__c",
                    content_hash="sha256:" + "f" * 64,
                    target_eid="kA1T0000000TXN",
                    target_feoid="00N",
                    target_refid="R",
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        assert manifest.total_count() == 0
    finally:
        manifest.close()
        os.unlink(path)


def test_lookup_handles_blank_inputs():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        assert manifest.lookup(target_kav_id="", target_field="x", content_hash="y") is None
        assert manifest.lookup(target_kav_id="x", target_field="", content_hash="y") is None
        assert manifest.lookup(target_kav_id="x", target_field="y", content_hash="") is None
        assert manifest.lookup_by_hash("") == []
    finally:
        manifest.close()
        os.unlink(path)


# ---------------------------------------------------------------- #
#  ContentVersion-kind manifest entries                             #
# ---------------------------------------------------------------- #

def test_record_content_version_kind_round_trip():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        entry = manifest.record(
            target_kav_id="kA1T0000000CVAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "1" * 64,
            kind="content_version",
            target_content_version_id="068TARGET000000001",
            target_content_document_id="069TARGET000000001",
            source_kav_id="kA00X0000004ABCAA0",
            source_content_version_id="068SOURCE000000001",
        )
        assert entry.kind == "content_version"
        assert entry.target_content_version_id == "068TARGET000000001"
        assert entry.target_content_document_id == "069TARGET000000001"
        # target_eid auto-defaults to target_kav_id for cv entries.
        assert entry.target_eid == "kA1T0000000CVAA0"

        looked = manifest.lookup(
            target_kav_id="kA1T0000000CVAA0",
            target_field="Article_Body__c",
            content_hash="sha256:" + "1" * 64,
        )
        assert looked is not None
        assert looked.kind == "content_version"
        assert looked.target_content_version_id == "068TARGET000000001"
        assert looked.target_content_document_id == "069TARGET000000001"
        assert looked.source_content_version_id == "068SOURCE000000001"
    finally:
        manifest.close()
        os.unlink(path)


def test_record_content_version_requires_target_version_id():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        try:
            manifest.record(
                target_kav_id="kA1T0000000CVAA0",
                target_field="Article_Body__c",
                content_hash="sha256:" + "2" * 64,
                kind="content_version",
                target_content_version_id="",
            )
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        manifest.close()
        os.unlink(path)


def test_record_rejects_unknown_kind():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        try:
            manifest.record(
                target_kav_id="kA1T0000000CVAA0",
                target_field="Article_Body__c",
                content_hash="sha256:" + "3" * 64,
                kind="bogus",
            )
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        manifest.close()
        os.unlink(path)


def test_rta_and_content_version_entries_coexist():
    path = _temp_path()
    manifest = ImageManifest(path=path)
    try:
        manifest.record(
            target_kav_id="kA1T0000000COEXIST",
            target_field="Article_Body__c",
            content_hash="sha256:" + "4" * 64,
            target_eid="kA1T0000000COEXIST",
            target_feoid="00N",
            target_refid="REF_OLD",
            kind="rta",
        )
        manifest.record(
            target_kav_id="kA1T0000000COEXIST",
            target_field="Article_Body__c",
            content_hash="sha256:" + "5" * 64,
            kind="content_version",
            target_content_version_id="068NEW",
        )
        assert manifest.total_count() == 2
    finally:
        manifest.close()
        os.unlink(path)


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
