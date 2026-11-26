"""OCPP 1.6 configuration keys served by the simulator (Core + Smart Charging profiles).

Source: OCPP 1.6 edition 2 core spec §9 "Standard Configuration Key Names & Values"
(p.97-108) plus the OCPP 1.6 errata sheet (2025-04). Every row was checked against it for
name, type, accessibility (R / RW) and unit; the per-row comment gives section, page, unit.

Facts from §9 that shape this table:

* §9 defines no default values except MeterValuesSampledData ("Default:
  Energy.Active.Import.Register", §9.1.17 p.101). All other defaults are simulator choices.
  MeterValuesSampledData deliberately also samples Power.Active.Import (the runtime's
  periodic MeterValues rely on it).
* §9 has no "reboot required" note for any key; whether ChangeConfiguration answers
  RebootRequired is up to the Charge Point. So no row sets ``reboot_required``.
* "R or RW, choice is up to Charge Point implementation": AuthorizeRemoteTxRequests
  (§9.1.3), StopTransactionOnEVSideDisconnect (errata 3.80, p.31-32, also made optional),
  UnlockConnectorOnEVSideDisconnect (errata 3.84, p.33). The simulator implements them RW.
* Types are as in §9; errata 4.28 (p.46-47) only renames ConnectorSwitch3to1PhaseSupported's
  type "bool" -> "boolean". Only WebSocketPingInterval states "Negative values are not
  allowed" (§9.1.34 p.105); the ConfigStore's int >= 0 rule for the other integer keys is a
  simulator choice.
* Errata-only keys not served here: MessageTimeout (3.83, p.32-33),
  StopTransactionMaxMeterValues (3.91, p.35), SupportedFileTransferProtocols (3.90, p.35,
  Firmware Management). SupportedFeatureProfilesMaxLength "does not have to be implemented"
  (errata 3.81, p.32).

The readonly flags also match the vendor document WY-L22-0061A p.9 where it lists the key.
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


V, U = True, False  # verified against OCPP 1.6 §9 (page per row) / unverified

KEYS: List[ConfigKeyDef] = [
    # ---- 9.1 Core Profile
    _k("AllowOfflineTxForUnknownId", B, False, "false", verified=V),  # §9.1.1 p.97
    _k("AuthorizationCacheEnabled", B, False, "false", verified=V),  # §9.1.2 p.97
    _k("AuthorizeRemoteTxRequests", B, False, "false", verified=V),  # §9.1.3 p.97, R or RW
    _k("BlinkRepeat", I, False, "0", verified=V),  # §9.1.4 p.98, times
    _k("ClockAlignedDataInterval", I, False, "0", verified=V),  # §9.1.5 p.98, s
    _k("ConnectionTimeOut", I, False, "60", verified=V),  # §9.1.6 p.98, s
    _k("ConnectorPhaseRotation", C, False, "0.Unknown", verified=V),  # §9.1.7 p.99
    _k("ConnectorPhaseRotationMaxLength", I, True, "10", verified=V),  # §9.1.8 p.99
    _k("GetConfigurationMaxKeys", I, True, "100", verified=V),  # §9.1.9 p.99
    _k("HeartbeatInterval", I, False, "300", verified=V),  # §9.1.10 p.99, s
    _k("LightIntensity", I, False, "100", verified=V),  # §9.1.11 p.100, %
    _k("LocalAuthorizeOffline", B, False, "true", verified=V),  # §9.1.12 p.100
    _k("LocalPreAuthorize", B, False, "false", verified=V),  # §9.1.13 p.100
    _k("MaxEnergyOnInvalidId", I, False, "0", verified=V),  # §9.1.14 p.100, Wh
    _k("MeterValuesAlignedData", C, False, "Energy.Active.Import.Register", allowed=_MEASURANDS, verified=V),  # §9.1.15 p.101
    _k("MeterValuesAlignedDataMaxLength", I, True, "10", verified=V),  # §9.1.16 p.101
    _k("MeterValuesSampledData", C, False, "Energy.Active.Import.Register,Power.Active.Import",
       allowed=_MEASURANDS, verified=V),  # §9.1.17 p.101, spec default Energy.Active.Import.Register
    _k("MeterValuesSampledDataMaxLength", I, True, "10", verified=V),  # §9.1.18 p.101
    _k("MeterValueSampleInterval", I, False, "60", verified=V),  # §9.1.19 p.102, s
    _k("MinimumStatusDuration", I, False, "0", verified=V),  # §9.1.20 p.102, s
    _k("NumberOfConnectors", I, True, "1", verified=V),  # §9.1.21 p.102; runtime overrides from ChargerConfig.connectors
    _k("ResetRetries", I, False, "1", verified=V),  # §9.1.22 p.102, times
    _k("StopTransactionOnEVSideDisconnect", B, False, "true", verified=V),  # §9.1.23 p.103, errata 3.80: optional, R or RW
    _k("StopTransactionOnInvalidId", B, False, "true", verified=V),  # §9.1.24 p.103
    _k("StopTxnAlignedData", C, False, "", allowed=_MEASURANDS, verified=V),  # §9.1.25 p.103
    _k("StopTxnAlignedDataMaxLength", I, True, "10", verified=V),  # §9.1.26 p.103
    _k("StopTxnSampledData", C, False, "", allowed=_MEASURANDS, verified=V),  # §9.1.27 p.104
    _k("StopTxnSampledDataMaxLength", I, True, "10", verified=V),  # §9.1.28 p.104
    _k("SupportedFeatureProfiles", C, True, SUPPORTED_FEATURE_PROFILES, allowed=_PROFILES, verified=V),  # §9.1.29 p.104
    _k("SupportedFeatureProfilesMaxLength", I, True, "6", verified=V),  # §9.1.30 p.104, errata 3.81
    _k("TransactionMessageAttempts", I, False, "3", verified=V),  # §9.1.31 p.105, times
    _k("TransactionMessageRetryInterval", I, False, "60", verified=V),  # §9.1.32 p.105, s
    _k("UnlockConnectorOnEVSideDisconnect", B, False, "true", verified=V),  # §9.1.33 p.105, errata 3.84: R or RW
    _k("WebSocketPingInterval", I, False, "0", verified=V),  # §9.1.34 p.105, s, >= 0
    # ---- 9.4 Smart Charging Profile
    _k("ChargeProfileMaxStackLevel", I, True, "10", verified=V),  # §9.4.1 p.107
    _k("ChargingScheduleAllowedChargingRateUnit", C, True, "Current,Power",
       allowed=frozenset({"Current", "Power"}), verified=V),  # §9.4.2 p.107, errata 3.88
    _k("ChargingScheduleMaxPeriods", I, True, "48", verified=V),  # §9.4.3 p.107
    _k("ConnectorSwitch3to1PhaseSupported", B, True, "false", verified=V),  # §9.4.4 p.107, errata 4.28
    _k("MaxChargingProfilesInstalled", I, True, "10", verified=V),  # §9.4.5 p.108
]

KEYS_BY_NAME: Dict[str, ConfigKeyDef] = {k.name: k for k in KEYS}
