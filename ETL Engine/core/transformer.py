"""
Transformer — applies the full transformation pipeline to staged raw data.

Pipeline per object:
  1. Load raw chunk from Stager
  2. Remap all lookup/reference field IDs (while source API names are intact)
  3. Apply field-level transform rules (including source→target field_rename)
  4. Handle cyclic fields: null them out for Pass 1, defer for Pass 2
  5. Validate required fields are non-null
  6. Write transformed chunk to xfm staging table

All processing is chunked — never loads full dataset into memory.
"""

import logging
import re
import polars as pl
from pathlib import Path
from core.stager import Stager
from core.id_mapper import IDMapper
from core.field_rules import ObjectFieldProfile, owner_id_uses_user_external_id, resolve_single_lookup_reference
from core.exclusion_rules import ExclusionRules
from core.connector import SFConnector
from transforms.rules import apply_rules

_EMAIL_PATTERN = re.compile(r"\S+@\S+\.\S+")

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


class Transformer:
    def __init__(
        self,
        stager: Stager,
        id_mapper: IDMapper,
        source_connector: SFConnector | None = None,
        target_connector: SFConnector | None = None,
        mask_emails: bool = False,
        remove_email_mask: bool = False,
    ):
        self.stager = stager
        self.id_mapper = id_mapper
        self.exclusion_rules = ExclusionRules()
        self.source_connector = source_connector
        self.target_connector = target_connector
        self.mask_emails = mask_emails
        self.remove_email_mask = remove_email_mask
        self._record_type_id_cache: dict[tuple[str, str], str | None] = {}
        self._target_relationship_cache: dict[tuple[str, str], str] = {}
        self.last_cyclic_failed_ids: set[str] = set()
        self.last_cyclic_failed_files: list[str] = []

    # ------------------------------------------------------------------ #
    #  Main transform entry point                                         #
    # ------------------------------------------------------------------ #

    def transform(
        self,
        object_name: str,
        profile: ObjectFieldProfile,
        transform_rules: list[dict],
        cyclic_fields: list[str] | None = None,
        owner_id_field: str = "OwnerId",
        external_id_field: str = "External_Id__c",
        ref_ext_id_map: dict[str, str] | None = None,
        lookup_mappings: dict[str, dict] | None = None,
        record_type_mappings: dict[str, str] | None = None,
        source_key_field: str = "Id",
    ) -> int:
        """
        Transform all raw staged chunks for object_name.
        Writes output to xfm staging table.
        Returns total records transformed.
        """
        self.stager.drop_table(object_name, "xfm")
        cyclic_fields = cyclic_fields or []
        total = 0
        excluded_fields: set[str] = set()

        for chunk in self.stager.read_raw_chunks(object_name):
            # Remap lookups while columns still use source API names.
            # field_rename (and other transforms) run after so renamed lookup
            # fields cannot bypass remapping.
            chunk = self._remap_lookups(
                chunk,
                object_name,
                profile,
                cyclic_fields,
                owner_id_field,
                external_id_field,
                ref_ext_id_map or {},
                lookup_mappings or {},
                record_type_mappings or {},
                source_key_field,
            )
            chunk = self._apply_transforms(chunk, transform_rules)
            chunk, chunk_excluded_fields = self._apply_exclusions(chunk, object_name, profile)
            excluded_fields.update(chunk_excluded_fields)
            chunk = self._validate(chunk, object_name, profile, excluded_fields=chunk_excluded_fields)
            if self.mask_emails:
                chunk = self._mask_emails(chunk, object_name)
            elif self.remove_email_mask:
                chunk = self._remove_email_mask(chunk, object_name)
            self.stager.write_xfm_chunk(object_name, chunk)
            total += len(chunk)
            logger.debug(f"[{object_name}] Transformed {total:,} records so far...")

        if excluded_fields:
            logger.info(
                f"[{object_name}] Applied exclusion rules | dropped/suppressed fields: {sorted(excluded_fields)}"
            )
        logger.info(f"[{object_name}] Transformation complete | {total:,} records")
        return total

    # ------------------------------------------------------------------ #
    #  Email masking — append .Invalid to email values                   #
    # ------------------------------------------------------------------ #

    def _mask_emails(self, df: pl.DataFrame, object_name: str) -> pl.DataFrame:
        """Append .Invalid to any cell value matching an email pattern."""
        email_cols = [
            col for col, dtype in zip(df.columns, df.dtypes)
            if dtype == pl.Utf8
        ]
        if not email_cols:
            return df
        masked_count = 0
        exprs = []
        for col in email_cols:
            exprs.append(
                pl.col(col)
                .str.replace_all(r"(\S+@\S+\.\S+)\.Invalid(\s|$)", r"${1}${2}")
                .str.replace_all(r"(\S+@\S+\.\S+)(\s|$)", r"${1}.Invalid${2}")
                .alias(col)
            )
        df = df.with_columns(exprs)
        for col in email_cols:
            masked_count += df[col].str.contains(r"\.Invalid$").sum()
        if masked_count:
            logger.info(
                f"[{object_name}] Masked {masked_count:,} email value(s) "
                f"across {len(email_cols)} column(s)"
            )
        return df

    def _remove_email_mask(self, df: pl.DataFrame, object_name: str) -> pl.DataFrame:
        """Remove trailing .Invalid from masked email values."""
        email_cols = [
            col for col, dtype in zip(df.columns, df.dtypes)
            if dtype == pl.Utf8
        ]
        if not email_cols:
            return df
        unmasked_count = 0
        exprs = []
        for col in email_cols:
            exprs.append(
                pl.col(col).str.replace_all(
                    r"(\S+@\S+\.\S+?)\.Invalid(\s|$)", r"${1}${2}"
                ).alias(col)
            )
        df = df.with_columns(exprs)
        for col in email_cols:
            unmasked_count += df[col].str.contains(r"\S+@\S+\.\S+(\s|$)").sum()
        if unmasked_count:
            logger.info(
                f"[{object_name}] Removed email mask from {unmasked_count:,} value(s) "
                f"across {len(email_cols)} column(s)"
            )
        return df

    # ------------------------------------------------------------------ #
    #  Step 1 — Apply configured transform rules                         #
    # ------------------------------------------------------------------ #

    def _apply_transforms(self, df: pl.DataFrame, rules: list[dict]) -> pl.DataFrame:
        if not rules:
            return df
        return apply_rules(df, rules)

    # ------------------------------------------------------------------ #
    #  Step 2 — Remap all lookup/reference IDs                           #
    # ------------------------------------------------------------------ #

    def _remap_lookups(
        self,
        df: pl.DataFrame,
        object_name: str,
        profile: ObjectFieldProfile,
        cyclic_fields: list[str],
        owner_id_field: str,
        external_id_field: str,
        ref_ext_id_map: dict[str, str],
        lookup_mappings: dict[str, dict],
        record_type_mappings: dict[str, str] | None = None,
        source_key_field: str = "Id",
    ) -> pl.DataFrame:
        for field_name, referenced_objects in profile.lookup_fields.items():
            explicit_lookup = lookup_mappings.get(field_name) or {}
            field_meta = profile.field_meta.get(field_name)
            rel_name = field_meta.relationship_name if field_meta else ""
            existing_rel_cols = [
                c for c in df.columns
                if rel_name and c.startswith(f"{rel_name}.")
            ]

            if field_name not in df.columns and not existing_rel_cols:
                continue

            # --- Handle cyclic fields: null out for Pass 1 ---
            if field_name in cyclic_fields:
                source_key_col = (
                    source_key_field if source_key_field in df.columns
                    else "Id" if "Id" in df.columns
                    else external_id_field if external_id_field in df.columns
                    else None
                )
                deferred_source_col = None
                used_relationship_reference = False
                referenced_source_key = str(
                    explicit_lookup.get("referenced_source_key") or ""
                ).strip()
                matching_relationship_col = next((
                    col for col in existing_rel_cols
                    if referenced_source_key and col.endswith(f".{referenced_source_key}")
                ), None)
                if matching_relationship_col:
                    deferred_source_col = matching_relationship_col
                    used_relationship_reference = True
                elif field_name in df.columns:
                    deferred_source_col = field_name
                elif existing_rel_cols:
                    deferred_source_col = existing_rel_cols[0]
                    used_relationship_reference = True

                if source_key_col and deferred_source_col:
                    self.id_mapper.save_deferred_column(
                        object_name, field_name,
                        df[source_key_col].cast(pl.Utf8),
                        df[deferred_source_col].cast(pl.Utf8),
                    )

                drop_cols = [col for col in [field_name, *existing_rel_cols] if col in df.columns]
                if drop_cols:
                    df = df.drop(drop_cols)
                logger.debug(
                    f"[{object_name}] Cyclic field '{field_name}' deferred for Pass 2 "
                    f"using {'relationship reference column' if used_relationship_reference else 'direct field'}"
                )
                continue

            if field_name == "RecordTypeId":
                df = self._remap_record_type_ids(
                    df,
                    object_name,
                    field_name,
                    record_type_mappings or {},
                )
                continue

            # A target_id/preserve policy means the incoming value is already a
            # valid ID in the destination org. Honor it before polymorphic
            # handling: WhoId/WhatId can reference hundreds of object types, and
            # attempting an IDMapper pass for each type is both incorrect and
            # prohibitively expensive for pre-resolved flat-file inputs.
            resolution = str(explicit_lookup.get("lookup_resolution") or "").strip().lower()
            if resolution in {"preserve", "target_id"}:
                logger.debug(
                    f"[{object_name}] Lookup '{field_name}' preserved as-is "
                    f"(lookup_resolution={resolution})"
                )
                continue
            if resolution in {"polymorphic_external_id", "polymorphic_target_first"}:
                df = self._resolve_polymorphic_external_ids(
                    df, field_name, explicit_lookup.get("polymorphic_mappings") or [],
                    target_first=resolution == "polymorphic_target_first",
                )
                continue

            # Relationship-less single-object references cannot use
            # Relationship.ExternalId syntax. Resolve their source external
            # keys to concrete target IDs before insert. This is required for
            # create-only references such as EmailMessage.ActivityId.
            if len(referenced_objects) == 1 and not str(rel_name or "").strip():
                external_key = str(
                    explicit_lookup.get("external_id")
                    or explicit_lookup.get("lookup_external_id")
                    or ref_ext_id_map.get(referenced_objects[0])
                    or ""
                ).strip()
                if external_key and resolution in {
                    "external_id",
                    "object_template",
                    "target_external_id",
                    "deferred_external_id",
                }:
                    df = self._resolve_external_id_to_target_id(
                        df,
                        field_name,
                        referenced_objects[0],
                        external_key,
                    )
                    continue

            # --- Handle polymorphic fields (WhatId, WhoId) ---
            # OwnerId is polymorphic in describe (User/Group). SF-to-SF runs with a
            # populated User ID map keep the polymorphic path; flat-file loads use
            # User.External_Id__c like CreatedById when no map exists.
            if field_name in profile.polymorphic_fields:
                if field_name == owner_id_field:
                    if owner_id_uses_user_external_id(
                        field_name, referenced_objects, explicit_lookup
                    ):
                        referenced_objects = ["User"]
                    elif self.id_mapper.has_mappings("User"):
                        df = self._remap_polymorphic(df, field_name, referenced_objects)
                        continue
                    elif "User" in referenced_objects:
                        referenced_objects = ["User"]
                    else:
                        df = self._remap_polymorphic(df, field_name, referenced_objects)
                        continue
                else:
                    df = self._remap_polymorphic(df, field_name, referenced_objects)
                    continue

            # --- Standard lookup: use external ID reference format ---
            if len(referenced_objects) == 1:
                ref_obj = referenced_objects[0]

                if existing_rel_cols:
                    if field_name in df.columns:
                        df = df.drop(field_name)
                    source_ref_key = str(explicit_lookup.get("referenced_source_key") or "").strip()
                    target_ext_key = str(explicit_lookup.get("external_id") or "").strip()
                    target_field = str(explicit_lookup.get("target_field") or field_name).strip()
                    target_rel_name = self._target_relationship_name(
                        object_name, target_field, rel_name
                    )
                    if source_ref_key and target_ext_key and target_rel_name:
                        expected_suffix = f".{source_ref_key}"
                        source_rel_col = next(
                            (col for col in existing_rel_cols if col.endswith(expected_suffix)),
                            existing_rel_cols[0],
                        )
                        target_rel_col = f"{target_rel_name}.{target_ext_key}"
                        if source_rel_col != target_rel_col:
                            if target_rel_col in df.columns:
                                df = df.drop(target_rel_col)
                            df = df.rename({source_rel_col: target_rel_col})
                    logger.debug(
                        f"[{object_name}] Lookup '{field_name}' → using extracted relationship reference "
                        f"column(s): {existing_rel_cols}"
                    )
                    continue

                # Special: OwnerId matched by Username via IDMapper when a User
                # map exists (SF → SF). In flat-file mode that map is usually
                # empty, so do not replace OwnerId with nulls. Send it the same
                # way CreatedBy/LastModifiedBy are sent: Owner.External_Id__c.
                if (
                    field_name == owner_id_field
                    and ref_obj == "User"
                    and not explicit_lookup.get("external_id")
                    and not explicit_lookup.get("lookup_resolution")
                ):
                    if self.id_mapper.has_mappings("User"):
                        df = self.id_mapper.resolve_column("User", df, field_name)
                        continue
                    if rel_name and field_name in df.columns:
                        owner_ext_id = (
                            str(explicit_lookup.get("external_id") or "").strip()
                            or ref_ext_id_map.get("User")
                            or "External_Id__c"
                        )
                        new_col = f"{rel_name}.{owner_ext_id}"
                        if new_col != field_name:
                            if new_col in df.columns:
                                df = df.drop(field_name)
                            else:
                                df = df.rename({field_name: new_col})
                        logger.debug(
                            f"[{object_name}] Lookup '{field_name}' → '{new_col}' "
                            "(no User ID map; using relationship external ID)"
                        )
                        continue

                # Only use RelationshipName.ExternalIdField format when the
                # referenced object is part of this migration (i.e. in ref_ext_id_map).
                # If the object is not being migrated, resolve via IDMapper instead.
                explicit_ref_object = explicit_lookup.get("referenced_object")
                ext_id_field = (
                    explicit_lookup.get("external_id")
                    if not explicit_ref_object or explicit_ref_object == ref_obj
                    else None
                ) or ref_ext_id_map.get(ref_obj)

                if resolution == "idmapper":
                    df = self.id_mapper.resolve_column(ref_obj, df, field_name)
                    logger.debug(
                        f"[{object_name}] Lookup '{field_name}' → resolved via IDMapper "
                        f"(explicit lookup_resolution=idmapper)"
                    )
                    continue

                # Only use RelationshipName.ExternalIdField format when we have
                # an external ID (from explicit mapping or in-migration parent).
                # Out-of-package parents (e.g. Pricebook2 already on target) work
                # when the user sets Object.ExternalId in lookup_mappings.
                target_field = str(explicit_lookup.get("target_field") or field_name).strip()
                target_rel_name = self._target_relationship_name(object_name, target_field, rel_name)
                if target_rel_name and ext_id_field:
                    new_col = f"{target_rel_name}.{ext_id_field}"
                    df = df.rename({field_name: new_col})
                    logger.debug(
                        f"[{object_name}] Lookup '{field_name}' → '{new_col}' "
                        f"(Salesforce will resolve by {ext_id_field})"
                    )
                else:
                    # In flat-file mode the target connector is also used as the
                    # metadata connector. A lookup to an object outside this
                    # migration may therefore already contain a valid target ID
                    # (for example Case.BusinessHoursId). Preserve it.
                    if self.source_connector is self.target_connector and self.target_connector is not None:
                        logger.debug(
                            f"[{object_name}] Lookup '{field_name}' preserved as a target-org reference "
                            f"({ref_obj} is outside the flat-file migration set)"
                        )
                        continue
                    # Referenced object not in migration set OR no relationship name
                    # → resolve via IDMapper (source ID → target ID)
                    df = self.id_mapper.resolve_column(ref_obj, df, field_name)
                    logger.debug(
                        f"[{object_name}] Lookup '{field_name}' → resolved via IDMapper "
                        f"({ref_obj} not in migration set)"
                    )

        return df

    def _target_relationship_name(
        self, object_name: str, target_field: str, fallback: str
    ) -> str:
        """Resolve relationshipName from target metadata for renamed lookups."""
        key = (object_name, target_field)
        if key in self._target_relationship_cache:
            return self._target_relationship_cache[key]
        relationship_name = str(fallback or "").strip()
        if self.target_connector is not None:
            try:
                fields = self.target_connector.describe_object(object_name).get("fields", [])
                target_meta = next(
                    (field for field in fields if field.get("name") == target_field), None
                )
                relationship_name = str(
                    (target_meta or {}).get("relationshipName") or fallback or ""
                ).strip()
            except Exception as exc:
                logger.warning(
                    f"[{object_name}] Could not resolve target relationship metadata for "
                    f"'{target_field}': {exc}"
                )
        self._target_relationship_cache[key] = relationship_name
        return relationship_name

    def _remap_record_type_ids(
        self,
        df: pl.DataFrame,
        object_name: str,
        field_name: str,
        record_type_mappings: dict[str, str] | None = None,
    ) -> pl.DataFrame:
        if field_name not in df.columns:
            return df

        explicit_mappings = {
            str(source).strip(): str(target).strip()
            for source, target in (record_type_mappings or {}).items()
            if str(source or "").strip() and str(target or "").strip()
        }

        if not self.source_connector or not self.target_connector:
            if explicit_mappings:
                return df.with_columns(
                    pl.col(field_name)
                    .cast(pl.Utf8)
                    .replace(explicit_mappings, default=None)
                    .alias(field_name)
                )
            logger.warning(
                f"[{object_name}] Source/target connectors unavailable for RecordTypeId remap; "
                f"preserving source values in '{field_name}'"
            )
            return df

        source_ids = [
            str(value).strip()
            for value in df[field_name].cast(pl.Utf8).drop_nulls().to_list()
            if str(value).strip()
        ]
        source_ids = sorted(set(source_ids))
        if not source_ids:
            return df

        for source_id, target_id in explicit_mappings.items():
            self._record_type_id_cache[(object_name, source_id)] = target_id

        uncached_ids = [
            source_id
            for source_id in source_ids
            if (object_name, source_id) not in self._record_type_id_cache
        ]
        if uncached_ids:
            self._populate_record_type_cache(object_name, uncached_ids)

        mapping_df = pl.DataFrame({
            field_name: source_ids,
            "__resolved_record_type_id__": [
                self._record_type_id_cache.get((object_name, source_id))
                for source_id in source_ids
            ],
        })

        df = df.join(mapping_df, on=field_name, how="left")
        unresolved_count = df.filter(
            pl.col(field_name).is_not_null() & pl.col("__resolved_record_type_id__").is_null()
        ).height
        if unresolved_count:
            logger.warning(
                f"[{object_name}] Could not resolve {unresolved_count:,} RecordTypeId value(s) "
                f"to target RecordType IDs; Salesforce may reject those rows"
            )

        return (
            df.drop(field_name)
            .rename({"__resolved_record_type_id__": field_name})
        )

    @staticmethod
    def _looks_like_salesforce_id(value: str) -> bool:
        text = str(value or "").strip()
        return len(text) in {15, 18} and text.isalnum()

    def _populate_record_type_cache(self, object_name: str, source_ids: list[str]):
        if not source_ids:
            return

        # Flat-file CSVs may already contain DeveloperName values (e.g. DS_IT).
        # Those cannot be queried with WHERE Id IN (...).
        id_values = [value for value in source_ids if self._looks_like_salesforce_id(value)]
        developer_values = [value for value in source_ids if not self._looks_like_salesforce_id(value)]

        developer_names: set[str] = set(developer_values)
        source_row_by_id: dict[str, dict] = {}
        if id_values:
            quoted_source_ids = ", ".join(f"'{self._escape_soql(value)}'" for value in id_values)
            source_rows = self.source_connector.query(
                "SELECT Id, DeveloperName, SobjectType "
                f"FROM RecordType WHERE Id IN ({quoted_source_ids})"
            )
            source_row_by_id = {
                row.get("Id"): row
                for row in source_rows
                if row.get("Id") and row.get("SobjectType") == object_name
            }
            developer_names.update(
                row.get("DeveloperName")
                for row in source_rows
                if row.get("DeveloperName") and row.get("SobjectType") == object_name
            )

        target_by_developer: dict[str, str] = {}
        if developer_names and self.target_connector:
            quoted_dev_names = ", ".join(
                f"'{self._escape_soql(value)}'" for value in sorted(developer_names)
            )
            target_rows = self.target_connector.query(
                "SELECT Id, DeveloperName "
                f"FROM RecordType WHERE SobjectType = '{self._escape_soql(object_name)}' "
                f"AND DeveloperName IN ({quoted_dev_names})"
            )
            target_by_developer = {
                row.get("DeveloperName"): row.get("Id")
                for row in target_rows
                if row.get("DeveloperName") and row.get("Id")
            }

        for source_id in source_ids:
            if self._looks_like_salesforce_id(source_id):
                row = source_row_by_id.get(source_id)
                developer_name = row.get("DeveloperName") if row else None
                self._record_type_id_cache[(object_name, source_id)] = (
                    target_by_developer.get(developer_name) if developer_name else None
                )
            else:
                self._record_type_id_cache[(object_name, source_id)] = target_by_developer.get(
                    source_id
                )

    @staticmethod
    def _escape_soql(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def _remap_polymorphic(
        self, df: pl.DataFrame, field_name: str, referenced_objects: list[str]
    ) -> pl.DataFrame:
        """
        Polymorphic fields (WhatId/WhoId) can reference multiple object types.
        Salesforce IDs encode the object type in the first 3 characters (key prefix).
        We try each referenced object's ID map until we find a match.
        """
        resolved = pl.Series(field_name, [None] * len(df), dtype=pl.Utf8)

        for ref_obj in referenced_objects:
            temp = self.id_mapper.resolve_column(ref_obj, df, field_name, f"__poly_{ref_obj}__")
            resolved_col = temp[f"__poly_{ref_obj}__"]
            # Fill nulls in resolved with values from this object's map
            resolved = pl.when(resolved.is_null()).then(resolved_col).otherwise(resolved)
            df = df.drop(f"__poly_{ref_obj}__") if f"__poly_{ref_obj}__" in df.columns else df

        return df.with_columns(resolved.alias(field_name))

    def _resolve_polymorphic_external_ids(
        self, df: pl.DataFrame, field_name: str, rules: list[dict], *, target_first: bool
    ) -> pl.DataFrame:
        """Resolve a mixed polymorphic column using explicit, user-audited rules."""
        if not self.target_connector or field_name not in df.columns:
            raise ValidationError(f"{field_name}: target connector is required for polymorphic external-ID resolution.")
        originals = [None if value is None else str(value).strip() for value in df[field_name].to_list()]
        resolved: list[str | None] = [None] * len(originals)
        for rule in rules:
            method = str(rule.get("identify_by") or "").strip()
            match_value = str(rule.get("match_value") or "").strip()
            companion = str(rule.get("companion_column") or "").strip()
            ref_object = str(rule.get("referenced_object") or "").strip()
            external_id = str(rule.get("external_id") or "").strip()
            indexes = []
            companion_values = df[companion].to_list() if method == "companion" and companion in df.columns else []
            for index, value in enumerate(originals):
                if not value or resolved[index] is not None:
                    continue
                matches = method == "fixed" or (method == "prefix" and value[:3] == match_value) or (
                    method == "companion" and str(companion_values[index] or "").strip().casefold() == match_value.casefold()
                )
                if matches:
                    indexes.append(index)
            if not indexes:
                continue
            unresolved = list(indexes)
            if target_first:
                id_indexes = [index for index in indexes if re.fullmatch(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?", originals[index] or "")]
                for start in range(0, len(id_indexes), 150):
                    batch = id_indexes[start:start + 150]
                    values = ",".join(f"'{self._escape_soql(originals[i])}'" for i in batch)
                    existing = {str(row.get("Id")) for row in self.target_connector.query(f"SELECT Id FROM {ref_object} WHERE Id IN ({values})") if row.get("Id")}
                    for index in batch:
                        if originals[index] in existing:
                            resolved[index] = originals[index]
                unresolved = [index for index in indexes if resolved[index] is None]
            for start in range(0, len(unresolved), 150):
                batch = unresolved[start:start + 150]
                values = ",".join(f"'{self._escape_soql(originals[i])}'" for i in batch)
                rows = self.target_connector.query(f"SELECT Id, {external_id} FROM {ref_object} WHERE {external_id} IN ({values})")
                by_key = {}
                duplicate_keys = set()
                for row in rows:
                    key, target_id = row.get(external_id), row.get("Id")
                    if key is None or not target_id:
                        continue
                    key = str(key)
                    if key in by_key and by_key[key] != str(target_id):
                        duplicate_keys.add(key)
                    by_key[key] = str(target_id)
                if duplicate_keys:
                    raise ValidationError(f"{field_name}: ambiguous {ref_object}.{external_id} values: {', '.join(sorted(duplicate_keys)[:5])}")
                for index in batch:
                    resolved[index] = by_key.get(originals[index]) or str(rule.get("fallback_id") or "").strip() or None
            preserve = str(rule.get("preserve_original_as") or "").strip()
            if preserve:
                if preserve not in df.columns:
                    df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias(preserve))
                preserved = df[preserve].cast(pl.Utf8).to_list()
                for index in indexes:
                    preserved[index] = originals[index]
                df = df.with_columns(pl.Series(preserve, preserved, dtype=pl.Utf8))
        unresolved_values = sorted({originals[index] for index, value in enumerate(resolved) if originals[index] and value is None})
        if unresolved_values:
            preview = ", ".join(unresolved_values[:5])
            raise ValidationError(
                f"{field_name}: {len(unresolved_values)} polymorphic value(s) were not resolved and had no fallback: {preview}"
            )
        return df.with_columns(pl.Series(field_name, resolved, dtype=pl.Utf8))

    def _resolve_external_id_to_target_id(
        self,
        df: pl.DataFrame,
        field_name: str,
        referenced_object: str,
        external_id: str,
    ) -> pl.DataFrame:
        """Resolve one relationship-less lookup column before target insert."""
        if not self.target_connector or field_name not in df.columns:
            raise ValidationError(
                f"{field_name}: target connector is required for target external-ID resolution."
            )
        originals = [
            None if value is None else str(value).strip()
            for value in df[field_name].cast(pl.Utf8).to_list()
        ]
        requested = sorted({value for value in originals if value})
        by_key: dict[str, str] = {}
        duplicate_keys: set[str] = set()
        for start in range(0, len(requested), 150):
            batch = requested[start:start + 150]
            values = ",".join(f"'{self._escape_soql(value)}'" for value in batch)
            rows = self.target_connector.query(
                f"SELECT Id, {external_id} FROM {referenced_object} "
                f"WHERE {external_id} IN ({values})"
            )
            for row in rows:
                key, target_id = row.get(external_id), row.get("Id")
                if key is None or not target_id:
                    continue
                key = str(key)
                if key in by_key and by_key[key] != str(target_id):
                    duplicate_keys.add(key)
                by_key[key] = str(target_id)
        if duplicate_keys:
            raise ValidationError(
                f"{field_name}: ambiguous {referenced_object}.{external_id} values: "
                f"{', '.join(sorted(duplicate_keys)[:5])}"
            )
        unresolved = sorted({value for value in requested if value not in by_key})
        if unresolved:
            raise ValidationError(
                f"{field_name}: {len(unresolved)} value(s) were not found in target "
                f"{referenced_object}.{external_id}; sample: {', '.join(unresolved[:5])}"
            )
        return df.with_columns(
            pl.Series(field_name, [by_key.get(value) if value else None for value in originals], dtype=pl.Utf8)
        )

    def _apply_exclusions(
        self,
        df: pl.DataFrame,
        object_name: str,
        profile: ObjectFieldProfile,
    ) -> tuple[pl.DataFrame, set[str]]:
        drop_columns, excluded_fields, default_values = self.exclusion_rules.apply(object_name, df.columns, profile)
        if object_name == "Account" and "Name" in drop_columns:
            drop_columns = [col for col in drop_columns if col != "Name"]
            excluded_fields = {field for field in excluded_fields if field != "Name"}
            logger.info(
                "[Account] Preserving 'Name' in transformed output for subtype-aware load handling"
            )
        if default_values:
            expressions = []
            for field_name, default_value in default_values.items():
                if field_name in df.columns:
                    expressions.append(
                        pl.when(pl.col(field_name).is_null() | (pl.col(field_name).cast(pl.Utf8).str.strip_chars() == ""))
                        .then(pl.lit(str(default_value)))
                        .otherwise(pl.col(field_name))
                        .alias(field_name)
                    )
                else:
                    expressions.append(pl.lit(str(default_value)).alias(field_name))
            if expressions:
                df = df.with_columns(expressions)
                logger.info(f"[{object_name}] Applied default values to transformed output: {sorted(default_values.keys())}")
        if not drop_columns:
            return df, set()

        logger.info(f"[{object_name}] Excluding columns from transformed output: {drop_columns}")
        return df.drop(drop_columns), excluded_fields

    # ------------------------------------------------------------------ #
    #  Step 3 — Validate required fields                                  #
    # ------------------------------------------------------------------ #

    def _validate(
        self,
        df: pl.DataFrame,
        object_name: str,
        profile: ObjectFieldProfile,
        excluded_fields: set[str] | None = None,
    ) -> pl.DataFrame:
        excluded_fields = excluded_fields or set()
        for field_name in profile.required_fields:
            if field_name in excluded_fields:
                logger.debug(
                    f"[{object_name}] Skipping required-field warning for intentionally excluded field '{field_name}'"
                )
                continue
            if field_name not in df.columns:
                field_meta = profile.field_meta.get(field_name)
                rel_name = field_meta.relationship_name if field_meta else ""
                if rel_name and any(c.startswith(f"{rel_name}.") for c in df.columns):
                    continue
                logger.warning(f"[{object_name}] Required field '{field_name}' missing from data")
                continue
            null_count = df[field_name].null_count()
            if null_count > 0:
                logger.warning(
                    f"[{object_name}] Required field '{field_name}' has {null_count} null values — "
                    f"these records will likely fail on load"
                )
        return df

    # ------------------------------------------------------------------ #
    #  Pass 2 — Resolve deferred cyclic fields after all records loaded  #
    # ------------------------------------------------------------------ #

    def resolve_cyclic_pass2(
        self,
        object_name: str,
        profile: ObjectFieldProfile,
        cyclic_fields: list[str],
        target_connector,
        attempt_number: int = 2,
        log_root: Path | None = None,
        referenced_external_id_field: str | None = None,
        lookup_mappings: dict[str, dict] | None = None,
    ) -> dict[str, int]:
        """
        After all records are in the target org (and ID map is populated),
        update the previously-nulled cyclic fields with the correct target IDs.
        Returns {field_name: updated_count}
        """
        from core.loader import Loader
        results = {}
        self.last_cyclic_failed_ids = set()
        self.last_cyclic_failed_files = []

        for field_name in cyclic_fields:
            referenced_objects = profile.lookup_fields.get(field_name, [])
            if len(referenced_objects) != 1:
                logger.warning(
                    f"[{object_name}] Skipping Pass 2 for '{field_name}' because it does not map to exactly one referenced object"
                )
                continue

            deferred_count = self.id_mapper.deferred_count(object_name, field_name)
            if deferred_count == 0:
                logger.info(f"[{object_name}] No deferred updates for '{field_name}'")
                continue

            logger.info(
                f"[{object_name}] Starting cyclic resolution | pass=2 | attempt={attempt_number} | field={field_name}"
            )

            updates_df = self.id_mapper.get_deferred_updates(
                object_name,
                field_name,
                referenced_objects[0],
            )
            if updates_df is None:
                updates_df = pl.DataFrame(schema={"target_record_id": pl.Utf8, "resolved_target_id": pl.Utf8})

            # A self-lookup may point to a parent migrated in an earlier run.
            # Resolve values absent from this run's ID map against the live
            # target object's configured external ID.
            explicit_lookup = (lookup_mappings or {}).get(field_name) or {}
            external_id_field = str(
                explicit_lookup.get("external_id")
                or explicit_lookup.get("lookup_external_id")
                or referenced_external_id_field
                or ""
            ).strip()
            deferred_df = self.id_mapper.get_deferred_records(object_name, field_name)
            if external_id_field and deferred_df is not None and not deferred_df.is_empty():
                already_resolved = set(
                    updates_df.get_column("target_record_id").drop_nulls().to_list()
                ) if not updates_df.is_empty() else set()
                unresolved = deferred_df.filter(
                    ~pl.col("target_record_id").is_in(list(already_resolved))
                )
                requested = sorted(set(
                    str(value) for value in unresolved.get_column("deferred_source_id").drop_nulls().to_list()
                    if str(value).strip()
                ))
                target_matches: dict[str, str] = {}
                for offset in range(0, len(requested), 150):
                    chunk = requested[offset:offset + 150]
                    literals = ",".join(
                        "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
                        for value in chunk
                    )
                    soql = (
                        f"SELECT Id, {external_id_field} FROM {referenced_objects[0]} "
                        f"WHERE {external_id_field} IN ({literals})"
                    )
                    for record in target_connector.query(soql):
                        key = record.get(external_id_field)
                        target_id = record.get("Id")
                        if key is not None and target_id:
                            target_matches[str(key)] = str(target_id)
                if target_matches:
                    external_updates = (
                        unresolved.with_columns(
                            pl.col("deferred_source_id")
                            .replace_strict(target_matches, default=None)
                            .alias("resolved_target_id")
                        )
                        .filter(pl.col("resolved_target_id").is_not_null())
                        .select(["target_record_id", "resolved_target_id"])
                    )
                    if not external_updates.is_empty():
                        updates_df = pl.concat(
                            [updates_df, external_updates], how="diagonal_relaxed"
                        ).unique(subset=["target_record_id"], keep="first")
                        logger.info(
                            f"[{object_name}] Pass 2 resolved {len(external_updates):,} "
                            f"'{field_name}' reference(s) from existing target "
                            f"{referenced_objects[0]}.{external_id_field}"
                        )

            if updates_df.is_empty():
                logger.info(f"[{object_name}] No resolvable deferred updates for '{field_name}'")
                continue

            # Build update payload: Id (target record id) + field value
            update_df = updates_df.rename({
                "target_record_id": "Id",
                "resolved_target_id": field_name,
            }).filter(pl.col(field_name).is_not_null())

            loader = Loader(target_connector, self.stager, log_root=log_root)
            count = loader.update_records(object_name, update_df)
            self.last_cyclic_failed_ids.update(loader.last_update_failed_ids)
            if loader.last_update_failed_file:
                self.last_cyclic_failed_files.append(loader.last_update_failed_file)
            results[field_name] = count
            logger.info(f"[{object_name}] Pass 2 cyclic fix: '{field_name}' → {count:,} records updated")

        return results
