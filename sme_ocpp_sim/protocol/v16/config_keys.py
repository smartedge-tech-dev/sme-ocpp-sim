"""OCPP 1.6 configuration keys served by the simulator (Core + Smart Charging profiles).

Source: OCPP 1.6 edition 2 core spec §9 "Standard Configuration Key Names & Values"
(p.97-108) plus the OCPP 1.6 errata sheet (2025-04). Every row was checked against it for
name, type, accessibility (R / RW) and unit; the per-row comment gives section, page, unit.

Facts from §9 that shape this table:

* §9 defines no default values except MeterValuesSampledData ("Default:
  Energy.Active.Import.Register", §9.1.17 p.101). All other defaults are simulator choices.
  MeterValuesSampledData deliberately also samples Power.Active.Import (the runtime's
  periodic MeterValues rely on it).
* LocalAuthListMaxLength / SendLocalListMaxLength (R) bound the Local Authorization List and one
  SendLocalList; ReserveConnectorZeroSupported (R, optional) decides whether ReserveNow on connector 0 is
  served. Their defaults are simulator choices; the charger YAML ``config_keys`` may override R keys too.
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

from typing import Dict, Tuple

from ..base import ConfigKeyDef, KeyType
from .enums import Measurand

_BOOL, _INT, _CSL = KeyType.BOOL, KeyType.INT, KeyType.CSL

_MEASURANDS = frozenset(m.value for m in Measurand)
_PROFILES = frozenset({"Core", "FirmwareManagement", "LocalAuthListManagement", "Reservation",
                       "SmartCharging", "RemoteTrigger"})

# Core (incl. Reset / Unlock / ChangeAvailability), SmartCharging and TriggerMessage since v0.1; Local Auth List
# Management (SendLocalList / GetLocalListVersion) and Reservation (ReserveNow / CancelReservation) since v0.2 M6.
SUPPORTED_FEATURE_PROFILES = "Core,LocalAuthListManagement,Reservation,SmartCharging,RemoteTrigger"


def _k(name: str, type_: KeyType, readonly: bool, default: str, **kw) -> ConfigKeyDef:
    """One key row; every row is checked against OCPP 1.6 §9 (page per row), hence ``verified=True``."""
    return ConfigKeyDef(name=name, type=type_, readonly=readonly, default=default, verified=True, **kw)


KEYS: Tuple[ConfigKeyDef, ...] = (
    # ---- 9.1 Core Profile
    _k("AllowOfflineTxForUnknownId", _BOOL, False, "false"),  # §9.1.1 p.97
    _k("AuthorizationCacheEnabled", _BOOL, False, "false"),  # §9.1.2 p.97
    _k("AuthorizeRemoteTxRequests", _BOOL, False, "false"),  # §9.1.3 p.97, R or RW
    _k("BlinkRepeat", _INT, False, "0"),  # §9.1.4 p.98, times
    _k("ClockAlignedDataInterval", _INT, False, "0"),  # §9.1.5 p.98, s
    _k("ConnectionTimeOut", _INT, False, "60"),  # §9.1.6 p.98, s
    _k("ConnectorPhaseRotation", _CSL, False, "0.Unknown"),  # §9.1.7 p.99
    _k("ConnectorPhaseRotationMaxLength", _INT, True, "10"),  # §9.1.8 p.99
    _k("GetConfigurationMaxKeys", _INT, True, "100"),  # §9.1.9 p.99
    _k("HeartbeatInterval", _INT, False, "300"),  # §9.1.10 p.99, s
    _k("LightIntensity", _INT, False, "100"),  # §9.1.11 p.100, %
    _k("LocalAuthorizeOffline", _BOOL, False, "true"),  # §9.1.12 p.100
    _k("LocalPreAuthorize", _BOOL, False, "false"),  # §9.1.13 p.100
    _k("MaxEnergyOnInvalidId", _INT, False, "0"),  # §9.1.14 p.100, Wh
    _k("MeterValuesAlignedData", _CSL, False, "Energy.Active.Import.Register", allowed=_MEASURANDS),  # §9.1.15 p.101
    _k("MeterValuesAlignedDataMaxLength", _INT, True, "10"),  # §9.1.16 p.101
    _k("MeterValuesSampledData", _CSL, False, "Energy.Active.Import.Register,Power.Active.Import",
       allowed=_MEASURANDS),  # §9.1.17 p.101, spec default Energy.Active.Import.Register
    _k("MeterValuesSampledDataMaxLength", _INT, True, "10"),  # §9.1.18 p.101
    _k("MeterValueSampleInterval", _INT, False, "60"),  # §9.1.19 p.102, s
    _k("MinimumStatusDuration", _INT, False, "0"),  # §9.1.20 p.102, s
    _k("NumberOfConnectors", _INT, True, "1"),  # §9.1.21 p.102; runtime overrides from ChargerConfig.connectors
    _k("ResetRetries", _INT, False, "1"),  # §9.1.22 p.102, times
    _k("StopTransactionOnEVSideDisconnect", _BOOL, False, "true"),  # §9.1.23 p.103, errata 3.80: optional, R or RW
    _k("StopTransactionOnInvalidId", _BOOL, False, "true"),  # §9.1.24 p.103
    _k("StopTxnAlignedData", _CSL, False, "", allowed=_MEASURANDS),  # §9.1.25 p.103
    _k("StopTxnAlignedDataMaxLength", _INT, True, "10"),  # §9.1.26 p.103
    _k("StopTxnSampledData", _CSL, False, "", allowed=_MEASURANDS),  # §9.1.27 p.104
    _k("StopTxnSampledDataMaxLength", _INT, True, "10"),  # §9.1.28 p.104
    _k("SupportedFeatureProfiles", _CSL, True, SUPPORTED_FEATURE_PROFILES, allowed=_PROFILES),  # §9.1.29 p.104
    _k("SupportedFeatureProfilesMaxLength", _INT, True, "6"),  # §9.1.30 p.104, errata 3.81
    _k("TransactionMessageAttempts", _INT, False, "3"),  # §9.1.31 p.105, times
    _k("TransactionMessageRetryInterval", _INT, False, "60"),  # §9.1.32 p.105, s
    _k("UnlockConnectorOnEVSideDisconnect", _BOOL, False, "true"),  # §9.1.33 p.105, errata 3.84: R or RW
    _k("WebSocketPingInterval", _INT, False, "0"),  # §9.1.34 p.105, s, >= 0
    # ---- 9.2 Local Auth List Management Profile (all required by the profile)
    _k("LocalAuthListEnabled", _BOOL, False, "true"),  # §9.2.1 p.106
    _k("LocalAuthListMaxLength", _INT, True, "1000"),  # §9.2.2 p.106
    _k("SendLocalListMaxLength", _INT, True, "100"),  # §9.2.3 p.106
    # ---- 9.3 Reservation Profile
    _k("ReserveConnectorZeroSupported", _BOOL, True, "true"),  # §9.3.1 p.106, optional
    # ---- 9.4 Smart Charging Profile
    _k("ChargeProfileMaxStackLevel", _INT, True, "10"),  # §9.4.1 p.107
    _k("ChargingScheduleAllowedChargingRateUnit", _CSL, True, "Current,Power",
       allowed=frozenset({"Current", "Power"})),  # §9.4.2 p.107, errata 3.88
    _k("ChargingScheduleMaxPeriods", _INT, True, "48"),  # §9.4.3 p.107
    _k("ConnectorSwitch3to1PhaseSupported", _BOOL, True, "false"),  # §9.4.4 p.107, errata 4.28
    _k("MaxChargingProfilesInstalled", _INT, True, "10"),  # §9.4.5 p.108
    # ---- Security Whitepaper ed.4 §7 (M8; all optional there). AdditionalRootCertificateCheck is required
    # unless only profile 1 is implemented and AuthorizationKey unless only profile 3 is (§7.1 / §7.2): both are
    # served. AdditionalRootCertificateCheck is not supported (always false, §7.1 / M05.FR.08 do not apply).
    # AuthorizationKey (W, A01) is the Basic Auth password: GetConfiguration lists it without a value, a change
    # reconnects with it (v16.station); its default is the charger's ``password``. CpoName sets the O RDN of a
    # CSR (A02.FR.29). CertificateStoreMaxLength bounds InstallCertificate (M05.FR.03, runtime.security.CERT_MAX).
    # Not served (optional): CertificateSignedMaxChainSize, AllowCentralSystemTLSWildcards,
    # AllowSecurityProfileDowngrade, CertSigningWaitMinimum, CertSigningRepeatTimes.
    _k("AdditionalRootCertificateCheck", _BOOL, True, "false"),  # WP ed.4 §7.1
    _k("AuthorizationKey", KeyType.STR, False, "", write_only=True),  # WP ed.4 §7.2, write-only
    _k("CertificateStoreMaxLength", _INT, True, "20"),  # WP ed.4 §7.4
    _k("CpoName", KeyType.STR, False, ""),  # WP ed.4 §7.5
    # WP ed.4 §7.6 has SecurityProfile RW (A05: ChangeConfiguration upgrades the profile and reconnects). The
    # simulator cannot switch profiles at run time (url scheme, certificates and password come from the YAML), so
    # it serves the key R, derived from the connection: a deliberate deviation, hence ``verified=False``.
    ConfigKeyDef("SecurityProfile", _INT, True, "0"),
)

KEYS_BY_NAME: Dict[str, ConfigKeyDef] = {k.name: k for k in KEYS}
