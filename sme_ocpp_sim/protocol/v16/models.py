"""OCPP 1.6J message models (pydantic v2, hand-written).

Source of truth for field names, required sets, enums, maxLength and integer/number types:
the OCA OCPP 1.6 JSON schemas (checked by ``tests/test_v16_schema_conformance.py``).

Conventions:

- Python attributes are snake_case; the wire name (spec camelCase) is the alias.
- Optional fields default to ``None`` and are left out on dump; an explicit JSON ``null`` is
  rejected by ``codec.parse`` (the schemas do not allow ``null``).
- Unknown fields are forbidden at model level; ``codec.parse`` decides whether to ignore them
  (lenient) or reject them (strict).
- Identifiers such as ``idTag`` are ``str``: leading zeros are preserved.
- ``dateTime`` fields serialize as UTC with milliseconds and ``Z``; naive values are UTC.

TODO(spec): numeric ranges stated only in the 1.6 spec text (e.g. connectorId >= 0 or > 0,
non-negative intervals / durations, stackLevel >= 0) are not in the schemas and not enforced
here; the handlers are expected to check them.
TODO(spec): the schemas declare ``multipleOf: 0.1`` for ``limit`` and ``minChargingRate``
("one fraction digit"); not enforced here because float ``multipleOf`` checks reject values
such as 0.3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
)
from pydantic.alias_generators import to_camel

from .enums import (
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingProfileStatus,
    ChargingRateUnitType,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    GetCompositeScheduleStatus,
    Location,
    Measurand,
    MessageTrigger,
    Phase,
    ReadingContext,
    Reason,
    RecurrencyKindType,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnitOfMeasure,
    UnlockStatus,
    ValueFormat,
)


def format_datetime(value: datetime) -> str:
    """UTC, millisecond precision, ``Z`` suffix; naive datetimes are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond // 1000:03d}Z"


DateTime = Annotated[datetime, PlainSerializer(format_datetime, return_type=str, when_used="json")]

CiString20 = Annotated[str, StringConstraints(max_length=20)]
CiString25 = Annotated[str, StringConstraints(max_length=25)]
CiString50 = Annotated[str, StringConstraints(max_length=50)]
CiString255 = Annotated[str, StringConstraints(max_length=255)]
CiString500 = Annotated[str, StringConstraints(max_length=500)]
IdToken = CiString20


