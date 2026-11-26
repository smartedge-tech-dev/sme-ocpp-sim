"""Per-charger configuration key store (version neutral: key -> value + attributes)."""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..protocol.base import ConfigEntry, ConfigKeyDef, ConfigStatus, KeyType

Listener = Callable[[str, str], None]

_TRUE, _FALSE = "true", "false"


def normalize(defn: ConfigKeyDef, value: str) -> Optional[str]:
    """Return the canonical string for ``value`` or ``None`` if it does not fit the key's type."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if defn.type is KeyType.BOOL:
        low = v.lower()
        return low if low in (_TRUE, _FALSE) else None
    if defn.type is KeyType.INT:
        # All 1.6 integer keys are counts / seconds / percentages. TODO(spec): verify ranges against OCPP 1.6 §9
        try:
            n = int(v)
        except ValueError:
            return None
        return str(n) if n >= 0 else None
    if defn.type is KeyType.CSL:
        items = [p.strip() for p in v.split(",")] if v else []
        if any(not p for p in items):
            return None
        if defn.allowed is not None and any(p not in defn.allowed for p in items):
            return None
        return ",".join(items)
    if defn.allowed is not None and v not in defn.allowed:
        return None
    return value


class ConfigStore:
    def __init__(self, keys: Iterable[ConfigKeyDef], overrides: Optional[Mapping[str, str]] = None):
        self._defs: Dict[str, ConfigKeyDef] = {}
        self._values: Dict[str, str] = {}
        self._listeners: List[Listener] = []
        for d in keys:
            self._defs[d.name] = d
            self._values[d.name] = d.default
        for key, value in (overrides or {}).items():
            d = self._defs.get(key)
            if d is None:
                raise ValueError(f"unknown configuration key {key!r}")
            norm = normalize(d, value)
            if norm is None:
                raise ValueError(f"configuration key {key!r}: {value!r} is not a valid {d.type.value}")
            self._values[key] = norm  # overrides may set readonly keys

    # ------------------------------------------------------------------ queries

    def __contains__(self, key: object) -> bool:
        return key in self._defs

    def definition(self, key: str) -> ConfigKeyDef:
        return self._defs[key]

    def value(self, key: str) -> str:
        return self._values[key]

    def get(self, keys: Sequence[str] = ()) -> Tuple[List[ConfigEntry], List[str]]:
        """``(known, unknown)``; empty ``keys`` = every key."""
        if not keys:
            return [ConfigEntry(k, d.readonly, self._values[k]) for k, d in self._defs.items()], []
        known, unknown = [], []
        for k in keys:
            d = self._defs.get(k)
            if d is None:
                unknown.append(k)
            else:
                known.append(ConfigEntry(k, d.readonly, self._values[k]))
        return known, unknown

    def get_int(self, key: str) -> int:
        self._expect(key, KeyType.INT)
        return int(self._values[key])

    def get_bool(self, key: str) -> bool:
        self._expect(key, KeyType.BOOL)
        return self._values[key] == _TRUE

    def get_csl(self, key: str) -> List[str]:
        self._expect(key, KeyType.CSL)
        v = self._values[key]
        return v.split(",") if v else []

    def _expect(self, key: str, t: KeyType) -> None:
        if self._defs[key].type is not t:
            raise TypeError(f"configuration key {key!r} is {self._defs[key].type.value}, not {t.value}")

    # ------------------------------------------------------------------ changes

    def set(self, key: str, value: str) -> ConfigStatus:
        """ChangeConfiguration semantics: NotSupported / Rejected / RebootRequired / Accepted."""
        d = self._defs.get(key)
        if d is None:
            return ConfigStatus.NOT_SUPPORTED
        if d.readonly:
            return ConfigStatus.REJECTED
        norm = normalize(d, value)
        if norm is None:
            return ConfigStatus.REJECTED
        changed = self._values[key] != norm
        self._values[key] = norm
        if changed:
            for cb in list(self._listeners):
                cb(key, norm)
        return ConfigStatus.REBOOT_REQUIRED if d.reboot_required else ConfigStatus.ACCEPTED

    def add_listener(self, cb: Listener) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Listener) -> None:
        self._listeners.remove(cb)
