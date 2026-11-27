"""OCPP 2.0.1 message models (pydantic v2, hand-written).

Source of truth for field names, required sets, enums, maxLength, array bounds and
integer/number types: the OCPP 2.0.1 JSON schemas (the copy shipped with the dev dependency
``ocpp``; checked by ``tests/test_v201_schema_conformance.py``). Nothing here is shared with
``protocol/v16``: the two versions are separate stacks.

Conventions (same shape as the 1.6 package):

- Python attributes are snake_case; the wire name (spec camelCase) is the alias.
- Optional fields default to ``None`` and are left out on dump; an explicit JSON ``null`` is
  rejected by ``codec.parse`` (the schemas do not allow ``null`` except inside the untyped
  ``DataTransfer.data`` and ``customData`` vendor fields).
- Unknown fields are forbidden at model level; ``codec.parse`` decides whether to ignore them
  (lenient) or reject them (strict). ``customData`` is the exception: its schema allows any
  vendor property next to ``vendorId``, so :class:`CustomDataType` keeps extra fields.
- Identifiers such as ``idToken`` and ``transactionId`` are ``str``: leading zeros are preserved.
- ``dateTime`` fields serialize as UTC with milliseconds and ``Z``; naive values are UTC.
- Class names are the spec data type names (``...Type``); a message is ``<Action>Request`` /
  ``<Action>Response``.

TODO(spec): value ranges stated only in the 2.0.1 spec text (e.g. evseId / connectorId > 0,
seqNo >= 0, a non-negative interval) are not in the schemas and not enforced here; the
station logic (M2) is expected to check them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
)
from pydantic.alias_generators import to_camel

from .enums import (
    AuthorizationStatusEnumType,
    AuthorizeCertificateStatusEnumType,
    BootReasonEnumType,
    ChargingStateEnumType,
    ConnectorStatusEnumType,
    DataTransferStatusEnumType,
    HashAlgorithmEnumType,
    IdTokenEnumType,
    LocationEnumType,
    MeasurandEnumType,
    MessageFormatEnumType,
    PhaseEnumType,
    ReadingContextEnumType,
    ReasonEnumType,
    RegistrationStatusEnumType,
    TransactionEventEnumType,
    TriggerReasonEnumType,
)


def format_datetime(value: datetime) -> str:
    """UTC, millisecond precision, ``Z`` suffix; naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond // 1000:03d}Z"


DateTime = Annotated[datetime, PlainSerializer(format_datetime, return_type=str, when_used="json")]


String8 = Annotated[str, StringConstraints(max_length=8)]
String20 = Annotated[str, StringConstraints(max_length=20)]
String25 = Annotated[str, StringConstraints(max_length=25)]
String36 = Annotated[str, StringConstraints(max_length=36)]
String40 = Annotated[str, StringConstraints(max_length=40)]
String50 = Annotated[str, StringConstraints(max_length=50)]
String128 = Annotated[str, StringConstraints(max_length=128)]
String255 = Annotated[str, StringConstraints(max_length=255)]
String512 = Annotated[str, StringConstraints(max_length=512)]
String2500 = Annotated[str, StringConstraints(max_length=2500)]
String5500 = Annotated[str, StringConstraints(max_length=5500)]


