"""Scenario file model (``format: ocppsim-scenario/1``, v0.3 plan §3.2 / §3.5 / §5; Q4: a standalone YAML that
names charger ids of the running config, one scenario at a time).

Every model forbids unknown keys, so a typo is an error with its position (``tracks.0.steps.2.do``). Charger ids
and ``${param}`` references are checked by :func:`expand` against the running config, which also unrolls
``for_each`` / ``repeat`` into :class:`PlannedTrack` s.

Times: ``after`` / ``wait.duration`` / ``within_sim`` are sim seconds (they follow ``time_scale`` and pause,
Q2); ``within`` is wall seconds (how fast a CSMS answers does not depend on the speed-up). A duration is a number
(seconds) or text such as ``500ms``, ``30s``, ``5m``, ``2h``, ``1h30m``, ``1d``. ``at`` / ``until`` is ``"HH:MM"``
/ ``"HH:MM:SS"`` (the next such sim clock time at or after the scenario start), an ISO date-time, or ``"+2h"``
(sim time after the scenario start).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator
from pydantic.functional_validators import BeforeValidator

from .match import MatcherError, check_matcher

FORMAT = "ocppsim-scenario/1"
#: Wall seconds an assertion waits when it gives neither ``within`` nor ``within_sim``.
DEFAULT_WITHIN_S = 30.0

ACTIONS = ("plug", "unplug", "swipe", "start", "stop", "fault", "clear_fault", "connect", "disconnect", "drop",
           "impair", "send", "set_ev", "wait", "log")
#: Parameters each action takes (``connector`` defaults to 1).
ACTION_PARAMS: Dict[str, frozenset] = {
    "plug": frozenset({"connector"}), "unplug": frozenset({"connector"}), "stop": frozenset({"connector"}),
    "clear_fault": frozenset({"connector"}), "swipe": frozenset({"connector", "id_tag"}),
    "start": frozenset({"connector", "id_tag"}), "fault": frozenset({"connector", "error_code"}),
    "connect": frozenset(), "disconnect": frozenset(), "drop": frozenset({"mode", "reconnect_after_s"}),
    "impair": frozenset({"impairments"}), "send": frozenset({"action", "payload", "raw", "validate", "timeout"}),
    "set_ev": frozenset({"connector", "ev_model", "initial_soc", "target_soc"}), "wait": frozenset({"duration"}),
    "log": frozenset({"message"}),
}
_REQUIRED = {"swipe": ("id_tag",), "impair": ("impairments",), "wait": ("duration",), "log": ("message",)}
ASSERTIONS = ("expect", "expect_not", "await_csms")
_TIMING = frozenset({"id", "at", "after", "on_fail"})
_ALIASES = {"validate_": "validate", "from_": "from"}
#: Most rounds of a ``repeat`` track.
MAX_REPEAT = 1000
_PARAM_FIELDS = frozenset().union(*ACTION_PARAMS.values())

_DUR_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class ScenarioError(ValueError):
    """A scenario that cannot be loaded or does not fit the running config (message names the position)."""


def parse_duration(v: Any) -> float:
    """Seconds of a duration: a number, or text of ``<n><unit>`` parts (``ms`` / ``s`` / ``m`` / ``h`` / ``d``)."""
    if isinstance(v, bool):
        raise ValueError("a duration is a number of seconds or text like 30s / 5m / 2h")
    if isinstance(v, (int, float)):
        out = float(v)
    elif isinstance(v, str) and v.strip():
        s = v.strip().replace(" ", "")
        pos, out = 0, 0.0
        for m in _DUR_PART.finditer(s):
            if m.start() != pos:
                break
            out += float(m.group(1)) * _UNIT_S[m.group(2)]
            pos = m.end()
        if pos != len(s):
            try:
                out = float(s)
            except ValueError:
                raise ValueError(f"{v!r} is not a duration (e.g. 30s, 5m, 2h, 1h30m)") from None
    else:
        raise ValueError("a duration is a number of seconds or text like 30s / 5m / 2h")
    if out < 0:
        raise ValueError("a duration must be >= 0")
    return out


Duration = Annotated[float, BeforeValidator(parse_duration)]
#: A parsed ``at`` / ``until``: ``("clock", time)``, ``("abs", aware datetime)`` or ``("rel", sim seconds)``.
TimeSpec = Tuple[str, Any]


def parse_time(v: str) -> TimeSpec:
    s = str(v).strip()
    if s.startswith("+"):
        return "rel", parse_duration(s[1:])
    m = _CLOCK.match(s)
    if m:
        h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if h > 23 or mi > 59 or se > 59:
            raise ValueError(f"{v!r}: not a clock time")
        return "clock", dtime(h, mi, se)
    try:
        return "abs", datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{v!r}: give HH:MM[:SS], an ISO date-time or +<duration> (e.g. +2h)") from None


def _time_text(v: Any) -> str:
    if isinstance(v, int) and not isinstance(v, bool):
        # YAML 1.1 reads an unquoted 08:00 as the sexagesimal integer 480 (and 08:00:30 as 28830): ambiguous.
        raise ValueError(f"quote clock times (\"08:00\"): YAML reads an unquoted one as the number {v}")
    if isinstance(v, datetime):
        return v.isoformat()
    if not isinstance(v, str):
        raise ValueError("a time is text: HH:MM[:SS], an ISO date-time or +<duration>")
    parse_time(v)
    return v


TimeText = Annotated[str, BeforeValidator(_time_text)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_payload(v: Dict[str, Any]) -> Dict[str, Any]:
    for path, m in v.items():
        try:
            check_matcher(m)
        except MatcherError as e:
            raise ValueError(f"{path}: {e}") from None
    return v


class FrameSpec(_Strict):
    """Which frame of the track's charger: direction (``tx`` = sent by the charger), action, type, payload."""
    dir: Optional[Literal["rx", "tx"]] = None
    action: Optional[str] = None
    message_type: Optional[Literal["CALL", "CALLRESULT", "CALLERROR"]] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

    _payload = field_validator("payload")(_check_payload)


