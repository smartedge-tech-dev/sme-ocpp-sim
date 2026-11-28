"""Streaming trace recorder (v0.3 plan §3.6, Q7).

:class:`TraceRecorder` writes a :mod:`.format` v1 trace. The event loop only serialises a line and puts it on a
bounded queue (:meth:`frame` / :meth:`event` never block and never raise); a background thread writes the file. A
full queue drops the line and counts it; the count is written into the footer (``dropped``).

The frames are the frame log's entries, so they are already masked (:meth:`.framelog.FrameLog.add`); nothing is
recorded unmasked. Credentials a replay needs are passed to the replay as arguments (Q7).

Rotation: with ``rotate_mb`` a part that reaches the size is closed with a footer whose ``next`` names the next
part (``run.jsonl`` -> ``run.1.jsonl`` -> ``run.2.jsonl`` ...); every part starts with its own header (``part``,
``previous``). :func:`.format.load` follows the chain.

``chargers`` (optional) limits the recording to those charger ids (a scenario records only its own chargers).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Dict, List, Optional, Union

from . import format as fmt

log = logging.getLogger(__name__)

#: Lines the write queue holds at most (a slow disk loses lines beyond it, counted in ``dropped``).
QUEUE_SIZE = 50_000
_STOP = object()


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def part_path(path: Path, part: int) -> Path:
    """File of part ``part`` (0 = ``path`` itself, then ``stem.N.suffix``)."""
    return path if part == 0 else path.with_name(f"{path.stem}.{part}{path.suffix}")


class TraceRecorder:
    def __init__(self, path: Union[str, Path], *, tool_version: str, time_scale: float = 1.0,
                 chargers_info: Optional[List[Dict[str, Any]]] = None, sim_start: Optional[str] = None,
                 sim_now: Optional[Callable[[], float]] = None, rotate_mb: Optional[float] = None,
                 chargers: Optional[Collection[str]] = None, queue_size: int = QUEUE_SIZE,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.tool_version = tool_version
        self.time_scale = time_scale
        self.chargers_info = list(chargers_info or [])
        self.sim_start = sim_start
        self._sim_now = sim_now
        self._sim0 = sim_now() if sim_now is not None else 0.0
        self.rotate_bytes = int(rotate_mb * 1024 * 1024) if rotate_mb else 0
        self.only = frozenset(chargers) if chargers is not None else None
        self._clock = clock
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=queue_size)
        self.frames = 0          # lines accepted (queued)
        self.events = 0
        self.dropped = 0
        self.started = _iso_now()
        self.ended: Optional[str] = None
        self.parts: List[str] = []
        self.error: Optional[str] = None
        self._bytes = 0
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    # ------------------------------------------------------------ lifecycle

    def start(self) -> "TraceRecorder":
        """Create the first part (errors such as a missing directory raise here, in the caller) and start the
        writer thread."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._open(0)
        self._thread = threading.Thread(target=self._writer, args=(fh,), name=f"trace-{self.path.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Flush the queue, write the footer and close (blocks until the writer is done; idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._q.put(_STOP)       # blocking put: the stop marker is never dropped
        if self._thread is not None:
            self._thread.join()
        self.ended = _iso_now()

    @property
    def recording(self) -> bool:
        return self._thread is not None and not self._closed

    def status(self) -> Dict[str, Any]:
        return {"recording": self.recording, "path": str(self.path), "parts": list(self.parts),
                "started": self.started, "ended": self.ended, "frames": self.frames, "events": self.events,
                "dropped": self.dropped, "chargers": sorted(self.only) if self.only is not None else None,
                "error": self.error}

    # ------------------------------------------------------------ feeding (event loop side)

    def sim_t(self) -> Optional[float]:
        if self._sim_now is None:
            return None
        return round(self._sim_now() - self._sim0, 3)

    def _put(self, line: Dict[str, Any]) -> bool:
        if self._closed:
            return False
        try:
            self._q.put_nowait(fmt.dumps(line))
            return True
        except queue.Full:
            self.dropped += 1
            return False
        except Exception:  # noqa: BLE001 - e.g. an unserialisable detail: never break the frame path
            log.exception("trace: cannot record a line")
            self.dropped += 1
            return False

    def frame(self, entry: Any) -> None:
        """Record one :class:`.framelog.FrameEntry`."""
        if self.only is not None and entry.charger not in self.only:
            return
        if self._put(fmt.frame_line(entry, self.sim_t())):
            self.frames += 1

    def event(self, charger: Optional[str], event: str, detail: Optional[Dict[str, Any]] = None) -> None:
        """Record one event (``charger`` None = engine-wide, e.g. ``clock``)."""
        if self.only is not None and charger is not None and charger not in self.only:
            return
        if self._put(fmt.event_line(self._clock(), self.sim_t(), charger, event, detail)):
            self.events += 1

    # ------------------------------------------------------------ writer thread

    def _open(self, part: int):
        p = part_path(self.path, part)
        fh = p.open("w", encoding="utf-8", newline="\n")
        prev = part_path(self.path, part - 1).name if part else None
        text = fmt.dumps(fmt.header(tool_version=self.tool_version, created=_iso_now(), time_scale=self.time_scale,
                                    sim_start=self.sim_start, chargers=self.chargers_info, part=part,
                                    previous=prev))
        fh.write(text)
        self._bytes = len(text.encode("utf-8"))
        self.parts.append(str(p))
        return fh

    def _footer(self, fh, next_part: Optional[str]) -> None:
        fh.write(fmt.dumps(fmt.footer(ended=_iso_now(), frames=self.frames, events=self.events,
                                      dropped=self.dropped, next_part=next_part)))

    def _writer(self, fh) -> None:
        part = 0
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                fh.write(item)
                self._bytes += len(item.encode("utf-8"))
                if self.rotate_bytes and self._bytes >= self.rotate_bytes:
                    part += 1
                    self._footer(fh, part_path(self.path, part).name)
                    fh.close()
                    fh = self._open(part)
            self._footer(fh, None)
        except Exception as e:  # noqa: BLE001 - a disk error ends the recording; reported in status()
            self.error = f"{type(e).__name__}: {e}"
            log.error("trace %s: %s", self.path, self.error)
            self._closed = True
            while True:  # unblock a stop() waiting to put its marker
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
