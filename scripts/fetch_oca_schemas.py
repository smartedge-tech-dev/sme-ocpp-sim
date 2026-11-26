"""Copy the OCA OCPP 1.6 JSON schemas into ``.oca-schemas/ocpp1.6/`` (dev only, stdlib only).

Download the OCPP 1.6 package from the Open Charge Alliance website yourself (this script does
no network access: the download URL is not known to be stable), then run::

    python scripts/fetch_oca_schemas.py path/to/OCPP_1.6_download.zip
    python scripts/fetch_oca_schemas.py path/to/extracted/dir

Zip files nested one level deep inside the package are searched too. The directory holding
``BootNotification.json`` is taken as the schema directory; all ``*.json`` files in it are
copied. Then ``tests/test_v16_schema_conformance.py`` runs instead of skipping.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / ".oca-schemas" / "ocpp1.6"

ACTIONS = (
    "Authorize", "BootNotification", "ChangeAvailability", "ChangeConfiguration",
    "ClearChargingProfile", "DataTransfer", "GetCompositeSchedule", "GetConfiguration",
    "Heartbeat", "MeterValues", "RemoteStartTransaction", "RemoteStopTransaction", "Reset",
    "SetChargingProfile", "StartTransaction", "StatusNotification", "StopTransaction",
    "TriggerMessage", "UnlockConnector",
)
EXPECTED = sorted(f"{a}{s}.json" for a in ACTIONS for s in ("", "Response"))


def _from_zip(zf: zipfile.ZipFile, depth: int = 0) -> dict[str, bytes] | None:
    names = [n for n in zf.namelist() if not n.endswith("/")]
    anchor = next((n for n in names if PurePosixPath(n).name == "BootNotification.json"), None)
    if anchor:
        parent = PurePosixPath(anchor).parent
        return {
            PurePosixPath(n).name: zf.read(n)
            for n in names
            if PurePosixPath(n).parent == parent and n.endswith(".json")
        }
    if depth == 0:
        for n in names:
            if n.lower().endswith(".zip"):
                found = _from_zip(zipfile.ZipFile(io.BytesIO(zf.read(n))), depth + 1)
                if found:
                    return found
    return None


def _from_dir(root: Path) -> dict[str, bytes] | None:
    anchor = next(root.rglob("BootNotification.json"), None)
    if anchor:
        return {p.name: p.read_bytes() for p in anchor.parent.glob("*.json")}
    for z in root.rglob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            found = _from_zip(zf)
        if found:
            return found
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path, help="OCA 1.6 download (zip) or extracted directory")
    ap.add_argument("--dest", type=Path, default=DEST, help=f"target dir (default {DEST})")
    args = ap.parse_args(argv)

    if args.source.is_dir():
        files = _from_dir(args.source)
    elif zipfile.is_zipfile(args.source):
        with zipfile.ZipFile(args.source) as zf:
            files = _from_zip(zf)
    else:
        print(f"error: {args.source} is neither a directory nor a zip file", file=sys.stderr)
        return 2
    if not files:
        print(f"error: no BootNotification.json found in {args.source}", file=sys.stderr)
        return 1

    missing = [n for n in EXPECTED if n not in files]
    if missing:
        print(f"error: schema files missing: {', '.join(missing)}", file=sys.stderr)
        return 1

    if args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)
    for name, data in sorted(files.items()):
        (args.dest / name).write_bytes(data)
    print(f"copied {len(files)} schema files to {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
