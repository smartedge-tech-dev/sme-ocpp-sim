"""OCPP 2.0.1 station: connector states, authorization, the TransactionEvent flow (plan §11.2, M2), the
CSMS remote control (M3: RequestStartTransaction / RequestStopTransaction / UnlockConnector / Reset /
ChangeAvailability / TriggerMessage), the Device Model (M4: GetVariables / SetVariables / GetBaseReport) and smart
charging (M5: SetChargingProfile / ClearChargingProfile / GetCompositeSchedule / GetChargingProfiles, the
operator's external limit with NotifyChargingLimit / ClearedChargingLimit), the Local Authorization List, the
Authorization Cache and reservations (M6: SendLocalList / GetLocalListVersion / ClearCache / ReserveNow /
CancelReservation, ReservationStatusUpdate).

One :class:`Station201` per 2.0.1 charging station, hosted by the shared shell ``runtime.charger.ChargerRuntime``
through :class:`~sme_ocpp_sim.protocol.station.StationHost`. It is a separate stack from ``protocol/v16``: it uses
only the 2.0.1 adapter, models and codec of this package and the version-neutral power model
(``charging.power`` through ``host.step_power``). Session tasks are started with ``host.spawn`` and end with the
session; transactions survive a disconnect.

Topology (plan §11.1 Q11): ``connectors: N`` is N EVSEs with one connector each, so EVSE ``i`` has connector 1
and the operator API's ``connector`` argument is the EVSE id. There is no StatusNotification for EVSE 0.

Transactions (2.0.1 E-series): the station generates the transaction id (a UUID4, 36 characters) and numbers the
TransactionEvents of one transaction with a continuous ``seqNo`` from 0: ``Started`` -> ``Updated`` x n ->
``Ended``. ``Started`` carries the id token, the EVSE (only this first event after the cable was connected,
E01.FR.16) and the meter start (``Transaction.Begin``); periodic
``Updated`` carry the configured readings (``Sample.Periodic``); a power-model suspension change sends an
``Updated`` with ``ChargingStateChanged``; ``Ended`` carries the final energy register (``Transaction.End``) and
``stoppedReason``.

Ended meter data (E06 / J02 / 2.7.8, v0.2.2 B): ``Ended.meterValue`` holds the union of SampledDataCtrlr and
AlignedDataCtrlr TxEndedMeasurands at the start (``Transaction.Begin``) and at the end (``Transaction.End``),
the SampledDataCtrlr.TxEndedMeasurands every SampledDataCtrlr.TxEndedInterval from the start
(``Sample.Periodic``; 0 = only the start and end values) and the AlignedDataCtrlr.TxEndedMeasurands at every
AlignedDataCtrlr.TxEndedInterval clock boundary (``Sample.Clock``; 0 = none). Readings are taken on the tick
(``charging.tx_data``); at most ``meter.tx_data_max_samples`` interval readings are kept. Simulator options
(``config.MeterOptions``): ``phase_values`` reports Voltage / Current.Import per phase, ``post_tx_delay_s`` holds
the Ended event (and what is queued behind it) for that long after the end.

Outbox: every TransactionEvent and every StatusNotification caused by a state change goes through ONE ordered
queue, sent by ONE worker per session that runs once the boot is accepted (as in the 1.6 station, but written
for 2.0.1). Enqueueing is synchronous, so the operator API never waits for the CSMS. A lost connection keeps the
head item queued for the next session; a CALLERROR / timeout / invalid answer of a TransactionEvent is retried
:data:`TX_MESSAGE_ATTEMPTS` times, :data:`TX_MESSAGE_RETRY_INTERVAL_S` x failures apart. StatusNotifications are
not retried and are dropped at the end of a session (the state is reported afresh after the next accepted boot).
The one exception to the single queue: the state report right after an accepted boot is sent directly, before the
worker starts, so it precedes the events queued while offline / Pending.

Remote control (M3): the adapter hands each remote-control request over as its 2.0.1 request model and gets the
2.0.1 response model back (:meth:`Station201.handle`). The answer is decided synchronously; what the request
causes (a transaction start / stop, a StatusNotification, a triggered message, the Reset) runs after the
CALLRESULT went out (``host.after_reply``), so the CSMS always sees the answer first.

Interpretations (not defined by the spec)
-----------------------------------------
- Transaction start / stop points: a transaction starts when the EV is connected AND the id token is authorized
  (whichever comes last is the ``triggerReason``: ``CablePluggedIn`` or ``Authorized``; TxStartPoint
  ``PowerPathClosed``) and charging begins at once. It ends on: the same token presented again or the operator
  stop (``StopAuthorized`` / ``Local``; the token is sent only when one was presented), cable unplugged
  (``EVCommunicationLost`` / ``EVDisconnected``), an operator fault (``AbnormalCondition`` / the fault code, which
  is a ``ReasonEnumType`` value), or a CSMS that does not accept the token in a TransactionEvent answer
  (``Deauthorized`` / ``DeAuthorized``, the default of ``StopTxOnInvalidId``). With StopTxOnInvalidId ``false``
  the transaction goes on but the energy transfer stops at once (the power model gets a 0 W limit), which sends
  an ``Updated`` ``ChargingStateChanged`` with ``SuspendedEVSE`` (E05.FR.02); MaxEnergyOnInvalidId is not
  modelled. ``Started`` already carries ``chargingState`` ``Charging`` (E02.FR.13 allows the start and the state
  in one event).
- A token authorized before the cable is plugged waits on the connector, which stays ``Available`` until the
  cable comes (2.0.1 has no ``Preparing``); the Web UI still sees the 1.6-style domain status. An unplug drops a
  waiting token (the next session needs a fresh authorization), and so does TxCtrlr.EVConnectionTimeOut (E03 /
  F02.FR.07; 0 = no timeout): with TxStartPoint ``PowerPathClosed`` no transaction exists yet, so no ``Ended``
  is sent.
- ``seqNo`` restarts at 0 for every transaction. A queued periodic ``Updated`` dropped by the queue bound
  (:data:`MAX_TX_QUEUE`) leaves a gap in ``seqNo``; ``Started`` / ``Ended`` / ``ChargingStateChanged`` are never
  dropped by the bound.
- ``offline`` is ``true`` for an event raised while the station had no WebSocket connection. An event raised while
  connected but before the boot was accepted (Pending) is held back until Accepted and sent with
  ``offline=false``. Queued events keep the flag they were raised with, also when they are resent after a
  reconnect; an event whose answer was lost with the connection is sent again (the CSMS can de-duplicate by
  ``transactionId`` + ``seqNo``).
- While the boot is not accepted only BootNotification is sent (no Authorize, StatusNotification or
  TransactionEvent; B02 / B03), except what a TriggerMessage asks for while Pending (B02.FR.02, see
  TriggerMessage). While Pending, RequestStartTransaction / RequestStopTransaction are ``Rejected``
  (B02.FR.05) and the other CSMS CALLs are answered as usual; while Rejected every CSMS CALL is answered CALLERROR
  ``SecurityError`` (B03, Table 32). A transaction may start while offline / Pending when the token is authorized
  locally (see the AuthCtrlr variables below); otherwise it is ``Offline`` then.
- Authorization of a token (presented, or of a RequestStartTransaction with AuthorizeRemoteStart) follows the
  AuthCtrlr variables (Part 2 §2.3), whose initial values come from ``auth_mode`` and which SetVariables may
  change: Enabled ``false`` -> accepted; DisableRemoteAuthorization ``true`` -> accepted when it is in
  ``id_tags`` (the local list), else ``Invalid``, no Authorize is sent; online, LocalPreAuthorize ``true`` and in
  ``id_tags`` -> accepted without Authorize, else Authorize; offline, LocalAuthorizeOffline ``true`` and in
  ``id_tags`` -> accepted, else OfflineTxForUnknownIdEnabled ``true`` -> accepted, else ``Offline``. With the
  initial values this is the ``auth_mode`` behaviour (``accept_all``, ``local_only``, ``local_then_csms``,
  ``csms_only``).
- The operator API returns once ``Started`` is queued (2.0.1 needs no CSMS answer to know the transaction id); a
  later refusal of the token in the answer ends the transaction (see above).
- Device Model (M4, :mod:`.device_model`): the station reads its settings from the standard variables, so a
  SetVariables change applies at once: HeartbeatInterval (also set by an accepted BootNotification),
  SampledDataCtrlr.TxUpdatedInterval (periodic ``Updated``, 0 = none; initial value ``meter_sample_interval_s``
  of the charger; a change applies after the current wait, and an empty TxUpdatedMeasurands sends no periodic
  ``Updated``), Tx{Started,Updated,Ended}Measurands (readings of the TransactionEvents), OCPPCommCtrlr
  MessageAttempts / MessageAttemptInterval [TransactionEvent] (the retry policy below), AuthCtrlr
  AuthorizeRemoteStart, the other AuthCtrlr variables (above) and TxCtrlr.StopTxOnInvalidId. ``config_keys``
  (the 1.6 key table) is not used. GetVariables with more items than DeviceDataCtrlr.ItemsPerMessage
  [GetVariables] is answered CALLERROR ``OccurrenceConstraintViolation`` (B06 "MAY respond with CALLERROR";
  Part 4 Table 8 spelling, B06 / B08 write ``Occurence``); SetVariables over ItemsPerMessage [SetVariables] is
  answered the same way, an extension of the B06 / B08 rule (B05 only forbids the CSMS to send more).
  GetBaseReport is answered first (``Accepted``; B07 has no ``EmptyResultSet``, so an empty report base sends
  one NotifyReport without ``reportData``); the NotifyReport parts (``seqNo`` from 0, B07.FR.10; ``tbc`` on all
  but the last, one ``generatedAt``) follow, also while the boot is Pending (they answer a CSMS request), and
  stop at the first part that fails. A report holds the values at the time of the request. Interpretation: a
  part holds at most ItemsPerMessage [GetReport] ``reportData`` entries; BytesPerMessage is not enforced.
- SecurityCtrlr.BasicAuthPassword (A01): a new value is ``Accepted``; after the SetVariables answer the station
  closes the connection and reconnects (without back-off) with the new password, SecurityProfile updated. The
  password lives in memory only (it is not written to the YAML).
- Triggered / operator MeterValues (outside the transaction flow) report AlignedDataCtrlr.Measurands
  (F06.FR.06), a triggered TransactionEvent the TxUpdatedMeasurands (F06.FR.07). Clock-aligned readings are
  modelled only for the Ended meterValue (AlignedDataCtrlr.TxEndedInterval); no clock-aligned MeterValues /
  Updated events are sent (AlignedDataCtrlr.Interval is not served).
- Operator fault codes are the fault-type values of ``ReasonEnumType`` (:attr:`Station201.fault_codes`): a 2.0.1
  StatusNotification has no error code, so the code is only the ``stoppedReason`` of an interrupted transaction
  (NotifyEvent is not sent).
- Smart charging (M5, :mod:`.profiles` / :mod:`.composite`, the 2.0.1 copy of the 1.6 rules): the power model of
  a charging EVSE is stepped with ``composite.limit_at`` (the evaluator that also answers GetCompositeSchedule)
  on every tick, as in the 1.6 station. Set / ClearChargingProfile integrate every charging EVSE up to now at the
  old limit; the new limit takes effect on the tick right after the CALLRESULT. A limit in A applies to the
  phases the EV uses while one charges and to the supply's phases otherwise. GetCompositeSchedule periods are
  capped at the hardware maximum (``max_power_w``, times the EVSE count for evseId 0), which also stands for "no
  limit"; an unknown EVSE or a duration < 1 is ``Rejected``. The TxProfiles of an EVSE are dropped when its
  transaction ends. A GetCompositeSchedule ``chargingRateUnit`` not in SmartChargingCtrlr.RateUnit is
  ``Rejected`` (K08.FR.07, reasonCode ``UnsupportedRateUnit``). A Rejected SetChargingProfile carries the
  reasonCode of ``ProfileStore.reject_reason`` (``DuplicateProfile``, ``UnsupportedPurpose`` ...).
- GetChargingProfiles: ``Accepted`` when a profile matches (the criteria and ``evseId`` are ANDed; ``evseId`` 0
  = the station-level profiles only, no ``evseId`` = all), else ``NoProfiles``. One ReportChargingProfiles per
  (EVSE, ``chargingLimitSource``) group follows the answer, ``tbc`` on all but the last, the profiles as the CSMS
  sent them (a RequestStartTransaction profile with its bound ``transactionId``); it stops at the first part that
  fails. CSMS profiles are ``CSO``, the operator's external constraints ``EMS``.
- External limit (operator / Web API, :meth:`Station201.set_external_limit`): installs a
  ChargingStationExternalConstraints profile (id :func:`external_profile_id`, stackLevel 0, Absolute from now,
  one period in W) on the EVSE (0 = the station) and sends NotifyChargingLimit (``chargingLimitSource`` ``EMS``,
  ``evseId`` only for an EVSE, the schedule when SmartChargingCtrlr.NotifyChargingLimitWithSchedules is true);
  removing it sends ClearedChargingLimit (K13.FR.03). Both are sent in the background on every change and only
  while the boot is accepted (nothing is queued offline or while Pending, and nothing is resent later). An EVSE
  whose transaction then charges at a rate that changed by more than SmartChargingCtrlr.LimitChangeSignificance
  percent (the limit capped at ``max_power_w``) queues an ``Updated`` ``ChargingRateChanged`` (K11.FR.04 /
  K13.FR.03). It is refused (``ValueError``) while ExternalControlSignalsEnabled is false (K11.FR.06: the
  profile has the purpose ChargingStationExternalConstraints and a negative id).
- RequestStartTransaction (M3): with AuthorizeRemoteStart ``false`` (the default) the CSMS has authorized the
  token, so it is not authorized again; with ``true`` it is authorized like a presented token (``auth_mode``)
  after the answer and dropped from the EVSE when that is not ``Accepted`` (F01.FR.01), except a token of
  ``type`` ``Central`` or ``NoAuthorization``, which is never authorized (F01.FR.02, F02.FR.09 / FR.10). Without
  ``evseId`` the first startable EVSE with a plugged cable is used, else the first startable one. A faulted,
  inoperative or busy EVSE (or none free) is ``Rejected``; so is a ``chargingProfile`` that is not a valid
  ``TxProfile`` or that carries a ``transactionId``.
  An accepted ``chargingProfile`` is installed, bound to the new ``transactionId``, when the transaction starts
  (M5); it is discarded with the token. An accepted token waits on an unplugged
  EVSE like a local one; the transaction starts at once when the cable is plugged or when it comes, with
  ``triggerReason`` ``RemoteStart`` either way (F02.FR.21). ``Started`` carries the token with the ``type`` the CSMS
  sent and ``remoteStartId`` in ``transactionInfo``; the answer has no ``transactionId`` (no transaction exists
  before the request with these start points). ``groupIdToken`` is not used. An EVSE with a waiting token (local
  or remote) is ``Rejected`` (``Occupied``) and not chosen without ``evseId``.
- RequestStopTransaction: ``Accepted`` for a running transaction of this station (it ends with ``RemoteStop`` /
  ``Remote``), ``Rejected`` for any other ``transactionId``.
- UnlockConnector: ``UnknownConnector`` for an unknown EVSE or a connector other than 1,
  ``OngoingAuthorizedTransaction`` while the EVSE has a transaction (it is not stopped), else ``Unlocked`` (or
  ``UnlockFailed`` when :attr:`Station201.unlock_fails` is set). Unlocking changes no state (the cable stays
  plugged until the operator unplugs it).
- Reset: an EVSE reset (``evseId`` given) is not supported and ``Rejected``. ``Immediate`` ends every transaction
  (``ResetCommand`` / ``ImmediateReset``), lets the queued messages go out (bounded by ``host.call_timeout``) and
  reboots: the session closes and reconnects without back-off, with BootNotification reason ``RemoteReset``.
  ``OnIdle`` does the same at once when no transaction runs, else answers ``Scheduled`` and resets when the last
  transaction has ended; new transactions may still start meanwhile. An ``Immediate`` Reset overrides a scheduled
  one. While a Reset runs no transaction can start (``Rejected`` / ``Unavailable``). Cable, fault and availability
  states survive the reboot; tokens waiting for a cable do not.
- ChangeAvailability: no ``evse`` or EVSE id 0 sets the station's own availability, kept apart from the EVSEs'
  (G04, G03 / G04 Note 1): an EVSE is operative only while both are, so an EVSE ``Operative`` does not undo a
  station ``Inoperative``, and a station ``Operative`` brings every EVSE back to its own state (G04.FR.08). An
  unknown EVSE or a connector other than 1 is ``Rejected``. An EVSE going ``Inoperative`` while it has a
  transaction is ``Scheduled`` and takes effect when the transaction ends (G03.FR.05); ``Operative`` cancels it.
  The station going ``Inoperative`` while any EVSE has a transaction is ``Scheduled`` (G04.FR.06): each EVSE
  with a transaction becomes Unavailable when its transaction ends, AMBIGUOUS: the idle EVSEs become
  Unavailable at once (G04.FR.07 read per EVSE). Every other change takes effect at once (``Accepted``, also
  when the state does not change, G03.FR.03 / G04.FR.04). Availability survives a Reset. A token waiting on an
  EVSE that becomes inoperative is dropped.
- TriggerMessage: BootNotification is ``Accepted`` only while the registration is not ``Accepted`` (it wakes the
  boot loop; reason ``Triggered`` for the next BootNotification sent), else ``Rejected``. The other messages are
  ``Rejected`` before a BootNotification answer and served while Accepted or Pending (B02.FR.02 allows them
  then). StatusNotification and MeterValues (context ``Trigger``, AlignedDataCtrlr.Measurands) are sent for the
  addressed EVSE, or every EVSE without ``evse``; TransactionEvent sends an ``Updated`` (``triggerReason``
  ``Trigger``, the TxUpdatedMeasurands readings) for the transaction of the addressed EVSE, or of every EVSE, and
  is ``Rejected`` when there is none. While Pending the StatusNotifications are sent directly; the
  TransactionEvent joins the outbox and goes out after the boot is accepted, so ``seqNo`` stays in order.
  LogStatusNotification sends ``Uploading`` (with the requestId) while a log upload runs, else ``Idle`` (F06.FR.13 /
  FR.14, N01.FR.13); FirmwareStatusNotification sends the last sent status with its requestId, ``Idle`` when that
  was ``Installed`` or none was sent (L02.FR.16 / FR.17, F06.FR.16; a stopped charger forgets an unfinished
  update, so it answers ``Idle``). An unknown EVSE or a connector other than 1 is ``Rejected``.
  SignChargingStationCertificate is ``Accepted`` and sends a SignCertificateRequest (M8, below). The other
  ``requestedMessage`` values (SignV2GCertificate, SignCombinedCertificate: ISO 15118;
  PublishFirmwareStatusNotification) answer ``NotImplemented``.
- Security (M8, A02 / A04 / M03 - M05; ``runtime.security`` has the shared interpretations): the CSR of a
  SignCertificateRequest (``certificateType`` ChargingStationCertificate) has CN = ``serial`` (the charger id
  without one, A00.FR.511) and O = SecurityCtrlr.OrganizationName (A00.FR.509). CertificateSigned is ``Accepted``
  for a chain matching the outstanding CSR, else ``Rejected`` (reasonCode ``InvalidCertificate``) with an
  InvalidChargingStationCertificate event (A02.FR.07); ``certificateType`` V2GCertificate is ``Rejected``
  (``NotSupported``). With security profile 3 the station then reconnects with the new certificate (A02.FR.08),
  falls back to the old one when that connection fails (A02.FR.28, DiscardedRenewedClientCertificate, A02.FR.29)
  and discards the old one once a boot is accepted (A02.FR.10); with profile 1 / 2 the certificate is only stored.
  CertSigningWaitMinimum / CertSigningRepeatTimes (A02.FR.17 - FR.19, optional variables) are not modelled: an
  unanswered CSR is not resent. InstallCertificate / DeleteCertificate / GetInstalledCertificateIds (M03 - M05)
  manage CSMSRootCertificate / ManufacturerRootCertificate / V2GRootCertificate / MORootCertificate entries;
  V2GCertificateChain is always ``NotFound``. SecurityEventNotification (A04): StartupOfTheDevice (the station is
  created), ResetOrReboot (a Reset or the firmware reboot), FirmwareUpdated (Installed), InvalidCsmsCertificate
  (TLS server check failed, A00.FR.310), InvalidTLSVersion (A00.FR.316), FailedToAuthenticateAtCsms (HTTP 401 or
  client certificate refused), InvalidChargingStationCertificate, DiscardedRenewedClientCertificate and
  ReconfigurationOfSecurityParameters (a new client certificate or BasicAuthPassword). SettingSystemTime,
  InvalidFirmwareSignature and the other events of the appendix are never raised (nothing in the simulator
  produces them).
- Firmware update (M7, L01 / L02; ``runtime.maintenance`` has the shared interpretations): UpdateFirmware is
  ``Accepted``, or ``AcceptedCanceled`` when it cancels an update that has not started installing (L02.FR.15, no
  status is sent for the cancelled one); one received while ``Installing`` or later is ``Rejected`` (L02.FR.18,
  reasonCode ``Installing``). Steps, each a FirmwareStatusNotification with the request's ``requestId``
  (L02.FR.01 / FR.06): ``DownloadScheduled`` (retrieveDateTime in the future, L02.FR.07) -> ``Downloading`` ->
  ``Downloaded`` / ``DownloadFailed`` -> ``SignatureVerified`` (only when the request has a ``signature``; it is
  taken as valid, no certificate or signature is checked, so ``InvalidCertificate`` / ``InvalidSignature`` are never
  used) -> ``InstallScheduled`` (installDateTime in the future, L02.FR.10) -> [EVSEs without a transaction
  Unavailable until no transaction is left, L02.FR.03 / FR.04] -> ``Installing`` -> ``InstallationFailed`` or
  ``InstallRebooting`` -> reboot (as a Reset, BootNotification reason ``FirmwareUpdate``) -> ``Installed`` after the
  boot is accepted (L02.FR.21 option (a)). DownloadPaused and InstallVerificationFailed are never sent; the
  optional InstallScheduled while waiting for transactions (L02.FR.23) is not sent.
- Log upload (M7, N01): GetLog is always ``Accepted`` (the log is "available", N01.FR.01) with ``filename``
  ``<charger id>_<logType>_<UTC time>.log``; a GetLog while an upload runs cancels it, answers
  ``AcceptedCanceled`` (N01.FR.12) and sends LogStatusNotification ``AcceptedCanceled`` for the old requestId
  before the new upload starts (N01.FR.20). Steps: ``Uploading`` -> ``Uploaded`` / ``UploadFailure`` (the failure
  injection; BadMessage / PermissionDenied / NotSupportedOperation are never sent). ``oldestTimestamp`` /
  ``latestTimestamp`` are not used; SecurityLog and DiagnosticsLog behave the same.
- Local Authorization List / Authorization Cache (M6, C10 - C14, D01, D02; ``runtime.local_auth``, in memory, kept
  across reconnects and Resets): with the list (LocalAuthListCtrlr.Enabled) or the cache
  (AuthCacheCtrlr.Enabled) a token is looked up there too, the list first (C13.FR.01 / C14.FR.01; a token in the
  list is never cached). In the AuthCtrlr order above: DisableRemoteAuthorization -> the entry's status, else
  ``Invalid``; offline with LocalAuthorizeOffline -> the entry's status (C13), OfflineTxForUnknownIdEnabled only
  for a token in neither (C13.FR.04); online with LocalPreAuthorize -> an Accepted entry without Authorize (C12 /
  C14.FR.02), an entry that is not Accepted is authorized again (C10.FR.03, C12.FR.05, C14.FR.03) unless the
  store's DisablePostAuthorize is true. Every AuthorizeResponse and TransactionEventResponse ``idTokenInfo``
  updates the cache (C10.FR.04 / FR.05). LocalAuthListCtrlr.SupportsExpiryDateTime is not served, so the list
  ignores ``cacheExpiryDateTime`` (C13.FR.02 / C14.FR.02); in the cache an expired entry, or one older than
  AuthCacheCtrlr.LifeTime, is ``Expired`` (C10.FR.10 / FR.13). SendLocalList: ``versionNumber`` < 1 is CALLERROR
  PropertyConstraintViolation (D01.FR.18, like the other range checks); while LocalAuthListCtrlr.Enabled is
  false, with more entries than LocalAuthListCtrlr.ItemsPerMessage, duplicate tokens (D01.FR.06), a Full entry
  without ``idTokenInfo`` or more than LocalAuthListCtrlr.Entries maxLimit tokens it is ``Failed`` and the list
  is unchanged; a Differential update must raise the version (D01.FR.19). The list is keyed by ``idToken``
  (the ``type`` is not compared). GetLocalListVersion is 0 while the list is disabled (D02.FR.03), else the stored
  version (0 = none received yet, D02.FR.02). ClearCache is ``Rejected`` while AuthCacheCtrlr.Enabled is false
  (C11.FR.04).
- Reservations (M6, H01 - H04): ReserveNow is ``Rejected`` while ReservationCtrlr.Enabled is false (H01.FR.01),
  without ``evseId`` while ReservationCtrlr.NonEvseSpecific is false (H01.FR.19), for an unknown EVSE
  (``UnknownEvse``) or an ``expiryDateTime`` that is not in the future; else ``Faulted`` / ``Unavailable`` /
  ``Occupied`` (a transaction, a cable, a waiting token or a reservation on it) for the EVSE, or for every EVSE
  without ``evseId`` (H01.FR.11 - FR.14), else ``Accepted``. The same ``id`` replaces a reservation (H01.FR.02;
  checked as if the old one did not exist, kept when the new one is refused). ``connectorType`` is not modelled:
  such a request is handled as one without ``evseId`` (TODO(spec): H01.FR.06 / FR.09 / FR.24). A reserved EVSE
  reports ``Reserved`` (H01.FR.23) and starts only for the reserved ``idToken`` or a token whose group matches
  the reservation's ``idToken`` / ``groupIdToken`` (H03; the group comes from the authorization, the list or the
  cache, else an AuthorizeRequest, H03.FR.07 / FR.08); any other token is refused (``Reserved``), also a
  RequestStartTransaction (``Rejected``, reasonCode ``Reserved``; its ``groupIdToken`` is compared). Matching is
  pairwise (token = reserved token, or group = reservation group; see ``runtime.local_auth``). Without ``evseId``
  one EVSE is kept free (H01.FR.07): while the free EVSEs are no more than those reservations they report
  ``Reserved`` (H01.FR.20), and a non-matching token is refused when its start would leave fewer free EVSEs than
  those reservations. "Free" = no fault, operative, no transaction, no waiting token, not reserved; a plugged
  cable does not make an EVSE unavailable for the reservation (it authorizes nothing), but ReserveNow for a
  plugged EVSE is ``Occupied``. A matching token that waits (for the cable, or an AuthorizeRemoteStart
  authorization) claims the reservation (H03.FR.09; the EVSE goes ``Available`` / ``Occupied``, H03.FR.10): it
  stays stored but holds no EVSE, ends when the transaction starts (``reservationId`` in the ``Started`` event,
  H01.FR.15) and holds its EVSE again when the token is dropped (failed authorization, EVConnectionTimeOut,
  unplug, Reset, availability change; no ReservationStatusUpdate, the reservation did not end). Stopping the
  charger cancels the expiry timers; the next accepted boot re-arms them. It ends at ``expiryDateTime``
  (ReservationStatusUpdate ``Expired``, H04), by
  CancelReservation (H02, no ReservationStatusUpdate) or when its EVSE (no ``evseId``: the station) becomes
  Faulted / Unavailable (``Removed``, H01.FR.16 / FR.17). A ReservationStatusUpdate goes through the outbox like
  a StatusNotification (dropped at the end of a session, never resent). The expiry timer runs on the wall clock.
  While ReservationCtrlr.Enabled is false the reservations are kept but not used.
- A ``Rejected`` (or ``UnknownConnector``) answer carries ``statusInfo.reasonCode`` with a short simulator code
  (:data:`REASON_UNKNOWN_EVSE` ...); the codes are informative only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from ...charging import power
from ...charging.power import PowerState
from ...charging.tx_data import TxDataBuffer, TxReading, union
from ...rpc import CallError, wait_event
from ...runtime.local_auth import AuthCache, ListUpdate, LocalAuthList, Reservation
from ...runtime.maintenance import FirmwareJob, LogJob, Maintenance
from ...runtime.security import Security
from .. import base as d
from ..base import AuthResult as A
from ..base import DomainStatus as S
from ..station import StationHost, VersionStation
from . import device_model as dm
from . import models as m
from .adapter import (LOCAL_TOKEN_TYPE, Adapter201, charging_profile, charging_profile_type, charging_schedule_type,
                      composite_schedule_type, connector_status, id_token, id_token_info, meter_value)
from .composite import ActiveTransaction, CompositePeriod, composite_schedule, convert, limit_at
from .enums import (BootReasonEnumType, CancelReservationStatusEnumType, ChangeAvailabilityStatusEnumType,
                    ChargingLimitSourceEnumType, ChargingProfileKindEnumType, ChargingProfilePurposeEnumType,
                    ChargingProfileStatusEnumType, ChargingRateUnitEnumType, ChargingStateEnumType,
                    ClearCacheStatusEnumType, ClearChargingProfileStatusEnumType, ConnectorStatusEnumType,
                    FirmwareStatusEnumType, GenericDeviceModelStatusEnumType, GenericStatusEnumType,
                    GetChargingProfileStatusEnumType, IdTokenEnumType, LogStatusEnumType, MessageTriggerEnumType,
                    OperationalStatusEnumType, ReadingContextEnumType, ReasonEnumType, RequestStartStopStatusEnumType,
                    ReservationUpdateStatusEnumType, ReserveNowStatusEnumType, ResetEnumType, ResetStatusEnumType,
                    SendLocalListStatusEnumType, TransactionEventEnumType, TriggerMessageStatusEnumType,
                    TriggerReasonEnumType, UnlockStatusEnumType, UpdateEnumType, UpdateFirmwareStatusEnumType,
                    UploadLogStatusEnumType)
from .enums import (CertificateSignedStatusEnumType, CertificateSigningUseEnumType, DeleteCertificateStatusEnumType,
                    GetCertificateIdUseEnumType, GetInstalledCertificateStatusEnumType, HashAlgorithmEnumType,
                    InstallCertificateStatusEnumType)
from .profiles import (REASON_INVALID, REASON_UNSUPPORTED_RATE_UNIT, ChargingProfile, ChargingSchedule, ClearStatus,
                       InstalledProfile, Limits, ProfileStatus, ProfileStore, SchedulePeriod)

log = logging.getLogger(__name__)

ENERGY, POWER, CURRENT, VOLTAGE, SOC = dm.ENERGY, dm.POWER, dm.CURRENT, dm.VOLTAGE, dm.SOC
POWER_DECIMALS = 1
CURRENT_DECIMALS = 3
SOC_DECIMALS = 1
LOCATION = "Outlet"
SOC_LOCATION = "EV"

#: Connector id of every EVSE (one connector per EVSE, plan §11.1 Q11).
CONNECTOR_ID = 1
#: Maximum number of queued periodic TransactionEvent Updated; the oldest is dropped first.
MAX_TX_QUEUE = 1000

#: Poll step while a Reset waits for the outbox to drain.
RESET_DRAIN_POLL_S = 0.05
#: The firmware steps this station reports (M7): every ``FirmwareStatusEnumType`` step the simulation has.
FIRMWARE_STATES = frozenset(d.FirmwareState) - {d.FirmwareState.IDLE}
#: statusInfo reasonCode of an UpdateFirmware refused while installing (L02.FR.18).
REASON_INSTALLING = "Installing"
#: ``statusInfo.reasonCode`` of a refused remote-control request (see the module docstring).
REASON_UNKNOWN_EVSE = "UnknownEvse"
REASON_OCCUPIED = "Occupied"
REASON_FAULTED = "Faulted"
REASON_UNAVAILABLE = "Unavailable"
REASON_INVALID_PROFILE = REASON_INVALID
REASON_UNKNOWN_TX = "UnknownTransaction"
REASON_NOT_SUPPORTED = "NotSupported"
REASON_NOT_REGISTERED = "NotRegistered"
REASON_REGISTERED = "AlreadyRegistered"
REASON_NO_TX = "NoTransaction"
REASON_INVALID_DURATION = "InvalidDuration"
REASON_RESERVED = "Reserved"
REASON_DISABLED = "Disabled"
REASON_INVALID_EXPIRY = "ExpiryNotInFuture"
REASON_TOO_MANY_ITEMS = "TooManyItems"
REASON_INVALID_LIST = "InvalidList"

PURPOSE = ChargingProfilePurposeEnumType
W = ChargingRateUnitEnumType.w
#: ``chargingLimitSource`` of the profiles the CSMS sets / of the operator's external constraints.
CSMS_LIMIT_SOURCE = ChargingLimitSourceEnumType.cso
EXTERNAL_LIMIT_SOURCE = ChargingLimitSourceEnumType.ems


def _aware(t: Optional[datetime]) -> Optional[datetime]:
    """A naive request time is taken as UTC (like the charging profiles)."""
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def external_profile_id(evse_id: int) -> int:
    """Profile id of the operator's external constraints on ``evse_id``: negative, so it is unlikely to look
    like a CSMS id in a report (the store keeps CSMS and external ids apart anyway)."""
    return -1 - evse_id

T = TriggerReasonEnumType
MT = MessageTriggerEnumType
SE = d.SecurityEventKind
#: statusInfo.reasonCode of a Rejected CertificateSigned (informative).
REASON_INVALID_CERTIFICATE = "InvalidCertificate"
#: TriggerMessage values this station serves; every other one is NotImplemented.
TRIGGERABLE = frozenset({MT.boot_notification, MT.heartbeat, MT.status_notification, MT.meter_values,
                         MT.transaction_event, MT.log_status_notification, MT.firmware_status_notification,
                         MT.sign_charging_station_certificate})
#: idToken types a RequestStartTransaction never authorizes, also with AuthorizeRemoteStart (F01.FR.02).
NOT_AUTHORIZED_TYPES = frozenset({IdTokenEnumType.central, IdTokenEnumType.no_authorization})
_ACCEPTED, _PENDING = d.BootStatus.ACCEPTED.value, d.BootStatus.PENDING.value
def _reservation_view(r: Reservation) -> Dict[str, Any]:
    return {"id": r.id, "connector": r.connector, "id_tag": r.id_token, "parent_id_token": r.parent_id_token,
            "expiry": r.expiry.isoformat()}


_SUSPENSION = {"SuspendedEV": ChargingStateEnumType.suspended_ev, "SuspendedEVSE": ChargingStateEnumType.suspended_evse}
#: Fault-type values of ReasonEnumType an operator may set (see the module docstring).
FAULT_CODES: Tuple[str, ...] = tuple(r.value for r in (
    ReasonEnumType.emergency_stop, ReasonEnumType.ground_fault, ReasonEnumType.overcurrent_fault,
    ReasonEnumType.power_loss, ReasonEnumType.power_quality, ReasonEnumType.other))


@dataclass
class _Tx:
    tx_id: str
    id_token: str
    meter_start_wh: int
    started_at: datetime
    token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE
    remote_start_id: Optional[int] = None
    next_seq: int = 0
    charging_state: Optional[ChargingStateEnumType] = None  # last one reported
    evse_sent: bool = False  # ``evse`` went out in the first event (E01.FR.16)
    deauthorized: bool = False  # the CSMS refused the token, StopTxOnInvalidId false: no energy (E05.FR.02)
    reservation_id: Optional[int] = None  # the reservation this transaction used (H01.FR.15)
    data: Optional[TxDataBuffer] = None  # readings for the Ended meterValue (TxEndedInterval)

    def seq(self) -> int:
        n, self.next_seq = self.next_seq, self.next_seq + 1
        return n


@dataclass
class _Evse:
    id: int
    status: S = S.AVAILABLE
    #: connectorStatus last reported (sent or queued) in this session; ``None`` = not yet.
    reported: Optional[ConnectorStatusEnumType] = None
    plugged: bool = False
    operative: bool = True
    fault: Optional[str] = None
    finishing: bool = False
    pending_tag: Optional[str] = None  # authorized, waiting for the cable
    pending_type: IdTokenEnumType = LOCAL_TOKEN_TYPE
    pending_remote_id: Optional[int] = None  # remoteStartId of a RequestStartTransaction token
    pending_authorizing: bool = False  # AuthorizeRemoteStart: the token is being authorized, a plug waits
    pending_operative: Optional[bool] = None  # ChangeAvailability "Scheduled"
    pending_timer: Optional[asyncio.TimerHandle] = None  # EVConnectionTimeOut of ``pending_tag``
    #: RequestStartTransaction TxProfile of ``pending_tag``: (domain profile, wire model).
    pending_profile: Optional[Tuple[ChargingProfile, m.ChargingProfileType]] = None
    pending_reservation: Optional[int] = None  # the reservation ``pending_tag`` used (H03.FR.09)
    tx: Optional[_Tx] = None
    meter_wh: float = 0.0
    pw: Optional[PowerState] = None
    last_mono: Optional[float] = None
    meter_task: Optional[asyncio.Task] = None


class _Delivery:
    DONE, RETRY, OFFLINE = "done", "retry", "offline"


@dataclass(eq=False)  # identity semantics: items are removed by identity
class _Out:
    what: str
    send: Callable[[], Awaitable[Any]]
    #: TransactionEvent (kept across sessions, retried) vs StatusNotification.
    transactional: bool = False
    #: Periodic Updated: may be dropped by the queue bound.
    droppable: bool = False
    evse: Optional[_Evse] = None
    tx: Optional[_Tx] = None
    req: Optional[m.TransactionEventRequest] = None
    failures: int = 0
    not_before: Optional[float] = None  # monotonic time before which it is not sent (meter.post_tx_delay_s)


class Station201(VersionStation):
    fault_codes = FAULT_CODES

    def __init__(self, host: StationHost, *, strict: bool = False) -> None:
        super().__init__(host, strict=strict)
        cfg = self.cfg = host.cfg
        self.ev, self.supply = host.ev, host.supply
        self._adapter = Adapter201(strict=strict)
        self.evses: Dict[int, _Evse] = {i: _Evse(i) for i in range(1, cfg.connectors + 1)}
        #: Local Authorization List / Authorization Cache / reservations by id (M6).
        self.local_list = LocalAuthList(dm.LOCAL_LIST_MAX)
        self.cache = AuthCache(dm.AUTH_CACHE_MAX)
        self.reservations: Dict[int, Reservation] = {}
        #: Security log / events, CA certificates, client certificate (M8).
        self.security = Security(clock=lambda: self.host.clock(), client_cert_file=cfg.client_cert_file,
                                 client_key_file=cfg.client_key_file, max_certificates=dm.CERTIFICATE_ENTRIES_MAX)
        self.security.record(SE.STARTUP_OF_THE_DEVICE, connected=False)
        #: The Device Model (M4); ``ValueError`` when the charger's ``variables`` do not apply.
        self.dm = dm.build(cfg, message_timeout_s=host.call_timeout, url=getattr(host, "csms_url", "") or "",
                           availability=lambda i: connector_status(self.evses[i].status).value,
                           power_w=self._power_w, profile_count=lambda: len(self.profiles.profiles()),
                           list_entries=lambda: len(self.local_list),
                           certificate_entries=lambda: self.security.certificate_entries)
        self._hb_wake = asyncio.Event()
        self.dm.on_change(dm.HEARTBEAT_INTERVAL, lambda v: self._wake_heartbeat())
        self.dm.on_change(dm.TX_UPDATED_INTERVAL, lambda v: self._restart_meters())
        self.dm.on_change(dm.BASIC_AUTH_PASSWORD, self._password_changed)
        self.dm.on_change(dm.RESERVATION_ENABLED, lambda v: self.host.after_reply(self._refresh_all))
        #: Charging profiles (M5), keyed by EVSE id (0 = the station).
        self.profiles = ProfileStore(cfg.connectors)
        #: (vendorId, messageId or None) -> answer for inbound DataTransfer; default UnknownVendorId.
        self.data_transfer_rules: Dict[Tuple[str, Optional[str]], d.DataTransferResult] = {}
        self._outq: Deque[_Out] = deque()
        self._outq_wake = asyncio.Event()
        self._outq_busy: Optional[_Out] = None
        #: UnlockConnector answers UnlockFailed (an operator / test switch; no lock is modelled).
        self.unlock_fails = False
        #: An OnIdle Reset waits for the transactions to end; kept across sessions (see ``on_online``).
        self._reset_scheduled = False
        #: A Reset is ending the transactions / draining the outbox; cleared when it closes the session.
        self._resetting = False
        #: The station's own availability (ChangeAvailability without EVSE), apart from the EVSEs' (G04).
        self.station_operative = True
        #: Firmware update / log upload (M7).
        self.maint = Maintenance(
            cfg.maintenance, clock=lambda: self.host.clock(), firmware_states=FIRMWARE_STATES,
            send_firmware=self._send_firmware_status, send_log=self._send_log_status,
            busy=lambda: any(e.tx is not None for e in self.evses.values()),
            hold_changed=self._refresh_all,
            reboot=functools.partial(self._do_reset, reason=BootReasonEnumType.firmware_update))
        self._remote: Dict[type, Callable[[Any], m.OcppModel]] = {
            m.RequestStartTransactionRequest: self._request_start,
            m.RequestStopTransactionRequest: self._request_stop,
            m.UnlockConnectorRequest: self._unlock,
            m.ResetRequest: self._reset,
            m.ChangeAvailabilityRequest: self._change_availability,
            m.TriggerMessageRequest: self._trigger,
            m.GetVariablesRequest: self._get_variables,
            m.SetVariablesRequest: self._set_variables,
            m.GetBaseReportRequest: self._get_base_report,
            m.SetChargingProfileRequest: self._set_charging_profile,
            m.ClearChargingProfileRequest: self._clear_charging_profile,
            m.GetCompositeScheduleRequest: self._get_composite_schedule,
            m.GetChargingProfilesRequest: self._get_charging_profiles,
            m.SendLocalListRequest: self._send_local_list,
            m.GetLocalListVersionRequest: self._get_local_list_version,
            m.ClearCacheRequest: self._clear_cache,
            m.ReserveNowRequest: self._reserve_now,
            m.CancelReservationRequest: self._cancel_reservation,
            m.UpdateFirmwareRequest: self._update_firmware,
            m.GetLogRequest: self._get_log,
            m.CertificateSignedRequest: self._certificate_signed,
            m.InstallCertificateRequest: self._install_certificate,
            m.DeleteCertificateRequest: self._delete_certificate,
            m.GetInstalledCertificateIdsRequest: self._get_installed_certificate_ids,
        }

    # ================================================================== session (called by the shell)

    @property
    def adapter(self) -> Adapter201:
        return self._adapter

    @property
    def connector_count(self) -> int:
        return len(self.evses)

    async def on_online(self) -> None:
        # Bring every state up to date first (a transaction may have ended while offline), then report.
        # ``reported`` is set for every EVSE before the first await, so a refresh meanwhile queues no duplicate.
        for e in self.evses.values():
            e.status = self._target(e)
            e.reported = connector_status(e.status)
        for e in self.evses.values():
            await self.host.safe(self._adapter.send_connector_status(self._status_event(e)), "StatusNotification")
        self._adapter.boot_reason = BootReasonEnumType.power_up  # a RemoteReset boot has been accepted
        self.host.spawn(self._outbox_worker())
        self.host.start_ticks()
        for e in self.evses.values():
            if e.tx is not None:
                self._start_meter(e)
        self._maybe_idle_reset()  # an OnIdle Reset whose last transaction ended while offline
        for r in list(self.reservations.values()):
            if r.timer is None:  # cancelled by close(): the charger was stopped and started again
                self._arm(r)
        self.maint.on_online()
        self.security.confirm()  # A02.FR.10
        self.host.spawn(self.security.deliver(self._adapter.send_security_event))

    def on_connect_failed(self, kind: str, message: str) -> None:
        files = self.security.connect_failed(kind, message)
        if files is not None:  # A02.FR.28: back to the previous client certificate
            self.host.use_client_certificate(*files, reconnect=False)

    def on_session_ended(self) -> None:
        for e in self.evses.values():
            e.meter_task = None  # cancelled with the other session tasks
            e.reported = None
        for item in [x for x in self._outq if not x.transactional]:
            self._outq.remove(item)

    @property
    def heartbeat_interval_s(self) -> int:
        """OCPPCommCtrlr.HeartbeatInterval (0 = no heartbeats)."""
        return self.dm.int_value(dm.HEARTBEAT_INTERVAL)

    @property
    def tx_updated_interval_s(self) -> int:
        """SampledDataCtrlr.TxUpdatedInterval (0 = no periodic Updated)."""
        return self.dm.int_value(dm.TX_UPDATED_INTERVAL)

    async def wait_heartbeat(self) -> bool:
        interval = self.heartbeat_interval_s
        return await wait_event(self._hb_wake, interval if interval > 0 else None)

    def set_heartbeat_interval(self, seconds: int) -> None:
        self.dm.put(dm.HEARTBEAT_INTERVAL, str(seconds))
        self._wake_heartbeat()

    def _wake_heartbeat(self) -> None:
        self._hb_wake.set()  # broadcast to the waiting loop, then re-arm
        self._hb_wake = asyncio.Event()

    def snapshot(self) -> Dict[str, Any]:
        def evse(e: _Evse) -> Dict[str, Any]:
            tx = None
            if e.tx is not None:
                tx = {"tx_id": e.tx.tx_id, "id_tag": e.tx.id_token, "meter_start_wh": e.tx.meter_start_wh,
                      "started_at": e.tx.started_at.isoformat(), "seq_no": e.tx.next_seq,
                      "charging_state": e.tx.charging_state.value if e.tx.charging_state else None}
            r = self._specific(e)
            return {"id": e.id, "evse_id": e.id, "connector_id": CONNECTOR_ID, "status": e.status.value,
                    "connector_status": connector_status(e.status).value, "error_code": e.fault,
                    "plugged": e.plugged, "operative": self._operative(e), "pending_operative": e.pending_operative,
                    "pending_tag": e.pending_tag, "meter_wh": round(e.meter_wh, 3),
                    "power_w": round(e.pw.power_w, POWER_DECIMALS) if e.pw is not None else 0.0,
                    "soc_pct": round(e.pw.soc_pct, SOC_DECIMALS) if e.pw is not None else None,
                    "transaction": tx, "reservation": _reservation_view(r) if r is not None else None}

        return {
            "tx_queue": sum(1 for x in self._outq if x.transactional),
            "local_list_version": self.local_list.version,
            "reservations": [_reservation_view(r) for r in self.reservations.values()],
            # 2.0.1 has no station-level status; the 1.6-shaped key keeps the Web UI working.
            "station": {"status": S.AVAILABLE.value if self.station_operative else S.UNAVAILABLE.value,
                        "operative": self.station_operative},
            "connectors": [evse(e) for e in self.evses.values()],
            **self.maint.snapshot(),
            "security": self.security.snapshot(),
        }

    def configuration(self) -> List[d.ConfigEntry]:
        return []  # no 1.6 configuration keys: see variables()

    def variables(self) -> List[Dict[str, Any]]:
        return self.dm.view()

    # ================================================================== outbox

    def _push(self, item: _Out) -> None:
        """Queue ``item`` (synchronous: never lost to a cancelled caller)."""
        if item.droppable:
            periodic = [x for x in self._outq if x.droppable and x is not self._outq_busy]
            if len(periodic) >= MAX_TX_QUEUE:
                self._outq.remove(periodic[0])
                log.warning("%s: outbox full (%d periodic TransactionEvents), oldest dropped", self.cfg.id,
                            MAX_TX_QUEUE)
        self._outq.append(item)
        self._outq_wake.set()

    async def _outbox_worker(self) -> None:
        """Session task: send queued messages strictly in order; stops (head kept) when the connection is lost."""
        while True:
            while self._outq:
                item = self._outq[0]
                self._outq_busy = item
                try:
                    if item.failures:
                        await asyncio.sleep(self.dm.int_value(dm.MESSAGE_ATTEMPT_INTERVAL, dm.INSTANCE_TX_EVENT)
                                            * item.failures)
                    if item.not_before is not None:
                        await asyncio.sleep(max(0.0, item.not_before - self.host.monotonic()))
                    delivery, value = await self._deliver(item)
                finally:
                    self._outq_busy = None
                if delivery == _Delivery.OFFLINE:
                    self.host.abort_session()  # a stalled outbox never coexists with a live session
                    return
                if delivery == _Delivery.DONE:
                    if item in self._outq:
                        self._outq.remove(item)
                    if value is not None and not self._apply_answer(item, value):
                        return
            self._outq_wake.clear()
            await self._outq_wake.wait()

    async def _deliver(self, item: _Out) -> Tuple[str, Any]:
        try:
            return _Delivery.DONE, await item.send()
        except asyncio.CancelledError:
            raise
        except ConnectionError as e:
            log.info("%s: %s kept queued (%r)", self.cfg.id, item.what, e)
            return _Delivery.OFFLINE, None
        except (CallError, TimeoutError, ValueError) as e:  # the CSMS failed to process it
            if not item.transactional:
                log.warning("%s: %s failed: %r", self.cfg.id, item.what, e)
                return _Delivery.DONE, None
            item.failures += 1
            attempts = self.dm.int_value(dm.MESSAGE_ATTEMPTS, dm.INSTANCE_TX_EVENT)
            if item.failures >= attempts:
                log.warning("%s: %s discarded after %d failed attempts: %r", self.cfg.id, item.what,
                            item.failures, e)
                return _Delivery.DONE, None
            log.warning("%s: %s failed (%d/%d): %r", self.cfg.id, item.what, item.failures, attempts, e)
            return _Delivery.RETRY, None
        except Exception:  # noqa: BLE001 - a local bug, not the CSMS: drop the item, keep the worker alive
            log.exception("%s: %s dropped (internal error)", self.cfg.id, item.what)
            return _Delivery.DONE, None

    def _apply_answer(self, item: _Out, res: Any) -> bool:
        """Apply a TransactionEvent answer; on a local bug end the session and return False."""
        try:
            if isinstance(res, m.TransactionEventResponse):
                self._on_tx_answer(item, res)
            return True
        except Exception as e:  # noqa: BLE001 - fail loudly, restart the session
            log.exception("%s: applying the %s answer failed", self.cfg.id, item.what)
            self.host.abort_session(repr(e))
            return False

    def _on_tx_answer(self, item: _Out, res: m.TransactionEventResponse) -> None:
        """A CSMS that does not accept the token of a running transaction ends it (TxCtrlr.StopTxOnInvalidId)."""
        e, tx, req = item.evse, item.tx, item.req
        if res.id_token_info is not None and req is not None and req.id_token is not None:
            self._remember(req.id_token.id_token, id_token_info(res.id_token_info))  # C10.FR.05
        if res.id_token_info is None or e is None or tx is None or req is None or req.id_token is None:
            return
        status = res.id_token_info.status.value
        if status == A.ACCEPTED.value or e.tx is not tx:
            return
        if self.dm.bool_value(dm.STOP_TX_ON_INVALID_ID):
            log.info("%s: EVSE %d token %s answered %s: transaction %s ends", self.cfg.id, e.id, tx.id_token,
                     status, tx.tx_id)
            self._end_tx(e, T.deauthorized, ReasonEnumType.de_authorized)
        elif not tx.deauthorized:  # E05.FR.02: stop the energy transfer, keep the transaction
            log.info("%s: EVSE %d token %s answered %s: transaction %s suspended", self.cfg.id, e.id, tx.id_token,
                     status, tx.tx_id)
            self._advance(e)  # up to now at the old limit
            tx.deauthorized = True
            self._advance(e)  # zero-length step at 0 W: SuspendedEVSE
            self._refresh(e)
            self._report_charging_state(e)

    # ================================================================== status

    def _operative(self, e: _Evse) -> bool:
        """The EVSE is operative: its own and the station's availability (a running transaction keeps a station
        ``Inoperative`` scheduled for it, see ChangeAvailability)."""
        return (e.operative and (self.station_operative or e.tx is not None)
                and not (self.maint.hold and e.tx is None))  # L02.FR.04: held Unavailable for a firmware install

    def _target(self, e: _Evse) -> S:
        if e.fault:
            return S.FAULTED
        if not self._operative(e):
            return S.UNAVAILABLE
        if e.tx is not None:
            return S(e.pw.suspension) if e.pw is not None and e.pw.suspension else S.CHARGING
        if e.plugged:
            return S.FINISHING if e.finishing else S.PREPARING
        if self._specific(e) is not None or self._held_for_zero(e):
            return S.RESERVED
        return S.AVAILABLE

    def _status_event(self, e: _Evse) -> d.ConnectorStatus:
        return d.ConnectorStatus(e.id, CONNECTOR_ID, e.status, timestamp=self.host.clock())

    def _refresh(self, e: _Evse) -> None:
        """Move ``e`` to its target state; when online and the connectorStatus changed, queue the
        StatusNotification (never waits). With reservations without ``evseId`` the other EVSEs may change too."""
        self._refresh_one(e)
        if self._general():
            self._refresh_all(skip=e)

    def _refresh_all(self, skip: Optional[_Evse] = None) -> None:
        for e in self.evses.values():
            if e is not skip:
                self._refresh_one(e)

    def _refresh_one(self, e: _Evse) -> None:
        e.status = self._target(e)
        wire = connector_status(e.status)
        if self.host.online and wire != e.reported:
            e.reported = wire
            ev = self._status_event(e)  # captured now: the state may change before it is sent
            self._push(_Out("StatusNotification", lambda: self._adapter.send_connector_status(ev)))

    # ================================================================== readings / power model

    def _samples(self, e: _Evse, measurands: Sequence[str]) -> List[d.Sample]:
        """Readings of ``measurands``; Voltage / Current.Import once without a phase unless
        ``meter.phase_values`` asks for one value per phase (SampledValue.phase)."""
        r = power.meter_reading(e.pw, self.ev, self.supply) if e.pw is not None else None
        watts = r.power_active_import_w if r else 0.0
        amps = r.current_import_a if r else 0.0
        mode = self.cfg.meter.phase_values
        out: List[d.Sample] = []
        for name in measurands:
            if mode != "off" and name == VOLTAGE:
                out += [d.Sample(name, v, "V", phase=p, location=LOCATION)
                        for p, v in power.phase_voltages(self.supply, mode)]
                continue
            if mode != "off" and name == CURRENT:
                used = r.phases if r else self.supply.phases_used(self.ev)
                out += [d.Sample(name, round(a, CURRENT_DECIMALS), "A", phase=p, location=LOCATION)
                        for p, a in power.phase_currents(amps, self.supply, used)]
                continue
            if name == ENERGY:
                out.append(d.Sample(name, round(e.meter_wh), "Wh", location=LOCATION))
            elif name == POWER:
                out.append(d.Sample(name, round(watts, POWER_DECIMALS), "W", location=LOCATION))
            elif name == CURRENT:
                out.append(d.Sample(name, round(amps, CURRENT_DECIMALS), "A", location=LOCATION))
            elif name == VOLTAGE:
                out.append(d.Sample(name, self.supply.voltage_v, "V", location=LOCATION))
            elif name == SOC and r is not None:
                out.append(d.Sample(name, round(r.soc_pct, SOC_DECIMALS), "Percent", location=SOC_LOCATION))
        return out

    def _power_w(self, evse_id: int) -> float:
        """Live active power of an EVSE (EVSE.Power); 0 when it does not charge."""
        e = self.evses[evse_id]
        if e.pw is None:
            return 0.0
        return round(power.meter_reading(e.pw, self.ev, self.supply).power_active_import_w, POWER_DECIMALS)

    def _phases(self, evse_id: int) -> int:
        """Phases a limit in A applies to: the EV's while one charges, else the supply's."""
        e = self.evses.get(evse_id)
        return self.supply.phases_used(self.ev) if e is not None and e.pw is not None else self.supply.phases

    def _transactions(self) -> Dict[int, ActiveTransaction]:
        return {e.id: ActiveTransaction(e.tx.tx_id, e.tx.started_at) for e in self.evses.values() if e.tx is not None}

    def _limit_w(self, e: _Evse) -> Optional[float]:
        if e.tx is not None and e.tx.deauthorized:
            return 0.0
        return limit_at(self.profiles, e.id, self.host.clock(), transactions=self._transactions(),
                        number_phases=self._phases(e.id), voltage=self.supply.phase_voltage_v())

    def _advance(self, e: _Evse) -> None:
        """Step the power model of a charging EVSE up to now and restart the interval."""
        now = self.host.monotonic()
        if e.pw is not None and not self.host.paused:  # paused: frozen, the interval just restarts
            dt = max(0.0, now - e.last_mono) if e.last_mono is not None else 0.0
            e.pw = self.host.step_power(e.pw, dt, self._limit_w(e))
            e.meter_wh = e.pw.energy_wh
        e.last_mono = now

    def settle(self) -> None:
        for e in self.evses.values():
            self._advance(e)

    def tick(self) -> None:
        for e in self.evses.values():
            if e.pw is not None:
                self._advance(e)
                self._refresh(e)
                self._report_charging_state(e)
            self._collect_tx_data(e)

    def _ended_measurands(self) -> Tuple[str, ...]:
        """SampledDataCtrlr ∪ AlignedDataCtrlr TxEndedMeasurands: the start / end readings of Ended."""
        return union(self.dm.list_value(dm.TX_ENDED_MEASURANDS), self.dm.list_value(dm.ALIGNED_TX_ENDED_MEASURANDS))

    def _collect_tx_data(self, e: _Evse) -> None:
        """Take the due readings for the Ended meterValue (Sampled / Aligned TxEndedInterval)."""
        tx = e.tx
        if tx is None or tx.data is None:
            return
        data, evicted = tx.data, tx.data.evicted
        at = data.sampled_due(self.host.monotonic(), self.dm.int_value(dm.TX_ENDED_INTERVAL))
        if at is not None:
            data.add_sampled(TxReading(at, self._samples(e, self.dm.list_value(dm.TX_ENDED_MEASURANDS)),
                                       ReadingContextEnumType.sample_periodic.value))
        at = data.aligned_due(self.host.clock(), self.dm.int_value(dm.ALIGNED_TX_ENDED_INTERVAL))
        if at is not None:
            data.add_aligned(TxReading(at, self._samples(e, self.dm.list_value(dm.ALIGNED_TX_ENDED_MEASURANDS)),
                                       ReadingContextEnumType.sample_clock.value))
        if data.evicted and not evicted:
            log.warning("%s: EVSE %d: Ended meterValue exceeds meter.tx_data_max_samples (%d), oldest readings "
                        "dropped", self.cfg.id, e.id, data.max_readings)

    @staticmethod
    def _charging_state(e: _Evse) -> ChargingStateEnumType:
        if e.tx is not None and e.pw is not None:
            return _SUSPENSION.get(e.pw.suspension or "", ChargingStateEnumType.charging)
        return ChargingStateEnumType.ev_connected if e.plugged else ChargingStateEnumType.idle

    def _report_charging_state(self, e: _Evse) -> None:
        tx = e.tx
        if tx is None:
            return
        state = self._charging_state(e)
        if state != tx.charging_state:
            tx.charging_state = state
            self._queue_event(e, tx, TransactionEventEnumType.updated, T.charging_state_changed,
                              m.TransactionType(transaction_id=tx.tx_id, charging_state=state))

    # ================================================================== transaction events

    def _queue_event(self, e: _Evse, tx: _Tx, event: TransactionEventEnumType, trigger: TriggerReasonEnumType,
                     info: m.TransactionType, *, samples: Sequence[d.Sample] = (),
                     context: Optional[ReadingContextEnumType] = None, token: Optional[str] = None,
                     token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE, droppable: bool = False,
                     reservation_id: Optional[int] = None, readings: Optional[Sequence[TxReading]] = None,
                     not_before: Optional[float] = None) -> None:
        """Queue a TransactionEvent; ``readings`` (Ended) replaces the one ``samples`` meterValue."""
        now = self.host.clock()
        if readings is not None:
            values = [meter_value(r.timestamp, r.samples, r.context) for r in readings if r.samples] or None
        else:
            values = [meter_value(now, samples, context.value)] if samples and context else None
        req = m.TransactionEventRequest(
            event_type=event, timestamp=now, trigger_reason=trigger, seq_no=tx.seq(),
            offline=True if not self.host.connected else None, transaction_info=info,
            evse=None if tx.evse_sent else m.EVSEType(id=e.id, connector_id=CONNECTOR_ID),
            id_token=id_token(token, token_type) if token is not None else None, reservation_id=reservation_id,
            meter_value=values)
        tx.evse_sent = True
        self._push(_Out(f"TransactionEvent {event.value}", lambda: self._adapter.send_transaction_event(req),
                        transactional=True, droppable=droppable, evse=e, tx=tx, req=req, not_before=not_before))

    def _start_tx(self, e: _Evse, tag: str, trigger: TriggerReasonEnumType, *,
                  token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE, remote_start_id: Optional[int] = None,
                  profile: Optional[Tuple[ChargingProfile, m.ChargingProfileType]] = None,
                  reservation: Optional[Reservation] = None) -> None:
        """Start a transaction on a plugged, operative EVSE for an authorized ``tag`` (synchronous); ``profile``
        is the RequestStartTransaction TxProfile, installed bound to the new transaction; ``reservation`` the one
        the token uses now (a waiting token brings its ``pending_reservation``)."""
        self._claim(e, reservation)
        reservation_id = self._consume(e)
        self._clear_pending(e)
        e.finishing = False
        tx = e.tx = _Tx(str(uuid.uuid4()), tag, int(round(e.meter_wh)), self.host.clock(), token_type=token_type,
                        remote_start_id=remote_start_id, reservation_id=reservation_id)
        e.pw = PowerState(soc_pct=self.cfg.session.initial_soc, energy_wh=e.meter_wh)  # a fresh EV session
        e.last_mono = None
        if profile is not None:
            self._bind_profile(e, tx, *profile)
        self._advance(e)  # zero-length step: initial power and suspension
        tx.data = TxDataBuffer(self.host.monotonic(), tx.started_at, self.cfg.meter.tx_data_max_samples,
                               begin=TxReading(tx.started_at, self._samples(e, self._ended_measurands()),
                                               ReadingContextEnumType.transaction_begin.value))
        tx.charging_state = self._charging_state(e)
        self._queue_event(e, tx, TransactionEventEnumType.started, trigger,
                          m.TransactionType(transaction_id=tx.tx_id, charging_state=tx.charging_state,
                                            remote_start_id=remote_start_id),
                          samples=self._samples(e, self.dm.list_value(dm.TX_STARTED_MEASURANDS)),
                          context=ReadingContextEnumType.transaction_begin,
                          token=tag, token_type=token_type, reservation_id=reservation_id)
        self._refresh(e)
        if self.host.online:
            self._start_meter(e)

    def _end_tx(self, e: _Evse, trigger: TriggerReasonEnumType, reason: ReasonEnumType, *,
                token: Optional[str] = None) -> None:
        """End the running transaction of ``e``; never waits for the CSMS."""
        tx = e.tx
        if tx is None:
            return
        self._advance(e)
        self._collect_tx_data(e)  # a reading due right now is not lost to the tick
        end = TxReading(self.host.clock(), self._samples(e, self._ended_measurands()),
                        ReadingContextEnumType.transaction_end.value)
        readings = tx.data.all(end) if tx.data is not None else [end]
        tx.data = None
        delay = self.cfg.meter.post_tx_delay_s
        if e.meter_task is not None and e.meter_task is not asyncio.current_task():
            e.meter_task.cancel()
        e.meter_task = None
        e.tx, e.pw, e.last_mono, e.finishing = None, None, None, e.plugged
        self.profiles.end_transaction(e.id)
        self._queue_event(e, tx, TransactionEventEnumType.ended, trigger,
                          m.TransactionType(transaction_id=tx.tx_id, charging_state=self._charging_state(e),
                                            stopped_reason=reason),
                          readings=readings, token=token,
                          not_before=self.host.monotonic() + delay if delay > 0 else None)
        if e.pending_operative is not None:  # a Scheduled ChangeAvailability takes effect now
            self._set_operative(e, e.pending_operative)
        self._refresh(e)
        self._maybe_idle_reset()
        self.maint.wake()

    def _restart_meters(self) -> None:
        """TxUpdatedInterval changed: the running meter loops pick it up after their current wait; start the
        missing ones (the interval was 0)."""
        if self.host.online:
            for e in self.evses.values():
                if e.tx is not None:
                    self._start_meter(e)

    def _start_meter(self, e: _Evse) -> None:
        if self.tx_updated_interval_s > 0 and (e.meter_task is None or e.meter_task.done()):
            e.meter_task = self.host.spawn(self._meter_loop(e))

    async def _meter_loop(self, e: _Evse) -> None:
        while e.tx is not None:
            await asyncio.sleep(self.tx_updated_interval_s)
            tx = e.tx
            if tx is None:
                return
            if self.tx_updated_interval_s <= 0:
                return  # switched off meanwhile (SetVariables)
            self._advance(e)
            samples = self._samples(e, self.dm.list_value(dm.TX_UPDATED_MEASURANDS))
            if not samples:
                continue  # TxUpdatedMeasurands is empty: nothing to report
            self._queue_event(e, tx, TransactionEventEnumType.updated, T.meter_value_periodic,
                              m.TransactionType(transaction_id=tx.tx_id), samples=samples,
                              context=ReadingContextEnumType.sample_periodic, droppable=True)

    # ================================================================== authorization

    async def _authorize(self, tag: str) -> A:
        return (await self._authorize_info(tag))[0].status

    def _known(self, tag: str) -> Tuple[Optional[d.IdTokenInfo], Optional[d.IdTokenInfo]]:
        """``(list entry, cache entry)`` of ``tag``: the Local Authorization List entry (when enabled) has
        priority, the cache (when enabled) is looked up only without one (C13.FR.01)."""
        if self.dm.bool_value(dm.LOCAL_LIST_ENABLED):
            entry = self.local_list.get(tag)
            if entry is not None:
                return entry, None
        if self.dm.bool_value(dm.AUTH_CACHE_ENABLED):
            return None, self.cache.get(tag, self.host.clock(), self.dm.int_value(dm.AUTH_CACHE_LIFETIME))
        return None, None

    def _remember(self, tag: str, info: Optional[d.IdTokenInfo]) -> None:
        """C10.FR.04 / FR.05: a received IdTokenInfo updates the cache; a token in the Local Authorization List is
        not cached."""
        if info is not None and self.dm.bool_value(dm.AUTH_CACHE_ENABLED) and self.local_list.get(tag) is None:
            self.cache.update(tag, info, self.host.clock())

    async def _authorize_info(self, tag: str) -> Tuple[d.IdTokenInfo, bool]:
        """Authorize a token by the AuthCtrlr variables, the Local Authorization List and the cache (module
        docstring; ``id_tags`` are accepted as before): ``(info, asked)``, ``asked`` = a CSMS answer."""
        accepted = d.IdTokenInfo(A.ACCEPTED)
        if not self.dm.bool_value(dm.AUTH_ENABLED):
            return accepted, False
        local = tag in self.cfg.id_tags
        listed, cached = self._known(tag)
        known = listed or cached
        if self.dm.bool_value(dm.DISABLE_REMOTE_AUTHORIZATION):
            if local:
                return accepted, False
            return (known if known is not None else d.IdTokenInfo(A.INVALID)), False
        if not self.host.online:
            if self.dm.bool_value(dm.LOCAL_AUTHORIZE_OFFLINE):
                if local:
                    return accepted, False
                if known is not None:
                    return known, False  # C13.FR.02: only an Accepted entry authorizes
            if known is None and self.dm.bool_value(dm.OFFLINE_TX_FOR_UNKNOWN_ID):  # C13.FR.04
                return accepted, False
            return d.IdTokenInfo(A.OFFLINE), False
        pre = self.dm.bool_value(dm.LOCAL_PRE_AUTHORIZE)
        if local and pre:
            return accepted, False
        if known is not None:
            no_post = self.dm.bool_value(dm.LOCAL_LIST_DISABLE_POST_AUTHORIZE if listed is not None
                                         else dm.AUTH_CACHE_DISABLE_POST_AUTHORIZE)
            if (known.accepted and pre) or (not known.accepted and no_post):
                return known, False  # C12 / C14.FR.02; DisablePostAuthorize (Part 2 2.4.6 / 2.5.7)
        info: Optional[d.IdTokenInfo] = await self.host.safe(
            self._adapter.send_authorize(d.AuthorizeRequest(tag)), "Authorize")
        self._remember(tag, info)
        return (info if info is not None else d.IdTokenInfo(A.ERROR)), True

    # ================================================================== reservations (M6)

    def _active(self) -> List[Reservation]:
        """The reservations in use (none while ReservationCtrlr.Enabled is false, Part 2 Controller note)."""
        if not self.dm.bool_value(dm.RESERVATION_ENABLED):
            return []
        return [r for r in self.reservations.values() if r.claimed_by is None]  # a claimed one holds no EVSE

    def _specific(self, e: _Evse) -> Optional[Reservation]:
        return next((r for r in self._active() if r.connector == e.id), None)

    def _general(self) -> List[Reservation]:
        """Reservations without ``evseId``."""
        return [r for r in self._active() if r.connector == 0]

    def _free(self, e: _Evse) -> bool:
        """``e`` could serve a reservation without ``evseId`` now: no fault, operative, no transaction, no waiting
        token, not reserved itself. A plugged cable does not count (module docstring)."""
        return (not e.fault and self._operative(e) and e.tx is None and e.pending_tag is None
                and self._specific(e) is None)

    def _held_for_zero(self, e: _Evse) -> bool:
        """``e`` is free and the free EVSEs are no more than the reservations without ``evseId`` (H01.FR.20)."""
        n = len(self._general())
        return n > 0 and self._free(e) and sum(1 for x in self.evses.values() if self._free(x)) <= n

    def _reservation_for(self, e: _Evse, tag: str, group: Optional[str]) -> Tuple[bool, Optional[Reservation]]:
        """May ``tag`` (of ``group``) start on ``e``; the reservation it uses, if any (H03)."""
        r = self._specific(e)
        if r is not None:
            return (True, r) if r.matches(tag, group) else (False, None)
        general = self._general()
        mine = next((x for x in general if x.matches(tag, group)), None)
        if mine is not None:
            return True, mine
        # H01.FR.07 "one EVSE remains available": refused only when this start takes the last free EVSE(s).
        left = sum(1 for x in self.evses.values() if self._free(x)) - (1 if self._free(e) else 0)
        return left >= len(general), None

    def _group_may_help(self, e: _Evse) -> bool:
        r = self._specific(e)
        return any(x.parent_id_token for x in ([r] if r is not None else self._general()))

    async def _group_of(self, tag: str, info: d.IdTokenInfo, asked: bool) -> Optional[str]:
        """The token's groupIdToken: from its authorization, the list or the cache, else an AuthorizeRequest
        (H03.FR.07 / FR.08) unless the CSMS was asked already or may not be asked."""
        if info.parent_id_token:
            return info.parent_id_token
        listed, cached = self._known(tag)
        known = listed or cached
        if known is not None and known.parent_id_token:
            return known.parent_id_token
        if (asked or not self.host.online or not self.dm.bool_value(dm.AUTH_ENABLED)
                or self.dm.bool_value(dm.DISABLE_REMOTE_AUTHORIZATION)):
            return None
        res: Optional[d.IdTokenInfo] = await self.host.safe(
            self._adapter.send_authorize(d.AuthorizeRequest(tag)), "Authorize")
        self._remember(tag, res)
        return res.parent_id_token if res is not None else None

    def _claim(self, e: _Evse, r: Optional[Reservation]) -> None:
        """The token waiting on ``e`` claims ``r`` (H03.FR.09 / FR.10); consumed when the transaction starts
        (:meth:`_consume`), released when the token is dropped (:meth:`_release`)."""
        if r is None or self.reservations.get(r.id) is not r:
            return
        r.claimed_by, e.pending_reservation = e.id, r.id
        self._refresh_all(skip=e)  # ``e`` itself is refreshed by the caller

    def _release(self, e: _Evse) -> None:
        """The waiting token of ``e`` is dropped: its reservation (if still there) holds its EVSE again."""
        rid, e.pending_reservation = e.pending_reservation, None
        r = self.reservations.get(rid) if rid is not None else None
        if r is not None and r.claimed_by == e.id:
            r.claimed_by = None

    def _consume(self, e: _Evse) -> Optional[int]:
        """The transaction on ``e`` starts: its claimed reservation ends; its id for the Started event."""
        rid, e.pending_reservation = e.pending_reservation, None
        r = self.reservations.get(rid) if rid is not None else None
        if r is None or r.claimed_by != e.id:
            return None  # expired / cancelled / removed meanwhile
        del self.reservations[r.id]
        r.cancel_timer()
        return rid

    def _arm(self, r: Reservation) -> None:
        r.cancel_timer()
        delay = (r.expiry - self.host.clock()).total_seconds()
        r.timer = asyncio.get_running_loop().call_later(max(0.0, delay), self._reservation_expired, r)

    def close(self) -> None:
        """The charger stops: cancel the reservation expiry and EVConnectionTimeOut timers (the reservation
        timers are re-armed by :meth:`on_online`)."""
        for r in self.reservations.values():
            r.cancel_timer()
        for e in self.evses.values():
            if e.pending_timer is not None:
                e.pending_timer.cancel()
                e.pending_timer = None
        self.maint.close()
        files = self.security.close()  # renewed certificates do not outlive the run (runtime.security)
        if files is not None and self.cfg.client_cert_file is not None:
            self.host.use_client_certificate(*files, reconnect=False)

    def _drop_reservation(self, r: Reservation, update: Optional[ReservationUpdateStatusEnumType]) -> None:
        """End ``r``; ``update`` = the ReservationStatusUpdate to send (before the StatusNotifications)."""
        if self.reservations.get(r.id) is not r:
            return
        del self.reservations[r.id]
        r.cancel_timer()
        log.info("%s: reservation %d ended (%s)", self.cfg.id, r.id, update.value if update else "cancelled")
        if update is not None and self.host.online:
            req = m.ReservationStatusUpdateRequest(reservation_id=r.id, reservation_update_status=update)
            self._push(_Out("ReservationStatusUpdate", lambda: self._adapter.send_reservation_status_update(req)))
        self._refresh_all()

    def _drop_reservations_of(self, evses: Sequence[_Evse], *, station: bool = False) -> None:
        """H01.FR.16 / FR.17: the EVSE (no ``evseId``: the station) became Faulted or Unavailable."""
        ids = {e.id for e in evses} | ({0} if station else set())
        for r in [r for r in self.reservations.values() if r.connector in ids]:
            self._drop_reservation(r, ReservationUpdateStatusEnumType.removed)

    def _reservation_expired(self, r: Reservation) -> None:
        r.timer = None
        self._drop_reservation(r, ReservationUpdateStatusEnumType.expired)  # H04.FR.01

    def _reserve_now(self, req: m.ReserveNowRequest) -> m.ReserveNowResponse:
        RS = ReserveNowStatusEnumType

        def answer(status: ReserveNowStatusEnumType, code: Optional[str] = None) -> m.ReserveNowResponse:
            return m.ReserveNowResponse(status=status, status_info=self._info(code))

        if not self.dm.bool_value(dm.RESERVATION_ENABLED):
            return answer(RS.rejected, REASON_DISABLED)  # H01.FR.01
        if req.expiry_date_time <= self.host.clock():
            return answer(RS.rejected, REASON_INVALID_EXPIRY)
        if req.evse_id is None:
            if not self.dm.bool_value(dm.RESERVATION_NON_EVSE_SPECIFIC):
                return answer(RS.rejected, REASON_NOT_SUPPORTED)  # H01.FR.19
            connector = 0
        elif req.evse_id in self.evses:
            connector = req.evse_id
        else:
            return answer(RS.rejected, REASON_UNKNOWN_EVSE)
        old = self.reservations.pop(req.id, None)  # checked as if it did not exist (H01.FR.02)
        status = self._reservation_status(connector)
        if status is not RS.accepted:
            if old is not None:
                self.reservations[old.id] = old
            return answer(status)
        if old is not None:
            old.cancel_timer()
        group = req.group_id_token.id_token if req.group_id_token is not None else None
        r = Reservation(req.id, connector, req.id_token.id_token, req.expiry_date_time, group)
        self._arm(r)
        self.reservations[r.id] = r
        self.host.after_reply(self._refresh_all)  # Reserved after the answer (H01.FR.20 / FR.23)
        return answer(RS.accepted)

    def _reservation_status(self, connector: int) -> ReserveNowStatusEnumType:
        RS = ReserveNowStatusEnumType
        if connector == 0:
            evses = list(self.evses.values())
            if sum(1 for e in evses if self._free(e)) > len(self._general()):
                return RS.accepted
            if all(e.fault for e in evses):
                return RS.faulted  # H01.FR.12
            if all(not self._operative(e) for e in evses):
                return RS.unavailable  # H01.FR.14
            return RS.occupied  # H01.FR.11
        e = self.evses[connector]
        if e.fault:
            return RS.faulted
        if not self._operative(e):
            return RS.unavailable
        if e.plugged or not self._free(e) or self._held_for_zero(e):
            return RS.occupied
        return RS.accepted

    def _cancel_reservation(self, req: m.CancelReservationRequest) -> m.CancelReservationResponse:
        r = self.reservations.pop(req.reservation_id, None)
        if r is None:
            return m.CancelReservationResponse(status=CancelReservationStatusEnumType.rejected)  # H02.FR.01
        r.cancel_timer()
        self.host.after_reply(self._refresh_all)  # H02.FR.04: Available after the answer
        return m.CancelReservationResponse(status=CancelReservationStatusEnumType.accepted)

    # ================================================================== local authorization list / cache (M6)

    def _send_local_list(self, req: m.SendLocalListRequest) -> m.SendLocalListResponse:
        def failed(code: str) -> m.SendLocalListResponse:
            return m.SendLocalListResponse(status=SendLocalListStatusEnumType.failed, status_info=self._info(code))

        if req.version_number < 1:  # D01.FR.18
            raise CallError(self._adapter.error_codes.property_constraint_violation,
                            f"versionNumber: {req.version_number} must be > 0", {"field": "versionNumber"})
        if not self.dm.bool_value(dm.LOCAL_LIST_ENABLED):
            return failed(REASON_DISABLED)
        items = req.local_authorization_list or []
        if len(items) > self.dm.int_value(dm.LOCAL_LIST_ITEMS_PER_MESSAGE):
            return failed(REASON_TOO_MANY_ITEMS)
        entries = [(x.id_token.id_token, id_token_info(x.id_token_info) if x.id_token_info is not None else None)
                   for x in items]
        res = self.local_list.update(req.version_number, req.update_type is UpdateEnumType.full, entries)
        if res is ListUpdate.FAILED:
            return failed(REASON_INVALID_LIST)
        return m.SendLocalListResponse(status=SendLocalListStatusEnumType(res.value))

    def _get_local_list_version(self, req: m.GetLocalListVersionRequest) -> m.GetLocalListVersionResponse:
        enabled = self.dm.bool_value(dm.LOCAL_LIST_ENABLED)
        return m.GetLocalListVersionResponse(version_number=self.local_list.version if enabled else 0)  # D02

    def _clear_cache(self, req: m.ClearCacheRequest) -> m.ClearCacheResponse:
        if not self.dm.bool_value(dm.AUTH_CACHE_ENABLED):
            return m.ClearCacheResponse(status=ClearCacheStatusEnumType.rejected,
                                        status_info=self._info(REASON_DISABLED))  # C11.FR.04
        self.cache.clear()
        return m.ClearCacheResponse(status=ClearCacheStatusEnumType.accepted)  # C11.FR.03

    # ================================================================== CSMS commands

    async def handle(self, cmd: d.Command) -> Any:
        if isinstance(cmd, d.DataTransfer):
            rule = (self.data_transfer_rules.get((cmd.vendor_id, cmd.message_id))
                    or self.data_transfer_rules.get((cmd.vendor_id, None)))
            return rule or d.DataTransferResult(d.DataTransferStatus.UNKNOWN_VENDOR_ID)
        remote = self._remote.get(type(cmd))
        if remote is not None:
            return remote(cmd)
        return d.CommandStatus.NOT_IMPLEMENTED

    def check_call(self, action: str) -> None:
        """B03 (Table 32): while the registration is Rejected every CSMS CALL is answered ``SecurityError``."""
        if self.host.registration == d.BootStatus.REJECTED.value:
            raise CallError(self._adapter.error_codes.security_error,
                            f"{action}: the BootNotification was Rejected")

    # ================================================================== remote control (M3)

    @staticmethod
    def _info(code: Optional[str]) -> Optional[m.StatusInfoType]:
        return m.StatusInfoType(reason_code=code) if code else None

    def _addressed(self, evse: Optional[m.EVSEType]) -> Optional[List[_Evse]]:
        """EVSEs an optional ``evse`` field addresses: no ``evse`` or EVSE id 0 = every EVSE. ``None`` when it is
        unknown: an EVSE id not on this station, a connector other than 1, or any connector of EVSE 0."""
        if evse is None:
            return list(self.evses.values())
        if evse.id == 0:
            return list(self.evses.values()) if evse.connector_id is None else None
        e = self.evses.get(evse.id)
        if e is None or evse.connector_id not in (None, CONNECTOR_ID):
            return None
        return [e]

    def _start_blocker(self, e: _Evse, token: Optional[str] = None, group: Optional[str] = None) -> Optional[str]:
        """Why a RequestStartTransaction (of ``token`` / ``group``) cannot use ``e`` now (``None`` = it can)."""
        if e.fault:
            return REASON_FAULTED
        if not self._operative(e) or self._resetting:
            return REASON_UNAVAILABLE
        if e.tx is not None or e.pending_tag is not None:  # a waiting token keeps the EVSE
            return REASON_OCCUPIED
        if token is not None and not self._reservation_for(e, token, group)[0]:
            return REASON_RESERVED
        return None

    def _clear_pending(self, e: _Evse) -> None:
        """Drop the waiting token; a reservation it claimed holds its EVSE again (the caller refreshes)."""
        self._release(e)
        e.pending_tag, e.pending_type, e.pending_remote_id = None, LOCAL_TOKEN_TYPE, None
        e.pending_authorizing, e.pending_profile = False, None
        if e.pending_timer is not None:
            e.pending_timer.cancel()
            e.pending_timer = None

    def _arm_connection_timeout(self, e: _Evse) -> None:
        """Drop ``e.pending_tag`` when no cable comes within TxCtrlr.EVConnectionTimeOut (E03 / F02.FR.07)."""
        if e.pending_timer is not None:
            e.pending_timer.cancel()
            e.pending_timer = None
        timeout = self.dm.int_value(dm.EV_CONNECTION_TIMEOUT)
        if timeout > 0 and e.pending_tag is not None:
            e.pending_timer = asyncio.get_running_loop().call_later(timeout, self._connection_timed_out, e,
                                                                    e.pending_tag)

    def _connection_timed_out(self, e: _Evse, tag: str) -> None:
        e.pending_timer = None
        if e.pending_tag != tag or e.tx is not None or e.plugged:
            return  # replaced, started, or plugged while being authorized
        log.info("%s: EVSE %d token %s dropped: no cable within EVConnectionTimeOut", self.cfg.id, e.id, tag)
        self._clear_pending(e)
        self._refresh(e)

    def _set_operative(self, e: _Evse, operative: bool) -> None:
        e.operative, e.pending_operative = operative, None
        if not operative:
            self._clear_pending(e)

    def _request_start(self, req: m.RequestStartTransactionRequest) -> m.RequestStartTransactionResponse:
        def rejected(code: str) -> m.RequestStartTransactionResponse:
            return m.RequestStartTransactionResponse(status=RequestStartStopStatusEnumType.rejected,
                                                     status_info=self._info(code))

        if self.host.registration == _PENDING:  # B02.FR.05, before every other check
            return rejected(REASON_NOT_REGISTERED)
        wire = req.charging_profile
        profile = charging_profile(wire) if wire is not None else None
        # Without evseId the EVSE is not known yet: checked as for EVSE 1 (every EVSE has the same rules).
        if profile is not None:
            reason = self.profiles.reject_reason(req.evse_id or 1, profile, for_remote_start=True,
                                                 limits=self._limits())
            if reason is not None:
                return rejected(reason)
        token = req.id_token.id_token
        group = req.group_id_token.id_token if req.group_id_token is not None else None
        if req.evse_id is not None:
            e = self.evses.get(req.evse_id)
            if e is None:
                return rejected(REASON_UNKNOWN_EVSE)
            blocker = self._start_blocker(e, token, group)
            if blocker:
                return rejected(blocker)
        else:
            free = [x for x in self.evses.values() if self._start_blocker(x, token, group) is None]
            if not free:
                return rejected(REASON_OCCUPIED)
            e = next((x for x in free if x.plugged), free[0])
        # Claimed now (a waiting token blocks the next request); the transaction starts after the answer.
        e.pending_tag, e.pending_type = req.id_token.id_token, req.id_token.type
        e.pending_remote_id = req.remote_start_id
        e.pending_profile = (profile, wire) if profile is not None and wire is not None else None
        self._claim(e, self._reservation_for(e, token, group)[1])  # H03.FR.09, consumed by the start
        # AuthorizeRemoteStart: from now on a plug waits for the authorization (see _remote_authorize); a Central /
        # NoAuthorization token is never authorized (F01.FR.02).
        e.pending_authorizing = (self.dm.bool_value(dm.AUTHORIZE_REMOTE_START)
                                 and req.id_token.type not in NOT_AUTHORIZED_TYPES)
        self._arm_connection_timeout(e)
        self.host.after_reply(functools.partial(self._remote_start_go, e, req.remote_start_id))
        return m.RequestStartTransactionResponse(status=RequestStartStopStatusEnumType.accepted)

    def _remote_start_go(self, e: _Evse, remote_start_id: int) -> None:
        if e.pending_remote_id != remote_start_id or e.pending_tag is None or e.tx is not None:
            return  # replaced, started or dropped meanwhile
        if e.fault or not self._operative(e) or self._resetting:
            self._clear_pending(e)
            self._refresh(e)
            return
        if e.pending_authorizing:
            if self.host.spawn(self._remote_authorize(e, remote_start_id)) is None:
                self._clear_pending(e)  # the session ended meanwhile
                self._refresh(e)
            return
        self._remote_start_now(e)

    def _remote_start_now(self, e: _Evse) -> None:
        if e.plugged and e.pending_tag is not None:
            self._start_tx(e, e.pending_tag, T.remote_start, token_type=e.pending_type,
                           remote_start_id=e.pending_remote_id, profile=e.pending_profile)
        # else the token waits for the cable (plug starts the transaction)

    async def _remote_authorize(self, e: _Evse, remote_start_id: int) -> None:
        """AuthorizeRemoteStart: the token of a RequestStartTransaction is authorized like a local one first."""
        tag = e.pending_tag
        if tag is None:
            return
        status = await self._authorize(tag)
        if e.pending_remote_id != remote_start_id or e.pending_tag != tag or e.tx is not None:
            return  # replaced, started or dropped meanwhile
        if status is not A.ACCEPTED or e.fault or not self._operative(e) or self._resetting:
            log.info("%s: EVSE %d remote start token %s not started (%s)", self.cfg.id, e.id, tag, status.value)
            self._clear_pending(e)
            self._refresh(e)  # a claimed reservation holds its EVSE again
            return
        e.pending_authorizing = False
        self._remote_start_now(e)

    def _request_stop(self, req: m.RequestStopTransactionRequest) -> m.RequestStopTransactionResponse:
        if self.host.registration == _PENDING:  # B02.FR.05
            return m.RequestStopTransactionResponse(status=RequestStartStopStatusEnumType.rejected,
                                                    status_info=self._info(REASON_NOT_REGISTERED))
        for e in self.evses.values():
            if e.tx is not None and e.tx.tx_id == req.transaction_id:
                self.host.after_reply(functools.partial(self._remote_stop, e, e.tx))
                return m.RequestStopTransactionResponse(status=RequestStartStopStatusEnumType.accepted)
        return m.RequestStopTransactionResponse(status=RequestStartStopStatusEnumType.rejected,
                                                status_info=self._info(REASON_UNKNOWN_TX))

    def _remote_stop(self, e: _Evse, tx: _Tx) -> None:
        if e.tx is tx:
            self._end_tx(e, T.remote_stop, ReasonEnumType.remote)

    def _unlock(self, req: m.UnlockConnectorRequest) -> m.UnlockConnectorResponse:
        e = self.evses.get(req.evse_id)
        if e is None or req.connector_id != CONNECTOR_ID:
            return m.UnlockConnectorResponse(status=UnlockStatusEnumType.unknown_connector,
                                             status_info=self._info(REASON_UNKNOWN_EVSE))
        if e.tx is not None:
            return m.UnlockConnectorResponse(status=UnlockStatusEnumType.ongoing_authorized_transaction)
        if self.unlock_fails:
            return m.UnlockConnectorResponse(status=UnlockStatusEnumType.unlock_failed)
        return m.UnlockConnectorResponse(status=UnlockStatusEnumType.unlocked)

    def _reset(self, req: m.ResetRequest) -> m.ResetResponse:
        if req.evse_id is not None:
            return m.ResetResponse(status=ResetStatusEnumType.rejected, status_info=self._info(REASON_NOT_SUPPORTED))
        if req.type is ResetEnumType.on_idle and any(e.tx is not None for e in self.evses.values()):
            self._reset_scheduled = True
            return m.ResetResponse(status=ResetStatusEnumType.scheduled)
        self._reset_scheduled = False
        self.host.after_reply(self._do_reset)
        return m.ResetResponse(status=ResetStatusEnumType.accepted)

    def _maybe_idle_reset(self) -> None:
        """Run a scheduled OnIdle Reset once no transaction is left (when connected; else at the next session)."""
        if not self._reset_scheduled or any(e.tx is not None for e in self.evses.values()):
            return
        self._reset_scheduled = False
        if self.host.spawn(self._do_reset(on_idle=True)) is None:
            self._reset_scheduled = True  # not connected: the next session runs it

    def _end_all_for_reset(self) -> None:
        for e in self.evses.values():
            self._end_tx(e, T.reset_command, ReasonEnumType.immediate_reset)
            self._clear_pending(e)

    async def _do_reset(self, *, on_idle: bool = False,
                        reason: BootReasonEnumType = BootReasonEnumType.remote_reset) -> None:
        if self._resetting:
            return
        if on_idle and any(e.tx is not None for e in self.evses.values()):
            self._reset_scheduled = True  # a transaction started before this task ran: wait for it again
            return
        self._resetting = True  # refuses new starts until the session is closed
        self.security.record(SE.RESET_OR_REBOOT, reason.value, connected=False)
        try:
            self._reset_scheduled = False
            self._end_all_for_reset()
            # Let the queued messages go out first; bounded, a silent CSMS must not block the Reset (anything
            # left stays queued for the next session). The outbox worker runs only while online.
            try:
                async with asyncio.timeout(self.host.call_timeout):
                    while self._outq and self.host.online:
                        await asyncio.sleep(RESET_DRAIN_POLL_S)
            except TimeoutError:
                log.info("%s: Reset closes with %d queued message(s)", self.cfg.id, len(self._outq))
            self._end_all_for_reset()  # e.g. a transaction a CSMS answer ended / started meanwhile
            self._adapter.boot_reason = reason
        finally:
            self._resetting = False
            self.host.end_session_for_reset()

    def _change_availability(self, req: m.ChangeAvailabilityRequest) -> m.ChangeAvailabilityResponse:
        targets = self._addressed(req.evse)
        if targets is None:
            return m.ChangeAvailabilityResponse(status=ChangeAvailabilityStatusEnumType.rejected,
                                                status_info=self._info(REASON_UNKNOWN_EVSE))
        operative = req.operational_status is OperationalStatusEnumType.operative
        scheduled = False
        if req.evse is None or req.evse.id == 0:  # the station's own availability (G04)
            busy = any(e.tx is not None for e in targets)
            scheduled = not operative and self.station_operative and busy  # G04.FR.06
            self.station_operative = operative
            if not operative:
                for e in targets:
                    if e.tx is None:
                        self._clear_pending(e)
        else:
            for e in targets:
                if not operative and e.operative and e.tx is not None:
                    e.pending_operative, scheduled = False, True  # G03.FR.05
                else:
                    self._set_operative(e, operative)
        if not operative:  # H01.FR.17: after the answer, before the StatusNotifications
            self.host.after_reply(functools.partial(
                self._drop_reservations_of, [e for e in targets if not self._operative(e)],
                station=req.evse is None or req.evse.id == 0))
        self.host.after_reply(functools.partial(self._refresh_targets, targets))
        return m.ChangeAvailabilityResponse(status=ChangeAvailabilityStatusEnumType.scheduled if scheduled
                                            else ChangeAvailabilityStatusEnumType.accepted)

    def _refresh_targets(self, targets: Sequence[_Evse]) -> None:
        for e in targets:
            self._refresh(e)

    def _trigger(self, req: m.TriggerMessageRequest) -> m.TriggerMessageResponse:
        def answer(status: TriggerMessageStatusEnumType, code: Optional[str] = None) -> m.TriggerMessageResponse:
            return m.TriggerMessageResponse(status=status, status_info=self._info(code))

        what = req.requested_message
        if what not in TRIGGERABLE:
            return answer(TriggerMessageStatusEnumType.not_implemented)
        targets = self._addressed(req.evse)
        if targets is None:
            return answer(TriggerMessageStatusEnumType.rejected, REASON_UNKNOWN_EVSE)
        if what is MT.boot_notification:
            if self.host.registration == d.BootStatus.ACCEPTED.value:
                return answer(TriggerMessageStatusEnumType.rejected, REASON_REGISTERED)
            self._adapter.trigger_boot_reason(BootReasonEnumType.triggered)
            self.host.after_reply(self.host.kick_boot)
            return answer(TriggerMessageStatusEnumType.accepted)
        if not self.host.connected or self.host.registration not in (_ACCEPTED, _PENDING):  # B02.FR.02
            return answer(TriggerMessageStatusEnumType.rejected, REASON_NOT_REGISTERED)
        if what is MT.log_status_notification:
            # F06.FR.13 / FR.14: Uploading while an upload runs, else Idle (without requestId, N01.FR.13).
            log_ev = (d.LogStatus(d.LogState.UPLOADING, self.maint.log_request_id) if self.maint.log_busy
                      else d.LogStatus(d.LogState.IDLE))
            self.host.after_reply(functools.partial(self._send_log_status, log_ev, triggered=True))
        elif what is MT.firmware_status_notification:
            # L02.FR.16 / FR.17 (F06.FR.16): the last sent status; Idle when it was Installed or there was none.
            last = self.maint.firmware_status
            fw_ev = (d.FirmwareStatus(last, self.maint.firmware_request_id)
                     if last not in (None, d.FirmwareState.INSTALLED) else d.FirmwareStatus(d.FirmwareState.IDLE))
            self.host.after_reply(functools.partial(self._send_firmware_status, fw_ev, triggered=True))
        elif what is MT.sign_charging_station_certificate:  # A02.FR.02: after the TriggerMessageResponse
            self.host.after_reply(self._sign_certificate)
        elif what is MT.heartbeat:
            self.host.after_reply(self.host.heartbeat)
        elif what is MT.status_notification:
            self.host.after_reply(functools.partial(self._trigger_statuses, targets))
        elif what is MT.meter_values:
            self.host.after_reply(functools.partial(self._trigger_meter_values, targets))
        else:  # TransactionEvent
            with_tx = [e for e in targets if e.tx is not None]
            if not with_tx:
                return answer(TriggerMessageStatusEnumType.rejected, REASON_NO_TX)
            self.host.after_reply(functools.partial(self._trigger_tx_events, with_tx))
        return answer(TriggerMessageStatusEnumType.accepted)

    # ================================================================== security (M8)

    async def _sign_certificate(self) -> None:
        """A02.FR.02 / FR.03: a new key pair and its CSR in a SignCertificateRequest; Rejected drops the key (a
        timeout or CALLERROR keeps it: the CSMS may still send the CertificateSigned)."""
        csr = self.security.new_csr(self.cfg.serial or self.cfg.id, self.dm.value(dm.ORGANIZATION_NAME) or None)
        req = m.SignCertificateRequest(csr=csr, certificate_type=CertificateSigningUseEnumType.charging_station_certificate)
        res = await self.host.safe(self._adapter.send_sign_certificate(req), "SignCertificate")
        if res is not None and res.status is not GenericStatusEnumType.accepted:
            self.security.drop_csr(csr)

    def _certificate_signed(self, req: m.CertificateSignedRequest) -> m.CertificateSignedResponse:
        def rejected(code: str, why: str) -> m.CertificateSignedResponse:
            return m.CertificateSignedResponse(status=CertificateSignedStatusEnumType.rejected,
                                               status_info=m.StatusInfoType(reason_code=code, additional_info=why))

        if req.certificate_type is CertificateSigningUseEnumType.v2g_certificate:
            return rejected(REASON_NOT_SUPPORTED, "ISO 15118 certificates are not supported")
        st, why = self.security.accept_signed(req.certificate_chain)
        if st is not d.CertStatus.ACCEPTED:  # A02.FR.07
            self.security.record(SE.INVALID_CHARGING_STATION_CERTIFICATE, why, connected=self.host.connected)
            return rejected(REASON_INVALID_CERTIFICATE, why or "")
        self.security.record(SE.RECONFIGURATION_OF_SECURITY_PARAMETERS, "new client certificate",
                             connected=self.host.connected)
        if self.cfg.client_cert_file is not None:  # profile 3: switch by reconnecting (A02.FR.08)
            self.host.use_client_certificate(self.security.client_cert_file, self.security.client_key_file,
                                             reconnect=True)
        return m.CertificateSignedResponse(status=CertificateSignedStatusEnumType.accepted)

    def _install_certificate(self, req: m.InstallCertificateRequest) -> m.InstallCertificateResponse:
        st = self.security.install(d.CertUse(req.certificate_type.value), req.certificate)
        if st is d.CertStatus.ACCEPTED:
            self.host.set_trusted_roots(self.security.trusted_pem())
        return m.InstallCertificateResponse(status=InstallCertificateStatusEnumType(st.value))

    def _delete_certificate(self, req: m.DeleteCertificateRequest) -> m.DeleteCertificateResponse:
        h = req.certificate_hash_data
        st = self.security.delete(d.CertificateHashData(h.hash_algorithm.value, h.issuer_name_hash, h.issuer_key_hash,
                                                        h.serial_number))
        if st is d.CertStatus.ACCEPTED:
            self.host.set_trusted_roots(self.security.trusted_pem())
        return m.DeleteCertificateResponse(status=DeleteCertificateStatusEnumType(st.value))

    def _get_installed_certificate_ids(
            self, req: m.GetInstalledCertificateIdsRequest) -> m.GetInstalledCertificateIdsResponse:
        # V2GCertificateChain never matches: no ISO 15118 certificates are installed.
        uses = tuple(d.CertUse(t.value) for t in req.certificate_type or ()
                     if t is not GetCertificateIdUseEnumType.v2g_certificate_chain)
        if req.certificate_type and not uses:
            found = []
        else:
            found = self.security.installed(uses)
        chain = [m.CertificateHashDataChainType(
            certificate_type=GetCertificateIdUseEnumType(use.value),
            certificate_hash_data=m.CertificateHashDataType(
                hash_algorithm=HashAlgorithmEnumType(h.hash_algorithm), issuer_name_hash=h.issuer_name_hash,
                issuer_key_hash=h.issuer_key_hash, serial_number=h.serial_number)) for use, h in found]
        if not chain:  # M03.FR.02
            return m.GetInstalledCertificateIdsResponse(status=GetInstalledCertificateStatusEnumType.not_found)
        return m.GetInstalledCertificateIdsResponse(status=GetInstalledCertificateStatusEnumType.accepted,
                                                    certificate_hash_data_chain=chain)

    # ================================================================== firmware / log (M7)

    async def _send_firmware_status(self, ev: d.FirmwareStatus, *, triggered: bool = False) -> None:
        """Progress needs an accepted boot; a triggered one may also go out while Pending (B02.FR.02)."""
        if ev.status is d.FirmwareState.INSTALLED and not triggered:
            self.security.record(SE.FIRMWARE_UPDATED, connected=self.host.connected)
        req = m.FirmwareStatusNotificationRequest(status=FirmwareStatusEnumType(ev.status.value),
                                                  request_id=ev.request_id)
        if self.host.connected if triggered else self.host.online:
            await self.host.safe(self._adapter.send_firmware_status_notification(req), "FirmwareStatusNotification")
        else:
            log.info("%s: FirmwareStatusNotification %s not sent (not online)", self.cfg.id, ev.status.value)

    async def _send_log_status(self, ev: d.LogStatus, *, triggered: bool = False) -> None:
        status = (UploadLogStatusEnumType.upload_failure if ev.status is d.LogState.UPLOAD_FAILED
                  else UploadLogStatusEnumType(ev.status.value))
        req = m.LogStatusNotificationRequest(status=status, request_id=ev.request_id)
        if self.host.connected if triggered else self.host.online:
            await self.host.safe(self._adapter.send_log_status_notification(req), "LogStatusNotification")
        else:
            log.info("%s: LogStatusNotification %s not sent (not online)", self.cfg.id, ev.status.value)

    def _update_firmware(self, req: m.UpdateFirmwareRequest) -> m.UpdateFirmwareResponse:
        if self.maint.installing:  # L02.FR.18: the installation cannot be cancelled
            return m.UpdateFirmwareResponse(status=UpdateFirmwareStatusEnumType.rejected,
                                            status_info=self._info(REASON_INSTALLING))
        fw = req.firmware
        job = FirmwareJob(fw.location, _aware(fw.retrieve_date_time), install_at=_aware(fw.install_date_time),
                          retries=req.retries, retry_interval_s=req.retry_interval, request_id=req.request_id,
                          signed=fw.signature is not None)
        canceled = self.maint.cancel_firmware()  # L02.FR.15
        self.host.after_reply(functools.partial(self.maint.start_firmware, job))
        return m.UpdateFirmwareResponse(status=UpdateFirmwareStatusEnumType.accepted_canceled if canceled
                                        else UpdateFirmwareStatusEnumType.accepted)

    def _get_log(self, req: m.GetLogRequest) -> m.GetLogResponse:
        name = f"{self.cfg.id}_{req.log_type.value}_{self.host.clock().strftime('%Y%m%dT%H%M%SZ')}.log"
        job = LogJob(req.log.remote_location, name, retries=req.retries, retry_interval_s=req.retry_interval,
                     request_id=req.request_id)
        old_id = self.maint.log_request_id
        canceled = self.maint.cancel_log()  # N01.FR.12

        async def go() -> None:
            if canceled:  # N01.FR.20
                await self._send_log_status(d.LogStatus(d.LogState.ACCEPTED_CANCELED, old_id))
            self.maint.start_log(job)

        self.host.after_reply(go)
        return m.GetLogResponse(status=LogStatusEnumType.accepted_canceled if canceled else LogStatusEnumType.accepted,
                                filename=name)

    async def _trigger_send(self, what: str, send: Callable[[], Awaitable[Any]]) -> None:
        await self.host.safe(send(), what)

    async def _trigger_statuses(self, targets: Sequence[_Evse]) -> None:
        """Send the current connectorStatus of ``targets`` (also when unchanged): queued while Accepted, directly
        while Pending (the outbox runs only once the boot is accepted)."""
        events = []  # captured now: a state change during the direct sends below is reported by _refresh
        for e in targets:
            e.status = self._target(e)
            e.reported = connector_status(e.status)
            events.append(self._status_event(e))
        for ev in events:
            send = functools.partial(self._adapter.send_connector_status, ev)
            if self.host.online:
                self._push(_Out("StatusNotification", send))
            else:
                await self.host.safe(send(), "StatusNotification")

    async def _trigger_meter_values(self, targets: Sequence[_Evse]) -> None:
        for e in targets:
            self._advance(e)
            ev = d.MeterSample(e.id, self.host.clock(), self._samples(e, self.dm.list_value(dm.ALIGNED_MEASURANDS)),
                               ReadingContextEnumType.trigger.value)
            await self.host.safe(self._adapter.send_meter_sample(ev), "MeterValues")

    def _trigger_tx_events(self, targets: Sequence[_Evse]) -> None:
        for e in targets:
            tx = e.tx
            if tx is None:
                continue  # ended meanwhile
            self._advance(e)
            tx.charging_state = self._charging_state(e)
            self._queue_event(e, tx, TransactionEventEnumType.updated, T.trigger,
                              m.TransactionType(transaction_id=tx.tx_id, charging_state=tx.charging_state),
                              samples=self._samples(e, self.dm.list_value(dm.TX_UPDATED_MEASURANDS)),
                              context=ReadingContextEnumType.trigger)

    # ================================================================== Device Model (M4)

    def _check_items(self, count: int, instance: str) -> None:
        limit = self.dm.int_value(dm.ITEMS_PER_MESSAGE, instance)
        if count > limit:
            raise CallError(self._adapter.error_codes.occurrence_constraint_violation,
                            f"{count} items, ItemsPerMessage[{instance}] is {limit}")

    def _get_variables(self, req: m.GetVariablesRequest) -> m.GetVariablesResponse:
        self._check_items(len(req.get_variable_data), dm.INSTANCE_GET_VARIABLES)
        return m.GetVariablesResponse(get_variable_result=[self.dm.get(x) for x in req.get_variable_data])

    def _set_variables(self, req: m.SetVariablesRequest) -> m.SetVariablesResponse:
        self._check_items(len(req.set_variable_data), dm.INSTANCE_SET_VARIABLES)
        return m.SetVariablesResponse(set_variable_result=[self.dm.set(x) for x in req.set_variable_data])

    def _get_base_report(self, req: m.GetBaseReportRequest) -> m.GetBaseReportResponse:
        data = self.dm.report(req.report_base)
        per = self.dm.int_value(dm.ITEMS_PER_MESSAGE, dm.INSTANCE_GET_REPORT)
        # B07 has no EmptyResultSet: an empty report is Accepted and sent as one part without reportData.
        parts = [data[i:i + per] for i in range(0, len(data), per)] or [[]]
        self.host.after_reply(functools.partial(self._send_report, req.request_id, parts, self.host.clock()))
        return m.GetBaseReportResponse(status=GenericDeviceModelStatusEnumType.accepted)

    def _password_changed(self, password: str) -> None:
        """BasicAuthPassword was set (A01): the shell uses it at once and reconnects after the answer went out."""
        self.dm.put(dm.SECURITY_PROFILE, str(dm.security_profile(password, getattr(self.host, "csms_url", "")
                                                                 or "")))
        self.security.record(SE.RECONFIGURATION_OF_SECURITY_PARAMETERS, "BasicAuthPassword changed",
                             connected=self.host.connected)  # A01.FR.12
        self.host.reconnect_with_password(password)

    async def _send_report(self, request_id: int, parts: Sequence[Sequence[m.ReportDataType]],
                           generated_at: datetime) -> None:
        """NotifyReport parts ``seqNo`` 0.. with ``tbc`` on all but the last; stops at the first failed part."""
        for seq, part in enumerate(parts):
            req = m.NotifyReportRequest(request_id=request_id, generated_at=generated_at, seq_no=seq,
                                        report_data=list(part) or None,
                                        tbc=True if seq < len(parts) - 1 else None)
            if await self.host.safe(self._adapter.send_notify_report(req), "NotifyReport") is None:
                log.info("%s: NotifyReport %d stopped at part %d of %d", self.cfg.id, request_id, seq,
                         len(parts))
                return

    # ================================================================== smart charging (M5)

    def _limits(self) -> Limits:
        return Limits(max_stack_level=self.dm.int_value(dm.PROFILE_STACK_LEVEL),
                      max_periods=self.dm.int_value(dm.PERIODS_PER_SCHEDULE),
                      units=frozenset(ChargingRateUnitEnumType(u) for u in self.dm.list_value(dm.RATE_UNIT)))

    def _install(self, evse_id: int, profile: ChargingProfile, wire: m.ChargingProfileType) -> Optional[str]:
        """ProfileStore.install for a CSMS profile, logging a same-id profile it replaces on another EVSE.
        Returns the reasonCode when it is Rejected, ``None`` when installed."""
        e = self.evses.get(evse_id)
        active = e.tx.tx_id if e is not None and e.tx is not None else None
        limits = self._limits()
        reason = self.profiles.reject_reason(evse_id, profile, active_transaction=active, limits=limits)
        if reason is not None:
            return reason
        moved = [ip.evse_id for ip in self.profiles.profiles()
                 if ip.profile.profile_id == profile.profile_id and ip.evse_id != evse_id
                 and ip.profile.purpose is not PURPOSE.charging_station_external_constraints]
        # install() validates again (same inputs, same verdict): it stays self-contained for other callers.
        self.profiles.install(evse_id, profile, active_transaction=active, limits=limits, wire=wire)
        if moved:
            log.info("%s: charging profile %d on EVSE %s replaced by the one for EVSE %d", self.cfg.id,
                     profile.profile_id, ",".join(map(str, moved)), evse_id)
        return None

    def _bind_profile(self, e: _Evse, tx: _Tx, profile: ChargingProfile, wire: m.ChargingProfileType) -> None:
        """Install the RequestStartTransaction TxProfile for the transaction it started."""
        bound = replace(profile, transaction_id=tx.tx_id)
        reason = self._install(e.id, bound, wire.model_copy(update={"transaction_id": tx.tx_id}))
        if reason is not None:
            log.warning("%s: RequestStartTransaction charging profile %d not installed (%s)", self.cfg.id,
                        profile.profile_id, reason)

    def _set_charging_profile(self, req: m.SetChargingProfileRequest) -> m.SetChargingProfileResponse:
        self.settle()
        reason = self._install(req.evse_id, charging_profile(req.charging_profile), req.charging_profile)
        if reason is not None:
            return m.SetChargingProfileResponse(status=ChargingProfileStatusEnumType.rejected,
                                                status_info=self._info(reason))
        self.host.after_reply(self.tick)
        return m.SetChargingProfileResponse(status=ChargingProfileStatusEnumType.accepted)

    def _clear_charging_profile(self, req: m.ClearChargingProfileRequest) -> m.ClearChargingProfileResponse:
        self.settle()
        c = req.charging_profile_criteria
        st = self.profiles.clear(profile_id=req.charging_profile_id, evse_id=c.evse_id if c else None,
                                 purpose=c.charging_profile_purpose if c else None,
                                 stack_level=c.stack_level if c else None)
        if st is not ClearStatus.ACCEPTED:
            return m.ClearChargingProfileResponse(status=ClearChargingProfileStatusEnumType.unknown)
        self.host.after_reply(self.tick)
        return m.ClearChargingProfileResponse(status=ClearChargingProfileStatusEnumType.accepted)

    def _composite(self, evse_id: int, duration: int,
                   unit: Optional[ChargingRateUnitEnumType]) -> d.GetCompositeResult:
        if not 0 <= evse_id <= self.connector_count or duration < 1:
            return d.GetCompositeResult(d.CommandStatus.REJECTED)
        now = self.host.clock()
        phases, volts = self._phases(evse_id), self.supply.phase_voltage_v()
        sched = composite_schedule(self.profiles, evse_id, now, duration, unit, transactions=self._transactions(),
                                   number_phases=phases, voltage=volts)
        max_w = self.cfg.max_power_w * (self.connector_count if evse_id == 0 else 1)

        def cap(p: CompositePeriod) -> CompositePeriod:
            # The hardware maximum caps every period and stands for "no limit" (see the module docstring).
            if sched.unit is W:
                return replace(p, limit=max_w if p.limit is None else min(p.limit, max_w))
            n = p.number_phases or phases
            hw = convert(max_w, W, ChargingRateUnitEnumType.a, n, volts)
            return replace(p, limit=hw if p.limit is None else min(p.limit, hw), number_phases=n)

        return d.GetCompositeResult(d.CommandStatus.ACCEPTED, evse_id, now,
                                    replace(sched, periods=tuple(cap(p) for p in sched.periods)))

    def _get_composite_schedule(self, req: m.GetCompositeScheduleRequest) -> m.GetCompositeScheduleResponse:
        unit = req.charging_rate_unit
        if unit is not None and unit.value not in self.dm.list_value(dm.RATE_UNIT):  # K08.FR.07
            return m.GetCompositeScheduleResponse(status=GenericStatusEnumType.rejected,
                                                  status_info=self._info(REASON_UNSUPPORTED_RATE_UNIT))
        res = self._composite(req.evse_id, req.duration, unit)
        if res.schedule is None:
            code = REASON_UNKNOWN_EVSE if not 0 <= req.evse_id <= self.connector_count else REASON_INVALID_DURATION
            return m.GetCompositeScheduleResponse(status=GenericStatusEnumType.rejected,
                                                  status_info=self._info(code))
        return m.GetCompositeScheduleResponse(status=GenericStatusEnumType.accepted,
                                              schedule=composite_schedule_type(res.schedule))

    @staticmethod
    def _source(ip: InstalledProfile) -> ChargingLimitSourceEnumType:
        external = ip.profile.purpose is PURPOSE.charging_station_external_constraints
        return EXTERNAL_LIMIT_SOURCE if external else CSMS_LIMIT_SOURCE

    def _get_charging_profiles(self, req: m.GetChargingProfilesRequest) -> m.GetChargingProfilesResponse:
        c = req.charging_profile

        def matches(ip: InstalledProfile) -> bool:
            p = ip.profile
            return ((req.evse_id is None or ip.evse_id == req.evse_id)
                    and (c.charging_profile_id is None or p.profile_id in c.charging_profile_id)
                    and (c.charging_profile_purpose is None or p.purpose is c.charging_profile_purpose)
                    and (c.stack_level is None or p.stack_level == c.stack_level)
                    and (c.charging_limit_source is None or self._source(ip) in c.charging_limit_source))

        groups: Dict[Tuple[int, ChargingLimitSourceEnumType], List[m.ChargingProfileType]] = {}
        for ip in sorted((x for x in self.profiles.profiles() if matches(x)), key=lambda x: x.evse_id):
            groups.setdefault((ip.evse_id, self._source(ip)), []).append(ip.wire)
        if not groups:
            return m.GetChargingProfilesResponse(status=GetChargingProfileStatusEnumType.no_profiles)
        last = len(groups) - 1
        parts = [m.ReportChargingProfilesRequest(request_id=req.request_id, evse_id=evse_id,
                                                 charging_limit_source=source, charging_profile=wires,
                                                 tbc=True if i < last else None)
                 for i, ((evse_id, source), wires) in enumerate(groups.items())]
        self.host.after_reply(functools.partial(self._send_profile_reports, parts))
        return m.GetChargingProfilesResponse(status=GetChargingProfileStatusEnumType.accepted)

    async def _send_profile_reports(self, parts: Sequence[m.ReportChargingProfilesRequest]) -> None:
        """ReportChargingProfiles parts in order; stops at the first one that fails."""
        for i, part in enumerate(parts):
            if await self.host.safe(self._adapter.send_report_charging_profiles(part),
                                    "ReportChargingProfiles") is None:
                log.info("%s: ReportChargingProfiles %d stopped at part %d of %d", self.cfg.id, part.request_id,
                         i, len(parts))
                return

    # ================================================================== operator API (engine / Web API)

    def _evse(self, connector: int) -> _Evse:
        try:
            return self.evses[connector]
        except KeyError:
            raise ValueError(f"no EVSE {connector} (1..{self.connector_count})") from None

    async def plug(self, connector: int) -> None:
        e = self._evse(connector)
        if e.plugged:
            return
        e.plugged = True
        if e.pending_tag and not e.pending_authorizing and self._operative(e) and not e.fault:
            # F02.FR.21: the first event after a RequestStartTransaction says RemoteStart.
            trigger = T.remote_start if e.pending_remote_id is not None else T.cable_plugged_in
            self._start_tx(e, e.pending_tag, trigger, token_type=e.pending_type,
                           remote_start_id=e.pending_remote_id, profile=e.pending_profile)
        else:
            self._refresh(e)

    async def unplug(self, connector: int) -> None:
        """Never waits for the CSMS."""
        e = self._evse(connector)
        if not e.plugged:
            return
        e.plugged, e.finishing = False, False
        self._clear_pending(e)  # a waiting token does not outlive the cable
        if e.tx is not None:
            self._end_tx(e, T.ev_communication_lost, ReasonEnumType.ev_disconnected)
        else:
            self._refresh(e)

    async def swipe(self, connector: int, id_tag: str) -> A:
        """Present an id token. ``Accepted`` = a transaction started (or the token waits for the cable),
        ``Stopped`` = it ended its own transaction; the authorization status when refused; ``Busy`` /
        ``Unavailable`` / ``Offline`` / ``Error`` when no transaction could be started."""
        e = self._evse(connector)
        if e.tx is not None:
            if e.tx.id_token == id_tag:
                self._end_tx(e, T.stop_authorized, ReasonEnumType.local, token=id_tag)
                return A.STOPPED
            return A.BUSY
        if e.fault or not self._operative(e) or self._resetting:
            return A.UNAVAILABLE
        if not self._reservation_for(e, id_tag, None)[0] and not self._group_may_help(e):
            return A.RESERVED  # reserved for another token: refused without asking the CSMS
        info, asked = await self._authorize_info(id_tag)
        if info.status is not A.ACCEPTED:
            return info.status
        group = None
        if not self._reservation_for(e, id_tag, None)[0]:
            group = await self._group_of(id_tag, info, asked)
        # Re-check: the await may have let another start / a fault / a reservation change in.
        if e.tx is not None:
            return A.BUSY
        if e.fault or not self._operative(e) or self._resetting:
            return A.UNAVAILABLE
        allowed, r = self._reservation_for(e, id_tag, group)
        if not allowed:
            return A.RESERVED
        if not e.plugged:
            self._clear_pending(e)
            e.pending_tag = id_tag
            self._claim(e, r)
            self._arm_connection_timeout(e)
            self._refresh(e)
            return A.ACCEPTED
        self._start_tx(e, id_tag, T.authorized, reservation=r)
        return A.ACCEPTED

    async def stop_tx(self, connector: int) -> bool:
        """Never waits for the CSMS: the TransactionEvent Ended is queued before this returns."""
        e = self._evse(connector)
        if e.tx is None:
            return False
        self._end_tx(e, T.stop_authorized, ReasonEnumType.local)
        return True

    async def fault(self, connector: int, error_code: str) -> None:
        """``error_code`` is one of :attr:`fault_codes` (a ``ReasonEnumType`` value)."""
        e = self._evse(connector)
        if error_code not in self.fault_codes:
            raise ValueError(f"fault code {error_code!r} is not one of {', '.join(self.fault_codes)}")
        e.fault = error_code
        if e.tx is not None:
            self._end_tx(e, T.abnormal_condition, ReasonEnumType(error_code))
        self._drop_reservations_of([e])  # H01.FR.16
        self._refresh(e)

    async def clear_fault(self, connector: int) -> bool:
        e = self._evse(connector)
        if e.fault is None:
            return False
        e.fault = None
        self._refresh(e)
        return True

    def has_transaction(self, connector: int) -> bool:
        return self._evse(connector).tx is not None

    def faulted(self, connector: int) -> Optional[str]:
        return self._evse(connector).fault

    def installed_profiles(self, connector: Optional[int] = None) -> Tuple[InstalledProfile, ...]:
        """Installed charging profiles (all, or those of EVSE ``connector``; 0 = the station)."""
        if connector:
            self._evse(connector)
        return self.profiles.profiles(connector)

    def composite(self, connector: int, duration: int, unit: Optional[str] = None) -> d.GetCompositeResult:
        """The GetCompositeSchedule answer for EVSE ``connector`` (0 = the station) right now, without a CSMS;
        ``unit`` is ``"W"`` / ``"A"``, ``None`` = W."""
        if unit not in (None, "W", "A"):
            raise ValueError(f"unit {unit!r}: W or A")
        return self._composite(connector, duration, ChargingRateUnitEnumType(unit) if unit else None)

    async def set_external_limit(self, connector: int, limit_w: Optional[float]) -> None:
        """Set (``limit_w`` in W) or remove (``None``) the external constraints of EVSE ``connector`` (0 = the
        station); see the module docstring. ``ValueError`` for an unknown EVSE, a negative limit or while
        SmartChargingCtrlr.ExternalControlSignalsEnabled is false."""
        if connector:
            self._evse(connector)
        if not 0 <= connector <= self.connector_count:
            raise ValueError(f"no EVSE {connector} (0..{self.connector_count})")
        if not self.dm.bool_value(dm.EXTERNAL_CONTROL_SIGNALS_ENABLED):
            raise ValueError("SmartChargingCtrlr.ExternalControlSignalsEnabled is false")
        if limit_w is not None and limit_w < 0:
            raise ValueError(f"limit_w {limit_w}: must be >= 0")
        self.settle()
        before = self._rates()
        if limit_w is None:
            if self.profiles.clear_external(connector):
                self._notify(lambda: self._adapter.send_cleared_charging_limit(m.ClearedChargingLimitRequest(
                    charging_limit_source=EXTERNAL_LIMIT_SOURCE, evse_id=connector or None)), "ClearedChargingLimit")
        else:
            sched = ChargingSchedule(id=1, unit=W, periods=(SchedulePeriod(0, float(limit_w)),),
                                     start_schedule=self.host.clock())
            profile = ChargingProfile(profile_id=external_profile_id(connector), stack_level=0,
                                      purpose=PURPOSE.charging_station_external_constraints,
                                      kind=ChargingProfileKindEnumType.absolute, schedule=sched)
            st = self.profiles.install(connector, profile, external=True, wire=charging_profile_type(profile))
            if st is not ProfileStatus.ACCEPTED:  # a fixed, valid profile: a local bug
                raise RuntimeError(f"external constraints not installed ({st.value})")
            req = m.NotifyChargingLimitRequest(
                charging_limit=m.ChargingLimitType(charging_limit_source=EXTERNAL_LIMIT_SOURCE),
                evse_id=connector or None,
                charging_schedule=([charging_schedule_type(sched)]
                                   if self.dm.bool_value(dm.NOTIFY_CHARGING_LIMIT_WITH_SCHEDULES) else None))
            self._notify(lambda: self._adapter.send_notify_charging_limit(req), "NotifyChargingLimit")
        self._report_rate_changes(before)
        self.tick()

    def _rates(self) -> Dict[int, float]:
        """The rate in W each EVSE with a transaction may charge at now (the limit capped at the hardware)."""
        out = {}
        for e in self.evses.values():
            if e.tx is not None:
                limit = self._limit_w(e)
                out[e.id] = self.cfg.max_power_w if limit is None else min(limit, self.cfg.max_power_w)
        return out

    def _report_rate_changes(self, before: Dict[int, float]) -> None:
        """K11.FR.04 / K13.FR.03: an ``Updated`` ``ChargingRateChanged`` for every transaction whose rate changed by
        more than LimitChangeSignificance percent."""
        significance = float(self.dm.value(dm.LIMIT_CHANGE_SIGNIFICANCE))
        for evse_id, new in self._rates().items():
            old = before.get(evse_id)
            e = self.evses[evse_id]
            if old is None or e.tx is None or old == new:
                continue
            if old != 0 and abs(new - old) / old * 100 <= significance:
                continue
            self._queue_event(e, e.tx, TransactionEventEnumType.updated, T.charging_rate_changed,
                              m.TransactionType(transaction_id=e.tx.tx_id))

    def external_limit(self, connector: int) -> Optional[float]:
        """The operator external limit in W of EVSE ``connector`` (0 = the station), ``None`` = none."""
        for ip in self.profiles.profiles(connector):
            if ip.profile.purpose is PURPOSE.charging_station_external_constraints:
                return ip.profile.schedule.periods[0].limit
        return None

    def _notify(self, send: Callable[[], Awaitable[Any]], what: str) -> None:
        """Run ``send`` in the background while the boot is accepted; otherwise nothing is sent."""
        async def run() -> None:
            await self.host.safe(send(), what)  # the message is only built inside the task

        if not self.host.online or self.host.spawn(run()) is None:
            log.info("%s: %s not sent (not online)", self.cfg.id, what)

    async def send_meter_values(self, connector: int, context: str = ReadingContextEnumType.other.value) -> None:
        """Send a MeterValues of ``connector`` now (outside the TransactionEvent flow)."""
        e = self._evse(connector)
        self._advance(e)
        ev = d.MeterSample(e.id, self.host.clock(), self._samples(e, self.dm.list_value(dm.ALIGNED_MEASURANDS)),
                           context)
        await self._adapter.send_meter_sample(ev)
