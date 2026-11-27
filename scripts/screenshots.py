"""Make the README / documentation screenshots (dev only).

Starts the dev CSMS peers ``tests/peers/ocpp16_peer.py`` and ``tests/peers/ocpp201_peer.py`` in-process
(MobilityHouse ``ocpp``, a dev dependency), runs the simulator with the mixed 1.6 / 2.0.1 site
``scripts/screenshots.yaml`` in a temporary directory on a free port of 127.0.0.1, starts a transaction on every
charger, sends a TxDefaultProfile to the 2.0.1 station CP-0001 and sets an external (EMS) limit on it, and
captures the web UI with Playwright in English, Japanese and Chinese (light) plus a dark overview.
Output: ``docs/assets/screenshots/{en,ja,zh}/``.

    .venv\\Scripts\\python scripts\\screenshots.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

import yaml  # noqa: E402
from ocpp.v201 import call as call201  # noqa: E402

from tests.peers.ocpp16_peer import CsmsPeer  # noqa: E402
from tests.peers.ocpp201_peer import CsmsPeer201  # noqa: E402

OUT = ROOT / "docs" / "assets" / "screenshots"
CONFIG = ROOT / "scripts" / "screenshots.yaml"
LANGS = ("en", "ja", "zh")
STORAGE = "sme-ocpp-sim"           # data-storage prefix of the shell script tag (index.html)
VIEWPORT = {"width": 1440, "height": 1000}
WIDE = {"width": 1600, "height": 1100}  # Japanese wide image (plan §9)
FOCUS = "CP-0001"                  # the OCPP 2.0.1 station with the Smart Charging profile
CARDS = "CP-0002"                  # the two-connector OCPP 2.0.1 station shown in charger.png
EXTERNAL_LIMIT_W = 5000            # external (EMS) limit of the whole FOCUS station
WARMUP_S = 15                      # let power ramp and frames accumulate before the first page
HISTORY_S = 25                     # time on the page before capturing, so the charts have a history
GIF_FRAMES, GIF_STEP_S, GIF_COLOURS = 12, 0.6, 128

# Relative TxDefaultProfile with a step every PERIOD_S seconds of wall time. Charging schedules run on the wall
# clock (time_scale only speeds up the power model), so short periods keep several steps inside the chart window
# (which starts small because the page only plots what it has seen). The periods cover the whole script run.
PERIOD_S, PERIOD_LIMITS = 30, (6000, 3500, 5000, 2500, 4500)
# OCPP 2.0.1 field names in snake_case (the ``ocpp`` library camel-cases the payload).
PROFILE = {
    "id": 101,
    "stack_level": 0,
    "charging_profile_purpose": "TxDefaultProfile",
    "charging_profile_kind": "Relative",
    "charging_schedule": [{
        "id": 1,
        "charging_rate_unit": "W",
        "charging_schedule_period": [{"start_period": i * PERIOD_S, "limit": PERIOD_LIMITS[i % len(PERIOD_LIMITS)]}
                                     for i in range(20)],
    }],
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


async def wait_http(base: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"simulator exited with {proc.returncode}")
        try:
            await asyncio.to_thread(http, base, "GET", "/api/version")
            return
        except OSError:
            await asyncio.sleep(0.3)
    raise TimeoutError("simulator did not start")


def charger_ids() -> dict:
    """Charger id -> OCPP version of the demo site."""
    site = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    default = site["csms_defaults"]["ocpp_version"]
    return {c["id"]: c.get("ocpp_version", default) for c in site["chargers"]}


async def page_for(browser, lang: str, theme: str, viewport: dict, base: str):
    ctx = await browser.new_context(viewport=viewport, device_scale_factor=1, locale=lang)
    await ctx.add_init_script(
        f"try {{ localStorage.setItem('{STORAGE}.lang', '{lang}');"
        f" localStorage.setItem('{STORAGE}.theme', '{theme}'); }} catch (e) {{}}")
    page = await ctx.new_page()
    await page.goto(base + "/")
    await page.wait_for_function("document.querySelectorAll('#chargerRows tr').length >= 8")
    await page.evaluate(f"openChargerDialog({FOCUS!r})")
    await page.fill("#extLimit", str(EXTERNAL_LIMIT_W))  # the value the script set through the HTTP API
    await page.wait_for_timeout(HISTORY_S * 1000)  # the charts plot what the page has seen
    return ctx, page


# Element captures run with the page in document flow: the tool layout (100vh body, footer at the bottom of the
# viewport) would otherwise put the footer over any element that reaches below the viewport.
FLOW_CSS = ("body.sh-tool { height: auto !important; overflow: visible !important; }"
            " body.sh-tool > .sh-body { overflow: visible !important; } .sh-footer { display: none !important; }")


async def flow_layout(page, on: bool) -> None:
    if on:
        await page.add_style_tag(content=FLOW_CSS)
        await page.evaluate("document.head.lastElementChild.id = 'shotFlow'")
    else:
        await page.evaluate("document.getElementById('shotFlow')?.remove()")
    await page.wait_for_timeout(200)


async def inner_rect(page, selector: str) -> dict:
    """The element's box rounded inwards to whole pixels, so no sliver of the page behind it is captured."""
    return await page.evaluate("""(sel) => {
      const r = document.querySelector(sel).getBoundingClientRect();
      const x = Math.ceil(r.left), y = Math.ceil(r.top);
      return { x, y, width: Math.floor(r.right) - x, height: Math.floor(r.bottom) - y };
    }""", selector)


