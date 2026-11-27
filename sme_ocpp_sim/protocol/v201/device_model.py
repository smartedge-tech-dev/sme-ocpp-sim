"""OCPP 2.0.1 Device Model: components, variables and their attributes (plan §4 / §11.1 Q7 / §11.2, M4).

A separate stack from ``protocol/v16``: the 1.6 ``ConfigStore`` and key table are not used. One
:class:`DeviceModel` per 2.0.1 station holds :class:`Variable` entries addressed by component (name, instance,
EVSE, connector) and variable (name, instance). It answers GetVariables / SetVariables item by item
(:meth:`DeviceModel.get` / :meth:`DeviceModel.set`) and lists the entries of a GetBaseReport
(:meth:`DeviceModel.report`); the station sends the NotifyReport parts.

The variable set is minimal (plan §11.1 Q7): :func:`standard_variables` holds the variables the station uses,
and the charger YAML ``variables:`` list overrides their values or adds user-defined variables
(:func:`apply_config`). Component / variable names of the standard set are the ones the ``ocpp`` library
(``ocpp.v201.enums``, sourced from OCPP 2.0.1 Part 2 Appendix 3) lists.

Spec references: OCPP 2.0.1 Edition 4 Part 2 "Referenced Components and Variables" (section numbers in brackets
below) and the Appendices v1.5 CSV ``dm_components_vars.csv``. dataType, mutability, unit and the limits of the
standard set are taken from there; where the spec leaves a choice (``ReadWrite or ReadOnly``, a default value, an
implementer-defined maxLimit) the choice is stated in the table.

Interpretations (not defined by the spec)
-----------------------------------------
- Lookup order of one GetVariables / SetVariables item: no component with that name, instance, EVSE and
  connector -> ``UnknownComponent``; no variable with that name and instance in it -> ``UnknownVariable``; the
  attribute type is not one the variable has -> ``NotSupportedAttributeType``; then the mutability. A component
  is matched exactly: an EVSE component is not found without its ``evse`` and a station-level one is not found
  with one. A missing ``attributeType`` is ``Actual`` (the schema default; B05.FR.12).
- Mutability belongs to the variable and applies to every attribute it has: GetVariables of a ``WriteOnly``
  variable and SetVariables of a ``ReadOnly`` one are ``Rejected`` (``statusInfo.reasonCode`` ``WriteOnly`` /
  ``ReadOnly``). A ``WriteOnly`` value is never reported (NotifyReport leaves ``value`` out; B07.FR.03).
- Value validation of SetVariables (``Rejected``, ``reasonCode`` :data:`REASON_INVALID_VALUE` /
  :data:`REASON_OUT_OF_RANGE` / :data:`REASON_NOT_IN_LIST`): ``integer`` = optional ``-`` and ASCII digits;
  ``decimal`` = a finite number; ``boolean`` = ``true`` / ``false`` (any case, stored lower case); ``dateTime`` =
  ISO 8601; ``OptionList`` = one value of ``valuesList``; ``SequenceList`` / ``MemberList`` = comma-separated
  values of ``valuesList`` (MemberList without duplicates; an empty value is the empty list). ``minLimit`` /
  ``maxLimit`` bound the number for ``integer`` / ``decimal`` and the length for ``string``; for a list type
  they are informative. Every value is at most :data:`MAX_VALUE_LENGTH` characters (``ConfigurationValueSize``
  is not modelled).
- ``RebootRequired`` (user-defined YAML variables only): the value of a variable marked ``reboot_required`` is
  validated, stored and reported at once, but the station does not act on it.
- A SetVariables value equal to the stored one is ``Accepted`` and changes nothing.
- SetVariables changes live in memory only: they are not written back to the YAML file (the 1.6
  ChangeConfiguration is not either), so a reload or restart starts from the YAML values again. This includes
  a new SecurityCtrlr.BasicAuthPassword.
- Report bases: ``FullInventory`` = every variable; ``ConfigurationInventory`` = every variable that is not
  ``ReadOnly`` (B07.FR.07); ``SummaryInventory`` (B07.FR.09) = ChargingStation.AvailabilityState and each
  EVSE's AvailabilityState (the Connector's equals its EVSE's, one connector per EVSE, so it is left out; no
  Problem / Tripped / Overload / Fallback variables are modelled), plus YAML variables with ``summary: true``.
  ``supportsMonitoring`` is ``false`` (monitoring is not modelled).
- "Fixed" variables are required by the spec, but the behaviour they configure is not modelled: they are
  ``ReadOnly`` (where the spec allows ReadWrite, too) with the value that describes what the simulator does, and
  YAML may not override them.
- Standard variables:

  ============================================  =========  ==========  ==================================
  Component . Variable [instance] (Part 2 §)    dataType   mutability  value / effect
  ============================================  =========  ==========  ==================================
  OCPPCommCtrlr.HeartbeatInterval (2.1.5)       integer s  ReadWrite   heartbeat interval, minLimit 1
                                                                       (spec "> 0"); an accepted
                                                                       BootNotification sets it too
  OCPPCommCtrlr.MessageTimeout [Default]        integer s  ReadOnly    the CALL timeout of the runtime
  OCPPCommCtrlr.MessageAttempts                 integer    ReadWrite   attempts of a TransactionEvent
  [TransactionEvent]                                                   (default 3)
  OCPPCommCtrlr.MessageAttemptInterval          integer s  ReadWrite   retry step of a TransactionEvent
  [TransactionEvent]                                                   (default 10)
  OCPPCommCtrlr.FileTransferProtocols (2.1.4)   MemberList ReadOnly    fixed: empty (no file transfer)
  OCPPCommCtrlr.NetworkConfigurationPriority    SequenceL. ReadOnly    fixed: ``1``, the one connection of
  (2.1.6)                                                              the YAML (valuesList ``1``)
  OCPPCommCtrlr                                 integer    ReadOnly    fixed: 3 (the shell retries with
  .NetworkProfileConnectionAttempts (2.1.7)                            back-off, there is no fallback)
  OCPPCommCtrlr.OfflineThreshold (2.1.8)        integer s  ReadOnly    fixed: 60 (informative)
  OCPPCommCtrlr.UnlockOnEVSideDisconnect        boolean    ReadOnly    fixed: true (no lock is modelled)
  (2.1.12)
  OCPPCommCtrlr.ResetRetries (2.1.14)           integer    ReadOnly    fixed: 0 (a Reset never fails)
  SampledDataCtrlr.TxUpdatedInterval            integer s  ReadWrite   periodic Updated interval (0 = none;
                                                                       ``meter_sample_interval_s`` / 60)
  SampledDataCtrlr.TxEndedInterval (2.7.8)      integer s  ReadOnly    fixed: 0 (only the start and end
                                                                       readings are sent)
  SampledDataCtrlr.TxStartedMeasurands /        MemberList ReadWrite   readings of Started / Updated /
  TxUpdatedMeasurands / TxEndedMeasurands                              Ended; maxLimit (implementer
  (2.7.9 ..)                                                           defined) = length of the full list
  AuthCtrlr.Enabled (2.3.1)                     boolean    ReadWrite   false: every token is accepted
                                                                       (initial: ``auth_mode`` is not
                                                                       ``accept_all``)
  AuthCtrlr.AuthorizeRemoteStart (2.3.4)        boolean    ReadWrite   authorize a RequestStartTransaction
                                                                       token first (default false)
  AuthCtrlr.LocalPreAuthorize (2.3.6)           boolean    ReadWrite   online: an ``id_tags`` token is
                                                                       accepted without Authorize (initial:
                                                                       ``local_only`` / ``local_then_csms``)
  AuthCtrlr.LocalAuthorizeOffline (2.3.5)       boolean    ReadWrite   offline: an ``id_tags`` token is
                                                                       accepted (initial: the same)
  AuthCtrlr.OfflineTxForUnknownIdEnabled        boolean    ReadWrite   offline: any other token is accepted
  (2.3.3)                                                              (initial: ``accept_all``)
  AuthCtrlr.DisableRemoteAuthorization (2.3.8)  boolean    ReadWrite   no Authorize is sent: ``id_tags``
                                                                       tokens accepted, others Invalid
                                                                       (initial: ``local_only``)
  TxCtrlr.EVConnectionTimeOut (2.6.1)           integer s  ReadOnly    fixed: 120 (informative: a waiting
                                                                       token does not time out)
  TxCtrlr.StopTxOnEVSideDisconnect (2.6.2)      boolean    ReadOnly    fixed: true (unplugging ends the
                                                                       transaction)
  TxCtrlr.TxStartPoint / TxStopPoint            MemberList ReadOnly    ``PowerPathClosed`` (M2 behaviour)
  TxCtrlr.StopTxOnInvalidId                     boolean    ReadWrite   end a transaction whose token the
                                                                       CSMS refuses (default true)
  SecurityCtrlr.SecurityProfile (2.2.5)         integer    ReadOnly    1 / 2 from password and ws(s)://;
                                                                       0 (no password) is a simulator-only
                                                                       value, the spec defines 1 / 2 / 3
  SecurityCtrlr.Identity (2.2.2)                string     ReadOnly    the charger id; maxLimit 48
  SecurityCtrlr.CertificateEntries (2.2.4)      integer    ReadOnly    fixed: 0, maxLimit 0 (no
                                                                       certificate management)
  SecurityCtrlr.OrganizationName                string     ReadWrite   ``vendor`` (not used)
  SecurityCtrlr.BasicAuthPassword (2.2.1)       string     WriteOnly   ``password``; 16..64 characters
                                                                       (maxLimit: spec "at least 40, at
                                                                       most 64"). A new value is
                                                                       ``Accepted``, then the station
                                                                       reconnects with it (A01)
  DeviceDataCtrlr.ItemsPerMessage               integer    ReadOnly    50: GetVariables / SetVariables
  [GetReport / GetVariables / SetVariables]                            items (more -> CALLERROR
                                                                       OccurrenceConstraintViolation),
                                                                       reportData per NotifyReport
  DeviceDataCtrlr.BytesPerMessage [same]        integer    ReadOnly    65000, informative (not enforced)
  SmartChargingCtrlr.Enabled (2.9.1)            boolean    ReadWrite   true; stored and reported, not
                                                                       acted on (profiles are always
                                                                       handled)
  SmartChargingCtrlr.Available (2.9.2)          boolean    ReadOnly    true
  SmartChargingCtrlr                            boolean    ReadWrite   false: the operator external limit
  .ExternalControlSignalsEnabled                                       is refused (default true)
  SmartChargingCtrlr                            boolean    ReadWrite   NotifyChargingLimit carries the
  .NotifyChargingLimitWithSchedules (2.9.8)                            schedule (default false)
  SmartChargingCtrlr.Entries [ChargingProfiles] integer    ReadOnly    the profiles installed now (no
  (2.9.10)                                                             capacity is modelled: no maxLimit)
  SmartChargingCtrlr.LimitChangeSignificance    decimal    ReadOnly    fixed: 0 (every change is sent)
  (2.9.11)
  SmartChargingCtrlr.ProfileStackLevel          integer    ReadOnly    100: higher stackLevel -> Rejected
  SmartChargingCtrlr.PeriodsPerSchedule         integer    ReadOnly    1024: more periods -> Rejected
  SmartChargingCtrlr.RateUnit                   MemberList ReadOnly    A,W: another unit -> Rejected
  ChargingStation.Available / AvailabilityState boolean /  ReadOnly    true; ``Unavailable`` when every
  (CSV, required)                               OptionList             EVSE is, else ``Available``
  EVSE(evse i).Available / AvailabilityState    boolean /  ReadOnly    true; the connectorStatus of EVSE i
  (CSV, required)                               OptionList
  EVSE(evse i).Power (CSV, required)            decimal W  ReadOnly    the power now; maxLimit
                                                                       ``max_power_w``
  EVSE(evse i).SupplyPhases (CSV, required)     integer    ReadOnly    ``phases`` of the charger
  Connector(evse i, connector 1).Available /    boolean /  ReadOnly    true; the connectorStatus of EVSE i
  AvailabilityState (CSV)                       OptionList
  ============================================  =========  ==========  ==================================

- The ReadOnly variables derived from other settings or the station state (the charger id, the CSMS url, the
  CALL timeout, the transaction start / stop points, the availability, the power) and the fixed ones cannot be
  overridden in YAML; the other standard variables take a YAML ``value`` (and ``target`` / ``min_set`` /
  ``max_set`` when they have that attribute). The AuthCtrlr variables get their initial value from
  ``auth_mode``; a YAML ``value`` overrides it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import models as m
from .enums import (AttributeEnumType, DataEnumType, GetVariableStatusEnumType, MutabilityEnumType,
                    ReportBaseEnumType, SetVariableStatusEnumType)

AT = AttributeEnumType
DT = DataEnumType
MU = MutabilityEnumType
GS = GetVariableStatusEnumType
SS = SetVariableStatusEnumType

#: Longest value a variable may hold (SetVariables ``attributeValue`` maxLength).
MAX_VALUE_LENGTH = 1000
#: ``statusInfo.reasonCode`` of a refused GetVariables / SetVariables item (informative simulator codes).
REASON_READ_ONLY = "ReadOnly"
REASON_WRITE_ONLY = "WriteOnly"
REASON_INVALID_VALUE = "InvalidValue"
REASON_OUT_OF_RANGE = "OutOfRange"
REASON_NOT_IN_LIST = "NotInValuesList"

#: Attribute types in report order.
ATTRIBUTE_ORDER: Tuple[AttributeEnumType, ...] = (AT.actual, AT.target, AT.min_set, AT.max_set)

# Standard names (ocpp.v201.enums ControllerComponentName / *VariableName / *InstanceName).
OCPP_COMM = "OCPPCommCtrlr"
SAMPLED_DATA = "SampledDataCtrlr"
AUTH = "AuthCtrlr"
TX = "TxCtrlr"
SECURITY = "SecurityCtrlr"
DEVICE_DATA = "DeviceDataCtrlr"
SMART_CHARGING = "SmartChargingCtrlr"
CHARGING_STATION = "ChargingStation"
EVSE_COMPONENT = "EVSE"
CONNECTOR_COMPONENT = "Connector"

HEARTBEAT_INTERVAL = (OCPP_COMM, "HeartbeatInterval")
TX_UPDATED_INTERVAL = (SAMPLED_DATA, "TxUpdatedInterval")
TX_STARTED_MEASURANDS = (SAMPLED_DATA, "TxStartedMeasurands")
TX_UPDATED_MEASURANDS = (SAMPLED_DATA, "TxUpdatedMeasurands")
TX_ENDED_MEASURANDS = (SAMPLED_DATA, "TxEndedMeasurands")
AUTHORIZE_REMOTE_START = (AUTH, "AuthorizeRemoteStart")
STOP_TX_ON_INVALID_ID = (TX, "StopTxOnInvalidId")
MESSAGE_ATTEMPTS = (OCPP_COMM, "MessageAttempts")
MESSAGE_ATTEMPT_INTERVAL = (OCPP_COMM, "MessageAttemptInterval")
ITEMS_PER_MESSAGE = (DEVICE_DATA, "ItemsPerMessage")
SC_ENABLED = (SMART_CHARGING, "Enabled")
SC_AVAILABLE = (SMART_CHARGING, "Available")
EXTERNAL_CONTROL_SIGNALS_ENABLED = (SMART_CHARGING, "ExternalControlSignalsEnabled")
NOTIFY_CHARGING_LIMIT_WITH_SCHEDULES = (SMART_CHARGING, "NotifyChargingLimitWithSchedules")
PROFILE_STACK_LEVEL = (SMART_CHARGING, "ProfileStackLevel")
PERIODS_PER_SCHEDULE = (SMART_CHARGING, "PeriodsPerSchedule")
RATE_UNIT = (SMART_CHARGING, "RateUnit")
AUTH_ENABLED = (AUTH, "Enabled")
LOCAL_PRE_AUTHORIZE = (AUTH, "LocalPreAuthorize")
LOCAL_AUTHORIZE_OFFLINE = (AUTH, "LocalAuthorizeOffline")
OFFLINE_TX_FOR_UNKNOWN_ID = (AUTH, "OfflineTxForUnknownIdEnabled")
DISABLE_REMOTE_AUTHORIZATION = (AUTH, "DisableRemoteAuthorization")
SECURITY_PROFILE = (SECURITY, "SecurityProfile")
BASIC_AUTH_PASSWORD = (SECURITY, "BasicAuthPassword")
INSTANCE_CHARGING_PROFILES = "ChargingProfiles"
INSTANCE_TX_EVENT = "TransactionEvent"
INSTANCE_GET_REPORT = "GetReport"
INSTANCE_GET_VARIABLES = "GetVariables"
INSTANCE_SET_VARIABLES = "SetVariables"

#: Heartbeat interval until a BootNotification answer (or SetVariables) sets one.
DEFAULT_HEARTBEAT_S = 300
#: TxUpdatedInterval when the charger's ``meter_sample_interval_s`` is not set.
DEFAULT_TX_UPDATED_INTERVAL_S = 60
DEFAULT_MESSAGE_TIMEOUT_S = 30.0
ENERGY = "Energy.Active.Import.Register"
POWER = "Power.Active.Import"
CURRENT = "Current.Import"
VOLTAGE = "Voltage"
SOC = "SoC"
#: Measurands the station can report (the valuesList of the Tx*Measurands variables).
MEASURANDS: Tuple[str, ...] = (ENERGY, POWER, CURRENT, VOLTAGE, SOC)
DEFAULT_TX_STARTED: Tuple[str, ...] = (ENERGY,)
DEFAULT_TX_UPDATED: Tuple[str, ...] = (ENERGY, POWER)
DEFAULT_TX_ENDED: Tuple[str, ...] = (ENERGY,)
DEFAULT_ITEMS_PER_MESSAGE = 50
DEFAULT_BYTES_PER_MESSAGE = 65000
DEFAULT_MESSAGE_ATTEMPTS = 3
DEFAULT_MESSAGE_ATTEMPT_INTERVAL_S = 10
DEFAULT_PROFILE_STACK_LEVEL = 100
DEFAULT_PERIODS_PER_SCHEDULE = 1024  # the schema's maxItems of chargingSchedulePeriod
RATE_UNITS = ("A", "W")
#: ``TxStartPoint`` / ``TxStopPoint`` values (OCPP 2.0.1 E01; the station uses PowerPathClosed only).
TX_POINTS = ("ParkingBayOccupancy", "EVConnected", "Authorized", "DataSigned", "PowerPathClosed",
             "EnergyTransfer")
AVAILABILITY_STATES = ("Available", "Occupied", "Reserved", "Unavailable", "Faulted")
#: FileTransferProtocols valuesList (Part 2 §2.1.4).
FILE_TRANSFER_PROTOCOLS = ("FTP", "FTPS", "HTTP", "HTTPS", "SFTP")
#: SecurityCtrlr.Identity maxLimit (Part 2 §2.2.2).
IDENTITY_MAX = 48
#: SecurityCtrlr.BasicAuthPassword length (Part 2 §2.2.1: at least 16; maxLimit at least 40, at most 64).
PASSWORD_MIN, PASSWORD_MAX = 16, 64

#: (component, component instance, EVSE id, connector id, variable, variable instance)
Key = Tuple[str, Optional[str], Optional[int], Optional[int], str, Optional[str]]
ComponentKey = Tuple[str, Optional[str], Optional[int], Optional[int]]


@dataclass
class Variable:
    """One variable of one component, with the values of the attributes it has (``Actual`` always)."""

    component: str
    variable: str
    data_type: DataEnumType
    values: Dict[AttributeEnumType, str]
    mutability: MutabilityEnumType = MU.read_write
    component_instance: Optional[str] = None
    variable_instance: Optional[str] = None
    evse_id: Optional[int] = None
    connector_id: Optional[int] = None
    unit: Optional[str] = None
    min_limit: Optional[float] = None
    max_limit: Optional[float] = None
    values_list: Optional[Tuple[str, ...]] = None
    reboot_required: bool = False
    persistent: bool = False
    constant: bool = False
    #: Part of a SummaryInventory report.
    summary: bool = False
    #: Derived from other settings / the station state: YAML may not override it.
    derived: bool = False
    #: A live ``Actual`` value (a station state); replaces ``values[Actual]`` when set.
    getter: Optional[Callable[[], str]] = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> Key:
        return (self.component, self.component_instance, self.evse_id, self.connector_id, self.variable,
                self.variable_instance)

    @property
    def component_key(self) -> ComponentKey:
        return self.key[:4]  # type: ignore[return-value]

    def value(self, attr: AttributeEnumType = AT.actual) -> Optional[str]:
        if attr is AT.actual and self.getter is not None:
            return self.getter()
        return self.values.get(attr)

    def label(self) -> str:
        """``Component[inst]@evse:conn.Variable[inst]`` for messages."""
        c = self.component + (f"[{self.component_instance}]" if self.component_instance else "")
        if self.evse_id is not None:
            c += f"@{self.evse_id}" + (f":{self.connector_id}" if self.connector_id is not None else "")
        return c + "." + self.variable + (f"[{self.variable_instance}]" if self.variable_instance else "")


class InvalidValue(ValueError):
    """A value that does not fit a variable; ``reason`` is the ``statusInfo.reasonCode``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _limits(var: Variable, number: float, what: str) -> None:
    if var.min_limit is not None and number < var.min_limit:
        raise InvalidValue(REASON_OUT_OF_RANGE, f"{what} {number:g} is below minLimit {var.min_limit:g}")
    if var.max_limit is not None and number > var.max_limit:
        raise InvalidValue(REASON_OUT_OF_RANGE, f"{what} {number:g} is above maxLimit {var.max_limit:g}")