class ReplySpec(_Strict):
    """The answer to the matched CALL (same uid, other direction); no type = CALLRESULT or CALLERROR."""
    message_type: Optional[Literal["CALLRESULT", "CALLERROR"]] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

    _payload = field_validator("payload")(_check_payload)


class StateSpec(_Strict):
    """Connector / charger state; each given field is a matcher (``has_transaction`` / ``online`` / ``connected``:
    true or false)."""
    connector: int = Field(1, ge=1)
    status: Any = None
    has_transaction: Optional[bool] = None
    power_w: Any = None
    soc_pct: Any = None
    meter_wh: Any = None
    online: Optional[bool] = None
    connected: Optional[bool] = None

    @model_validator(mode="after")
    def _some(self) -> "StateSpec":
        given = self.checks()
        if not given:
            raise ValueError("state: give at least one of status / has_transaction / power_w / soc_pct / "
                             "meter_wh / online / connected")
        for k, m in given.items():
            try:
                check_matcher(m)
            except MatcherError as e:
                raise ValueError(f"{k}: {e}") from None
        return self

    def checks(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in ("status", "has_transaction", "power_w", "soc_pct", "meter_wh",
                                              "online", "connected") if getattr(self, k) is not None}


class Expect(_Strict):
    frame: Optional[FrameSpec] = None
    then: Optional[ReplySpec] = None
    state: Optional[StateSpec] = None

    @model_validator(mode="after")
    def _one(self) -> "Expect":
        if (self.frame is None) == (self.state is None):
            raise ValueError("expect: give exactly one of frame / state")
        if self.then is not None and self.frame is None:
            raise ValueError("expect.then: only with expect.frame")
        return self


class AwaitCsms(_Strict):
    """A CALL the CSMS has to send (e.g. an operator action on the CSMS); ``prompt`` is shown while waiting."""
    action: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    prompt: Optional[str] = None

    _payload = field_validator("payload")(_check_payload)


