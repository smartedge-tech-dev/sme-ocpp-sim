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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

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
    reason: Optional[str] = None
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


@dataclass(frozen=True)
class IdTokenInfo:
    status: str  # "Accepted" | "Blocked" | "Expired" | "Invalid" | "ConcurrentTx"
    expiry: Optional[datetime] = None
    parent_id_token: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status == "Accepted"


@dataclass(frozen=True)
class TxStartedResult:
    tx_id: str
    id_token_info: IdTokenInfo


# --------------------------------------------------------------------------------------------- adapter

Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


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

    @abstractmethod
    def handlers(self, runtime: Any) -> Dict[str, Handler]:
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
