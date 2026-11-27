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
    FirmwareStatusEnumType,
    UploadLogStatusEnumType,
    SetVariableStatusEnumType,
    ReportBaseEnumType,
    MutabilityEnumType,
    GetVariableStatusEnumType,
    GenericDeviceModelStatusEnumType,
    DataEnumType,
    AttributeEnumType,
    AuthorizationStatusEnumType,
    AuthorizeCertificateStatusEnumType,
    BootReasonEnumType,
    ChangeAvailabilityStatusEnumType,
    ChargingLimitSourceEnumType,
    ChargingProfileKindEnumType,
    ChargingProfileStatusEnumType,
    ClearChargingProfileStatusEnumType,
    GenericStatusEnumType,
    GetChargingProfileStatusEnumType,
    ChargingProfilePurposeEnumType,
    ChargingRateUnitEnumType,
    ChargingStateEnumType,
    ConnectorStatusEnumType,
    CostKindEnumType,
    DataTransferStatusEnumType,
    HashAlgorithmEnumType,
    IdTokenEnumType,
    LocationEnumType,
    MeasurandEnumType,
    MessageFormatEnumType,
    MessageTriggerEnumType,
    OperationalStatusEnumType,
    PhaseEnumType,
    ReadingContextEnumType,
    ReasonEnumType,
    RecurrencyKindEnumType,
    RegistrationStatusEnumType,
    RequestStartStopStatusEnumType,
    ResetEnumType,
    ResetStatusEnumType,
    TransactionEventEnumType,
    TriggerMessageStatusEnumType,
    TriggerReasonEnumType,
    UnlockStatusEnumType,
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
String32 = Annotated[str, StringConstraints(max_length=32)]
String36 = Annotated[str, StringConstraints(max_length=36)]
String40 = Annotated[str, StringConstraints(max_length=40)]
String50 = Annotated[str, StringConstraints(max_length=50)]
String128 = Annotated[str, StringConstraints(max_length=128)]
String255 = Annotated[str, StringConstraints(max_length=255)]
String512 = Annotated[str, StringConstraints(max_length=512)]
String1000 = Annotated[str, StringConstraints(max_length=1000)]
String16 = Annotated[str, StringConstraints(max_length=16)]
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


# --- M3 remote control: nested types ---------------------------------------------------------
# The charging profile of RequestStartTransaction (the same types serve the M5 smart-charging messages).


class RelativeTimeIntervalType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    start: int
    duration: Optional[int] = None


class CostType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    cost_kind: CostKindEnumType
    amount: int
    amount_multiplier: Optional[int] = None


class ConsumptionCostType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    start_value: float
    cost: Annotated[list[CostType], Field(min_length=1, max_length=3)]


class SalesTariffEntryType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    relative_time_interval: RelativeTimeIntervalType
    e_price_level: Optional[int] = None
    consumption_cost: Optional[Annotated[list[ConsumptionCostType], Field(min_length=1, max_length=3)]] = None


class SalesTariffType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    id: int
    sales_tariff_description: Optional[String32] = None
    num_e_price_levels: Optional[int] = None
    sales_tariff_entry: Annotated[list[SalesTariffEntryType], Field(min_length=1, max_length=1024)]


class ChargingSchedulePeriodType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    start_period: int
    limit: float
    number_phases: Optional[int] = None
    phase_to_use: Optional[int] = None


class ChargingScheduleType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    id: int
    start_schedule: Optional[DateTime] = None
    duration: Optional[int] = None
    charging_rate_unit: ChargingRateUnitEnumType
    charging_schedule_period: Annotated[list[ChargingSchedulePeriodType], Field(min_length=1, max_length=1024)]
    min_charging_rate: Optional[float] = None
    sales_tariff: Optional[SalesTariffType] = None


class ChargingProfileType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    id: int
    stack_level: int
    charging_profile_purpose: ChargingProfilePurposeEnumType
    charging_profile_kind: ChargingProfileKindEnumType
    recurrency_kind: Optional[RecurrencyKindEnumType] = None
    valid_from: Optional[DateTime] = None
    valid_to: Optional[DateTime] = None
    charging_schedule: Annotated[list[ChargingScheduleType], Field(min_length=1, max_length=3)]
    transaction_id: Optional[String36] = None


# --- M3 remote control: messages (CSMS -> CS) -------------------------------------------------


class RequestStartTransactionRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse_id: Optional[int] = None
    group_id_token: Optional[IdTokenType] = None
    id_token: IdTokenType
    remote_start_id: int
    charging_profile: Optional[ChargingProfileType] = None


class RequestStartTransactionResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: RequestStartStopStatusEnumType
    status_info: Optional[StatusInfoType] = None
    transaction_id: Optional[String36] = None


class RequestStopTransactionRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    transaction_id: String36


class RequestStopTransactionResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: RequestStartStopStatusEnumType
    status_info: Optional[StatusInfoType] = None


class UnlockConnectorRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse_id: int
    connector_id: int


class UnlockConnectorResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: UnlockStatusEnumType
    status_info: Optional[StatusInfoType] = None


class ResetRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    type: ResetEnumType
    evse_id: Optional[int] = None


class ResetResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: ResetStatusEnumType
    status_info: Optional[StatusInfoType] = None


class ChangeAvailabilityRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse: Optional[EVSEType] = None
    operational_status: OperationalStatusEnumType


class ChangeAvailabilityResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: ChangeAvailabilityStatusEnumType
    status_info: Optional[StatusInfoType] = None


class TriggerMessageRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse: Optional[EVSEType] = None
    requested_message: MessageTriggerEnumType


class TriggerMessageResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: TriggerMessageStatusEnumType
    status_info: Optional[StatusInfoType] = None


# --- M4 Device Model --------------------------------------------------------------------


class ComponentType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse: Optional[EVSEType] = None
    name: String50
    instance: Optional[String50] = None


class VariableType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    name: String50
    instance: Optional[String50] = None


class GetVariableDataType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    attribute_type: Optional[AttributeEnumType] = None  # schema default Actual
    component: ComponentType
    variable: VariableType


class GetVariableResultType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    attribute_status_info: Optional[StatusInfoType] = None
    attribute_status: GetVariableStatusEnumType
    attribute_type: Optional[AttributeEnumType] = None
    attribute_value: Optional[String2500] = None
    component: ComponentType
    variable: VariableType


class GetVariablesRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    get_variable_data: Annotated[list[GetVariableDataType], Field(min_length=1)]


class GetVariablesResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    get_variable_result: Annotated[list[GetVariableResultType], Field(min_length=1)]


class SetVariableDataType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    attribute_type: Optional[AttributeEnumType] = None  # schema default Actual
    attribute_value: String1000
    component: ComponentType
    variable: VariableType


class SetVariableResultType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    attribute_type: Optional[AttributeEnumType] = None
    attribute_status: SetVariableStatusEnumType
    attribute_status_info: Optional[StatusInfoType] = None
    component: ComponentType
    variable: VariableType


class SetVariablesRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    set_variable_data: Annotated[list[SetVariableDataType], Field(min_length=1)]


class SetVariablesResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    set_variable_result: Annotated[list[SetVariableResultType], Field(min_length=1)]


class GetBaseReportRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    request_id: int
    report_base: ReportBaseEnumType


class GetBaseReportResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: GenericDeviceModelStatusEnumType
    status_info: Optional[StatusInfoType] = None


class VariableAttributeType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    type: Optional[AttributeEnumType] = None  # schema default Actual
    value: Optional[String2500] = None
    mutability: Optional[MutabilityEnumType] = None  # schema default ReadWrite
    persistent: Optional[bool] = None  # schema default false
    constant: Optional[bool] = None  # schema default false


class VariableCharacteristicsType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    unit: Optional[String16] = None
    data_type: DataEnumType
    min_limit: Optional[float] = None
    max_limit: Optional[float] = None
    values_list: Optional[String1000] = None
    supports_monitoring: bool


class ReportDataType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    component: ComponentType
    variable: VariableType
    variable_attribute: Annotated[list[VariableAttributeType], Field(min_length=1, max_length=4)]
    variable_characteristics: Optional[VariableCharacteristicsType] = None


class NotifyReportRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    request_id: int
    generated_at: DateTime
    report_data: Optional[Annotated[list[ReportDataType], Field(min_length=1)]] = None
    tbc: Optional[bool] = None  # schema default false
    seq_no: int


class NotifyReportResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


# --- M5 smart charging -----------------------------------------------------------------------


class SetChargingProfileRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse_id: int
    charging_profile: ChargingProfileType


class SetChargingProfileResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: ChargingProfileStatusEnumType
    status_info: Optional[StatusInfoType] = None


class ClearChargingProfileType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    evse_id: Optional[int] = None
    charging_profile_purpose: Optional[ChargingProfilePurposeEnumType] = None
    stack_level: Optional[int] = None


class ClearChargingProfileRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_profile_id: Optional[int] = None
    charging_profile_criteria: Optional[ClearChargingProfileType] = None


class ClearChargingProfileResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: ClearChargingProfileStatusEnumType
    status_info: Optional[StatusInfoType] = None


class GetCompositeScheduleRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    duration: int
    charging_rate_unit: Optional[ChargingRateUnitEnumType] = None
    evse_id: int


class CompositeScheduleType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_schedule_period: Annotated[list[ChargingSchedulePeriodType], Field(min_length=1)]
    evse_id: int
    duration: int
    schedule_start: DateTime
    charging_rate_unit: ChargingRateUnitEnumType


class GetCompositeScheduleResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: GenericStatusEnumType
    status_info: Optional[StatusInfoType] = None
    schedule: Optional[CompositeScheduleType] = None


class ChargingProfileCriterionType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_profile_purpose: Optional[ChargingProfilePurposeEnumType] = None
    stack_level: Optional[int] = None
    charging_profile_id: Optional[Annotated[list[int], Field(min_length=1)]] = None
    charging_limit_source: Optional[Annotated[list[ChargingLimitSourceEnumType], Field(min_length=1,
                                                                                        max_length=4)]] = None


class GetChargingProfilesRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    request_id: int
    evse_id: Optional[int] = None
    charging_profile: ChargingProfileCriterionType


class GetChargingProfilesResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: GetChargingProfileStatusEnumType
    status_info: Optional[StatusInfoType] = None


class ReportChargingProfilesRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    request_id: int
    charging_limit_source: ChargingLimitSourceEnumType
    charging_profile: Annotated[list[ChargingProfileType], Field(min_length=1)]
    tbc: Optional[bool] = None  # schema default false
    evse_id: int


class ReportChargingProfilesResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


class ChargingLimitType(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_limit_source: ChargingLimitSourceEnumType
    is_grid_critical: Optional[bool] = None


class NotifyChargingLimitRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_schedule: Optional[Annotated[list[ChargingScheduleType], Field(min_length=1)]] = None
    evse_id: Optional[int] = None
    charging_limit: ChargingLimitType


class NotifyChargingLimitResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


class ClearedChargingLimitRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    charging_limit_source: ChargingLimitSourceEnumType
    evse_id: Optional[int] = None


class ClearedChargingLimitResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


class LogStatusNotificationRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: UploadLogStatusEnumType
    request_id: Optional[int] = None


class LogStatusNotificationResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


class FirmwareStatusNotificationRequest(OcppModel):
    custom_data: Optional[CustomDataType] = None
    status: FirmwareStatusEnumType
    request_id: Optional[int] = None


class FirmwareStatusNotificationResponse(OcppModel):
    custom_data: Optional[CustomDataType] = None


# --- registry ---------------------------------------------------------------------------

ACTIONS = (
    "BootNotification",
    "Heartbeat",
    "StatusNotification",
    "Authorize",
    "TransactionEvent",
    "MeterValues",
    "DataTransfer",
    # M3 remote control (CSMS -> CS)
    "RequestStartTransaction",
    "RequestStopTransaction",
    "UnlockConnector",
    "Reset",
    "ChangeAvailability",
    "TriggerMessage",
    # M4 Device Model
    "GetVariables",
    "SetVariables",
    "GetBaseReport",
    "NotifyReport",
    # M5 smart charging
    "SetChargingProfile",
    "ClearChargingProfile",
    "GetCompositeSchedule",
    "GetChargingProfiles",
    "ReportChargingProfiles",
    "NotifyChargingLimit",
    "ClearedChargingLimit",
    # TriggerMessage LogStatusNotification / FirmwareStatusNotification (F06.FR.14 / FR.16)
    "LogStatusNotification",
    "FirmwareStatusNotification",
)

REQUESTS: dict[str, type[OcppModel]] = {a: globals()[f"{a}Request"] for a in ACTIONS}
RESPONSES: dict[str, type[OcppModel]] = {a: globals()[f"{a}Response"] for a in ACTIONS}