class Step(_Strict):
    """One step: an action (``do``) or an assertion (``expect`` / ``expect_not`` / ``await_csms``)."""
    id: Optional[str] = None
    at: Optional[TimeText] = None
    after: Optional[Duration] = None
    on_fail: Literal["stop", "continue"] = "stop"
    # action
    do: Optional[Literal[ACTIONS]] = None  # type: ignore[valid-type]
    connector: int = Field(1, ge=1)
    id_tag: Optional[str] = Field(None, min_length=1, max_length=36)
    error_code: Optional[str] = None
    mode: Optional[Literal["abort", "close"]] = None
    reconnect_after_s: Optional[float] = Field(None, ge=0, le=86400)
    impairments: Optional[Dict[str, Any]] = None
    action: Optional[str] = Field(None, min_length=1)
    payload: Any = None
    raw: Optional[str] = Field(None, min_length=1)
    validate_: Optional[bool] = Field(None, alias="validate")
    timeout: Optional[float] = Field(None, gt=0, le=300)
    from_: Optional[Literal["prev_step", "action", "scenario_start"]] = Field(None, alias="from")
    ev_model: Optional[str] = Field(None, min_length=1)
    initial_soc: Optional[float] = Field(None, ge=0, le=100)
    target_soc: Optional[float] = Field(None, ge=0, le=100)
    message: Optional[str] = None
    duration: Optional[Duration] = None
    # assertion
    expect: Optional[Expect] = None
    expect_not: Optional[FrameSpec] = None
    await_csms: Optional[AwaitCsms] = None
    within: Optional[Duration] = None
    within_sim: Optional[Duration] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        kinds = [k for k in ("do",) + ASSERTIONS if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError("a step has exactly one of do / expect / expect_not / await_csms")
        if self.at is not None and self.after is not None:
            raise ValueError("give at or after, not both")
        given = {_ALIASES.get(f, f) for f in self.model_fields_set} - _TIMING - {"from"}
        if self.from_ is not None and (self.do is not None or (self.expect is not None and self.expect.state)):
            raise ValueError("from: only on frame assertions (expect.frame / expect_not / await_csms)")
        if self.do is not None:
            extra = sorted(given - {"do"} - ACTION_PARAMS[self.do])
            if extra:
                raise ValueError(f"do: {self.do} does not take {extra}")
            missing = [f for f in _REQUIRED.get(self.do, ()) if getattr(self, f) is None]
            if missing:
                raise ValueError(f"do: {self.do} needs {missing}")
            if self.do == "send" and (self.raw is None) == (self.action is None):
                raise ValueError("do: send needs action (+ payload) or raw")
            if self.do == "send" and self.raw is not None and "payload" in given:
                raise ValueError("do: send takes raw or action / payload, not both")
            if self.do == "set_ev" and not given & {"ev_model", "initial_soc", "target_soc"}:
                raise ValueError("do: set_ev needs ev_model, initial_soc or target_soc")
        else:
            extra = sorted(given & _PARAM_FIELDS)
            if extra:
                raise ValueError(f"{kinds[0]}: action parameters {extra} belong to do steps")
            if self.within is not None and self.within_sim is not None:
                raise ValueError("give within (wall) or within_sim (sim), not both")
        return self

    @property
    def kind(self) -> str:
        return f"do:{self.do}" if self.do is not None else next(k for k in ASSERTIONS if getattr(self, k) is not None)


class ForEach(_Strict):
    chargers: Union[List[str], Literal["*"]]


class Track(_Strict):
    """Steps run in order on one charger; tracks run in parallel. ``for_each`` makes one track per charger
    (``${charger}`` / ``${i}`` = its id / 0-based index), ``repeat`` runs the steps n times (``${round}``)."""
    name: Optional[str] = None
    charger: Optional[str] = Field(None, min_length=1)
    for_each: Optional[ForEach] = None
    repeat: int = Field(1, ge=1, le=MAX_REPEAT)
    steps: List[Step] = Field(min_length=1)
    generate: Optional[Any] = None

    @model_validator(mode="after")
    def _target(self) -> "Track":
        if self.generate is not None:
            raise ValueError("generate: generator tracks come with the traffic generator (v0.3 M4)")
        if (self.charger is None) == (self.for_each is None):
            raise ValueError("a track has exactly one of charger / for_each")
        if self.repeat > 1 and any(s.at is not None and parse_time(s.at)[0] != "rel" for s in self.steps):
            raise ValueError("a repeat track takes only relative times (at: +dur from the round start, after)")
        return self


class ClockSpec(_Strict):
    start_at: Optional[datetime] = None        # sim clock anchor (the engine's clock restarts there)
    time_scale: Optional[float] = Field(None, gt=0, le=3600)  # sim speed for this run (default: the config's)


class Scenario(_Strict):
    format: Literal["ocppsim-scenario/1"]
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    seed: Optional[int] = None
    clock: ClockSpec = Field(default_factory=ClockSpec)
    params: Dict[str, Union[bool, int, float, str]] = Field(default_factory=dict)
    tracks: List[Track] = Field(min_length=1)
    until: Optional[TimeText] = None

    #: The tracks as loaded, before ``${...}`` replacement (:func:`expand` validates the replaced steps).
    _raw_tracks: Optional[List[Any]] = PrivateAttr(default=None)


# ------------------------------------------------------------------ loading

def _errors(e: ValidationError) -> str:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(out)


def load_data(data: Any) -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioError("a scenario is a mapping with format: ocppsim-scenario/1")
    if data.get("format") != FORMAT:
        raise ScenarioError(f"format: must be {FORMAT!r} (got {data.get('format')!r})")
    # Pass 1: a whole-string ${...} may stand in any field (connector: "${conn}"), so the structure is checked
    # with the parameters (and placeholders for the per-track names) put in; expand() checks the real values.
    params = data.get("params") or {}
    probe = data
    if isinstance(params, dict) and isinstance(data.get("tracks"), list):
        env = {**params, "charger": "CHARGER", "i": 1, "round": 1}
        probe = {**data, "tracks": _subst(data["tracks"], env, "tracks")}
    try:
        sc = Scenario.model_validate(probe)
    except ValidationError as e:
        raise ScenarioError(_errors(e)) from None
    sc._raw_tracks = data.get("tracks")
    return sc


def load_text(text: str) -> Scenario:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ScenarioError(f"YAML: {e}") from None
    return load_data(data)


def load_file(path: Union[str, Path]) -> Scenario:
    return load_text(Path(path).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ expansion

_VAR = re.compile(r"\$\{(\w+)\}")


def _subst(obj: Any, env: Dict[str, Any], loc: str) -> Any:
    if isinstance(obj, str):
        whole = _VAR.fullmatch(obj)
        if whole:
            if whole.group(1) not in env:
                raise ScenarioError(f"{loc}: unknown parameter ${{{whole.group(1)}}} (known: {sorted(env)})")
            return env[whole.group(1)]

        def one(m: re.Match) -> str:
            if m.group(1) not in env:
                raise ScenarioError(f"{loc}: unknown parameter ${{{m.group(1)}}} (known: {sorted(env)})")
            return str(env[m.group(1)])
        return _VAR.sub(one, obj)
    if isinstance(obj, dict):
        return {_subst(k, env, loc): _subst(v, env, f"{loc}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, env, f"{loc}.{i}") for i, v in enumerate(obj)]
    return obj


@dataclass(frozen=True)
class PlannedStep:
    id: str
    step: Step
    at: Optional[TimeSpec]
    round: int = 0


@dataclass(frozen=True)
class PlannedTrack:
    name: str
    charger: str
    steps: Tuple[PlannedStep, ...]


def expand(sc: Scenario, charger_ids: List[str]) -> List[PlannedTrack]:
    """The tracks to run: ``for_each`` / ``repeat`` unrolled, ``${...}`` replaced (a whole-string reference keeps
    the parameter's type), every charger id checked against ``charger_ids``. Raises :class:`ScenarioError`."""
    known = set(charger_ids)
    out: List[PlannedTrack] = []
    for ti, tr in enumerate(sc.tracks):
        loc = f"tracks.{ti}"
        if tr.for_each is not None:
            targets = list(charger_ids) if tr.for_each.chargers == "*" else list(tr.for_each.chargers)
            if not targets:
                raise ScenarioError(f"{loc}.for_each: no chargers")
        else:
            raw_charger = sc._raw_tracks[ti].get("charger") if sc._raw_tracks else tr.charger
            targets = [str(_subst(raw_charger, dict(sc.params), f"{loc}.charger"))]
        for i, cid in enumerate(targets):
            if cid not in known:
                where = f"{loc}.for_each.chargers" if tr.for_each is not None else f"{loc}.charger"
                raise ScenarioError(f"{where}: unknown charger {cid!r} (config: {sorted(known)})")
            steps: List[PlannedStep] = []
            for r in range(tr.repeat):
                env = {**sc.params, "charger": cid, "i": i, "round": r}
                for si, st in enumerate(tr.steps):
                    sloc = f"{loc}.steps.{si}"
                    raw = (sc._raw_tracks[ti]["steps"][si] if sc._raw_tracks
                           else st.model_dump(exclude_unset=True, by_alias=True))
                    data = _subst(raw, env, sloc)
                    try:
                        s = Step.model_validate(data)
                    except ValidationError as e:
                        raise ScenarioError(f"{sloc}: {_errors(e)}") from None
                    sid = s.id or f"{si + 1}"
                    if tr.repeat > 1:
                        sid = f"{sid}#{r + 1}"
                    steps.append(PlannedStep(sid, s, parse_time(s.at) if s.at is not None else None, r))
            base = tr.name or f"track{ti + 1}"
            name = f"{base}:{cid}" if tr.for_each is not None or tr.name is None else base
            out.append(PlannedTrack(name, cid, tuple(steps)))
    names = [t.name for t in out]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise ScenarioError(f"tracks: duplicate track name(s) {dup}")
    return out
