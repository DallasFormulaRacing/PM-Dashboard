"""Typed Notion source policies and pure property/lifecycle normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


DocumentKind = Literal[
    "announcement", "meeting_note", "task", "project", "technical_doc", "timeline_item", "hub"
]
PropertyField = Literal[
    "status", "categories", "priority", "date", "start_date", "due_date", "end_date",
    "summary", "description", "assignees", "attendees",
]

# Only these semantic fields are copied. Files, relations, emails, and arbitrary
# raw property payloads are deliberately outside this ingestion contract.
PROPERTY_ALIASES: dict[str, tuple[str, ...]] = {
    "status": ("status",),
    "categories": ("category", "categories", "tags"),
    "priority": ("priority",),
    "date": ("date", "meeting date"),
    "start_date": ("start date",),
    "due_date": ("due date",),
    "end_date": ("end date",),
    "summary": ("summary", "tldr"),
    "description": ("description",),
    "assignees": ("assignee", "assignees", "assigned to", "assign"),
    "attendees": ("attendees",),
}
ACTIVE_STATUSES = frozenset({"published", "not started", "in progress", "waiting"})


class NotionSourcePolicy(BaseModel):
    """Source context only; supplying a policy never grants source approval.

    Overrides map semantic fields to a Notion property ID (preferred, stable
    across renames) or an exact property name. Paths are administrator-supplied;
    the REST API does not return the plugin's ancestor-path envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_kind: DocumentKind | None = None
    ancestor_path: tuple[str, ...] = ()
    property_overrides: dict[PropertyField, str] = Field(default_factory=dict)
    remove_template_placeholders: bool = True

    @field_validator("ancestor_path")
    @classmethod
    def validate_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part.strip() for part in value):
            raise ValueError("Ancestor path entries cannot be blank")
        return tuple(part.strip() for part in value)

    @field_validator("property_overrides")
    @classmethod
    def validate_overrides(cls, value: dict[PropertyField, str]) -> dict[PropertyField, str]:
        if any(not selector.strip() for selector in value.values()):
            raise ValueError("Property selectors cannot be blank")
        return value


class NotionPropertyError(ValueError):
    """Malformed or ambiguous properties must not silently lose evidence."""


def normalized_status(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())


def properties_of(page: Mapping[str, Any]) -> Mapping[str, Any]:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        raise NotionPropertyError("Notion page omitted properties")
    if any(
        not isinstance(prop, Mapping) or not isinstance(prop.get("type"), str)
        for prop in properties.values()
    ):
        raise NotionPropertyError("Notion page has a malformed property")
    return properties


def find_property(
    properties: Mapping[str, Any], field: str, policy: NotionSourcePolicy,
) -> tuple[str, Mapping[str, Any]] | None:
    override = policy.property_overrides.get(field)
    if override is not None:
        matches = [
            (name, prop) for name, prop in properties.items()
            if isinstance(prop, Mapping) and (name == override or prop.get("id") == override)
        ]
        if len(matches) != 1:
            raise NotionPropertyError(f"Configured {field} property is missing or ambiguous")
        return matches[0]

    matches = [
        (name, prop) for name, prop in properties.items()
        if isinstance(prop, Mapping)
        and (
            " ".join(str(name).casefold().split()) in PROPERTY_ALIASES[field]
            or (field == "status" and prop.get("type") == "status")
        )
    ]
    if len(matches) > 1:
        raise NotionPropertyError(f"Ambiguous {field} properties; configure a property override")
    return matches[0] if matches else None


def lifecycle_reason(page: Mapping[str, Any], policy: NotionSourcePolicy) -> str | None:
    """Return an exclusion reason; a declared-but-empty status is excluded."""

    for flag in ("archived", "in_trash"):
        value = page.get(flag, False)
        if not isinstance(value, bool):
            raise NotionPropertyError(f"Notion page has an invalid {flag} flag")
        if value:
            return flag
    status_property = find_property(properties_of(page), "status", policy)
    if status_property is None:
        return None
    value = property_value(status_property[1])
    if not isinstance(value, str) or normalized_status(value) not in ACTIVE_STATUSES:
        return "unpublished_or_inactive_status"
    return None


def lifecycle_query_filter(
    schema: Mapping[str, Any], policy: NotionSourcePolicy,
) -> dict[str, Any] | None:
    """Build the schema-backed eligibility filter shared by sync and diagnostics.

    Local fresh-page checks remain mandatory, including for statusless sources.
    """

    status = find_property(schema["properties"], "status", policy)
    if status is None or status[1].get("type") not in {"status", "select"}:
        return None
    kind = status[1]["type"]
    settings = status[1].get(kind)
    if not isinstance(settings, Mapping) or not isinstance(settings.get("options"), list):
        raise NotionPropertyError("Notion status schema omitted its options")
    conditions = [
        {"property": status[1].get("id", status[0]), kind: {"equals": option["name"]}}
        for option in settings["options"]
        if isinstance(option, Mapping) and isinstance(option.get("name"), str)
        and normalized_status(option["name"]) in ACTIVE_STATUSES
    ]
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else {"or": conditions}


