"""Version-neutral domain model shared by the runtime and the version adapters (plan §2.3).

The runtime only speaks these types; a :class:`VersionAdapter` translates them to and from one OCPP version's
messages. Domain shape follows 2.0.1 (Station -> EVSE -> Connector); 1.6 projects 1 connector per EVSE with
``connectorId == evse_id`` and ``evse_id == 0`` meaning the whole charger.

Nothing product-specific is imported here. Charging profile payloads are ``Any`` until M4 brings
``charging.profiles``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

# --------------------------------------------------------------------------------------------- status


class DomainStatus(str, Enum):
    """Connector status; the fine-grained 1.6 set. 2.0.1 adapters project it down (Occupied + chargingState)."""

    AVAILABLE = "Available"
    PREPARING = "Preparing"
    CHARGING = "Charging"
    SUSPENDED_EV = "SuspendedEV"
    SUSPENDED_EVSE = "SuspendedEVSE"
    FINISHING = "Finishing"
    RESERVED = "Reserved"
    UNAVAILABLE = "Unavailable"
    FAULTED = "Faulted"


# --------------------------------------------------------------------------------------------- config keys


class KeyType(str, Enum):
    BOOL = "bool"
    INT = "int"
    CSL = "csl"  # comma separated list
    STR = "str"


@dataclass(frozen=True)
class ConfigKeyDef:
    """One configuration key as the runtime's ConfigStore sees it (1.6 key; 2.0.1 maps Component/Variable)."""

    name: str
    type: KeyType
    readonly: bool
    default: str
    reboot_required: bool = False
    #: Allowed CSL items / STR values (``None`` = anything).
    allowed: Optional[frozenset] = None
    #: True when name and readonly flag were confirmed by a local source document (see the table module).
    verified: bool = False


class ConfigStatus(str, Enum):
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    REBOOT_REQUIRED = "RebootRequired"
    NOT_SUPPORTED = "NotSupported"


# --------------------------------------------------------------------------------------------- commands
# CSMS -> CP. The adapter parses a version message into one of these and hands it to the runtime.


@dataclass(frozen=True)
class RequestStart:
    evse_id: Optional[int]
    id_token: str
    profile: Any = None


@dataclass(frozen=True)
class RequestStop:
    tx_id: str


class ResetKind(str, Enum):
    SOFT = "Soft"
    HARD = "Hard"


@dataclass(frozen=True)
class Reset:
    kind: ResetKind


@dataclass(frozen=True)
class ChangeAvailability:
    evse_id: int  # 0 = whole charger
    operative: bool


@dataclass(frozen=True)
class Unlock:
    evse_id: int


class TriggerWhat(str, Enum):
    BOOT = "BootNotification"
    HEARTBEAT = "Heartbeat"
    METER_VALUES = "MeterValues"
    STATUS = "StatusNotification"
    DIAGNOSTICS_STATUS = "DiagnosticsStatusNotification"
    FIRMWARE_STATUS = "FirmwareStatusNotification"


@dataclass(frozen=True)
class Trigger:
    what: TriggerWhat
    evse_id: Optional[int] = None


@dataclass(frozen=True)
class SetProfile:
    evse_id: int
    profile: Any


@dataclass(frozen=True)
class ClearProfiles:
    profile_id: Optional[int] = None
    evse_id: Optional[int] = None
    purpose: Optional[str] = None
    stack_level: Optional[int] = None


@dataclass(frozen=True)
class GetComposite:
    evse_id: int
    duration: int
    unit: Optional[str] = None  # "W" | "A"


@dataclass(frozen=True)
class GetConfig:
    keys: Tuple[str, ...] = ()  # empty = all


@dataclass(frozen=True)
class SetConfig:
    key: str
    value: str


@dataclass(frozen=True)
class DataTransfer:
    vendor_id: str
    message_id: Optional[str] = None
    data: Optional[str] = None


Command = Any  # union of the command dataclasses above

