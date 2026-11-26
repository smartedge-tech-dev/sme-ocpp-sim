"""OCPP 1.6J payload fixtures: one minimal and one all-optional-fields payload per message.

Plain dicts in wire form (camelCase, datetimes as UTC with milliseconds and Z), so that
``dump(parse(...))`` must give back exactly the same dict. Shared by the model tests and
the OCA schema conformance test.
"""

T1 = "2026-11-26T09:35:32.123Z"
T2 = "2026-11-27T00:00:00.000Z"

ID_TAG_INFO_FULL = {"status": "Accepted", "expiryDate": T2, "parentIdTag": "PARENT01"}

SAMPLED_VALUE_FULL = {
    "value": "1234.5",
    "context": "Sample.Periodic",
    "format": "Raw",
    "measurand": "Energy.Active.Import.Register",
    "phase": "L1-N",
    "location": "Outlet",
    "unit": "Wh",
}

SCHEDULE_FULL = {
    "duration": 3600,
    "startSchedule": T1,
    "chargingRateUnit": "W",
    "chargingSchedulePeriod": [
        {"startPeriod": 0, "limit": 11000.0, "numberPhases": 3},
        {"startPeriod": 1800, "limit": 7400},  # integer JSON value for a number field
    ],
    "minChargingRate": 1000.0,
}

SCHEDULE_MIN = {
    "chargingRateUnit": "A",
    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16.0}],
}

PROFILE_FULL = {
    "chargingProfileId": 7,
    "transactionId": 42,
    "stackLevel": 1,
    "chargingProfilePurpose": "TxProfile",
    "chargingProfileKind": "Recurring",
    "recurrencyKind": "Daily",
    "validFrom": T1,
    "validTo": T2,
    "chargingSchedule": SCHEDULE_FULL,
}

PROFILE_MIN = {
    "chargingProfileId": 1,
    "stackLevel": 0,
    "chargingProfilePurpose": "TxDefaultProfile",
    "chargingProfileKind": "Absolute",
    "chargingSchedule": SCHEDULE_MIN,
}

