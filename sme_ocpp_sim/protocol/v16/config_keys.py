"""OCPP 1.6 configuration keys served by the simulator (Core + Smart Charging profiles).

Sources actually checked (the official OCPP 1.6 spec §9 is not available locally):

* NAME: every key name below matches ``ocpp.v16.enums.ConfigurationKey`` of the ``ocpp`` library
  (sections "9.1 Core Profile" / "9.4 Smart Charging Profile").
* READONLY: ``verified=True`` rows have their R / RW flag confirmed by the vendor document
  WY-L22-0061A "EV急速充電器 OCPP1.6メッセージリスト", p.9 ("Profile Key一覧", columns Readonly R / RW).
  ``verified=False`` rows are not in that table; their readonly flag is from memory.

NOT verified for ANY row (memory only, neither source lists them):
type, default value, reboot_required, and the int >= 0 rule applied by the ConfigStore.
TODO(spec): verify type / default / reboot_required of every row against OCPP 1.6 §9.

1.6 lets the charge point decide whether a change needs a reboot, so no row sets ``reboot_required`` yet.
"""

from __future__ import annotations

from typing import Dict, List

from ..base import ConfigKeyDef, KeyType
from .enums import Measurand

B, I, C, S = KeyType.BOOL, KeyType.INT, KeyType.CSL, KeyType.STR

_MEASURANDS = frozenset(m.value for m in Measurand)
_PROFILES = frozenset({"Core", "FirmwareManagement", "LocalAuthListManagement", "Reservation",
                       "SmartCharging", "RemoteTrigger"})

# v0.1 implements Core (incl. Reset / Unlock / ChangeAvailability), SmartCharging and TriggerMessage.
SUPPORTED_FEATURE_PROFILES = "Core,SmartCharging,RemoteTrigger"


def _k(name: str, type_: KeyType, readonly: bool, default: str, *, verified: bool, **kw) -> ConfigKeyDef:
    return ConfigKeyDef(name=name, type=type_, readonly=readonly, default=default, verified=verified, **kw)


V, U = True, False  # verified (WY-L22-0061A p.9) / unverified

KEYS: List[ConfigKeyDef] = [
    # ---- 9.1 Core Profile
    _k("AllowOfflineTxForUnknownId", B, False, "false", verified=V),
    _k("AuthorizationCacheEnabled", B, False, "false", verified=V),
    _k("AuthorizeRemoteTxRequests", B, False, "false", verified=V),
    _k("BlinkRepeat", I, False, "0", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("ClockAlignedDataInterval", I, False, "0", verified=V),
    _k("ConnectionTimeOut", I, False, "60", verified=V),
    _k("ConnectorPhaseRotation", C, False, "0.Unknown", verified=V),
    _k("ConnectorPhaseRotationMaxLength", I, True, "10", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("GetConfigurationMaxKeys", I, True, "100", verified=V),
    _k("HeartbeatInterval", I, False, "300", verified=V),
    _k("LightIntensity", I, False, "100", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("LocalAuthorizeOffline", B, False, "true", verified=V),
    _k("LocalPreAuthorize", B, False, "false", verified=V),
    _k("MaxEnergyOnInvalidId", I, False, "0", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("MeterValuesAlignedData", C, False, "Energy.Active.Import.Register", allowed=_MEASURANDS, verified=V),
    _k("MeterValuesAlignedDataMaxLength", I, True, "10", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("MeterValuesSampledData", C, False, "Energy.Active.Import.Register,Power.Active.Import",
       allowed=_MEASURANDS, verified=V),
    _k("MeterValuesSampledDataMaxLength", I, True, "10", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("MeterValueSampleInterval", I, False, "60", verified=V),
    _k("MinimumStatusDuration", I, False, "0", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("NumberOfConnectors", I, True, "1", verified=V),  # runtime overrides from ChargerConfig.connectors
    _k("ResetRetries", I, False, "1", verified=V),
    _k("StopTransactionOnEVSideDisconnect", B, False, "true", verified=V),
    _k("StopTransactionOnInvalidId", B, False, "true", verified=V),
    _k("StopTxnAlignedData", C, False, "", allowed=_MEASURANDS, verified=V),
    _k("StopTxnAlignedDataMaxLength", I, True, "10", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("StopTxnSampledData", C, False, "", allowed=_MEASURANDS, verified=V),
    _k("StopTxnSampledDataMaxLength", I, True, "10", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("SupportedFeatureProfiles", C, True, SUPPORTED_FEATURE_PROFILES, allowed=_PROFILES, verified=V),
    _k("SupportedFeatureProfilesMaxLength", I, True, "6", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("TransactionMessageAttempts", I, False, "3", verified=V),
    _k("TransactionMessageRetryInterval", I, False, "60", verified=V),
    _k("UnlockConnectorOnEVSideDisconnect", B, False, "true", verified=V),
    _k("WebSocketPingInterval", I, False, "0", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    # ---- 9.4 Smart Charging Profile
    _k("ChargeProfileMaxStackLevel", I, True, "10", verified=V),
    _k("ChargingScheduleAllowedChargingRateUnit", C, True, "Current,Power",
       allowed=frozenset({"Current", "Power"}), verified=V),
    _k("ChargingScheduleMaxPeriods", I, True, "48", verified=V),
    _k("ConnectorSwitch3to1PhaseSupported", B, True, "false", verified=U),  # TODO(spec): verify against OCPP 1.6 §9
    _k("MaxChargingProfilesInstalled", I, True, "10", verified=V),
]

KEYS_BY_NAME: Dict[str, ConfigKeyDef] = {k.name: k for k in KEYS}