def normalize(var: Variable, value: str) -> str:
    """The stored form of ``value`` for ``var``; :class:`InvalidValue` when it does not fit (module docstring)."""
    if not isinstance(value, str):
        raise InvalidValue(REASON_INVALID_VALUE, f"value must be a string, got {type(value).__name__}")
    if len(value) > MAX_VALUE_LENGTH:
        raise InvalidValue(REASON_INVALID_VALUE, f"value longer than {MAX_VALUE_LENGTH} characters")
    t = var.data_type
    if t is DT.string:
        _limits(var, len(value), "length")
        return value
    v = value.strip()
    if t is DT.integer:
        digits = v[1:] if v.startswith("-") else v
        if not (digits.isascii() and digits.isdigit()):
            raise InvalidValue(REASON_INVALID_VALUE, f"{value!r} is not an integer")
        _limits(var, int(v), "value")
        return str(int(v))
    if t is DT.decimal:
        try:
            number = float(v)
        except ValueError:
            number = math.nan
        if not math.isfinite(number) or not v.isascii() or "_" in v:
            raise InvalidValue(REASON_INVALID_VALUE, f"{value!r} is not a decimal number")
        _limits(var, number, "value")
        return v
    if t is DT.boolean:
        low = v.lower()
        if low not in ("true", "false"):
            raise InvalidValue(REASON_INVALID_VALUE, f"{value!r} is not true / false")
        return low
    if t is DT.date_time:
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise InvalidValue(REASON_INVALID_VALUE, f"{value!r} is not an ISO 8601 date-time") from None
        return v
    allowed = var.values_list or ()
    if t is DT.option_list:
        if v not in allowed:
            raise InvalidValue(REASON_NOT_IN_LIST, f"{value!r} is not one of {','.join(allowed)}")
        return v
    items = [p.strip() for p in v.split(",")] if v else []
    if any(not p for p in items):
        raise InvalidValue(REASON_INVALID_VALUE, f"{value!r} has an empty list item")
    bad = [p for p in items if p not in allowed]
    if bad:
        raise InvalidValue(REASON_NOT_IN_LIST, f"{','.join(bad)} not in {','.join(allowed)}")
    if t is DT.member_list and len(set(items)) != len(items):
        raise InvalidValue(REASON_INVALID_VALUE, f"{value!r} lists a member twice")
    return ",".join(items)