# Expand the JSON of a short frame (StatusNotification, else Heartbeat, else the newest) and scroll it to the top.
OPEN_FRAME_JS = """() => {
  document.getElementById('fFollow').checked = false;
  const rows = [...document.querySelectorAll('#frameRows tr:not(.xrow)')];
  const pick = (a) => rows.filter((tr) => tr.cells[5] && tr.cells[5].textContent === a).pop();
  const tr = pick('StatusNotification') || pick('Heartbeat') || rows[rows.length - 1];
  tr.click();
  const wrap = document.getElementById('frameWrap');
  wrap.scrollTop = tr.offsetTop - wrap.querySelector('thead').offsetHeight;
}"""

# Crop the variables table (a 360 px scroll box) at the last row boundary that fits.
CROP_VARS_JS = """() => {
  const wrap = document.querySelector('.tablewrap.vars');
  wrap.scrollTop = 0;
  const limit = parseFloat(getComputedStyle(wrap).maxHeight);
  let h = wrap.querySelector('thead').offsetHeight;
  for (const tr of wrap.querySelectorAll('tbody tr')) {
    if (tr.offsetTop + tr.offsetHeight > limit) break;
    h = tr.offsetTop + tr.offsetHeight;
  }
  wrap.style.maxHeight = h + 'px';
}"""

# The detail pane from its top edge down to the bottom of the EVSE cards.
CARDS_CLIP_JS = """() => {
  const pane = document.getElementById('detail').getBoundingClientRect();
  const cards = document.getElementById('connectors').getBoundingClientRect();
  const x = Math.ceil(pane.left), y = Math.ceil(pane.top);
  return { x, y, width: Math.floor(pane.right) - x, height: Math.ceil(cards.bottom) + 12 - y };
}"""


