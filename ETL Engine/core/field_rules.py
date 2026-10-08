"""
Field Rules Engine — uses Salesforce Describe API to determine:
  - Which fields to extract (exclude system/formula/auto-number fields)
  - Which fields are required (must be present in load)
  - Which fields are lookup/master-detail (need ID remapping)
  - Which fields are external ID candidates
  - Data type metadata for type-safe transformation
"""

from dataclasses import dataclass, field
from typing import Optional
from core.connector import SFConnector


# Audit fields Salesforce can preserve on record creation when the org setting
# and migration-user permission are enabled. Describe commonly reports these
# as non-createable, so they require an explicit migration exception.
MIGRATABLE_AUDIT_FIELDS = {
    "CreatedDate",
    "CreatedById",
    "LastModifiedDate",
    "LastModifiedById",
}

# Fields that are never extractable/loadable regardless of describe flags
SYSTEM_FIELD_BLACKLIST = {
    "Id",
    "SystemModstamp",
    "IsDeleted",
    "LastActivityDate",
    "LastViewedDate",
    "LastReferencedDate",
    "MasterRecordId",
    # User authentication/session/platform state. These values are maintained
    # by Salesforce and cannot be loaded through normal insert/upsert DML.
    "LastLoginDate",
    "LastPasswordChangeDate",
    "PasswordExpirationDate",
    "NumberOfFailedLogins",
    "SuAccessExpirationDate",
    "OfflineTrialExpirationDate",
    "OfflinePdaTrialExpirationDate",
    "IsPartner",
}

# Field types that are always read-only (even if createable flag is inconsistent)
READONLY_FIELD_TYPES = {"address", "location", "anyType"}

# Lookup field types
REFERENCE_TYPES = {"reference"}

# Polymorphic owner field — Salesforce allows User or Group, but flat-file /
# migration loads normally resolve user owners via User.External_Id__c.
OWNER_ID_FIELD = "OwnerId"
USER_REFERENCE_OBJECT = "User"
OWNER_RELATIONSHIP_NAME = "Owner"
DEFAULT_USER_LOOKUP_EXTERNAL_ID = "External_Id__c"


def apply_default_owner_id_mapping(
    mapping: dict,
    field_describe: dict | None,
    lookup_meta: dict | None,
) -> None:
    """Default a new polymorphic OwnerId mapping to User.External_Id__c."""
    if str(mapping.get("target") or "").strip() != OWNER_ID_FIELD:
        return
    references = [str(value) for value in (field_describe or {}).get("referenceTo") or [] if value]
    # Some objects (including Campaign in certain orgs) describe OwnerId as a
    # User-only reference. It still needs the Owner mapping dialog and the
    # default User.External_Id__c rule used by polymorphic OwnerId fields.
    if USER_REFERENCE_OBJECT not in references:
        return
    # Never replace an explicit user choice, including target-ID mode.
    if str(mapping.get("lookup_resolution") or "").strip():
        return
    external_ids = list((lookup_meta or {}).get("external_id_fields") or [])
    if DEFAULT_USER_LOOKUP_EXTERNAL_ID not in external_ids:
        return
    mapping.update({
        "is_polymorphic": True,
        "referenced_objects": references,
        "referenced_object": USER_REFERENCE_OBJECT,
        "lookup_external_id_fields": external_ids,
        "lookup_external_id": "",
        "lookup_resolution": "polymorphic_external_id",
        "polymorphic_mappings": [{
            "identify_by": "prefix",
            "match_value": "005",
            "companion_column": "",
            "referenced_object": USER_REFERENCE_OBJECT,
            "external_id": DEFAULT_USER_LOOKUP_EXTERNAL_ID,
            "external_id_fields": external_ids,
        }],
    })


@dataclass
class FieldMeta:
    name: str
    label: str
    field_type: str             # string, reference, picklist, boolean, date, datetime, etc.
    createable: bool
    updateable: bool
    nillable: bool
    is_required: bool           # not nillable + createable
    is_lookup: bool             # type == 'reference'
    references_to: list[str]    # objects this field can point to (polymorphic = multiple)
    is_external_id: bool
    is_auto_number: bool
    is_formula: bool
    is_polymorphic: bool        # WhatId / WhoId style (references_to has >1 entry)
    relationship_name: str = ""  # e.g. "Pricebook2" for Pricebook2Id — used for ext ID ref format
    length: Optional[int] = None
    picklist_values: list[str] = field(default_factory=list)


@dataclass
class ObjectFieldProfile:
    object_name: str
    include_fields: list[str]           # fields to put in SOQL + load payload
    required_fields: list[str]          # subset of include_fields — must not be null
    lookup_fields: dict[str, list[str]] # field_name → [referenced_object, ...]
    external_id_fields: list[str]       # fields marked as external ID
    polymorphic_fields: list[str]       # WhatId/WhoId style — need special handling
    field_meta: dict[str, FieldMeta]    # full metadata keyed by field name


