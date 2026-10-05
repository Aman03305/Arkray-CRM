"""Opportunity and pipeline-configuration rules, applied by the services to every write (API,
conversion, future imports and tools). Serializers check shapes; these decide validity and
canonical form.

Opportunities:
- Title: never set by a client. The services derive it from the customer and instrument
  (naming.py, ADR-0028), so it is not a field here.
- Value (shown as "Installation price"): a Decimal (or int) amount in the organisation
  currency, 0 to 999,999,999,999.99, at most 2 decimal places. Floats are refused outright:
  money is never converted through binary floating point, and NaN/Infinity can't even be
  represented. The negotiated price follows the same rules.
- Probability: a Decimal (or int) percentage, 0 to 100, at most 2 decimal places.
- Opportunity date and expected close date: dates (not datetimes: no time zone can shift
  them), 2000-2099.
- Account and customer names: one line, up to 200 characters, never blank once set.
- Contact: a phone number and an email address, each checked as a lead's are.
- Address: multi-line, up to 1,000. Instrument: one line, up to 200, and one of
  instruments.INSTRUMENTS (checked by the services, which know the current value: an
  opportunity from before the list keeps its own text until the instrument is changed).
  Work load and Expected CPT: one line, up to 100 (free text: the product defines no
  workload unit and no meaning or unit for CPT, so none is invented).
- Description: multi-line text up to 5,000 characters. Lost reason: up to 500.

Configuration (docs/pipeline.md#configuration): stage and custom-field specifications,
bounded in number and size, names plain text (no markup).

Custom values (docs/pipeline.md#custom-fields): checked against the pipeline's active field
definitions, stored in a canonical JSON form; nothing is ever evaluated or rendered as markup.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from arkray.core.errors import InvalidInputError
from arkray.core.text import TextRejected, clean_line, clean_multiline
from arkray.leads.validation import clean_email_address, clean_phone_number

from . import models as m

VALUE_INVALID = "Enter an amount such as 1250000 or 1250000.50."
PROBABILITY_INVALID = "Enter a percentage from 0 to 100, with at most 2 decimal places."
NAME_REQUIRED = "Enter a name."
MARKUP_REFUSED = "Use plain text: tags such as <b> aren't allowed."
# Something that starts like an HTML/XML tag, a comment or a processing instruction. Plain
# comparisons ("< 5 tests") stay allowed. Values are always rendered as text, never markup;
# this keeps markup out of configuration and custom values in the first place.
_MARKUP = re.compile(r"<\s*[A-Za-z/!?]")


def _exact(value: Any, *, message: str) -> Decimal:
    # bool is an int subclass; float would already have lost exactness.
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise ValueError(message)
    number = Decimal(value)
    try:
        if not number.is_finite() or number != number.quantize(m.HUNDREDTH):
            raise ValueError(message)
    except InvalidOperation:  # too many digits to quantize: certainly out of range
        raise ValueError(message) from None
    return number.quantize(m.HUNDREDTH)


def clean_value(value: Any) -> Decimal:
    amount = _exact(value, message=VALUE_INVALID)
    if amount < 0:
        raise ValueError("The value can't be negative.")
    if amount > m.MAX_VALUE:
        raise ValueError("The value must be at most 999,999,999,999.99.")
    return amount


def clean_price(value: Any) -> Decimal:
    """A negotiated price: the same rules as the value."""
    if value is None:
        raise ValueError("Enter the negotiated price.")
    return clean_value(value)


def clean_probability(value: Any) -> Decimal | None:
    """None means "use the stage's default"."""
    if value is None:
        return None
    probability = _exact(value, message=PROBABILITY_INVALID)
    if not m.IMPOSSIBLE <= probability <= m.CERTAIN:
        raise ValueError(PROBABILITY_INVALID)
    return probability


def _business_date(value: Any) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("Enter a date.")
    day: date = value
    if not m.EARLIEST_CLOSE_DATE <= day <= m.LATEST_CLOSE_DATE:
        raise ValueError("Enter a date between 2000 and 2099.")
    return day


def clean_expected_close_date(value: Any) -> date | None:
    if value is None:
        return None
    return _business_date(value)


def clean_opportunity_date(value: Any) -> date:
    if value is None:
        raise ValueError("Enter the opportunity date.")
    return _business_date(value)


def _line(max_length: int, *, required: bool = False) -> Callable[[Any], str]:
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Enter text.")
        text = clean_line(value)
        if required and not text:
            raise ValueError(NAME_REQUIRED)
        if len(text) > max_length:
            raise ValueError(f"Use at most {max_length} characters.")
        return text

    return clean


def _multiline(max_length: int) -> Callable[[Any], str]:
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Enter text.")
        text = clean_multiline(value)
        if len(text) > max_length:
            raise ValueError(f"Use at most {max_length:,} characters.")
        return text

    return clean


def _custom_values_shape(value: Any) -> dict[str, Any]:
    """Only the shape here (an object of field id -> value); the definitions decide the rest
    (clean_custom_values, in the services, which know the pipeline)."""
    if not isinstance(value, dict):
        raise ValueError("Send an object of field ids and values.")
    if len(value) > m.MAX_FIELDS_PER_PIPELINE:
        raise ValueError("Too many fields.")
    return dict(value)


CLEANERS: Mapping[str, Callable[[Any], Any]] = {
    "value": clean_value,
    "probability": clean_probability,
    "expected_close_date": clean_expected_close_date,
    "description": _multiline(m.DESCRIPTION_MAX_LENGTH),
    "lost_reason": _multiline(m.LOST_REASON_MAX_LENGTH),
    "opportunity_date": clean_opportunity_date,
    "account_name": _line(m.ACCOUNT_NAME_MAX_LENGTH, required=True),
    "customer_name": _line(m.CUSTOMER_NAME_MAX_LENGTH, required=True),
    "contact_phone": clean_phone_number,
    "contact_email": clean_email_address,
    "address": _multiline(m.ADDRESS_MAX_LENGTH),
    "instrument_name": _line(m.INSTRUMENT_NAME_MAX_LENGTH),
    "work_load": _line(m.WORK_LOAD_MAX_LENGTH),
    "expected_cpt": _line(m.EXPECTED_CPT_MAX_LENGTH),
    "custom_fields": _custom_values_shape,
}
# What PATCH may change. Lead, owner, pipeline, stage, status, closed_at, provenance,
# timestamps, archive state, the negotiated price and version have their own rules and
# operations; the title follows the customer and instrument (naming.py).
EDITABLE_FIELDS = frozenset(CLEANERS)


def clean_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every given field cleaned, or InvalidInputError listing every problem at once."""
    unknown = set(data) - EDITABLE_FIELDS
    if unknown:
        raise InvalidInputError(
            details={
                "non_field_errors": [f"These fields can't be set: {', '.join(sorted(unknown))}."]
            }
        )
    cleaned: dict[str, Any] = {}
    errors: dict[str, list[str]] = {}
    for field, value in data.items():
        try:
            cleaned[field] = CLEANERS[field](value)
        except (ValueError, TextRejected) as exc:
            errors[field] = [str(exc)]
    if errors:
        raise InvalidInputError(details=errors)
    return cleaned


