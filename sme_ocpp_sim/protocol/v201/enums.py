"""OCPP 2.0.1 enumerations.

Wire values are exactly those of the OCPP 2.0.1 JSON schemas (checked by
``tests/test_v201_schema_conformance.py``); class names are the spec data type names
(``...EnumType``). Only the enumerations of the messages in ``models.py`` are here (M2 core, M3 remote
control); later milestones add the ones their messages need.
"""

from __future__ import annotations

from enum import Enum


class AuthorizationStatusEnumType(str, Enum):
    accepted = "Accepted"
    blocked = "Blocked"
    concurrent_tx = "ConcurrentTx"
    expired = "Expired"
    invalid = "Invalid"
    no_credit = "NoCredit"
    not_allowed_type_evse = "NotAllowedTypeEVSE"
    not_at_this_location = "NotAtThisLocation"
    not_at_this_time = "NotAtThisTime"
    unknown = "Unknown"


class AuthorizeCertificateStatusEnumType(str, Enum):
    accepted = "Accepted"
    signature_error = "SignatureError"
    certificate_expired = "CertificateExpired"
    certificate_revoked = "CertificateRevoked"
    no_certificate_available = "NoCertificateAvailable"
    cert_chain_error = "CertChainError"
    contract_cancelled = "ContractCancelled"


class BootReasonEnumType(str, Enum):
    application_reset = "ApplicationReset"
    firmware_update = "FirmwareUpdate"
    local_reset = "LocalReset"
    power_up = "PowerUp"
    remote_reset = "RemoteReset"
    scheduled_reset = "ScheduledReset"
    triggered = "Triggered"
    unknown = "Unknown"
    watchdog = "Watchdog"


class ChargingStateEnumType(str, Enum):
    charging = "Charging"
    ev_connected = "EVConnected"
    suspended_ev = "SuspendedEV"
    suspended_evse = "SuspendedEVSE"
    idle = "Idle"


class ConnectorStatusEnumType(str, Enum):
    available = "Available"
    occupied = "Occupied"
    reserved = "Reserved"
    unavailable = "Unavailable"
    faulted = "Faulted"


