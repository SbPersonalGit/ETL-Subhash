"""Offline dependency checks for flat-file Knowledge rich-text content."""

from __future__ import annotations

import csv
from pathlib import Path

from knowledge_article.richtext_parser import scan
from core.flat_file import open_csv


DEFAULT_RICHTEXT_FIELDS = ("Article_Body__c", "Summary")


def _richtext_source_columns(file_config: dict) -> list[str]:
    configured = (
        (file_config.get("extensions") or {}).get("richtext_fields")
        if isinstance(file_config.get("extensions"), dict)
        else None
    ) or DEFAULT_RICHTEXT_FIELDS
    targets = set(configured)
    return sorted({
        str(mapping.get("source") or "").strip()
        for mapping in file_config.get("mappings") or []
        if not mapping.get("ignored")
        and str(mapping.get("target") or "").strip() in targets
        and str(mapping.get("source") or "").strip()
    })


def validate_flat_knowledge_dependencies(
    source_path: Path,
    file_config: dict,
    id_mapper=None,
    row_limit: int | None = None,
) -> dict:
    """Inventory embedded SF dependencies and return blocking preparation issues.

    Public image URLs are intentionally not reported: they are preserved as-is.
    Salesforce-hosted images cannot be rehosted without a source-org session.
    Record links are resolvable only when their source IDs are present in the
    shared IDMapper at runtime; prevalidation reports the IDs so the operator can
    ensure dependent-record migrations have populated that mapping.
    """
    columns = _richtext_source_columns(file_config)
    image_urls: set[str] = set()
    record_links: dict[tuple[str, str], dict] = {}
    rows_scanned = 0
    with open_csv(source_path) as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            if row_limit is not None and rows_scanned >= row_limit:
                break
            rows_scanned += 1
            for column in columns:
                result = scan(row.get(column))
                image_urls.update(image.full_url for image in result.images if image.full_url)
                for link in result.record_links:
                    key = (link.object_name or "", link.record_id)
                    record_links.setdefault(key, {
                        "object": link.object_name,
                        "source_id": link.record_id,
                        "example_row": row_number,
                        "field": column,
                    })
    unresolved_links = []
    resolved_link_count = 0
    for link in record_links.values():
        object_name = str(link.get("object") or "").strip()
        target_id = (
            id_mapper.get_target_id(object_name, link["source_id"])
            if id_mapper is not None and object_name
            else None
        )
        if target_id:
            resolved_link_count += 1
            link["target_id"] = target_id
        else:
            unresolved_links.append(link)

    issues = []
    if image_urls:
        issues.append({
            "code": "SOURCE_SALESFORCE_IMAGES_UNAVAILABLE",
            "severity": "error",
            "count": len(image_urls),
            "message": (
                "Salesforce-hosted images require manual rehosting and URL replacement "
                "before File-to-Salesforce Knowledge migration."
            ),
            "examples": sorted(image_urls)[:20],
        })
    if unresolved_links:
        issues.append({
            "code": "SOURCE_RECORD_ID_MAPPINGS_REQUIRED",
            "severity": "error",
            "count": len(unresolved_links),
            "message": (
                "Embedded Salesforce record/article links require source-to-target IDMapper "
                "entries created by an earlier dependent-record migration or this pipeline."
            ),
            "examples": unresolved_links[:50],
        })
    return {
        "richtext_columns": columns,
        "rows_scanned": rows_scanned,
        "salesforce_image_count": len(image_urls),
        "record_link_count": len(record_links),
        "resolved_record_link_count": resolved_link_count,
        "unresolved_record_link_count": len(unresolved_links),
        "issues": issues,
    }
