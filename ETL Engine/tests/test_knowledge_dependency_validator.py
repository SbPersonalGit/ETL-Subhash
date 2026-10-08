import csv
import tempfile
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from knowledge_article.dependency_validator import validate_flat_knowledge_dependencies


def _write_csv(path, body):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["SourceId", "Body"])
        writer.writeheader()
        writer.writerow({"SourceId": "ka0000000000001AAA", "Body": body})


def _config():
    return {
        "object": "Knowledge__kav",
        "mappings": [
            {"source": "SourceId", "target": "Id"},
            {"source": "Body", "target": "Article_Body__c"},
        ],
    }


def test_public_images_are_preserved_without_dependency_issue():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        source = Path(temp_dir) / "knowledge.csv"
        _write_csv(source, '<p><img src="https://cdn.example.com/a.png"></p>')
        report = validate_flat_knowledge_dependencies(source, _config())
        assert report["salesforce_image_count"] == 0
        assert report["record_link_count"] == 0
        assert report["issues"] == []


def test_salesforce_images_and_record_links_require_preparation():
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        source = Path(temp_dir) / "knowledge.csv"
        source_id = "001000000000001AAA"
        body = (
            '<img src="https://src.file.force.com/sfc/servlet/rtaImage'
            '?eid=ka0000000000001AAA&amp;feoid=Body&amp;refid=REF1">'
            f'<a href="/lightning/r/Account/{source_id}/view">Account</a>'
        )
        _write_csv(source, body)
        report = validate_flat_knowledge_dependencies(source, _config())
        assert report["salesforce_image_count"] == 1
        assert report["record_link_count"] == 1
        assert {issue["code"] for issue in report["issues"]} == {
            "SOURCE_SALESFORCE_IMAGES_UNAVAILABLE",
            "SOURCE_RECORD_ID_MAPPINGS_REQUIRED",
        }
        assert report["issues"][1]["examples"][0]["source_id"] == source_id


def test_existing_idmapper_entry_resolves_record_link():
    class Mapper:
        def get_target_id(self, object_name, source_id):
            return "001000000000009AAA" if object_name == "Account" else None

    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
        source = Path(temp_dir) / "knowledge.csv"
        _write_csv(
            source,
            '<a href="/lightning/r/Account/001000000000001AAA/view">Account</a>',
        )
        report = validate_flat_knowledge_dependencies(source, _config(), Mapper())
        assert report["resolved_record_link_count"] == 1
        assert report["unresolved_record_link_count"] == 0
        assert report["issues"] == []


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
