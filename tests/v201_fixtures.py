"""OCPP 2.0.1 payload fixtures: one minimal and one all-optional-fields payload per core message.

Plain dicts in wire form (camelCase, datetimes as UTC with milliseconds and Z), so that
``dump(parse(...))`` must give back exactly the same dict. Shared by the model tests and the
2.0.1 schema conformance test. Independent of ``v16_fixtures.py``.
"""

T1 = "2026-11-26T09:35:32.123Z"
T2 = "2026-11-27T00:00:00.000Z"

CUSTOM = {"vendorId": "com.example", "anything": {"nested": [1, None, "x"]}}
STATUS_INFO_FULL = {"customData": CUSTOM, "reasonCode": "R1", "additionalInfo": "more"}

ID_TOKEN_MIN = {"idToken": "00123", "type": "ISO14443"}
ID_TOKEN_FULL = {
    "customData": CUSTOM,
    "additionalInfo": [{"customData": CUSTOM, "additionalIdToken": "ADD-1", "type": "Contract"}],
    "idToken": "04A1B2C3D4E5F6",
    "type": "ISO14443",
}

MESSAGE_CONTENT_FULL = {"customData": CUSTOM, "format": "UTF8", "language": "ja", "content": "Hello"}

ID_TOKEN_INFO_FULL = {
    "customData": CUSTOM,
    "status": "Accepted",
    "cacheExpiryDateTime": T2,
    "chargingPriority": 1,
    "language1": "en",
    "evseId": [1, 2],
    "groupIdToken": {"idToken": "GROUP01", "type": "Central"},
    "language2": "ja",
    "personalMessage": MESSAGE_CONTENT_FULL,
}

OCSP_FULL = {
    "customData": CUSTOM,
    "hashAlgorithm": "SHA256",
    "issuerNameHash": "a" * 64,
    "issuerKeyHash": "b" * 64,
    "serialNumber": "0123456789",
    "responderURL": "http://ocsp.example.com",
}

SAMPLED_VALUE_FULL = {
    "customData": CUSTOM,
    "value": 1234.5,
    "context": "Sample.Periodic",
    "measurand": "Energy.Active.Import.Register",
    "phase": "L1-N",
    "location": "Outlet",
    "signedMeterValue": {
        "customData": CUSTOM,
        "signedMeterData": "c2lnbmVk",
        "signingMethod": "ECDSA-secp256r1-SHA256",
        "encodingMethod": "OCMF",
        "publicKey": "cHVi",
    },
    "unitOfMeasure": {"customData": CUSTOM, "unit": "Wh", "multiplier": 0},
}

METER_VALUE_MIN = {"timestamp": T1, "sampledValue": [{"value": 0}]}  # integer JSON value for a number field
METER_VALUE_FULL = {"customData": CUSTOM, "timestamp": T1, "sampledValue": [SAMPLED_VALUE_FULL]}

TRANSACTION_EVENT_MIN = {
    "eventType": "Started",
    "timestamp": T1,
    "triggerReason": "CablePluggedIn",
    "seqNo": 0,
    "transactionInfo": {"transactionId": "0001"},
}

FIXTURES: dict = {
    ("BootNotification", "request"): {
        "min": {"chargingStation": {"model": "SME-AC22", "vendorName": "SME"}, "reason": "PowerUp"},
        "full": {
            "customData": CUSTOM,
            "chargingStation": {
                "customData": CUSTOM,
                "serialNumber": "SN-0001",
                "model": "SME-AC22",
                "modem": {"customData": CUSTOM, "iccid": "8981100000000000000", "imsi": "440100000000000"},
                "vendorName": "SME",
                "firmwareVersion": "0.2.0",
            },
            "reason": "RemoteReset",
        },
    },
    ("BootNotification", "response"): {
        "min": {"currentTime": T1, "interval": 300, "status": "Accepted"},
        "full": {
            "customData": CUSTOM,
            "currentTime": T1,
            "interval": 0,
            "status": "Pending",
            "statusInfo": STATUS_INFO_FULL,
        },
    },
    ("Heartbeat", "request"): {"min": {}, "full": {"customData": CUSTOM}},
    ("Heartbeat", "response"): {
        "min": {"currentTime": T1},
        "full": {"customData": CUSTOM, "currentTime": T2},
    },
    ("StatusNotification", "request"): {
        "min": {"timestamp": T1, "connectorStatus": "Available", "evseId": 1, "connectorId": 1},
        "full": {
            "customData": CUSTOM,
            "timestamp": T1,
            "connectorStatus": "Occupied",
            "evseId": 2,
            "connectorId": 1,
        },
    },
    ("StatusNotification", "response"): {"min": {}, "full": {"customData": CUSTOM}},
    ("Authorize", "request"): {
        "min": {"idToken": ID_TOKEN_MIN},
        "full": {
            "customData": CUSTOM,
            "idToken": ID_TOKEN_FULL,
            "certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "iso15118CertificateHashData": [OCSP_FULL],
        },
    },
    ("Authorize", "response"): {
        "min": {"idTokenInfo": {"status": "Invalid"}},
        "full": {"customData": CUSTOM, "idTokenInfo": ID_TOKEN_INFO_FULL, "certificateStatus": "Accepted"},
    },
    ("TransactionEvent", "request"): {
        "min": TRANSACTION_EVENT_MIN,
        "full": {
            "customData": CUSTOM,
            "eventType": "Ended",
            "meterValue": [METER_VALUE_FULL, METER_VALUE_MIN],
            "timestamp": T2,
            "triggerReason": "StopAuthorized",
            "seqNo": 7,
            "offline": False,
            "numberOfPhasesUsed": 3,
            "cableMaxCurrent": 32,
            "reservationId": 5,
            "transactionInfo": {
                "customData": CUSTOM,
                "transactionId": "4f1c2b6e-0000-4000-8000-000000000001",
                "chargingState": "EVConnected",
                "timeSpentCharging": 3600,
                "stoppedReason": "Local",
                "remoteStartId": 42,
            },
            "evse": {"customData": CUSTOM, "id": 1, "connectorId": 1},
            "idToken": ID_TOKEN_FULL,
        },
    },
    ("TransactionEvent", "response"): {
        "min": {},
        "full": {
            "customData": CUSTOM,
            "totalCost": 12.5,
            "chargingPriority": -9,
            "idTokenInfo": ID_TOKEN_INFO_FULL,
            "updatedPersonalMessage": MESSAGE_CONTENT_FULL,
        },
    },
    ("MeterValues", "request"): {
        "min": {"evseId": 1, "meterValue": [METER_VALUE_MIN]},
        "full": {"customData": CUSTOM, "evseId": 0, "meterValue": [METER_VALUE_FULL]},
    },
    ("MeterValues", "response"): {"min": {}, "full": {"customData": CUSTOM}},
    ("DataTransfer", "request"): {
        "min": {"vendorId": "com.example"},
        "full": {
            "customData": CUSTOM,
            "messageId": "ping",
            "data": {"any": ["json", 1, None, True]},
            "vendorId": "com.example",
        },
    },
    ("DataTransfer", "response"): {
        "min": {"status": "UnknownVendorId"},
        "full": {"customData": CUSTOM, "status": "Accepted", "statusInfo": STATUS_INFO_FULL, "data": "pong"},
    },
}