# (action, kind) -> {"minimal": payload, "full": payload}
FIXTURES: dict[tuple[str, str], dict[str, dict]] = {
    ("BootNotification", "request"): {
        "minimal": {"chargePointVendor": "SME", "chargePointModel": "OCPPSim"},
        "full": {
            "chargePointVendor": "SME",
            "chargePointModel": "OCPPSim",
            "chargePointSerialNumber": "CP-0001",
            "chargeBoxSerialNumber": "CB-0001",
            "firmwareVersion": "0.1.0",
            "iccid": "8981100000000000000",
            "imsi": "440100000000000",
            "meterType": "SimMeter",
            "meterSerialNumber": "M-0001",
        },
    },
    ("BootNotification", "response"): {
        "minimal": {"status": "Accepted", "currentTime": T1, "interval": 300},
        "full": {"status": "Pending", "currentTime": T1, "interval": 0},
    },
    ("Heartbeat", "request"): {"minimal": {}, "full": {}},
    ("Heartbeat", "response"): {
        "minimal": {"currentTime": T1},
        "full": {"currentTime": T2},
    },
    ("StatusNotification", "request"): {
        "minimal": {"connectorId": 0, "errorCode": "NoError", "status": "Available"},
        "full": {
            "connectorId": 1,
            "errorCode": "OtherError",
            "info": "info text",
            "status": "Faulted",
            "timestamp": T1,
            "vendorId": "com.example",
            "vendorErrorCode": "E42",
        },
    },
    ("StatusNotification", "response"): {"minimal": {}, "full": {}},
    ("Authorize", "request"): {
        "minimal": {"idTag": "00123"},
        "full": {"idTag": "ABCDEFGHIJ0123456789"},
    },
    ("Authorize", "response"): {
        "minimal": {"idTagInfo": {"status": "Invalid"}},
        "full": {"idTagInfo": ID_TAG_INFO_FULL},
    },
    ("StartTransaction", "request"): {
        "minimal": {"connectorId": 1, "idTag": "00123", "meterStart": 0, "timestamp": T1},
        "full": {
            "connectorId": 1,
            "idTag": "00123",
            "meterStart": 1000,
            "reservationId": 5,
            "timestamp": T1,
        },
    },
    ("StartTransaction", "response"): {
        "minimal": {"idTagInfo": {"status": "Accepted"}, "transactionId": 42},
        "full": {"idTagInfo": ID_TAG_INFO_FULL, "transactionId": 42},
    },
    ("StopTransaction", "request"): {
        "minimal": {"transactionId": 42, "timestamp": T2, "meterStop": 2000},
        "full": {
            "idTag": "00123",
            "meterStop": 2000,
            "timestamp": T2,
            "transactionId": 42,
            "reason": "EVDisconnected",
            "transactionData": [
                {"timestamp": T1, "sampledValue": [SAMPLED_VALUE_FULL]},
                {"timestamp": T2, "sampledValue": [{"value": "2000"}]},
            ],
        },
    },
    ("StopTransaction", "response"): {
        "minimal": {},
        "full": {"idTagInfo": ID_TAG_INFO_FULL},
    },
    ("MeterValues", "request"): {
        "minimal": {
            "connectorId": 1,
            "meterValue": [{"timestamp": T1, "sampledValue": [{"value": "0"}]}],
        },
        "full": {
            "connectorId": 1,
            "transactionId": 42,
            "meterValue": [
                {
                    "timestamp": T1,
                    "sampledValue": [
                        SAMPLED_VALUE_FULL,
                        {"value": "50.0", "measurand": "Frequency", "unit": "Hertz"},
                    ],
                }
            ],
        },
    },
    ("MeterValues", "response"): {"minimal": {}, "full": {}},
    ("DataTransfer", "request"): {
        "minimal": {"vendorId": "com.example"},
        "full": {"vendorId": "com.example", "messageId": "Ping", "data": '{"a": 1}'},
    },
    ("DataTransfer", "response"): {
        "minimal": {"status": "UnknownVendorId"},
        "full": {"status": "Accepted", "data": "pong"},
    },
    ("RemoteStartTransaction", "request"): {
        "minimal": {"idTag": "00123"},
        "full": {"connectorId": 1, "idTag": "00123", "chargingProfile": PROFILE_FULL},
    },
    ("RemoteStartTransaction", "response"): {
        "minimal": {"status": "Accepted"},
        "full": {"status": "Rejected"},
    },
    ("RemoteStopTransaction", "request"): {
        "minimal": {"transactionId": 42},
        "full": {"transactionId": 43},
    },
    ("RemoteStopTransaction", "response"): {
        "minimal": {"status": "Accepted"},
        "full": {"status": "Rejected"},
    },
    ("GetConfiguration", "request"): {
        "minimal": {},
        "full": {"key": ["HeartbeatInterval", "MeterValueSampleInterval"]},
    },
    ("GetConfiguration", "response"): {
        "minimal": {},
        "full": {
            "configurationKey": [
                {"key": "HeartbeatInterval", "readonly": False, "value": "300"},
                {"key": "NumberOfConnectors", "readonly": True},
            ],
            "unknownKey": ["FooBar"],
        },
    },
    ("ChangeConfiguration", "request"): {
        "minimal": {"key": "HeartbeatInterval", "value": "60"},
        "full": {"key": "MeterValuesSampledData", "value": "Energy.Active.Import.Register"},
    },
    ("ChangeConfiguration", "response"): {
        "minimal": {"status": "Accepted"},
        "full": {"status": "RebootRequired"},
    },
    ("Reset", "request"): {"minimal": {"type": "Soft"}, "full": {"type": "Hard"}},
    ("Reset", "response"): {"minimal": {"status": "Accepted"}, "full": {"status": "Rejected"}},
    ("TriggerMessage", "request"): {
        "minimal": {"requestedMessage": "Heartbeat"},
        "full": {"requestedMessage": "StatusNotification", "connectorId": 1},
    },
    ("TriggerMessage", "response"): {
        "minimal": {"status": "Accepted"},
        "full": {"status": "NotImplemented"},
    },
    ("UnlockConnector", "request"): {
        "minimal": {"connectorId": 1},
        "full": {"connectorId": 2},
    },
    ("UnlockConnector", "response"): {
        "minimal": {"status": "Unlocked"},
        "full": {"status": "NotSupported"},
    },
    ("ChangeAvailability", "request"): {
        "minimal": {"connectorId": 0, "type": "Inoperative"},
        "full": {"connectorId": 1, "type": "Operative"},
    },
    ("ChangeAvailability", "response"): {
        "minimal": {"status": "Accepted"},
        "full": {"status": "Scheduled"},
    },
    ("SetChargingProfile", "request"): {
        "minimal": {"connectorId": 0, "csChargingProfiles": PROFILE_MIN},
        "full": {"connectorId": 1, "csChargingProfiles": PROFILE_FULL},
    },
    ("SetChargingProfile", "response"): {
        "minimal": {"status": "Accepted"},
        "full": {"status": "NotSupported"},
    },
    ("ClearChargingProfile", "request"): {
        "minimal": {},
        "full": {
            "id": 7,
            "connectorId": 1,
            "chargingProfilePurpose": "ChargePointMaxProfile",
            "stackLevel": 0,
        },
    },
    ("ClearChargingProfile", "response"): {
        "minimal": {"status": "Unknown"},
        "full": {"status": "Accepted"},
    },
    ("GetCompositeSchedule", "request"): {
        "minimal": {"connectorId": 0, "duration": 86400},
        "full": {"connectorId": 1, "duration": 3600, "chargingRateUnit": "W"},
    },
    ("GetCompositeSchedule", "response"): {
        "minimal": {"status": "Rejected"},
        "full": {
            "status": "Accepted",
            "connectorId": 1,
            "scheduleStart": T1,
            "chargingSchedule": SCHEDULE_FULL,
        },
    },
}