class OcppModel(BaseModel):
    """Base for every 1.6J payload and nested type."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# --- nested types -----------------------------------------------------------------------


class IdTagInfo(OcppModel):
    status: AuthorizationStatus
    expiry_date: Optional[DateTime] = None
    parent_id_tag: Optional[IdToken] = None


class SampledValue(OcppModel):
    value: str
    context: Optional[ReadingContext] = None
    format: Optional[ValueFormat] = None
    measurand: Optional[Measurand] = None
    phase: Optional[Phase] = None
    location: Optional[Location] = None
    unit: Optional[UnitOfMeasure] = None


class MeterValue(OcppModel):
    timestamp: DateTime
    sampled_value: list[SampledValue]


class MeterValueNonEmpty(MeterValue):
    """MeterValue as used in MeterValues.req: at least one sampledValue.

    Core spec 1.6 ed.2 §7.33 MeterValue (p.88): sampledValue is 1..*; §6.31
    MeterValues.req (p.67): meterValue is 1..*. The official OCA JSON schema has no
    minItems here, so this is stricter than the schema on purpose (allowlisted in
    tests/test_v16_schema_conformance.py).
    """

    sampled_value: Annotated[list[SampledValue], Field(min_length=1)]


class ChargingSchedulePeriod(OcppModel):
    start_period: int
    limit: float
    number_phases: Optional[int] = None


class ChargingSchedule(OcppModel):
    charging_rate_unit: ChargingRateUnitType
    charging_schedule_period: list[ChargingSchedulePeriod]
    duration: Optional[int] = None
    start_schedule: Optional[DateTime] = None
    min_charging_rate: Optional[float] = None


class ChargingProfile(OcppModel):
    charging_profile_id: int
    stack_level: int
    charging_profile_purpose: ChargingProfilePurposeType
    charging_profile_kind: ChargingProfileKindType
    charging_schedule: ChargingSchedule
    transaction_id: Optional[int] = None
    recurrency_kind: Optional[RecurrencyKindType] = None
    valid_from: Optional[DateTime] = None
    valid_to: Optional[DateTime] = None


class KeyValue(OcppModel):
    key: CiString50
    readonly: bool
    value: Optional[CiString500] = None


# --- CP -> CSMS -------------------------------------------------------------------------


class BootNotificationRequest(OcppModel):
    charge_point_vendor: CiString20
    charge_point_model: CiString20
    charge_point_serial_number: Optional[CiString25] = None
    charge_box_serial_number: Optional[CiString25] = None
    firmware_version: Optional[CiString50] = None
    iccid: Optional[CiString20] = None
    imsi: Optional[CiString20] = None
    meter_type: Optional[CiString25] = None
    meter_serial_number: Optional[CiString25] = None


class BootNotificationResponse(OcppModel):
    status: RegistrationStatus
    current_time: DateTime
    interval: int


class HeartbeatRequest(OcppModel):
    pass


class HeartbeatResponse(OcppModel):
    current_time: DateTime


class StatusNotificationRequest(OcppModel):
    connector_id: int
    error_code: ChargePointErrorCode
    status: ChargePointStatus
    info: Optional[CiString50] = None
    timestamp: Optional[DateTime] = None
    vendor_id: Optional[CiString255] = None
    vendor_error_code: Optional[CiString50] = None


class StatusNotificationResponse(OcppModel):
    pass


class AuthorizeRequest(OcppModel):
    id_tag: IdToken


class AuthorizeResponse(OcppModel):
    id_tag_info: IdTagInfo


class StartTransactionRequest(OcppModel):
    connector_id: int
    id_tag: IdToken
    meter_start: int
    timestamp: DateTime
    reservation_id: Optional[int] = None


class StartTransactionResponse(OcppModel):
    id_tag_info: IdTagInfo
    transaction_id: int


class StopTransactionRequest(OcppModel):
    transaction_id: int
    timestamp: DateTime
    meter_stop: int
    id_tag: Optional[IdToken] = None
    reason: Optional[Reason] = None
    # The schema has no minItems on transactionData or its sampledValue arrays. §7.33
    # (p.88) makes sampledValue 1..* in every MeterValue, but transactionData is kept
    # schema-lenient so CSMS-side parsing accepts what the schema accepts.
    transaction_data: Optional[list[MeterValue]] = None


class StopTransactionResponse(OcppModel):
    id_tag_info: Optional[IdTagInfo] = None


class MeterValuesRequest(OcppModel):
    connector_id: int
    meter_value: Annotated[list[MeterValueNonEmpty], Field(min_length=1)]
    transaction_id: Optional[int] = None


class MeterValuesResponse(OcppModel):
    pass


class DataTransferRequest(OcppModel):
    vendor_id: CiString255
    message_id: Optional[CiString50] = None
    data: Optional[str] = None


class DataTransferResponse(OcppModel):
    status: DataTransferStatus
    data: Optional[str] = None


# --- CSMS -> CP -------------------------------------------------------------------------


class RemoteStartTransactionRequest(OcppModel):
    id_tag: IdToken
    connector_id: Optional[int] = None
    charging_profile: Optional[ChargingProfile] = None


class RemoteStartTransactionResponse(OcppModel):
    status: RemoteStartStopStatus


class RemoteStopTransactionRequest(OcppModel):
    transaction_id: int


class RemoteStopTransactionResponse(OcppModel):
    status: RemoteStartStopStatus


class GetConfigurationRequest(OcppModel):
    key: Optional[list[CiString50]] = None


class GetConfigurationResponse(OcppModel):
    configuration_key: Optional[list[KeyValue]] = None
    unknown_key: Optional[list[CiString50]] = None


class ChangeConfigurationRequest(OcppModel):
    key: CiString50
    value: CiString500


class ChangeConfigurationResponse(OcppModel):
    status: ConfigurationStatus


class ResetRequest(OcppModel):
    type: ResetType


class ResetResponse(OcppModel):
    status: ResetStatus


class TriggerMessageRequest(OcppModel):
    requested_message: MessageTrigger
    connector_id: Optional[int] = None


class TriggerMessageResponse(OcppModel):
    status: TriggerMessageStatus


class UnlockConnectorRequest(OcppModel):
    connector_id: int


class UnlockConnectorResponse(OcppModel):
    status: UnlockStatus


class ChangeAvailabilityRequest(OcppModel):
    connector_id: int
    type: AvailabilityType


class ChangeAvailabilityResponse(OcppModel):
    status: AvailabilityStatus


class SetChargingProfileRequest(OcppModel):
    connector_id: int
    cs_charging_profiles: ChargingProfile


class SetChargingProfileResponse(OcppModel):
    status: ChargingProfileStatus


class ClearChargingProfileRequest(OcppModel):
    id: Optional[int] = None
    connector_id: Optional[int] = None
    charging_profile_purpose: Optional[ChargingProfilePurposeType] = None
    stack_level: Optional[int] = None


class ClearChargingProfileResponse(OcppModel):
    status: ClearChargingProfileStatus


class GetCompositeScheduleRequest(OcppModel):
    connector_id: int
    duration: int
    charging_rate_unit: Optional[ChargingRateUnitType] = None


class GetCompositeScheduleResponse(OcppModel):
    status: GetCompositeScheduleStatus
    connector_id: Optional[int] = None
    schedule_start: Optional[DateTime] = None
    charging_schedule: Optional[ChargingSchedule] = None


# --- registry ---------------------------------------------------------------------------

ACTIONS = (
    "BootNotification",
    "Heartbeat",
    "StatusNotification",
    "Authorize",
    "StartTransaction",
    "StopTransaction",
    "MeterValues",
    "DataTransfer",
    "RemoteStartTransaction",
    "RemoteStopTransaction",
    "GetConfiguration",
    "ChangeConfiguration",
    "Reset",
    "TriggerMessage",
    "UnlockConnector",
    "ChangeAvailability",
    "SetChargingProfile",
    "ClearChargingProfile",
    "GetCompositeSchedule",
)

REQUESTS: dict[str, type[OcppModel]] = {a: globals()[f"{a}Request"] for a in ACTIONS}
RESPONSES: dict[str, type[OcppModel]] = {a: globals()[f"{a}Response"] for a in ACTIONS}