def build_field_profile(connector: SFConnector, object_name: str) -> ObjectFieldProfile:
    """
    Call Describe API and build a complete field profile for the object.
    This is the main entry point for the field rules engine.
    """
    describe = connector.describe_object(object_name)
    fields_raw = describe.get("fields", [])

    include_fields = []
    required_fields = []
    lookup_fields = {}
    external_id_fields = []
    polymorphic_fields = []
    field_meta = {}

    for f in fields_raw:
        name = f["name"]
        ftype = f["type"]

        # --- Hard exclusions ---
        if name in SYSTEM_FIELD_BLACKLIST:
            continue
        if ftype in READONLY_FIELD_TYPES:
            continue
        if f.get("calculated", False):         # formula
            continue
        if f.get("autoNumber", False):         # auto-number
            continue
        if (
            not f.get("createable", False)
            and not f.get("updateable", False)
            and name not in MIGRATABLE_AUDIT_FIELDS
        ):
            continue                            # system-managed, not writable at all
        if f.get("deprecatedAndHidden", False):
            continue

        # --- Build metadata ---
        refs = [r for r in (f.get("referenceTo") or []) if r]
        is_lookup = ftype in REFERENCE_TYPES
        is_poly = is_lookup and len(refs) > 1
        is_required = (not f.get("nillable", True)) and f.get("createable", False)
        is_ext_id = f.get("externalId", False)

        meta = FieldMeta(
            name=name,
            label=f.get("label", name),
            field_type=ftype,
            createable=f.get("createable", False),
            updateable=f.get("updateable", False),
            nillable=f.get("nillable", True),
            is_required=is_required,
            is_lookup=is_lookup,
            references_to=refs,
            is_external_id=is_ext_id,
            is_auto_number=f.get("autoNumber", False),
            is_formula=f.get("calculated", False),
            is_polymorphic=is_poly,
            relationship_name=f.get("relationshipName", ""),
            length=f.get("length"),
            picklist_values=[pv["value"] for pv in f.get("picklistValues", []) if pv.get("active")],
        )

        include_fields.append(name)
        field_meta[name] = meta

        if is_required:
            required_fields.append(name)
        if is_lookup:
            lookup_fields[name] = refs
        if is_ext_id:
            external_id_fields.append(name)
        if is_poly:
            polymorphic_fields.append(name)

    return ObjectFieldProfile(
        object_name=object_name,
        include_fields=include_fields,
        required_fields=required_fields,
        lookup_fields=lookup_fields,
        external_id_fields=external_id_fields,
        polymorphic_fields=polymorphic_fields,
        field_meta=field_meta,
    )


def resolve_single_lookup_reference(field_name: str, references: list[str]) -> str | None:
    """Return one referenced object when migration can treat the lookup as single-target."""
    refs = [ref for ref in references if ref]
    if len(refs) == 1:
        return refs[0]
    if field_name == OWNER_ID_FIELD and USER_REFERENCE_OBJECT in refs:
        return USER_REFERENCE_OBJECT
    return None


def is_polymorphic_reference_field(reference_to: list[str]) -> bool:
    refs = [ref for ref in reference_to if ref]
    return len(refs) > 1


def resolve_polymorphic_owner_reference_object(
    relationship_name: str,
    reference_to: list[str],
) -> str | None:
    """Owner is polymorphic in describe, but user owners resolve via User external ID."""
    refs = [ref for ref in reference_to if ref]
    if relationship_name == OWNER_RELATIONSHIP_NAME and USER_REFERENCE_OBJECT in refs:
        return USER_REFERENCE_OBJECT
    return None


def describe_lookup_external_ids(connector: SFConnector, referenced_object: str) -> dict:
    """Build lookup metadata for a referenced object's external-ID fields."""
    referenced_fields = connector.describe_object(referenced_object).get("fields", [])
    candidates = [
        ref_field.get("name")
        for ref_field in referenced_fields
        if ref_field.get("externalId") and ref_field.get("name")
    ]
    recommended = (
        DEFAULT_USER_LOOKUP_EXTERNAL_ID
        if DEFAULT_USER_LOOKUP_EXTERNAL_ID in candidates
        else candidates[0] if candidates else ""
    )
    return {
        "referenced_object": referenced_object,
        "external_id_fields": candidates,
        "recommended_external_id": recommended,
    }


def build_target_lookup_fields(
    connector: SFConnector,
    fields: list[dict],
    incoming_names: set[str],
) -> dict[str, dict]:
    """Return per-field lookup metadata for mapped flat-file / template columns."""
    lookup_fields: dict[str, dict] = {}
    for field in fields:
        field_name = str(field.get("name") or "")
        references = [ref for ref in (field.get("referenceTo") or []) if ref]
        if field.get("type") != "reference" or field_name.casefold() not in incoming_names:
            continue
        referenced_object = resolve_single_lookup_reference(field_name, references)
        if not referenced_object:
            continue
        lookup_fields[field_name] = describe_lookup_external_ids(connector, referenced_object)
    return lookup_fields