# --------------------------------------------------------------------------------------------- results


class CommandStatus(str, Enum):
    """Result of RequestStart / RequestStop / Reset / Trigger / SetProfile / ClearProfiles / GetComposite."""

    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    NOT_SUPPORTED = "NotSupported"
    NOT_IMPLEMENTED = "NotImplemented"
    UNKNOWN = "Unknown"


class AvailabilityStatus(str, Enum):
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    SCHEDULED = "Scheduled"


class UnlockStatus(str, Enum):
    UNLOCKED = "Unlocked"
    UNLOCK_FAILED = "UnlockFailed"
    NOT_SUPPORTED = "NotSupported"


@dataclass(frozen=True)
class GetCompositeResult:
    status: CommandStatus
    evse_id: Optional[int] = None
    schedule_start: Optional[datetime] = None
    schedule: Any = None


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    readonly: bool
    value: Optional[str]


@dataclass(frozen=True)
class GetConfigResult:
    known: List[ConfigEntry]
    unknown: List[str]


class DataTransferStatus(str, Enum):
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    UNKNOWN_MESSAGE_ID = "UnknownMessageId"
    UNKNOWN_VENDOR_ID = "UnknownVendorId"


@dataclass(frozen=True)
class DataTransferResult:
    status: DataTransferStatus
    data: Optional[str] = None


# --------------------------------------------------------------------------------------------- events
# CP -> CSMS. The runtime emits these; the adapter turns them into version messages.


@dataclass(frozen=True)
class Sample:
    measurand: str
    value: float
    unit: Optional[str] = None
    phase: Optional[str] = None
    location: Optional[str] = None


@dataclass(frozen=True)
class Booting:
    vendor: str
    model: str
    serial: Optional[str] = None
    firmware: Optional[str] = None


@dataclass(frozen=True)
class Heartbeat:
    pass


@dataclass(frozen=True)
class ConnectorStatus:
    evse_id: int
    connector_id: int
    status: DomainStatus
    error_code: Optional[str] = None  # None = no error
    info: Optional[str] = None
    vendor_error_code: Optional[str] = None
    timestamp: Optional[datetime] = None


@dataclass(frozen=True)
class TxStarted:
    evse_id: int
    id_token: str
    meter_start_wh: int
    timestamp: datetime
    reservation_id: Optional[int] = None


@dataclass(frozen=True)
class TxUpdated:
    evse_id: int
    tx_id: str
    timestamp: datetime
    samples: Sequence[Sample] = ()
    context: str = "Sample.Periodic"


@dataclass(frozen=True)
class TxEnded:
    evse_id: int
    tx_id: str
    meter_stop_wh: int
    timestamp: datetime
    reason: Optional["StopReason"] = None
    id_token: Optional[str] = None
    samples: Sequence[Sample] = ()


@dataclass(frozen=True)
class MeterSample:
    evse_id: int
    timestamp: datetime
    samples: Sequence[Sample]
    context: str = "Sample.Periodic"
    tx_id: Optional[str] = None


@dataclass(frozen=True)
class AuthorizeRequest:
    id_token: str


# --------------------------------------------------------------------------------------------- event results


class BootStatus(str, Enum):
    ACCEPTED = "Accepted"
    PENDING = "Pending"
    REJECTED = "Rejected"


@dataclass(frozen=True)
class BootResult:
    status: BootStatus
    interval: int
    current_time: Optional[datetime] = None


class StopReason(str, Enum):
    """Why a transaction ended. Values are domain names; each adapter maps them to its wire reason."""

    LOCAL = "local"                      # stopped at the charger (same id token / local stop)
    REMOTE = "remote"                    # RequestStop from the CSMS
    EV_DISCONNECTED = "ev_disconnected"  # cable unplugged on the EV side
    DE_AUTHORIZED = "de_authorized"      # the CSMS did not accept the id token of a started transaction
    UNLOCK_COMMAND = "unlock_command"    # Unlock from the CSMS
    SOFT_RESET = "soft_reset"
    HARD_RESET = "hard_reset"
    OTHER = "other"                      # e.g. a connector fault


