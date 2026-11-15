"""Read-only pluggable data sources for authenticated Mini App views."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from dataclasses import dataclass
from functools import lru_cache
import re
import subprocess
import sys
from typing import Any, Callable


class SourceError(ValueError):
    """A deliberately non-sensitive provider failure."""


@dataclass(frozen=True)
class DataSource:
    name: str
    title: str
    args_schema: dict[str, Any]
    output_schema: dict[str, Any]
    sensitivity: str
    query: Callable[[dict[str, Any]], dict[str, Any]]
    profiles: tuple[str, ...] = ()

    def available(self, profile: str) -> bool:
        return not self.profiles or profile in self.profiles


def _json_copy(value: Any, path: str = "value") -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SourceError(f"{path} must be JSON data") from exc


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": type(value) in (int, float),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }.get(expected, False)


def _bound(schema: dict[str, Any], name: str) -> int | float | None:
    if name not in schema:
        return None
    value = schema[name]
    if type(value) not in (int, float):
        raise SourceError("Data source schema is invalid")
    return value


def validate_schema(value: Any, schema: dict[str, Any], path: str = "value") -> None:
    """Small JSON Schema subset used for source input/output contracts."""
    if not isinstance(schema, dict):
        raise SourceError("Data source schema is invalid")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not any(type(item) is type(value) and item == value for item in enum):
            raise SourceError(f"{path} is not an allowed value")
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not isinstance(types, list) or not types or not all(isinstance(item, str) for item in types):
            raise SourceError("Data source schema is invalid")
        if not any(_matches_type(value, item) for item in types):
            raise SourceError(f"{path} has the wrong type")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SourceError("Data source schema is invalid")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise SourceError("Data source schema is invalid")
        if any(key not in value for key in required):
            raise SourceError(f"{path} is missing a required field")
        additional = schema.get("additionalProperties", True)
        if additional is not True and additional is not False and not isinstance(additional, dict):
            raise SourceError("Data source schema is invalid")
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                if additional is False:
                    raise SourceError(f"{path} has unknown fields")
                child = additional if isinstance(additional, dict) else None
            if child is not None:
                validate_schema(item, child, f"{path}.{key}")
    elif isinstance(value, list):
        maximum = _bound(schema, "maxItems")
        minimum = _bound(schema, "minItems")
        if maximum is not None and len(value) > maximum:
            raise SourceError(f"{path} has too many items")
        if minimum is not None and len(value) < minimum:
            raise SourceError(f"{path} has too few items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{i}]")
    elif isinstance(value, str):
        maximum = _bound(schema, "maxLength")
        minimum = _bound(schema, "minLength")
        if maximum is not None and len(value) > maximum:
            raise SourceError(f"{path} is too long")
        if minimum is not None and len(value) < minimum:
            raise SourceError(f"{path} is too short")
    elif type(value) in (int, float):
        minimum = _bound(schema, "minimum")
        maximum = _bound(schema, "maximum")
        if minimum is not None and value < minimum:
            raise SourceError(f"{path} is below the minimum")
        if maximum is not None and value > maximum:
            raise SourceError(f"{path} is above the maximum")


class SourceRegistry:
    """A registry exposes contracts to the model but queries only after Mini App auth."""

    def __init__(self, sources: list[DataSource]):
        self._sources = {source.name: source for source in sources}
        if len(self._sources) != len(sources):
            raise ValueError("Duplicate data source name")
        for source in sources:
            if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", source.name):
                raise ValueError("Invalid data source name")
            if source.sensitivity not in ("public", "private", "sensitive"):
                raise ValueError("Invalid data source sensitivity")

    def _source(self, name: str, profile: str) -> DataSource:
        source = self._sources.get(name)
        if source is None or not source.available(profile):
            raise SourceError("Data source is unavailable for this profile")
        return source

    def validate_args(self, name: str, args: Any, profile: str) -> dict[str, Any]:
        source = self._source(name, profile)
        clean = _json_copy(args, "Source arguments")
        validate_schema(clean, source.args_schema, "Source arguments")
        if not isinstance(clean, dict):
            raise SourceError("Source arguments must be an object")
        return clean

    def output_schema(self, name: str, profile: str) -> dict[str, Any]:
        """Return a source contract for save-time placeholder validation only."""
        return copy.deepcopy(self._source(name, profile).output_schema)

    def sensitivity(self, name: str, profile: str) -> str:
        """Return a source classification without fetching provider data."""
        return self._source(name, profile).sensitivity

    def manifest(self, profile: str) -> list[dict[str, Any]]:
        """Return contracts only. Never invoke a provider or include result data."""
        return [
            {
                "name": source.name,
                "title": source.title,
                "args": copy.deepcopy(source.args_schema),
                "output": copy.deepcopy(source.output_schema),
                "sensitivity": source.sensitivity,
            }
            for source in self._sources.values()
            if source.available(profile)
        ]

    def query(self, name: str, args: Any, profile: str) -> dict[str, Any]:
        source = self._source(name, profile)
        clean_args = self.validate_args(name, args, profile)
        try:
            result = _json_copy(source.query(clean_args), "Data source result")
            if not isinstance(result, dict):
                raise SourceError("Data source returned an invalid result")
            validate_schema(result, source.output_schema, "Data source result")
            return result
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError("Data source is temporarily unavailable") from exc


def _run_json(command: list[str], *, timeout: int = 15) -> Any:
    environment = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "LANG", "TZ")
        if key in os.environ
    }
    environment["BASECAMP_NONINTERACTIVE"] = "1"
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError("Data source is temporarily unavailable") from exc
    if completed.returncode:
        raise SourceError("Data source is temporarily unavailable")
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise SourceError("Data source returned invalid JSON") from exc


def _finance_query(name: str, args: dict[str, Any]) -> dict[str, Any]:
    script = os.environ.get("HTE_FINANCE_SOURCES_SCRIPT", "/opt/data/tools/plaid-finance-export/finance_sources.py")
    return _run_json([sys.executable, script, "query", name, json.dumps(args, ensure_ascii=False)])


@lru_cache(maxsize=1)
def _finance_sources() -> tuple[DataSource, ...]:
    """Load only the finance provider's schema-only manifest, never account values."""
    script = os.environ.get("HTE_FINANCE_SOURCES_SCRIPT", "/opt/data/tools/plaid-finance-export/finance_sources.py")
    described = _run_json([sys.executable, script, "describe"])
    entries = described.get("sources", []) if isinstance(described, dict) else []
    if not isinstance(entries, list):
        raise SourceError("Finance manifest is invalid")
    sources = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceError("Finance manifest is invalid")
        name, title = entry.get("name"), entry.get("title")
        if not isinstance(name, str) or not isinstance(title, str):
            raise SourceError("Finance manifest is invalid")
        sources.append(
            DataSource(
                name=name,
                title=title,
                args_schema=_json_copy(entry.get("args"), "Finance args schema"),
                output_schema=_json_copy(entry.get("output"), "Finance output schema"),
                sensitivity="sensitive",
                profiles=("default", "coordinator"),
                query=lambda args, source_name=name: _finance_query(source_name, args),
            )
        )
    return tuple(sources)