def enrich_flat_file_mapping_lookup(
    mapping: dict,
    field_describe: dict | None,
    connector: SFConnector | None = None,
) -> None:
    """Fill missing User owner lookup metadata on a flat-file mapping row."""
    if not isinstance(mapping, dict) or mapping.get("ignored"):
        return
    target_field = str(mapping.get("target") or "").strip()
    if not target_field:
        return
    if str(mapping.get("referenced_object") or "").strip():
        return
    field = field_describe or {}
    references = [ref for ref in (field.get("referenceTo") or []) if ref]
    referenced_object = resolve_single_lookup_reference(target_field, references)
    if not referenced_object or not connector:
        return
    lookup_meta = describe_lookup_external_ids(connector, referenced_object)
    mapping.setdefault("referenced_object", referenced_object)
    mapping.setdefault(
        "lookup_external_id_fields",
        lookup_meta.get("external_id_fields") or [],
    )
    if str(mapping.get("lookup_resolution") or "").strip() != "target_id":
        mapping.setdefault(
            "lookup_external_id",
            lookup_meta.get("recommended_external_id") or DEFAULT_USER_LOOKUP_EXTERNAL_ID,
        )
        mapping.setdefault("lookup_resolution", "external_id")


def owner_id_uses_user_external_id(
    field_name: str,
    referenced_objects: list[str],
    explicit_lookup: dict | None,
) -> bool:
    """True when OwnerId is explicitly configured to resolve via User external ID."""
    if field_name != OWNER_ID_FIELD:
        return False
    lookup = explicit_lookup or {}
    resolution = str(lookup.get("lookup_resolution") or "").strip().lower()
    if resolution in {"preserve", "target_id", "idmapper"}:
        return False
    explicit_ref = str(lookup.get("referenced_object") or "").strip()
    ext_id = str(
        lookup.get("external_id") or lookup.get("lookup_external_id") or ""
    ).strip()
    return explicit_ref == USER_REFERENCE_OBJECT and bool(ext_id)


def build_soql(profile: ObjectFieldProfile, query_filter: str = "") -> str:
    """Build a SOQL SELECT statement from field profile. Always includes Id."""
    fields_clause = ", ".join(["Id"] + profile.include_fields)
    soql = f"SELECT {fields_clause} FROM {profile.object_name}"
    normalized_filter = normalize_query_filter(query_filter)
    if normalized_filter:
        soql += f" {normalized_filter}"
    return soql


def normalize_query_filter(query_filter: str = "") -> str:
    where_part, tail_part = split_query_filter(query_filter)
    normalized = ""
    if where_part:
        normalized = f"WHERE {where_part}"
    if tail_part:
        normalized = f"{normalized} {tail_part}".strip()
    return normalized


def split_query_filter(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if not text:
        return "", ""

    where_idx = _find_top_level_keyword(text, "WHERE")
    clause_keywords = ("GROUP BY", "HAVING", "ORDER BY", "LIMIT", "OFFSET")
    clause_points = [
        _find_top_level_keyword(text, keyword)
        for keyword in clause_keywords
    ]
    clause_points = [idx for idx in clause_points if idx != -1]
    tail_start = min(clause_points) if clause_points else -1

    where_part = ""
    tail_part = ""

    if where_idx != -1:
        where_end = tail_start if tail_start > where_idx else len(text)
        where_part = text[where_idx + len("WHERE"):where_end].strip()
    elif tail_start != -1:
        where_part = text[:tail_start].strip()
    else:
        where_part = text

    if tail_start != -1:
        tail_part = text[tail_start:].strip()

    return where_part, tail_part


def _find_top_level_keyword(text: str, keyword: str) -> int:
    upper = text.upper()
    target = keyword.upper()
    depth = 0
    in_quote = False
    i = 0

    while i <= len(text) - len(target):
        char = text[i]

        if char == "'" and (i == 0 or text[i - 1] != "\\"):
            in_quote = not in_quote
            i += 1
            continue

        if not in_quote:
            if char == "(":
                depth += 1
            elif char == ")" and depth > 0:
                depth -= 1
            elif depth == 0 and upper[i:i + len(target)] == target:
                before_ok = i == 0 or upper[i - 1].isspace()
                after_idx = i + len(target)
                after_ok = after_idx >= len(text) or upper[after_idx].isspace()
                if before_ok and after_ok:
                    return i

        i += 1

    return -1


def get_dependency_edges(profile: ObjectFieldProfile) -> list[tuple[str, str]]:
    """
    Return (object_name, referenced_object) edges for dependency graph.
    Excludes self-references (handled separately as cyclic).
    Excludes polymorphic fields (too ambiguous for ordering).
    """
    edges = []
    for field_name, refs in profile.lookup_fields.items():
        if len(refs) == 1:
            ref = refs[0]
            if ref != profile.object_name:     # skip self-reference here
                edges.append((profile.object_name, ref))
    return edges