# --- configuration -------------------------------------------------------------------------------
def clean_config_name(value: Any, max_length: int) -> str:
    """A pipeline, stage, field or option name: one line, required, bounded, no markup."""
    text = _line(max_length, required=True)(value)
    if _MARKUP.search(text):
        raise ValueError(MARKUP_REFUSED)
    return text


@dataclass(frozen=True, slots=True)
class StageSpec:
    """One stage as the stage manager states it, in board order."""

    id: UUID | None  # an existing stage of the pipeline, or None for a new one
    name: str
    stage_type: m.StageType
    probability: Decimal

    @property
    def category(self) -> m.StageCategory:
        if self.stage_type == m.StageType.WON:
            return m.StageCategory.WON
        if self.stage_type == m.StageType.LOST:
            return m.StageCategory.LOST
        return m.StageCategory.OPEN

    @property
    def is_negotiation(self) -> bool:
        return self.stage_type == m.StageType.NEGOTIATION


FIXED_PROBABILITY = {m.StageType.WON: m.CERTAIN, m.StageType.LOST: m.IMPOSSIBLE}


def clean_stage_specs(raw: Sequence[Mapping[str, Any]]) -> list[StageSpec]:
    """A pipeline's whole stage list: 1-20 stages, unique names (case-insensitively), at least
    one open stage (new opportunities start in the first). Won is always 100 % and lost 0 %.
    Errors are reported per position: {"stages": {"2": {"name": [...]}}} flattened as
    "stages[2].name"."""
    if not 1 <= len(raw) <= m.MAX_STAGES_PER_PIPELINE:
        raise InvalidInputError(
            details={"stages": [f"Use 1 to {m.MAX_STAGES_PER_PIPELINE} stages."]}
        )
    errors: dict[str, list[str]] = {}
    specs: list[StageSpec] = []
    seen_names: set[str] = set()
    seen_ids: set[UUID] = set()
    for index, item in enumerate(raw):
        where = f"stages[{index}]"
        try:
            name = clean_config_name(item.get("name"), m.STAGE_NAME_MAX_LENGTH)
        except (ValueError, TextRejected) as exc:
            errors[f"{where}.name"] = [str(exc)]
            continue
        if name.casefold() in seen_names:
            errors[f"{where}.name"] = ["Each stage needs a different name."]
        seen_names.add(name.casefold())
        try:
            stage_type = m.StageType(str(item.get("type")))
        except ValueError:
            errors[f"{where}.type"] = ["Choose open, negotiation, won or lost."]
            continue
        stage_id = item.get("id")
        if stage_id is not None:
            if stage_id in seen_ids:
                errors[f"{where}.id"] = ["This stage is listed twice."]
            seen_ids.add(stage_id)
        requested = item.get("probability")
        if stage_type in FIXED_PROBABILITY:
            fixed = FIXED_PROBABILITY[stage_type]
            try:
                if requested is not None and clean_probability(requested) != fixed:
                    raise ValueError(f"A {stage_type.label.lower()} stage is always {fixed:.0f}%.")
            except ValueError as exc:
                errors[f"{where}.probability"] = [str(exc)]
            probability = fixed
        else:
            try:
                cleaned = clean_probability(requested)
            except ValueError as exc:
                errors[f"{where}.probability"] = [str(exc)]
                continue
            if cleaned is None:
                errors[f"{where}.probability"] = ["Enter a probability."]
                continue
            probability = cleaned
        specs.append(StageSpec(stage_id, name, stage_type, probability))
    if errors:
        raise InvalidInputError(details=errors)
    if not any(spec.category == m.StageCategory.OPEN for spec in specs):
        raise InvalidInputError(details={"stages": ["Add at least one open stage."]})
    return specs


