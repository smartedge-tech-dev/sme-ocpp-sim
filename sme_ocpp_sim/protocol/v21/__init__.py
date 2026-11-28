"""OCPP 2.1 (Edition 2) stack, built on the 2.0.1 one (``protocol/v201``) by inheritance.

- ``models``: GENERATED from the vendored 2.1 JSON schemas (``scripts/gen_v21_models.py``).
- ``codec``: the 2.0.1 codec bound to the 2.1 tables (:data:`codec.ACTIONS` = the actions answered / sent).
- ``adapter.Adapter21`` / ``station.Station21``: subclasses of the 2.0.1 ones with the 2.1 models injected
  (class attributes ``models`` / ``codec`` / ``adapter_cls``), subprotocol ``ocpp2.1`` and the OCPP-J 2.1 framing
  (CALLRESULTERROR / SEND, unknown MessageTypeId ignored; ``rpc.errors.OCPP21_ERRORS``).

New 2.1 CSMS -> CS messages: store-and-ack ones (tariffs, DER control, AFRRSignal, NotifyAllowedEnergyTransfer)
answer schema-valid; the others (payment, battery swap, periodic event streams, dynamic schedules, priority
charging) are not in the tables and answer CALLERROR NotImplemented (see docs ``messages.md``).
"""