class OcppModel(BaseModel):
    """Base for every 2.0.1 payload and nested type."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# --- nested types -----------------------------------------------------------------------


class CustomDataType(OcppModel):
    """Vendor extension point present on every 2.0.1 type; extra vendor properties are kept."""

    model_config = ConfigDict(extra="allow")

    vendor_id: String255


class StatusInfoType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    reason_code: String20
    additional_info: Optional[String512] = None


class ModemType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    iccid: Optional[String20] = None
    imsi: Optional[String20] = None


class ChargingStationType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    serial_number: Optional[String25] = None
    model: String20
    modem: Optional[ModemType] = None
    vendor_name: String50
    firmware_version: Optional[String50] = None


class AdditionalInfoType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    additional_id_token: String36
    type: String50


class IdTokenType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    additional_info: Optional[Annotated[list[AdditionalInfoType], Field(min_length=1)]] = None
    id_token: String36
    type: IdTokenEnumType


class OCSPRequestDataType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    hash_algorithm: HashAlgorithmEnumType
    issuer_name_hash: String128
    issuer_key_hash: String128
    serial_number: String40
    responder_url: String512 = Field(alias="responderURL")


class MessageContentType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    format: MessageFormatEnumType
    language: Optional[String8] = None
    content: String512


class IdTokenInfoType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: AuthorizationStatusEnumType
    cache_expiry_date_time: Optional[DateTime] = None
    charging_priority: Optional[int] = None
    language1: Optional[String8] = None
    evse_id: Optional[Annotated[list[int], Field(min_length=1)]] = None
    group_id_token: Optional[IdTokenType] = None
    language2: Optional[String8] = None
    personal_message: Optional[MessageContentType] = None


class EVSEType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    id: int
    connector_id: Optional[int] = None


class SignedMeterValueType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    signed_meter_data: String2500
    signing_method: String50
    encoding_method: String50
    public_key: String2500


class UnitOfMeasureType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    unit: Optional[String20] = None
    multiplier: Optional[int] = None


class SampledValueType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    value: float
    context: Optional[ReadingContextEnumType] = None
    measurand: Optional[MeasurandEnumType] = None
    phase: Optional[PhaseEnumType] = None
    location: Optional[LocationEnumType] = None
    signed_meter_value: Optional[SignedMeterValueType] = None
    unit_of_measure: Optional[UnitOfMeasureType] = None


class MeterValueType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    sampled_value: Annotated[list[SampledValueType], Field(min_length=1)]
    timestamp: DateTime


class TransactionType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    transaction_id: String36
    charging_state: Optional[ChargingStateEnumType] = None
    time_spent_charging: Optional[int] = None
    stopped_reason: Optional[ReasonEnumType] = None
    remote_start_id: Optional[int] = None


# --- messages ---------------------------------------------------------------------------


class BootNotificationRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_station: ChargingStationType
    reason: BootReasonEnumType


class BootNotificationResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    current_time: DateTime
    interval: int
    status: RegistrationStatusEnumType
    status_info: Optional[StatusInfoType] = None


class HeartbeatRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None


class HeartbeatResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    current_time: DateTime


class StatusNotificationRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    timestamp: DateTime
    connector_status: ConnectorStatusEnumType
    evse_id: int
    connector_id: int


class StatusNotificationResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


class AuthorizeRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    id_token: IdTokenType
    certificate: Optional[String5500] = None
    iso15118_certificate_hash_data: Optional[
        Annotated[list[OCSPRequestDataType], Field(min_length=1, max_length=4)]
    ] = Field(default=None, alias="iso15118CertificateHashData")


class AuthorizeResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    id_token_info: IdTokenInfoType
    certificate_status: Optional[AuthorizeCertificateStatusEnumType] = None


class TransactionEventRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    event_type: TransactionEventEnumType
    meter_value: Optional[Annotated[list[MeterValueType], Field(min_length=1)]] = None
    timestamp: DateTime
    trigger_reason: TriggerReasonEnumType
    seq_no: int
    offline: Optional[bool] = None
    number_of_phases_used: Optional[int] = None
    cable_max_current: Optional[int] = None
    reservation_id: Optional[int] = None
    transaction_info: TransactionType
    evse: Optional[EVSEType] = None
    id_token: Optional[IdTokenType] = None


class TransactionEventResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    total_cost: Optional[float] = None
    charging_priority: Optional[int] = None
    id_token_info: Optional[IdTokenInfoType] = None
    updated_personal_message: Optional[MessageContentType] = None


class MeterValuesRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse_id: int
    meter_value: Annotated[list[MeterValueType], Field(min_length=1)]


class MeterValuesResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


class DataTransferRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    message_id: Optional[String50] = None
    # Untyped in the schema ("anyType"): any JSON value.
    data: Any = None
    vendor_id: String255


class DataTransferResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: DataTransferStatusEnumType
    status_info: Optional[StatusInfoType] = None
    data: Any = None


# --- registry ---------------------------------------------------------------------------

ACTIONS = (
    "BootNotification",
    "Heartbeat",
    "StatusNotification",
    "Authorize",
    "TransactionEvent",
    "MeterValues",
    "DataTransfer",
)

REQUESTS: dict[str, type[OcppModel]] = {a: globals()[f"{a}Request"] for a in ACTIONS}
RESPONSES: dict[str, type[OcppModel]] = {a: globals()[f"{a}Response"] for a in ACTIONS}