@dataclass(frozen=True, slots=True)
class FieldSpec:
    id: UUID | None
    name: str
    field_type: m.FieldType
    required: bool
    # Select fields: (existing option id or None, label), in order.
    options: tuple[tuple[str | None, str], ...]


_OPTION_ID = re.compile(r"^[0-9a-f]{10}$")


def new_option_id() -> str:
    return secrets.token_hex(5)


def clean_field_specs(raw: Sequence[Mapping[str, Any]]) -> list[FieldSpec]:
    """A pipeline's custom fields, in display order: at most 30, unique names, select fields
    with 1-50 unique options, no options on other types."""
    if len(raw) > m.MAX_FIELDS_PER_PIPELINE:
        raise InvalidInputError(
            details={"fields": [f"Use at most {m.MAX_FIELDS_PER_PIPELINE} custom fields."]}
        )
    errors: dict[str, list[str]] = {}
    specs: list[FieldSpec] = []
    seen_names: set[str] = set()
    seen_ids: set[UUID] = set()
    for index, item in enumerate(raw):
        where = f"fields[{index}]"
        try:
            name = clean_config_name(item.get("name"), m.FIELD_NAME_MAX_LENGTH)
        except (ValueError, TextRejected) as exc:
            errors[f"{where}.name"] = [str(exc)]
            continue
        if name.casefold() in seen_names:
            errors[f"{where}.name"] = ["Each field needs a different name."]
        seen_names.add(name.casefold())
        try:
            field_type = m.FieldType(str(item.get("type")))
        except ValueError:
            errors[f"{where}.type"] = ["Choose a field type from the list."]
            continue
        field_id = item.get("id")
        if field_id is not None:
            if field_id in seen_ids:
                errors[f"{where}.id"] = ["This field is listed twice."]
            seen_ids.add(field_id)
        options: list[tuple[str | None, str]] = []
        raw_options = item.get("options") or []
        if field_type in m.SELECT_TYPES:
            if not 1 <= len(raw_options) <= m.MAX_FIELD_OPTIONS:
                errors[f"{where}.options"] = [f"Add 1 to {m.MAX_FIELD_OPTIONS} choices."]
                continue
            labels: set[str] = set()
            for option in raw_options:
                option_id = option.get("id")
                try:
                    label = clean_config_name(option.get("label"), m.FIELD_OPTION_MAX_LENGTH)
                except (ValueError, TextRejected) as exc:
                    errors[f"{where}.options"] = [str(exc)]
                    break
                if option_id is not None and not (
                    isinstance(option_id, str) and _OPTION_ID.fullmatch(option_id)
                ):
                    errors[f"{where}.options"] = ["Unknown choice."]
                    break
                if label.casefold() in labels:
                    errors[f"{where}.options"] = ["Each choice needs a different label."]
                    break
                labels.add(label.casefold())
                options.append((option_id, label))
        elif raw_options:
            errors[f"{where}.options"] = ["Only choice fields have choices."]
            continue
        specs.append(
            FieldSpec(field_id, name, field_type, bool(item.get("required")), tuple(options))
        )
    if errors:
        raise InvalidInputError(details=errors)
    return specs