def _basecamp_projects(args: dict[str, Any]) -> dict[str, Any]:
    del args
    payload = _run_json(["/opt/data/home/.local/bin/basecamp", "projects", "list", "--json"])
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        raise SourceError("Basecamp returned an invalid project list")
    rows = []
    for project in data:
        if not isinstance(project, dict):
            continue
        rows.append(
            {
                "id": str(project.get("id", "")),
                "name": str(project.get("name") or project.get("title") or ""),
                "description": str(project.get("description") or ""),
                "status": str(project.get("status") or ""),
            }
        )
    return {
        "source": "basecamp.projects",
        "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "rows": rows,
    }


def _basecamp_source() -> DataSource:
    return DataSource(
        name="basecamp.projects",
        title="Basecamp projects",
        args_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={
            "type": "object",
            "required": ["source", "generated_at", "rows"],
            "properties": {
                "source": {"type": "string"},
                "generated_at": {"type": "string"},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "name", "description", "status"],
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "status": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        sensitivity="private",
        query=_basecamp_projects,
        profiles=("default", "coordinator", "research"),
    )


def default_registry(profile: str | None = None) -> SourceRegistry:
    """Built-in sources; finance never starts for profiles that cannot use it."""
    sources = []
    if profile in (None, "default", "coordinator", "research"):
        sources.append(_basecamp_source())
    if profile in (None, "default", "coordinator"):
        try:
            sources.extend(_finance_sources())
        except SourceError:
            # Finance values are never substituted with a plausible fake; the source simply is unavailable.
            pass
    return SourceRegistry(sources)
