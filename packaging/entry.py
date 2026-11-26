"""PyInstaller entry point: run the package's CLI with absolute imports."""
from sme_ocpp_sim.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
