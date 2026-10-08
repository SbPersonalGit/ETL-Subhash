"""Compile product-specific Knowledge settings into the shared object contract."""

from __future__ import annotations

from copy import deepcopy


KNOWLEDGE_OBJECT = "Knowledge__kav"


def compile_knowledge_policy(objects: list[dict], input_mode: str) -> list[dict]:
    """Attach safe defaults without replacing explicitly configured settings.

    Salesforce-source pipelines retain the complete Data Seed behaviour.  A
    flat-file pipeline has no authenticated source org, so source discovery and
    Salesforce image downloads are disabled while post-load record-link
    rewriting and publication remain available.
    """
    compiled = deepcopy(objects)
    for obj in compiled:
        if str(obj.get("name") or "").strip() != KNOWLEDGE_OBJECT:
            continue
        configured = obj.get("extensions")
        extensions = deepcopy(configured) if isinstance(configured, dict) else {}
        if input_mode == "flat_file":
            extensions.setdefault("scope_expansion", {"enabled": False})
            extensions.setdefault("image_handling", {"enabled": False})
        else:
            extensions.setdefault("scope_expansion", {"enabled": True})
            extensions.setdefault("image_handling", {"enabled": True})
        extensions.setdefault("url_rewriting", {"enabled": True})
        extensions.setdefault(
            "publishing",
            {"enabled": True, "only_if_source_status": "Online"},
        )
        obj["extensions"] = extensions
        if input_mode != "flat_file":
            fields = list(obj.get("fields") or [])
            required = ["Id", "PublishStatus"] + list(
                extensions.get("richtext_fields") or ("Article_Body__c", "Summary")
            )
            for field_name in required:
                if field_name not in fields:
                    fields.append(field_name)
            obj["fields"] = fields
    return compiled