def check_definition(var: Variable) -> None:
    """``ValueError`` when ``var`` is not a consistent definition (used for YAML variables)."""
    where = var.label()
    if var.data_type in (DT.option_list, DT.sequence_list, DT.member_list):
        if not var.values_list:
            raise ValueError(f"{where}: dataType {var.data_type.value} needs values_list")
        if len(",".join(var.values_list)) > MAX_VALUE_LENGTH or any("," in x or not x for x in var.values_list):
            raise ValueError(f"{where}: values_list items must be non-empty, without ',' and at most "
                             f"{MAX_VALUE_LENGTH} characters together")
    if var.min_limit is not None and var.max_limit is not None and var.min_limit > var.max_limit:
        raise ValueError(f"{where}: min_limit is above max_limit")
    if var.connector_id is not None and var.evse_id is None:
        raise ValueError(f"{where}: connector needs evse")
    if AT.actual not in var.values and var.getter is None:
        raise ValueError(f"{where}: no value")
    for attr, value in list(var.values.items()):
        try:
            var.values[attr] = normalize(var, value)
        except InvalidValue as e:
            raise ValueError(f"{where} {attr.value}: {e}") from None


Listener = Callable[[str], None]


class DeviceModel:
    """The variables of one station (see the module docstring)."""

    def __init__(self, variables: Iterable[Variable] = ()) -> None:
        self._vars: Dict[Key, Variable] = {}
        self._components: Dict[ComponentKey, int] = {}
        self._listeners: Dict[Key, Listener] = {}
        for v in variables:
            self.add(v)

    # ------------------------------------------------------------------ table

    def add(self, var: Variable) -> None:
        if var.key in self._vars:
            raise ValueError(f"variable {var.label()} defined twice")
        self._vars[var.key] = var
        self._components[var.component_key] = self._components.get(var.component_key, 0) + 1

    def variables(self) -> List[Variable]:
        return list(self._vars.values())

    def lookup(self, component: str, variable: str, *, component_instance: Optional[str] = None,
               variable_instance: Optional[str] = None, evse_id: Optional[int] = None,
               connector_id: Optional[int] = None) -> Optional[Variable]:
        return self._vars.get((component, component_instance, evse_id, connector_id, variable, variable_instance))

    def _var(self, name: Tuple[str, str], instance: Optional[str] = None) -> Variable:
        var = self.lookup(name[0], name[1], variable_instance=instance)
        if var is None:
            raise KeyError(f"{name[0]}.{name[1]}" + (f"[{instance}]" if instance else ""))
        return var

    def value(self, name: Tuple[str, str], instance: Optional[str] = None) -> str:
        """``Actual`` value of a station-level standard variable (``KeyError`` when it does not exist)."""
        value = self._var(name, instance).value()
        assert value is not None  # Actual is always there (check_definition / standard_variables)
        return value

    def int_value(self, name: Tuple[str, str], instance: Optional[str] = None) -> int:
        return int(self.value(name, instance))

    def bool_value(self, name: Tuple[str, str], instance: Optional[str] = None) -> bool:
        return self.value(name, instance) == "true"

    def list_value(self, name: Tuple[str, str], instance: Optional[str] = None) -> Tuple[str, ...]:
        v = self.value(name, instance)
        return tuple(v.split(",")) if v else ()

    def put(self, name: Tuple[str, str], value: str, instance: Optional[str] = None) -> None:
        """Store an ``Actual`` value the station itself decided (e.g. the BootNotification interval); no
        listener is called. ``InvalidValue`` when it does not fit."""
        var = self._var(name, instance)
        var.values[AT.actual] = normalize(var, value)

    def on_change(self, name: Tuple[str, str], fn: Listener, instance: Optional[str] = None) -> None:
        """Call ``fn(new_actual_value)`` after an accepted SetVariables changed that variable's ``Actual``."""
        self._listeners[self._var(name, instance).key] = fn

    # ------------------------------------------------------------------ GetVariables / SetVariables

    def _find(self, c: m.ComponentType, v: m.VariableType) -> Tuple[Optional[str], Optional[Variable]]:
        """``(status, variable)``: status ``None`` when found, else UnknownComponent / UnknownVariable."""
        evse_id = c.evse.id if c.evse is not None else None
        conn_id = c.evse.connector_id if c.evse is not None else None
        if (c.name, c.instance, evse_id, conn_id) not in self._components:
            return GS.unknown_component.value, None
        var = self._vars.get((c.name, c.instance, evse_id, conn_id, v.name, v.instance))
        if var is None:
            return GS.unknown_variable.value, None
        return None, var

    def get(self, item: m.GetVariableDataType) -> m.GetVariableResultType:
        attr = item.attribute_type or AT.actual

        def result(status: GetVariableStatusEnumType, value: Optional[str] = None,
                   reason: Optional[str] = None) -> m.GetVariableResultType:
            return m.GetVariableResultType(
                attribute_status=status, attribute_type=item.attribute_type, attribute_value=value,
                attribute_status_info=m.StatusInfoType(reason_code=reason) if reason else None,
                component=item.component, variable=item.variable)

        missing, var = self._find(item.component, item.variable)
        if var is None:
            return result(GS(missing))
        if var.value(attr) is None:
            return result(GS.not_supported_attribute_type)
        if var.mutability is MU.write_only:
            return result(GS.rejected, reason=REASON_WRITE_ONLY)
        return result(GS.accepted, var.value(attr))

    def set(self, item: m.SetVariableDataType) -> m.SetVariableResultType:
        """Validate and store one item; listeners of a changed ``Actual`` are called (not for RebootRequired)."""
        attr = item.attribute_type or AT.actual

        def result(status: SetVariableStatusEnumType, reason: Optional[str] = None,
                   info: Optional[str] = None) -> m.SetVariableResultType:
            return m.SetVariableResultType(
                attribute_status=status, attribute_type=item.attribute_type,
                attribute_status_info=m.StatusInfoType(reason_code=reason, additional_info=info[:512] if info
                                                       else None) if reason else None,
                component=item.component, variable=item.variable)

        missing, var = self._find(item.component, item.variable)
        if var is None:
            return result(SS(missing))
        if var.value(attr) is None:
            return result(SS.not_supported_attribute_type)
        if var.mutability is MU.read_only:
            return result(SS.rejected, REASON_READ_ONLY)
        try:
            value = normalize(var, item.attribute_value)
        except InvalidValue as e:
            return result(SS.rejected, e.reason, str(e))
        if var.reboot_required:
            var.values[attr] = value
            return result(SS.reboot_required)
        changed = var.values.get(attr) != value
        var.values[attr] = value
        listener = self._listeners.get(var.key)
        if changed and attr is AT.actual and listener is not None:
            listener(value)
        return result(SS.accepted)

    # ------------------------------------------------------------------ GetBaseReport

    def report(self, base: ReportBaseEnumType) -> List[m.ReportDataType]:
        """Every ``reportData`` entry of a GetBaseReport, in definition order (see the module docstring)."""
        if base is ReportBaseEnumType.full_inventory:
            chosen = self.variables()
        elif base is ReportBaseEnumType.configuration_inventory:
            chosen = [v for v in self._vars.values() if v.mutability is not MU.read_only]
        else:
            chosen = [v for v in self._vars.values() if v.summary]
        return [report_data(v) for v in chosen]

    def view(self) -> List[Dict[str, Any]]:
        """JSON view for the Web API (``WriteOnly`` values are left out)."""
        out = []
        for v in self._vars.values():
            attrs = {a.value: (None if v.mutability is MU.write_only else v.value(a))
                     for a in ATTRIBUTE_ORDER if v.value(a) is not None}
            out.append({"component": v.component, "component_instance": v.component_instance,
                        "evse_id": v.evse_id, "connector_id": v.connector_id, "variable": v.variable,
                        "variable_instance": v.variable_instance, "data_type": v.data_type.value,
                        "mutability": v.mutability.value, "unit": v.unit, "min_limit": v.min_limit,
                        "max_limit": v.max_limit,
                        "values_list": list(v.values_list) if v.values_list is not None else None,
                        "reboot_required": v.reboot_required, "attributes": attrs})
        return out


