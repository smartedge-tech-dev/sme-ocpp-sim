"""Version-neutral domain model shared by the runtime and the version adapters (plan §2.3).

The runtime only speaks these types; a :class:`VersionAdapter` translates them to and from one OCPP version's
messages. Domain shape follows 2.0.1 (Station -> EVSE -> Connector); 1.6 projects 1 connector per EVSE with
``connectorId == evse_id`` and ``evse_id == 0`` meaning the whole charger.

There are no product imports. The smart-charging commands carry version-specific payloads (charging profile,
purpose, unit, composite schedule); their fields are typed ``Any`` and documented with the 1.6 type.
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
    #: TxProfile without transactionId; bound to the transaction this request starts (1.6 §5.16.2).
    profile: Optional[Any] = None  # version profile type (1.6: protocol.v16.profiles.ChargingProfile)


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
    #: 1.6 Security ExtendedTriggerMessage only (M8).
    LOG_STATUS = "LogStatusNotification"
    SIGN_CERTIFICATE = "SignChargePointCertificate"


@dataclass(frozen=True)
class Trigger:
    what: TriggerWhat
    evse_id: Optional[int] = None
    #: Sent as 1.6 ExtendedTriggerMessage (M8) instead of TriggerMessage.
    extended: bool = False


@dataclass(frozen=True)
class SetProfile:
    evse_id: int  # 0 = whole charger
    profile: Any  # version profile type (1.6: protocol.v16.profiles.ChargingProfile)


@dataclass(frozen=True)
class ClearProfiles:
    profile_id: Optional[int] = None
    evse_id: Optional[int] = None
    purpose: Optional[Any] = None  # version purpose enum (1.6: profiles.ChargingProfilePurpose)
    stack_level: Optional[int] = None


@dataclass(frozen=True)
class GetComposite:
    evse_id: int
    duration: int  # seconds
    unit: Optional[Any] = None  # version unit enum (1.6: profiles.ChargingRateUnit); None = W


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


@dataclass(frozen=True)
class SendLocalList:
    version: int
    full: bool  # Full (replace) / Differential (apply changes)
    #: ``(id token, info)``; ``info`` ``None`` = remove the token (Differential only).
    entries: Tuple[Tuple[str, Optional["IdTokenInfo"]], ...] = ()


@dataclass(frozen=True)
class GetLocalListVersion:
    pass


@dataclass(frozen=True)
class ClearCache:
    pass


@dataclass(frozen=True)
class ReserveNow:
    reservation_id: int
    evse_id: int  # 0 = no specific connector
    id_token: str
    expiry: datetime
    parent_id_token: Optional[str] = None


@dataclass(frozen=True)
class CancelReservation:
    reservation_id: int


@dataclass(frozen=True)
class UpdateFirmware:
    """1.6 UpdateFirmware (M7); 2.0.1 hands its request model to the station. ``retrieve_at`` is UTC."""

    location: str
    retrieve_at: datetime
    retries: Optional[int] = None
    retry_interval_s: Optional[int] = None


@dataclass(frozen=True)
class GetDiagnostics:
    """1.6 GetDiagnostics (M7); the result is the file name (``None`` = no diagnostics available)."""

    location: str
    retries: Optional[int] = None
    retry_interval_s: Optional[int] = None
    start: Optional[datetime] = None
    stop: Optional[datetime] = None


# --------------------------------------------------------------------------------------------- security (M8)


class CertUse(str, Enum):
    """Kind of an installed CA certificate. Values are the 2.0.1 ``InstallCertificateUseEnumType`` names; 1.6
    Security calls the CSMS root ``CentralSystemRootCertificate`` and has only the first two kinds."""

    CSMS_ROOT = "CSMSRootCertificate"
    MANUFACTURER_ROOT = "ManufacturerRootCertificate"
    V2G_ROOT = "V2GRootCertificate"
    MO_ROOT = "MORootCertificate"


class CertStatus(str, Enum):
    """Result of a certificate command (Install / Delete / CertificateSigned / GetInstalledCertificateIds)."""

    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    FAILED = "Failed"
    NOT_FOUND = "NotFound"


@dataclass(frozen=True)
class CertificateHashData:
    """``CertificateHashDataType`` (1.6 Security / 2.0.1 §2.6): hex digests of the issuer name and key."""

    hash_algorithm: str  # "SHA256" | "SHA384" | "SHA512"
    issuer_name_hash: str
    issuer_key_hash: str
    serial_number: str  # hex, no "0x", no leading zeroes


class SecurityEventKind(str, Enum):
    """Security events the simulator raises. Values are the 2.0.1 names (Part 2 Appendices, security_events.csv);
    each version station maps them to its wire name (1.6 Security says ``CentralSystem`` / ``ChargePoint``)."""

    FIRMWARE_UPDATED = "FirmwareUpdated"
    FAILED_TO_AUTHENTICATE_AT_CSMS = "FailedToAuthenticateAtCsms"
    STARTUP_OF_THE_DEVICE = "StartupOfTheDevice"
    RESET_OR_REBOOT = "ResetOrReboot"
    RECONFIGURATION_OF_SECURITY_PARAMETERS = "ReconfigurationOfSecurityParameters"
    INVALID_CSMS_CERTIFICATE = "InvalidCsmsCertificate"
    INVALID_CHARGING_STATION_CERTIFICATE = "InvalidChargingStationCertificate"
    DISCARDED_RENEWED_CLIENT_CERTIFICATE = "DiscardedRenewedClientCertificate"
    INVALID_TLS_VERSION = "InvalidTLSVersion"


@dataclass(frozen=True)
class SecurityEvent:
    """SecurityEventNotification payload (the wire ``type`` is the version's name of ``kind``)."""

    kind: SecurityEventKind
    timestamp: datetime
    tech_info: Optional[str] = None
    critical: bool = False


@dataclass(frozen=True)
class CertificateSigned:
    """1.6 CertificateSigned (M8); 2.0.1 hands its request model to the station."""

    chain: str


@dataclass(frozen=True)
class InstallCertificate:
    use: CertUse
    certificate: str


@dataclass(frozen=True)
class DeleteCertificate:
    hash_data: CertificateHashData


@dataclass(frozen=True)
class GetInstalledCertificateIds:
    """Result: ``(CertStatus, tuple of (CertUse, CertificateHashData))``."""

    uses: Tuple[CertUse, ...] = ()  # empty = every kind


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


class LocalListStatus(str, Enum):
    """Result of SendLocalList (1.6 ``UpdateStatus``)."""

    ACCEPTED = "Accepted"
    FAILED = "Failed"
    NOT_SUPPORTED = "NotSupported"
    VERSION_MISMATCH = "VersionMismatch"


class ReservationStatus(str, Enum):
    """Result of ReserveNow (1.6 ``ReservationStatus``)."""

    ACCEPTED = "Accepted"
    FAULTED = "Faulted"
    OCCUPIED = "Occupied"
    REJECTED = "Rejected"
    UNAVAILABLE = "Unavailable"


@dataclass(frozen=True)
class GetCompositeResult:
    status: CommandStatus
    evse_id: Optional[int] = None
    schedule_start: Optional[datetime] = None
    #: Every period has a limit (the runtime fills unconstrained intervals with the hardware maximum).
    schedule: Optional[Any] = None  # version schedule (1.6: protocol.v16.composite.CompositeSchedule)


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


class FirmwareState(str, Enum):
    """Steps of a firmware update (M7). The values are the wire names of 2.0.1 ``FirmwareStatusEnumType``; 1.6
    ``FirmwareStatus`` has the subset Idle / Downloading / Downloaded / DownloadFailed / Installing / Installed /
    InstallationFailed under the same names."""

    IDLE = "Idle"
    DOWNLOAD_SCHEDULED = "DownloadScheduled"
    DOWNLOADING = "Downloading"
    DOWNLOADED = "Downloaded"
    DOWNLOAD_FAILED = "DownloadFailed"
    SIGNATURE_VERIFIED = "SignatureVerified"
    INSTALL_SCHEDULED = "InstallScheduled"
    INSTALLING = "Installing"
    INSTALL_REBOOTING = "InstallRebooting"
    INSTALLED = "Installed"
    INSTALLATION_FAILED = "InstallationFailed"


class LogState(str, Enum):
    """Steps of a diagnostics / log upload (M7): 1.6 ``DiagnosticsStatus`` names; 2.0.1 ``UploadLogStatusEnumType``
    calls the failure ``UploadFailure`` and adds ``AcceptedCanceled`` (sent for an upload a new GetLog cancelled)."""

    IDLE = "Idle"
    UPLOADING = "Uploading"
    UPLOADED = "Uploaded"
    UPLOAD_FAILED = "UploadFailed"
    ACCEPTED_CANCELED = "AcceptedCanceled"


@dataclass(frozen=True)
class FirmwareStatus:
    """FirmwareStatusNotification; ``request_id`` is the 2.0.1 requestId (``None`` for 1.6 and for Idle)."""

    status: FirmwareState
    request_id: Optional[int] = None


@dataclass(frozen=True)
class LogStatus:
    """DiagnosticsStatusNotification (1.6) / LogStatusNotification (2.0.1)."""

    status: LogState
    request_id: Optional[int] = None


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
    RESERVED = "Reserved"        # connector reserved for another id token (ReserveNow)


@dataclass(frozen=True)
class IdTokenInfo:
    status: AuthResult  # one of the first five (wire) values
    expiry: Optional[datetime] = None
    parent_id_token: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status == AuthResult.ACCEPTED

    def expired(self, now: datetime) -> bool:
        """``expiry`` has passed (1.6 ``expiryDate`` / 2.0.1 ``cacheExpiryDateTime``)."""
        return self.expiry is not None and now >= self.expiry

    def valid(self, now: datetime) -> bool:
        """Accepted and not expired."""
        return self.accepted and not self.expired(now)


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
