"""OCPP 1.6J enumerations.

Wire values are exactly those of the OCA OCPP 1.6 JSON schemas; type names follow the
OCPP 1.6 specification data types.
"""

from __future__ import annotations

from enum import Enum


class RegistrationStatus(str, Enum):
    accepted = "Accepted"
    pending = "Pending"
    rejected = "Rejected"


class ChargePointErrorCode(str, Enum):
    connector_lock_failure = "ConnectorLockFailure"
    ev_communication_error = "EVCommunicationError"
    ground_failure = "GroundFailure"
    high_temperature = "HighTemperature"
    internal_error = "InternalError"
    local_list_conflict = "LocalListConflict"
    no_error = "NoError"
    other_error = "OtherError"
    over_current_failure = "OverCurrentFailure"
    power_meter_failure = "PowerMeterFailure"
    power_switch_failure = "PowerSwitchFailure"
    reader_failure = "ReaderFailure"
    reset_failure = "ResetFailure"
    under_voltage = "UnderVoltage"
    over_voltage = "OverVoltage"
    weak_signal = "WeakSignal"


class ChargePointStatus(str, Enum):
    available = "Available"
    preparing = "Preparing"
    charging = "Charging"
    suspended_evse = "SuspendedEVSE"
    suspended_ev = "SuspendedEV"
    finishing = "Finishing"
    reserved = "Reserved"
    unavailable = "Unavailable"
    faulted = "Faulted"


class AuthorizationStatus(str, Enum):
    accepted = "Accepted"
    blocked = "Blocked"
    expired = "Expired"
    invalid = "Invalid"
    concurrent_tx = "ConcurrentTx"


class Reason(str, Enum):
    emergency_stop = "EmergencyStop"
    ev_disconnected = "EVDisconnected"
    hard_reset = "HardReset"
    local = "Local"
    other = "Other"
    power_loss = "PowerLoss"
    reboot = "Reboot"
    remote = "Remote"
    soft_reset = "SoftReset"
    unlock_command = "UnlockCommand"
    de_authorized = "DeAuthorized"


class ReadingContext(str, Enum):
    interruption_begin = "Interruption.Begin"
    interruption_end = "Interruption.End"
    sample_clock = "Sample.Clock"
    sample_periodic = "Sample.Periodic"
    transaction_begin = "Transaction.Begin"
    transaction_end = "Transaction.End"
    trigger = "Trigger"
    other = "Other"


class ValueFormat(str, Enum):
    raw = "Raw"
    signed_data = "SignedData"


class Measurand(str, Enum):
    energy_active_export_register = "Energy.Active.Export.Register"
    energy_active_import_register = "Energy.Active.Import.Register"
    energy_reactive_export_register = "Energy.Reactive.Export.Register"
    energy_reactive_import_register = "Energy.Reactive.Import.Register"
    energy_active_export_interval = "Energy.Active.Export.Interval"
    energy_active_import_interval = "Energy.Active.Import.Interval"
    energy_reactive_export_interval = "Energy.Reactive.Export.Interval"
    energy_reactive_import_interval = "Energy.Reactive.Import.Interval"
    power_active_export = "Power.Active.Export"
    power_active_import = "Power.Active.Import"
    power_offered = "Power.Offered"
    power_reactive_export = "Power.Reactive.Export"
    power_reactive_import = "Power.Reactive.Import"
    power_factor = "Power.Factor"
    current_import = "Current.Import"
    current_export = "Current.Export"
    current_offered = "Current.Offered"
    voltage = "Voltage"
    frequency = "Frequency"
    temperature = "Temperature"
    soc = "SoC"
    rpm = "RPM"


class Phase(str, Enum):
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


class Location(str, Enum):
    cable = "Cable"
    ev = "EV"
    inlet = "Inlet"
    outlet = "Outlet"
    body = "Body"


class UnitOfMeasure(str, Enum):
    # Follows the official OCA schemas (MeterValues.json / StopTransaction.json), which
    # list both "Celcius" (sic) and "Celsius"; OCPP-J errata 2025-04 §2.1 (p.2-3) advises
    # a Charge Point to send "Celcius" and a Central System to accept both. There is deliberately no "Hertz": core
    # spec 1.6 ed.2 §7.31 Measurand "Frequency" (p.87) and errata sheet 3.86 (§7.45,
    # p.35) say 1.6 has no UnitOfMeasure for frequency, the implied unit is Hertz, so
    # a Frequency sample is sent without a unit. (The ``ocpp`` library adds "Hertz".)
    wh = "Wh"
    kwh = "kWh"
    varh = "varh"
    kvarh = "kvarh"
    w = "W"
    kw = "kW"
    va = "VA"
    kva = "kVA"
    var = "var"
    kvar = "kvar"
    a = "A"
    v = "V"
    k = "K"
    celcius = "Celcius"
    celsius = "Celsius"
    fahrenheit = "Fahrenheit"
    percent = "Percent"


class DataTransferStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    unknown_message_id = "UnknownMessageId"
    unknown_vendor_id = "UnknownVendorId"


class ChargingProfilePurposeType(str, Enum):
    charge_point_max_profile = "ChargePointMaxProfile"
    tx_default_profile = "TxDefaultProfile"
    tx_profile = "TxProfile"


class ChargingProfileKindType(str, Enum):
    absolute = "Absolute"
    recurring = "Recurring"
    relative = "Relative"


class RecurrencyKindType(str, Enum):
    daily = "Daily"
    weekly = "Weekly"


class ChargingRateUnitType(str, Enum):
    a = "A"
    w = "W"


class RemoteStartStopStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"


class ConfigurationStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    reboot_required = "RebootRequired"
    not_supported = "NotSupported"


class ResetType(str, Enum):
    hard = "Hard"
    soft = "Soft"


class ResetStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"


class MessageTrigger(str, Enum):
    boot_notification = "BootNotification"
    diagnostics_status_notification = "DiagnosticsStatusNotification"
    firmware_status_notification = "FirmwareStatusNotification"
    heartbeat = "Heartbeat"
    meter_values = "MeterValues"
    status_notification = "StatusNotification"


class TriggerMessageStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    not_implemented = "NotImplemented"


class UnlockStatus(str, Enum):
    unlocked = "Unlocked"
    unlock_failed = "UnlockFailed"
    not_supported = "NotSupported"


class AvailabilityType(str, Enum):
    inoperative = "Inoperative"
    operative = "Operative"


class AvailabilityStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    scheduled = "Scheduled"


class ChargingProfileStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
    not_supported = "NotSupported"


class ClearChargingProfileStatus(str, Enum):
    accepted = "Accepted"
    unknown = "Unknown"


class GetCompositeScheduleStatus(str, Enum):
    accepted = "Accepted"
    rejected = "Rejected"