def plain_text(value: Any) -> str:
    if not isinstance(value, list):
        raise NotionPropertyError("Notion rich-text property is not an array")
    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise NotionPropertyError("Notion rich-text item is malformed")
        text = item.get("plain_text")
        if text is None:
            raw_text = item.get("text")
            text = raw_text.get("content") if isinstance(raw_text, Mapping) else None
        if not isinstance(text, str):
            raise NotionPropertyError("Notion rich-text item has no readable text")
        parts.append(text)
    return "".join(parts).strip()


def property_value(prop: Mapping[str, Any]) -> Any:
    kind = prop.get("type")
    value = prop.get(kind) if isinstance(kind, str) else None
    if kind in {"rich_text", "title"}:
        return plain_text(value)
    if kind in {"select", "status"}:
        if value is None:
            return None
        if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
            raise NotionPropertyError("Notion selection property is malformed")
        return value["name"]
    if kind == "multi_select":
        if not isinstance(value, list) or any(
            not isinstance(item, Mapping) or not isinstance(item.get("name"), str)
            for item in value
        ):
            raise NotionPropertyError("Notion multi-select property is malformed")
        return [item["name"] for item in value]
    if kind == "date":
        if value is None:
            return None
        if not isinstance(value, Mapping) or not isinstance(value.get("start"), str):
            raise NotionPropertyError("Notion date property is malformed")
        if any(value.get(key) is not None and not isinstance(value[key], str)
               for key in ("end", "time_zone")):
            raise NotionPropertyError("Notion date range or timezone is malformed")
        return {key: value.get(key) for key in ("start", "end", "time_zone")}
    if kind == "people":
        if not isinstance(value, list):
            raise NotionPropertyError("Notion people property is not an array")
        people: list[dict[str, str | None]] = []
        for person in value:
            if not isinstance(person, Mapping) or not isinstance(person.get("id"), str):
                raise NotionPropertyError("Notion person is missing an ID")
            name = person.get("name")
            people.append({"id": person["id"], "name": name if isinstance(name, str) else None})
        return people
    raise NotionPropertyError(f"Unsupported mapped Notion property type: {kind}")


def normalize_properties(page: Mapping[str, Any], policy: NotionSourcePolicy) -> dict[str, Any]:
    properties = properties_of(page)
    output: dict[str, Any] = {}
    for field in PROPERTY_ALIASES:
        match = find_property(properties, field, policy)
        if match is None:
            continue
        value = property_value(match[1])
        if value is None or value == "" or value == []:
            continue
        expected_types = {
            "status": {"status", "select", "rich_text"},
            "categories": {"multi_select", "select", "rich_text"},
            "priority": {"select", "status", "rich_text"},
            "summary": {"rich_text"}, "description": {"rich_text"},
            "assignees": {"people"}, "attendees": {"people"},
            "date": {"date"}, "start_date": {"date"},
            "due_date": {"date"}, "end_date": {"date"},
        }
        if match[1].get("type") not in expected_types[field]:
            raise NotionPropertyError(f"Notion property has an invalid type for {field}")
        if field == "status":
            if not isinstance(value, str):
                raise NotionPropertyError("Status must be a single selection or text value")
            value = normalized_status(value)
        if field == "categories" and isinstance(value, str):
            value = [value]
        output[field] = value
    return output


def infer_document_kind(page: Mapping[str, Any], policy: NotionSourcePolicy) -> DocumentKind:
    if policy.document_kind is not None:
        return policy.document_kind
    for name, prop in properties_of(page).items():
        if not isinstance(prop, Mapping) or prop.get("type") != "title":
            continue
        title_field = str(name).strip().casefold()
        return {
            "meeting name": "meeting_note", "task name": "task",
            "project name": "project", "doc name": "technical_doc",
        }.get(title_field, "technical_doc")
    return "technical_doc"


def property_summary(properties: Mapping[str, Any]) -> str:
    """Render selected facts into evidence text without serializing raw JSON."""

    lines: list[str] = []
    for field, value in properties.items():
        if field in {"assignees", "attendees"}:
            # IDs remain searchable if the source omits a display name. No user
            # directory traversal or email addresses are required.
            rendered = ", ".join(
                f"{person['name']} ({person['id']})" if person["name"] else person["id"]
                for person in value
            )
        elif isinstance(value, dict):
            rendered = value["start"]
            if value.get("end"):
                rendered += f" to {value['end']}"
            if value.get("time_zone"):
                rendered += f" ({value['time_zone']})"
        elif isinstance(value, list):
            rendered = ", ".join(value)
        else:
            rendered = str(value)
        lines.append(f"{field.replace('_', ' ').capitalize()}: {rendered}")
    return "\n".join(lines)