def component_type(var: Variable) -> m.ComponentType:
    evse = m.EVSEType(id=var.evse_id, connector_id=var.connector_id) if var.evse_id is not None else None
    return m.ComponentType(name=var.component, instance=var.component_instance, evse=evse)


def report_data(var: Variable) -> m.ReportDataType:
    attrs = [m.VariableAttributeType(type=a, value=None if var.mutability is MU.write_only else var.value(a),
                                     mutability=var.mutability, persistent=var.persistent, constant=var.constant)
             for a in ATTRIBUTE_ORDER if var.value(a) is not None]
    chars = m.VariableCharacteristicsType(
        unit=var.unit, data_type=var.data_type, min_limit=var.min_limit, max_limit=var.max_limit,
        values_list=",".join(var.values_list) if var.values_list is not None else None, supports_monitoring=False)
    return m.ReportDataType(component=component_type(var),
                            variable=m.VariableType(name=var.variable, instance=var.variable_instance),
                            variable_attribute=attrs, variable_characteristics=chars)


# ====================================================================== the standard set and YAML

def _b(flag: bool) -> str:
    return "true" if flag else "false"


def security_profile(password: Optional[str], url: str) -> int:
    """1 / 2 as the shell connects (password: Basic Auth; wss://: TLS); 0 without a password is a
    simulator-only value (Part 2 §2.2.5 / A00 define profiles 1 / 2 / 3)."""
    if password is None:
        return 0
    return 2 if url.startswith("wss://") else 1