class AuthResult(str, Enum):
    """Outcome of presenting an id token.

    The first five values equal the authorization status of the CSMS answer (same spelling in 1.6 and 2.0.1
    for these). The rest are simulator-internal outcomes of the runtime API and are never sent on the wire.
    """

    ACCEPTED = "Accepted"
    BLOCKED = "Blocked"
    EXPIRED = "Expired"
    INVALID = "Invalid"
    CONCURRENT_TX = "ConcurrentTx"
    # ---- internal (runtime API only)
    OFFLINE = "Offline"          # CSMS authorization needed but not online
    ERROR = "Error"              # the CSMS call failed
    BUSY = "Busy"                # connector has a transaction of another id token
    UNAVAILABLE = "Unavailable"  # connector inoperative or faulted
    STOPPED = "Stopped"          # the token ended its own transaction
    PENDING = "Pending"          # connection lost before the StartTransaction answer; it is resent later


@dataclass(frozen=True)
class IdTokenInfo:
    status: AuthResult  # one of the first five (wire) values
    expiry: Optional[datetime] = None
    parent_id_token: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status == AuthResult.ACCEPTED


@dataclass(frozen=True)
class TxStartedResult:
    tx_id: str
    id_token_info: IdTokenInfo


# --------------------------------------------------------------------------------------------- adapter

Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
#: Outbound CALL of the current connection: ``(action, payload) -> response payload``.
CallFn = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class CommandSink(Protocol):
    """What an adapter needs from the runtime to answer CSMS CALLs."""

    @property
    def connector_count(self) -> int: ...

    async def handle(self, cmd: Command) -> Any:
        """Execute one domain command and return its domain result (see the command / result types)."""


class VersionAdapter(ABC):
    """Translates between the domain model and one OCPP version (1.6 in v0.1, 2.0.1 / 2.1 later)."""

    #: "1.6" | "2.0.1" | "2.1"
    version: str = ""
    #: WebSocket subprotocol, e.g. "ocpp1.6"
    subprotocol: str = ""
    #: ``rpc.errors.ErrorCodes`` instance for this version (typed ``Any`` to keep this module dependency free)
    error_codes: Any = None
    #: Configuration key table the runtime builds its ConfigStore from.
    config_keys: Sequence[ConfigKeyDef] = ()

    def __init__(self, *, strict: bool = False) -> None:
        #: Strict inbound parsing (unknown fields rejected) instead of the lenient default.
        self.strict = strict
        self._call: Optional[CallFn] = None

    def bind(self, call: Optional[CallFn]) -> None:
        """Attach the outbound CALL function of the current connection (``None`` while offline)."""
        self._call = call

    @abstractmethod
    def handlers(self, runtime: CommandSink) -> Dict[str, Handler]:
        """Inbound CALL handlers by action; each parses the payload, calls the runtime, returns the response."""

    @abstractmethod
    async def send_booting(self, ev: Booting) -> BootResult: ...

    @abstractmethod
    async def send_heartbeat(self, ev: Heartbeat) -> Optional[datetime]: ...

    @abstractmethod
    async def send_connector_status(self, ev: ConnectorStatus) -> None: ...

    @abstractmethod
    async def send_authorize(self, ev: AuthorizeRequest) -> IdTokenInfo: ...

    @abstractmethod
    async def send_tx_started(self, ev: TxStarted) -> TxStartedResult: ...

    @abstractmethod
    async def send_tx_updated(self, ev: TxUpdated) -> None: ...

    @abstractmethod
    async def send_tx_ended(self, ev: TxEnded) -> Optional[IdTokenInfo]: ...

    @abstractmethod
    async def send_meter_sample(self, ev: MeterSample) -> None: ...

    @abstractmethod
    async def send_data_transfer(self, cmd: DataTransfer) -> DataTransferResult: ...
