"""
Metadata Validator — pre-flight checks before migration.

Validates:
  1. Object exists on target org
  2. External ID field exists and is marked as externalId on target
  3. Every config field exists on target and is createable/updateable
  4. Picklist field values in source staging data exist in target org
  5. Required fields (nillable=False) are not null in staged data
"""

import logging
from dataclasses import dataclass, field
from core.connector import SFConnector
from core.stager import Stager

logger = logging.getLogger(__name__)


@dataclass
class FieldIssue:
    field_name: str
    issue: str
    severity: str  # "error" | "warning"


@dataclass
class ObjectValidationResult:
    object_name: str
    passed: bool
    issues: list[FieldIssue] = field(default_factory=list)

    def add_error(self, field_name: str, message: str):
        self.issues.append(FieldIssue(field_name, message, "error"))
        self.passed = False

    def add_warning(self, field_name: str, message: str):
        self.issues.append(FieldIssue(field_name, message, "warning"))


class MetadataValidator:
    """
    Validates migration config against source + target org metadata
    and optionally against staged data in DuckDB.
    """

    def __init__(
        self,
        source_connector: SFConnector,
        target_connector: SFConnector,
        stager: Stager | None = None,
    ):
        self.source = source_connector
        self.target = target_connector
        self.stager = stager
        self._target_describe_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    #  Public entry point                                                 #
    # ------------------------------------------------------------------ #

    def validate_pipeline(self, object_configs: list[dict]) -> list[ObjectValidationResult]:
        """
        Run all validation checks for a list of object configs.
        Returns one ObjectValidationResult per object.
        """
        target_objects = self._get_target_object_names()
        results = []

        for ocfg in object_configs:
            result = self._validate_object(ocfg, target_objects)
            results.append(result)

        return results

    # ------------------------------------------------------------------ #
    #  Per-object validation                                              #
    # ------------------------------------------------------------------ #

    def _validate_object(
        self, ocfg: dict, target_objects: set[str]
    ) -> ObjectValidationResult:
        object_name = ocfg["name"]
        result = ObjectValidationResult(object_name=object_name, passed=True)

        # 1. Object exists on target
        if object_name not in target_objects:
            result.add_error(object_name, f"Object '{object_name}' does not exist on target org")
            return result  # No point checking fields if object missing

        # Get target field map
        target_fields = self._get_target_fields(object_name)
        if not target_fields:
            result.add_error(object_name, f"Could not describe '{object_name}' on target org")
            return result

        # 2. External ID field exists and is writeable on target.
        # Note: the External ID FLAG is no longer required here. The loader
        # auto-detects: if the flag is present we use Bulk upsert; otherwise
        # we fall back to the match-by-text-field path (insert+update split).
        # The only objects in this category in practice are those Salesforce
        # forbids the flag on (Knowledge__kav being the canonical case).
        operation = (ocfg.get("operation") or "upsert").strip().lower()
        ext_id = (ocfg.get("external_id") or "").strip()
        if operation == "upsert":
            if not ext_id:
                result.add_error(
                    object_name,
                    f"No external ID field is configured for upsert on target '{object_name}'"
                )
            elif ext_id not in target_fields:
                result.add_error(
                    ext_id,
                    f"External ID field '{ext_id}' does not exist on target '{object_name}'"
                )
            else:
                fmeta = target_fields[ext_id]
                if not fmeta.get("externalId", False):
                    result.add_warning(
                        ext_id,
                        f"'{ext_id}' is not flagged External ID on target '{object_name}' — "
                        "loader will use the match-by load path (insert+update split)."
                    )
                if not fmeta.get("updateable", False) and not fmeta.get("createable", False):
                    result.add_error(
                        ext_id,
                        f"'{ext_id}' is not writeable on target '{object_name}'"
                    )

        # 3. Check each configured field
        config_fields = ocfg.get("fields", [])
        for fname in config_fields:
            if fname in ("Id",):
                continue  # system field, skip
            if fname not in target_fields:
                result.add_error(fname, f"Field '{fname}' does not exist on target '{object_name}'")
                continue

            fmeta = target_fields[fname]
            if not fmeta.get("createable", False) and not fmeta.get("updateable", False):
                result.add_warning(
                    fname,
                    f"Field '{fname}' is not createable/updateable on target — will be skipped by Salesforce"
                )

            # 4. Picklist value validation (if staging data exists)
            if fmeta.get("type") in ("picklist", "multipicklist") and self.stager:
                self._validate_picklist(object_name, fname, fmeta, result)

        # 5. Required field null check (if staging data exists)
        if self.stager and config_fields:
            self._validate_required_fields(object_name, config_fields, target_fields, result)

        return result

    # ------------------------------------------------------------------ #
    #  Picklist validation                                                #
    # ------------------------------------------------------------------ #

    def _validate_picklist(
        self,
        object_name: str,
        field_name: str,
        field_meta: dict,
        result: ObjectValidationResult,
    ):
        """Check that picklist values in staged data exist in target picklist."""
        if not self.stager.table_exists(f"raw_{object_name}"):
            return

        target_values = {
            pv["value"]
            for pv in field_meta.get("picklistValues", [])
            if pv.get("active", True)
        }
        if not target_values:
            return

        try:
            staged_values = self.stager.execute_sql(
                f'SELECT DISTINCT "{field_name}" FROM "raw_{object_name}" '
                f'WHERE "{field_name}" IS NOT NULL'
            )
            for val in staged_values[field_name].to_list():
                if val and val not in target_values:
                    result.add_error(
                        field_name,
                        f"Picklist value '{val}' in source data not valid on target "
                        f"(valid: {sorted(target_values)})"
                    )
        except Exception as e:
            logger.debug(f"Picklist check skipped for {object_name}.{field_name}: {e}")

    # ------------------------------------------------------------------ #
    #  Required field null check                                          #
    # ------------------------------------------------------------------ #

    def _validate_required_fields(
        self,
        object_name: str,
        config_fields: list[str],
        target_fields: dict,
        result: ObjectValidationResult,
    ):
        """Warn if required (non-nillable) fields have nulls in staged data."""
        if not self.stager.table_exists(f"raw_{object_name}"):
            return

        for fname in config_fields:
            if fname not in target_fields:
                continue
            fmeta = target_fields[fname]
            if not fmeta.get("nillable", True) and fmeta.get("createable", False):
                try:
                    null_count = self.stager.execute_sql(
                        f'SELECT count(*) AS cnt FROM "raw_{object_name}" '
                        f'WHERE "{fname}" IS NULL'
                    )["cnt"][0]
                    if null_count > 0:
                        result.add_warning(
                            fname,
                            f"Required field '{fname}' has {null_count:,} null value(s) in source data"
                        )
                except Exception as e:
                    logger.debug(f"Null check skipped for {object_name}.{fname}: {e}")

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _get_target_object_names(self) -> set[str]:
        sobjects = self.target.describe_global()
        return {o["name"] for o in sobjects}

    def _get_target_fields(self, object_name: str) -> dict[str, dict]:
        """Return {field_name: field_metadata} for an object on target."""
        if object_name in self._target_describe_cache:
            return self._target_describe_cache[object_name]
        try:
            describe = self.target.describe_object(object_name)
            fields = {f["name"]: f for f in describe.get("fields", [])}
            self._target_describe_cache[object_name] = fields
            return fields
        except Exception as e:
            logger.error(f"Describe failed for {object_name} on target: {e}")
            return {}