class DataTransferStatusEnumType(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    unknown_message_id = "UnknownMessageId"
    unknown_vendor_id = "UnknownVendorId"


class HashAlgorithmEnumType(str, Enum):
    sha256 = "SHA256"
    sha384 = "SHA384"
    sha512 = "SHA512"


class IdTokenEnumType(str, Enum):
    central = "Central"
    e_maid = "eMAID"
    iso14443 = "ISO14443"
    iso15693 = "ISO15693"
    key_code = "KeyCode"
    local = "Local"
    mac_address = "MacAddress"
    no_authorization = "NoAuthorization"


class LocationEnumType(str, Enum):
    body = "Body"
    cable = "Cable"
    ev = "EV"
    inlet = "Inlet"
    outlet = "Outlet"


class MeasurandEnumType(str, Enum):
    current_export = "Current.Export"
    current_import = "Current.Import"
    current_offered = "Current.Offered"
    energy_active_export_register = "Energy.Active.Export.Register"
    energy_active_import_register = "Energy.Active.Import.Register"
    energy_reactive_export_register = "Energy.Reactive.Export.Register"
    energy_reactive_import_register = "Energy.Reactive.Import.Register"
    energy_active_export_interval = "Energy.Active.Export.Interval"
    energy_active_import_interval = "Energy.Active.Import.Interval"
    energy_active_net = "Energy.Active.Net"
    energy_reactive_export_interval = "Energy.Reactive.Export.Interval"
    energy_reactive_import_interval = "Energy.Reactive.Import.Interval"
    energy_reactive_net = "Energy.Reactive.Net"
    energy_apparent_net = "Energy.Apparent.Net"
    energy_apparent_import = "Energy.Apparent.Import"
    energy_apparent_export = "Energy.Apparent.Export"
    frequency = "Frequency"
    power_active_export = "Power.Active.Export"
    power_active_import = "Power.Active.Import"
    power_factor = "Power.Factor"
    power_offered = "Power.Offered"
    power_reactive_export = "Power.Reactive.Export"
    power_reactive_import = "Power.Reactive.Import"
    soc = "SoC"
    voltage = "Voltage"


class MessageFormatEnumType(str, Enum):
    ascii = "ASCII"
    html = "HTML"
    uri = "URI"
    utf8 = "UTF8"


class PhaseEnumType(str, Enum):
    l1 = "L1"
    l2 = "L2"
    l3 = "L3"
    n = "N"
    l1_n = "L1-N"
    l2_n = "L2-N"
    l3_n = "L3-N"
    l1_l2 = "L1-L2"
    l2_l3 = "L2-L3"
    l3_l1 = "L3-L1"


class ReadingContextEnumType(str, Enum):
    interruption_begin = "Interruption.Begin"
    interruption_end = "Interruption.End"
    other = "Other"
    sample_clock = "Sample.Clock"
    sample_periodic = "Sample.Periodic"
    transaction_begin = "Transaction.Begin"
    transaction_end = "Transaction.End"
    trigger = "Trigger"


class ReasonEnumType(str, Enum):
    de_authorized = "DeAuthorized"
    emergency_stop = "EmergencyStop"
    energy_limit_reached = "EnergyLimitReached"
    ev_disconnected = "EVDisconnected"
    ground_fault = "GroundFault"
    immediate_reset = "ImmediateReset"
    local = "Local"
    local_out_of_credit = "LocalOutOfCredit"
    master_pass = "MasterPass"
    other = "Other"
    overcurrent_fault = "OvercurrentFault"
    power_loss = "PowerLoss"
    power_quality = "PowerQuality"
    reboot = "Reboot"
    remote = "Remote"
    soc_limit_reached = "SOCLimitReached"
    stopped_by_ev = "StoppedByEV"
    time_limit_reached = "TimeLimitReached"
    timeout = "Timeout"


class RegistrationStatusEnumType(str, Enum):
    accepted = "Accepted"
    pending = "Pending"
    rejected = "Rejected"


class TransactionEventEnumType(str, Enum):
    ended = "Ended"
    started = "Started"
    updated = "Updated"


class TriggerReasonEnumType(str, Enum):
    authorized = "Authorized"
    cable_plugged_in = "CablePluggedIn"
    charging_rate_changed = "ChargingRateChanged"
    charging_state_changed = "ChargingStateChanged"
    deauthorized = "Deauthorized"
    energy_limit_reached = "EnergyLimitReached"
    ev_communication_lost = "EVCommunicationLost"
    ev_connect_timeout = "EVConnectTimeout"
    meter_value_clock = "MeterValueClock"
    meter_value_periodic = "MeterValuePeriodic"
    time_limit_reached = "TimeLimitReached"
    trigger = "Trigger"
    unlock_command = "UnlockCommand"
    stop_authorized = "StopAuthorized"
    ev_departed = "EVDeparted"
    ev_detected = "EVDetected"
    remote_stop = "RemoteStop"
    remote_start = "RemoteStart"
    abnormal_condition = "AbnormalCondition"
    signed_data_received = "SignedDataReceived"
    reset_command = "ResetCommand"


# --- M3 remote control -------------------------------------------------------------------


class ChangeAvailabilityStatusEnumType(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    scheduled = "Scheduled"


class ChargingProfileKindEnumType(str, Enum):
    absolute = "Absolute"
    recurring = "Recurring"
    relative = "Relative"


class ChargingProfilePurposeEnumType(str, Enum):
    charging_station_external_constraints = "ChargingStationExternalConstraints"
    charging_station_max_profile = "ChargingStationMaxProfile"
    tx_default_profile = "TxDefaultProfile"
    tx_profile = "TxProfile"


class ChargingRateUnitEnumType(str, Enum):
    w = "W"
    a = "A"


class CostKindEnumType(str, Enum):
    carbon_dioxide_emission = "CarbonDioxideEmission"
    relative_price_percentage = "RelativePricePercentage"
    renewable_generation_percentage = "RenewableGenerationPercentage"


class MessageTriggerEnumType(str, Enum):
    boot_notification = "BootNotification"
    log_status_notification = "LogStatusNotification"
    firmware_status_notification = "FirmwareStatusNotification"
    heartbeat = "Heartbeat"
    meter_values = "MeterValues"
    sign_charging_station_certificate = "SignChargingStationCertificate"
    sign_v2g_certificate = "SignV2GCertificate"
    status_notification = "StatusNotification"
    transaction_event = "TransactionEvent"
    sign_combined_certificate = "SignCombinedCertificate"
    publish_firmware_status_notification = "PublishFirmwareStatusNotification"


class OperationalStatusEnumType(str, Enum):
    inoperative = "Inoperative"
    operative = "Operative"


class RecurrencyKindEnumType(str, Enum):
    daily = "Daily"
    weekly = "Weekly"


class RequestStartStopStatusEnumType(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"


class ResetEnumType(str, Enum):
    immediate = "Immediate"
    on_idle = "OnIdle"


class ResetStatusEnumType(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    scheduled = "Scheduled"


class TriggerMessageStatusEnumType(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    not_implemented = "NotImplemented"


class UnlockStatusEnumType(str, Enum):
    unlocked = "Unlocked"
    unlock_failed = "UnlockFailed"
    ongoing_authorized_transaction = "OngoingAuthorizedTransaction"
    unknown_connector = "UnknownConnector"