# --- custom values ----------------------------------------------------------------------------
TEXT_VALUE_MAX_LENGTH = 500
LONG_TEXT_VALUE_MAX_LENGTH = 5000
# Numbers: up to 15 digits before and 4 after the decimal point, optionally negative; sent as
# a JSON string (or integer) like money, never as a binary float.
_NUMBER = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,4})?")
_MONEY = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,2})?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
EARLIEST_CUSTOM_DATE, LATEST_CUSTOM_DATE = date(1900, 1, 1), date(2199, 12, 31)


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _text_value(value: Any, *, multiline: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter text.")
    text = clean_multiline(value) if multiline else clean_line(value)
    limit = LONG_TEXT_VALUE_MAX_LENGTH if multiline else TEXT_VALUE_MAX_LENGTH
    if len(text) > limit:
        raise ValueError(f"Use at most {limit:,} characters.")
    if _MARKUP.search(text):
        raise ValueError(MARKUP_REFUSED)
    return text


def _number_value(
    value: Any, pattern: re.Pattern[str], message: str, *, places: int | None = None
) -> str:
    """One spelling per amount (audit compares stored values): money to `places` decimals
    like `value` ("250000" -> "250000.00"), other numbers without padding ("1.50" -> "1.5",
    "-0" and "000" -> "0")."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError(message)
    text = str(value)
    if not pattern.fullmatch(text):
        raise ValueError(message)
    number = Decimal(text)
    if places is not None:
        return format(number.quantize(Decimal(1).scaleb(-places)), "f")
    return "0" if number.is_zero() else format(number.normalize(), "f")


def _date_value(value: Any) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("Enter a date as YYYY-MM-DD.")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter a real date.") from None
    if not EARLIEST_CUSTOM_DATE <= day <= LATEST_CUSTOM_DATE:
        raise ValueError("Enter a date between 1900 and 2199.")
    return day.isoformat()


def clean_custom_value(field: m.CustomField, value: Any) -> Any:
    """One value in canonical form (strings for text, numbers, money and dates; booleans;
    option ids). Raises ValueError with a message for the person."""
    kind = field.field_type
    if kind == m.FieldType.TEXT:
        return _text_value(value, multiline=False)
    if kind == m.FieldType.LONG_TEXT:
        return _text_value(value, multiline=True)
    if kind == m.FieldType.NUMBER:
        return _number_value(value, _NUMBER, "Enter a number such as 42 or -3.5.")
    if kind == m.FieldType.CURRENCY:
        return _number_value(value, _MONEY, VALUE_INVALID, places=2)
    if kind == m.FieldType.DATE:
        return _date_value(value)
    if kind == m.FieldType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError("Choose yes or no.")
        return value
    option_ids = {option["id"] for option in field.options}
    if kind == m.FieldType.SINGLE_SELECT:
        if not isinstance(value, str) or value not in option_ids:
            raise ValueError("Choose one of the options.")
        return value
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("Choose options from the list.")
    chosen = set(value)
    if not chosen <= option_ids or len(chosen) != len(value):
        raise ValueError("Choose each option at most once, from the list.")
    order = [option["id"] for option in field.options]
    return sorted(chosen, key=order.index)


def clean_custom_values(
    fields: Sequence[m.CustomField],
    raw: Mapping[str, Any],
    *,
    current: Mapping[str, Any],
    creating: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Merge `raw` ({field id: value or null}) into `current` and check the result against
    the pipeline's active `fields`. Returns the new values and the ids of the fields whose
    value changed. Unknown or archived field ids are refused; null or an empty value clears
    a field (a required one can't be cleared, and must be given at creation). Values of
    archived fields already stored are kept, hidden (docs/pipeline.md#custom-fields)."""
    by_id = {str(field.pk): field for field in fields}
    errors: dict[str, list[str]] = {}
    merged = dict(current)
    for key, value in raw.items():
        field = by_id.get(key)
        if field is None:
            errors[f"custom_fields.{str(key)[:40]}"] = ["Unknown field."]
            continue
        if _empty(value):
            merged.pop(key, None)
            continue
        try:
            merged[key] = clean_custom_value(field, value)
        except (ValueError, TextRejected) as exc:
            errors[f"custom_fields.{key}"] = [str(exc)]
    for key, field in by_id.items():
        if not field.required or key in errors:
            continue
        if (creating or key in raw) and _empty(merged.get(key)):
            errors[f"custom_fields.{key}"] = [f"Enter {field.name}."]
    if errors:
        raise InvalidInputError(details=errors)
    if len(json.dumps(merged, separators=(",", ":")).encode()) > m.CUSTOM_VALUES_MAX_BYTES:
        raise InvalidInputError(details={"custom_fields": ["These values are too long."]})
    changed = sorted(k for k in set(merged) | set(current) if merged.get(k) != current.get(k))
    return merged, changed
