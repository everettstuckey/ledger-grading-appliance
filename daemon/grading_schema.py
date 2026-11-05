"""Convert and validate the Ledger GUI's grading schema."""

import json
import math


_FIELD_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}
GRADING_INSTRUCTIONS = 'Return the grading array inside a JSON object with the single key "results".'


class GradingSchemaError(ValueError):
    """A grading format error whose message can be returned to the GUI."""


def grading_fields(schema):
    """Translate the GUI's array of flat Vertex-schema records."""
    try:
        item = schema["items"]
        fields = {name: field["type"].lower() for name, field in item["properties"].items()}
        if (
            schema["type"].lower() == "array"
            and item["type"].lower() == "object"
            and fields
            and set(item["required"]) == fields.keys()
            and all(field_type in _FIELD_TYPES for field_type in fields.values())
        ):
            return fields
    except (AttributeError, KeyError, TypeError):
        pass
    raise GradingSchemaError("invalid_request: Ledger grading requires an array of typed grading records.")


def grading_json_schema(fields):
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {name: {"type": kind} for name, kind in fields.items()},
                    "required": list(fields),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def unwrap_grades(text, fields, provider):
    try:
        document = json.loads(text)
    except ValueError:
        raise GradingSchemaError(f"invalid_request: {provider} returned malformed grading JSON.") from None
    return serialize_grades(document, fields, provider)


def serialize_grades(document, fields, provider):
    if not isinstance(document, dict) or document.keys() != {"results"}:
        raise GradingSchemaError(f"invalid_request: {provider} returned an invalid grading document.")
    grades = document["results"]
    if not isinstance(grades, list) or not grades:
        raise GradingSchemaError(f"invalid_request: {provider} returned no grading rows.")
    for row in grades:
        if not isinstance(row, dict) or row.keys() != fields.keys():
            raise GradingSchemaError(f"invalid_request: {provider} returned an invalid grading row.")
        for name, kind in fields.items():
            value = row[name]
            if type(value) not in _FIELD_TYPES[kind] or (type(value) is float and not math.isfinite(value)):
                raise GradingSchemaError(f"invalid_request: {provider} returned an invalid grading value.")
    return json.dumps(grades)