def _decimal(x: float) -> str:
    """A decimal value without exponent or trailing zeros (``2469.0`` -> ``2469``, ``1e6`` -> ``1000000``)."""
    return format(x, "f").rstrip("0").rstrip(".") or "0"


def station_availability(states: Sequence[str]) -> str:
    """ChargingStation.AvailabilityState from the EVSE states: ``Unavailable`` when every EVSE is, else
    ``Available`` (the station has no state of its own; see the module docstring)."""
    return "Unavailable" if states and all(x == "Unavailable" for x in states) else "Available"


def standard_variables(cfg: Any, *, message_timeout_s: float = DEFAULT_MESSAGE_TIMEOUT_S, url: str = "",
                       availability: Optional[Callable[[int], str]] = None,
                       power_w: Optional[Callable[[int], float]] = None,
                       profile_count: Optional[Callable[[], int]] = None) -> List[Variable]:
    """The default set (module docstring table) for the charger config ``cfg`` (``ChargerConfig``):
    ``message_timeout_s`` is the CALL timeout, ``url`` the CSMS url, ``availability(evse_id)`` the live
    connectorStatus of an EVSE, ``power_w(evse_id)`` its live power and ``profile_count()`` the number of
    installed charging profiles."""
    mode = cfg.auth_mode
    local_first = mode in ("local_only", "local_then_csms")
    meas = MEASURANDS
    meas_max = len(",".join(meas))
    started, updated, ended = DEFAULT_TX_STARTED, DEFAULT_TX_UPDATED, DEFAULT_TX_ENDED
    heartbeat_s = DEFAULT_HEARTBEAT_S
    interval = cfg.meter_sample_interval_s
    tx_updated_interval_s = DEFAULT_TX_UPDATED_INTERVAL_S if interval is None else interval
    evses = range(1, cfg.connectors + 1)

    def var(name: Tuple[str, str], data_type: DataEnumType, value: str, **kw: Any) -> Variable:
        return Variable(name[0], name[1], data_type, {AT.actual: value}, **kw)

    def ro(name: Tuple[str, str], data_type: DataEnumType, value: str, **kw: Any) -> Variable:
        """ReadOnly, derived from other settings / the station state, or fixed (not modelled)."""
        return var(name, data_type, value, mutability=MU.read_only, derived=True, **kw)

    def state(i: int) -> str:
        return availability(i) if availability is not None else "Available"

    out = [
        var(HEARTBEAT_INTERVAL, DT.integer, str(heartbeat_s), unit="s", min_limit=1),
        ro((OCPP_COMM, "MessageTimeout"), DT.integer, str(max(1, math.ceil(message_timeout_s))), unit="s",
           variable_instance="Default"),
        var(MESSAGE_ATTEMPTS, DT.integer, str(DEFAULT_MESSAGE_ATTEMPTS), min_limit=1,
            variable_instance=INSTANCE_TX_EVENT),
        var(MESSAGE_ATTEMPT_INTERVAL, DT.integer, str(DEFAULT_MESSAGE_ATTEMPT_INTERVAL_S), unit="s", min_limit=0,
            variable_instance=INSTANCE_TX_EVENT),
        ro((OCPP_COMM, "FileTransferProtocols"), DT.member_list, "", values_list=FILE_TRANSFER_PROTOCOLS),
        ro((OCPP_COMM, "NetworkConfigurationPriority"), DT.sequence_list, "1", values_list=("1",)),
        ro((OCPP_COMM, "NetworkProfileConnectionAttempts"), DT.integer, "3"),
        ro((OCPP_COMM, "OfflineThreshold"), DT.integer, "60", unit="s"),
        ro((OCPP_COMM, "UnlockOnEVSideDisconnect"), DT.boolean, "true"),
        ro((OCPP_COMM, "ResetRetries"), DT.integer, "0"),
        var(TX_UPDATED_INTERVAL, DT.integer, str(tx_updated_interval_s), unit="s", min_limit=0),
        ro((SAMPLED_DATA, "TxEndedInterval"), DT.integer, "0", unit="s"),
        var(TX_STARTED_MEASURANDS, DT.member_list, ",".join(started), values_list=meas, max_limit=meas_max),
        var(TX_UPDATED_MEASURANDS, DT.member_list, ",".join(updated), values_list=meas, max_limit=meas_max),
        var(TX_ENDED_MEASURANDS, DT.member_list, ",".join(ended), values_list=meas, max_limit=meas_max),
        var(AUTH_ENABLED, DT.boolean, _b(mode != "accept_all")),
        var(AUTHORIZE_REMOTE_START, DT.boolean, "false"),
        var(LOCAL_PRE_AUTHORIZE, DT.boolean, _b(local_first)),
        var(LOCAL_AUTHORIZE_OFFLINE, DT.boolean, _b(local_first)),
        var(OFFLINE_TX_FOR_UNKNOWN_ID, DT.boolean, _b(mode == "accept_all")),
        var(DISABLE_REMOTE_AUTHORIZATION, DT.boolean, _b(mode == "local_only")),
        ro((TX, "EVConnectionTimeOut"), DT.integer, "120", unit="s"),
        ro((TX, "StopTxOnEVSideDisconnect"), DT.boolean, "true"),
        ro((TX, "TxStartPoint"), DT.member_list, "PowerPathClosed", values_list=TX_POINTS),
        ro((TX, "TxStopPoint"), DT.member_list, "PowerPathClosed", values_list=TX_POINTS),
        var(STOP_TX_ON_INVALID_ID, DT.boolean, "true"),
        ro(SECURITY_PROFILE, DT.integer, str(security_profile(cfg.password, url))),
        ro((SECURITY, "Identity"), DT.string, cfg.id, max_limit=IDENTITY_MAX),
        ro((SECURITY, "CertificateEntries"), DT.integer, "0", max_limit=0),
        var((SECURITY, "OrganizationName"), DT.string, cfg.vendor, max_limit=50),
        var(BASIC_AUTH_PASSWORD, DT.string, cfg.password or "", mutability=MU.write_only,
            min_limit=PASSWORD_MIN, max_limit=PASSWORD_MAX),
    ]
    out += [
        var(SC_ENABLED, DT.boolean, "true"),
        ro(SC_AVAILABLE, DT.boolean, "true"),
        var(EXTERNAL_CONTROL_SIGNALS_ENABLED, DT.boolean, "true"),
        var(NOTIFY_CHARGING_LIMIT_WITH_SCHEDULES, DT.boolean, "false"),
        ro((SMART_CHARGING, "Entries"), DT.integer, "0", variable_instance=INSTANCE_CHARGING_PROFILES,
           getter=(lambda: str(profile_count())) if profile_count is not None else None),
        ro((SMART_CHARGING, "LimitChangeSignificance"), DT.decimal, "0"),
        var(PROFILE_STACK_LEVEL, DT.integer, str(DEFAULT_PROFILE_STACK_LEVEL), mutability=MU.read_only, min_limit=0),
        var(PERIODS_PER_SCHEDULE, DT.integer, str(DEFAULT_PERIODS_PER_SCHEDULE), mutability=MU.read_only,
            min_limit=1, max_limit=DEFAULT_PERIODS_PER_SCHEDULE),
        var(RATE_UNIT, DT.member_list, ",".join(RATE_UNITS), mutability=MU.read_only, values_list=RATE_UNITS),
    ]
    for instance in (INSTANCE_GET_REPORT, INSTANCE_GET_VARIABLES, INSTANCE_SET_VARIABLES):
        out.append(var(ITEMS_PER_MESSAGE, DT.integer, str(DEFAULT_ITEMS_PER_MESSAGE), mutability=MU.read_only,
                       variable_instance=instance, min_limit=1))
        out.append(var((DEVICE_DATA, "BytesPerMessage"), DT.integer, str(DEFAULT_BYTES_PER_MESSAGE),
                       mutability=MU.read_only, variable_instance=instance, min_limit=1))
    # Physical components (Appendices CSV, Required "yes"; Connector.AvailabilityState is optional there).
    out += [
        ro((CHARGING_STATION, "Available"), DT.boolean, "true"),
        ro((CHARGING_STATION, "AvailabilityState"), DT.option_list, "Available", values_list=AVAILABILITY_STATES,
           summary=True, getter=lambda: station_availability([state(i) for i in evses])),
    ]
    for evse in evses:
        live = (lambda i=evse: state(i))  # type: ignore[misc]
        watts = (lambda i=evse: _decimal(power_w(i))) if power_w is not None else None  # type: ignore[misc]
        out += [
            ro((EVSE_COMPONENT, "Available"), DT.boolean, "true", evse_id=evse),
            ro((EVSE_COMPONENT, "AvailabilityState"), DT.option_list, "Available", evse_id=evse,
               values_list=AVAILABILITY_STATES, summary=True, getter=live),
            ro((EVSE_COMPONENT, "Power"), DT.decimal, "0", evse_id=evse, unit="W", max_limit=cfg.max_power_w,
               getter=watts),
            ro((EVSE_COMPONENT, "SupplyPhases"), DT.integer, str(cfg.phases), evse_id=evse),
            ro((CONNECTOR_COMPONENT, "Available"), DT.boolean, "true", evse_id=evse, connector_id=1),
            ro((CONNECTOR_COMPONENT, "AvailabilityState"), DT.option_list, "Available", evse_id=evse,
               connector_id=1, values_list=AVAILABILITY_STATES, getter=live),
        ]
    return out