async def capture_lang(browser, lang: str, base: str) -> None:
    out = OUT / lang
    out.mkdir(parents=True, exist_ok=True)
    ctx, page = await page_for(browser, lang, "light", VIEWPORT, base)
    await page.screenshot(path=out / "overview.png")

    await flow_layout(page, True)
    sc = page.locator("#scChart").locator("xpath=ancestor::*[.//h3[@data-i18n='sc.title']][1]")
    await sc.screenshot(path=out / "smart-charging.png")
    # Frame log with one frame's JSON detail expanded (the row toggle), the row scrolled to the top of the log.
    await page.evaluate(OPEN_FRAME_JS)
    await page.wait_for_timeout(300)
    await page.locator("section.pane.frames").screenshot(path=out / "frames.png")
    await page.evaluate("document.querySelector('#frameRows tr.xrow')?.previousElementSibling.click();"
                        " document.getElementById('fFollow').checked = true")
    await page.evaluate("document.getElementById('varsBox').open = true")  # the toggle loads the variables
    await page.wait_for_function("document.querySelectorAll('#varRows tr').length > 5")
    await page.evaluate(CROP_VARS_JS)
    await page.locator("#varsBox").screenshot(path=out / "variables.png")
    await page.evaluate("document.getElementById('varsBox').open = false;"
                        " document.querySelector('.tablewrap.vars').style.maxHeight = ''")
    await flow_layout(page, False)

    frames = []
    for _ in range(GIF_FRAMES):
        im = Image.open(io.BytesIO(await page.screenshot())).convert("RGB")
        im = im.resize((im.width * 2 // 3, im.height * 2 // 3), Image.LANCZOS)
        frames.append(im.quantize(colors=GIF_COLOURS, method=Image.Quantize.MEDIANCUT))
        await page.wait_for_timeout(int(GIF_STEP_S * 1000))
    frames[0].save(out / "live.gif", save_all=True, append_images=frames[1:],
                   duration=int(GIF_STEP_S * 1000), loop=0, optimize=True)

    await page.evaluate(f"openSettingsDialog({FOCUS!r})")
    await page.wait_for_timeout(800)
    await page.screenshot(path=out / "settings.png", clip=await inner_rect(page, "#dlgSettings"))
    await page.keyboard.press("Escape")

    # EVSE cards of the two-connector 2.0.1 station: the detail header and the cards only.
    await page.evaluate(f"openChargerDialog({CARDS!r})")
    await page.wait_for_timeout(1500)
    await page.screenshot(path=out / "charger.png", clip=await page.evaluate(CARDS_CLIP_JS))
    await ctx.close()

    ctx, page = await page_for(browser, lang, "dark", VIEWPORT, base)
    await page.screenshot(path=out / "overview-dark.png")
    await ctx.close()

    if lang == "ja":
        ctx, page = await page_for(browser, lang, "light", WIDE, base)
        await page.screenshot(path=out / "overview-wide.png")
        await ctx.close()


async def main() -> None:
    versions = charger_ids()
    ids = list(versions)
    peer = CsmsPeer(hb_interval=10, registered={c for c, v in versions.items() if v == "1.6"})
    peer201 = CsmsPeer201(hb_interval=10, registered={c for c, v in versions.items() if v == "2.0.1"})
    await peer.start()
    await peer201.start()
    peers = {"1.6": peer, "2.0.1": peer201}
    tmp = Path(tempfile.mkdtemp(prefix="sme-ocpp-sim-shots-"))
    site = CONFIG.read_text(encoding="utf-8").replace("__CSMS201_URL__", peer201.url).replace("__CSMS_URL__", peer.url)
    with open(tmp / "site.yaml", "w", encoding="utf-8", newline="") as f:
        f.write(site)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    log = open(tmp / "sim.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "sme_ocpp_sim", "-c", "site.yaml", "--host", "127.0.0.1",
                             "--http-port", str(port), "--no-browser"], cwd=tmp, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        await wait_http(base, proc)
        async with asyncio.timeout(30):
            for cid in ids:
                sessions = peers[versions[cid]].sessions
                while cid not in sessions:
                    await asyncio.sleep(0.2)
                await sessions[cid].booted.wait()
        res = await peer201.request(FOCUS, call201.SetChargingProfile(evse_id=1, charging_profile=PROFILE))
        print("SetChargingProfile", FOCUS, res.status)
        for cid in ids:
            await asyncio.to_thread(http, base, "POST", f"/api/chargers/{cid}/start", {"connector": 1})
        await asyncio.to_thread(http, base, "POST", f"/api/chargers/{FOCUS}/external-limit",
                                {"connector": 0, "limit_w": EXTERNAL_LIMIT_W})
        print("fault codes of", FOCUS, http(base, "GET", f"/api/chargers/{FOCUS}")["fault_codes"])
        await asyncio.sleep(WARMUP_S)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            for lang in LANGS:
                await capture_lang(browser, lang, base)
            await browser.close()
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()
        await peer.stop()
        await peer201.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    for p in sorted(OUT.rglob("*.*")):
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size / 1024:.0f} KiB")


if __name__ == "__main__":
    asyncio.run(main())
