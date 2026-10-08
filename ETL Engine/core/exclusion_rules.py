import json
import logging
import re
from pathlib import Path

from core.field_rules import ObjectFieldProfile


logger = logging.getLogger(__name__)


class ExclusionRules:
    def __init__(self, config_path: str = "config/exclusion_rules.json"):
        self.config_path = Path(config_path)
        self.rules = self._load_rules()

    def _load_rules(self) -> list[dict]:
        if not self.config_path.exists():
            logger.debug(f"[ExclusionRules] No config found at {self.config_path}; exclusions disabled")
            return []

        with self.config_path.open(encoding="utf-8") as f:
            payload = json.load(f)

        rules = payload.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError(f"Invalid exclusion rules file: {self.config_path} | 'rules' must be a list")
        return rules

    def apply(
        self,
        object_name: str,
        df_columns: list[str],
        profile: ObjectFieldProfile,
    ) -> tuple[list[str], set[str], dict[str, str]]:
        if not self.rules:
            return [], set(), {}

        columns = list(df_columns)
        drop_columns: list[str] = []
        excluded_fields: set[str] = set()
        default_values: dict[str, str] = {}

        for idx, rule in enumerate(self.rules, start=1):
            if not self._matches_scope(rule.get("scope", {}), object_name):
                continue

            matched = self._resolve_rule_columns(rule, columns, profile)
            if not matched:
                self._collect_default_values(rule, default_values)
                continue

            for col in matched:
                if col not in drop_columns:
                    drop_columns.append(col)
            excluded_fields.update(matched)
            self._collect_default_values(rule, default_values)

            logger.debug(
                f"[{object_name}] Exclusion rule {rule.get('name') or idx} matched columns: {matched}"
            )

        return drop_columns, excluded_fields, default_values

    def _matches_scope(self, scope: dict, object_name: str) -> bool:
        if not scope:
            return True

        obj = scope.get("object")
        if obj is not None:
            if obj == "*":
                return True
            if isinstance(obj, str):
                return obj == object_name
            return False

        objects = scope.get("objects")
        if isinstance(objects, list):
            return object_name in objects or "*" in objects

        return True

    def _resolve_rule_columns(
        self,
        rule: dict,
        columns: list[str],
        profile: ObjectFieldProfile,
    ) -> list[str]:
        matched: list[str] = []
        column_set = set(columns)
        keep_columns = set(rule.get("keep_columns", []))

        for col in rule.get("drop_columns", []):
            if col in column_set and col not in matched and col not in keep_columns:
                matched.append(col)

        for pattern in rule.get("drop_column_patterns", []):
            regex = re.compile(pattern)
            for col in columns:
                if regex.search(col) and col not in matched and col not in keep_columns:
                    matched.append(col)

        for target_object in rule.get("drop_lookup_targets", []):
            for field_name, refs in profile.lookup_fields.items():
                if target_object not in refs:
                    continue
                if field_name in keep_columns:
                    continue
                if field_name in column_set and field_name not in matched:
                    matched.append(field_name)

                field_meta = profile.field_meta.get(field_name)
                rel_name = field_meta.relationship_name if field_meta else ""
                if not rel_name:
                    continue
                prefix = f"{rel_name}."
                for col in columns:
                    if col.startswith(prefix) and col not in matched and col not in keep_columns:
                        matched.append(col)

        return matched

    def _collect_default_values(self, rule: dict, default_values: dict[str, str]):
        for field_name, value in (rule.get("set_defaults") or {}).items():
            default_values[field_name] = value

    def get_ignored_required_fields(self, object_name: str) -> set[str]:
        ignored: set[str] = set()
        for rule in self.rules:
            if not self._matches_scope(rule.get("scope", {}), object_name):
                continue
            ignored.update(rule.get("ignore_required_fields", []))
        return ignored