_DEFINITION_FIELDS = ("data_type", "mutability", "unit", "min_limit", "max_limit", "values_list",
                      "reboot_required", "persistent", "constant", "summary")
_ATTRIBUTE_FIELDS: Mapping[str, AttributeEnumType] = {"value": AT.actual, "target": AT.target,
                                                      "min_set": AT.min_set, "max_set": AT.max_set}


def apply_config(model: DeviceModel, entries: Iterable[Any], evse_count: int) -> None:
    """Apply the YAML ``variables:`` entries (``config.VariableConfig``): an entry addressing a standard variable
    sets its attribute values; any other entry adds a user-defined variable. ``ValueError`` with the reason."""
    seen = set()
    for e in entries:
        if e.evse is not None and not 1 <= e.evse <= evse_count:
            raise ValueError(f"variables: {e.component}.{e.variable}: no EVSE {e.evse} (1..{evse_count})")
        key: Key = (e.component, e.component_instance, e.evse, e.connector, e.variable, e.variable_instance)
        values = {attr: getattr(e, name) for name, attr in _ATTRIBUTE_FIELDS.items() if getattr(e, name) is not None}
        if key in seen:
            raise ValueError(f"variables: {e.component}.{e.variable} listed twice")
        seen.add(key)
        known = model.lookup(e.component, e.variable, component_instance=e.component_instance,
                             variable_instance=e.variable_instance, evse_id=e.evse, connector_id=e.connector)
        if known is not None:
            where = known.label()
            if known.derived:
                raise ValueError(f"variables: {where} is derived from other settings and cannot be set")
            given = [f for f in _DEFINITION_FIELDS if getattr(e, f) is not None]
            if given:
                raise ValueError(f"variables: {where} is a standard variable; only value / target / min_set / "
                                 f"max_set may be set (not {', '.join(given)})")
            extra = [a.value for a in values if a not in known.values]
            if extra:
                raise ValueError(f"variables: {where} has no {', '.join(extra)} attribute")
            for attr, value in values.items():
                try:
                    known.values[attr] = normalize(known, value)
                except InvalidValue as err:
                    raise ValueError(f"variables: {where} {attr.value}: {err}") from None
            continue
        if e.data_type is None:
            raise ValueError(f"variables: {e.component}.{e.variable} is not a standard variable: it needs "
                             "data_type (a user-defined variable)")
        var = Variable(e.component, e.variable, DT(e.data_type), values,
                       mutability=MU(e.mutability or MU.read_write.value), component_instance=e.component_instance,
                       variable_instance=e.variable_instance, evse_id=e.evse, connector_id=e.connector, unit=e.unit,
                       min_limit=e.min_limit, max_limit=e.max_limit,
                       values_list=tuple(e.values_list) if e.values_list is not None else None,
                       reboot_required=bool(e.reboot_required), persistent=bool(e.persistent),
                       constant=bool(e.constant), summary=bool(e.summary))
        try:
            check_definition(var)
        except ValueError as err:
            raise ValueError(f"variables: {err}") from None
        model.add(var)


def build(cfg: Any, *, message_timeout_s: float = DEFAULT_MESSAGE_TIMEOUT_S, url: str = "",
          availability: Optional[Callable[[int], str]] = None, power_w: Optional[Callable[[int], float]] = None,
          profile_count: Optional[Callable[[], int]] = None) -> DeviceModel:
    """The Device Model of the charger config ``cfg``: the standard set plus its ``variables``."""
    model = DeviceModel(standard_variables(cfg, message_timeout_s=message_timeout_s, url=url,
                                           availability=availability, power_w=power_w,
                                           profile_count=profile_count))
    apply_config(model, cfg.variables, cfg.connectors)
    return model


def check_charger(cfg: Any) -> None:
    """Config validation: ``ValueError`` when the ``variables`` of ``cfg`` do not apply."""
    build(cfg)
