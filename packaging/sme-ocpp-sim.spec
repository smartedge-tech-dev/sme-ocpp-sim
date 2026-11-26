# PyInstaller spec (onefile). Build from the repo root:
#   pyinstaller packaging/sme-ocpp-sim.spec --noconfirm
# SPECPATH is the directory of this spec file (packaging/).
#
# sme-sim-common ships a PyInstaller hook (pyinstaller40 entry point) that collects its own static
# files (shell / vendor / common i18n) and the uvicorn / multipart / yaml / websockets hidden imports.
# Do NOT list those here. Only add what belongs to this product.
#
# No protocol library data: the OCPP-J RPC layer and message models are in-house, and the `ocpp`
# package is a dev-only test peer, so there is no collect_data_files("ocpp") / collect_submodules.
import os
from PyInstaller.utils.hooks import collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    # Product static files (index.html, app.js, style.css, i18n/, favicon)
    (os.path.join(root, "sme_ocpp_sim", "static"), "sme_ocpp_sim/static"),
    # Demo config, beside the package as in the source tree and the Docker image
    (os.path.join(root, "configs", "demo-site.yaml"), "configs"),
    (os.path.join(root, "LICENSE"), "."),
    (os.path.join(root, "THIRD-PARTY-NOTICES"), "."),
]

a = Analysis(
    [os.path.join(root, "packaging", "entry.py")],
    pathex=[root],
    datas=datas,
    # Version adapters (sme_ocpp_sim.protocol.v16, ...) are imported by name at runtime
    hiddenimports=collect_submodules("sme_ocpp_sim"),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sme-ocpp-sim",
    console=True,
    upx=False,
)
