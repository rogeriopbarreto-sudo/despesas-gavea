"""Screenshot mobile via CDP — uso interno de verificação, não vai para o deploy."""
import base64
import json
import subprocess
import sys
import time
import urllib.request

import websocket

PORT = 9555
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "shot.png"
TAB_JS = sys.argv[3] if len(sys.argv) > 3 else ""

import os
import tempfile

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
chrome_exe = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
if not chrome_exe:
    sys.exit("Chrome não encontrado.")

profile = os.path.join(tempfile.gettempdir(), "cdp-prof")
chrome = subprocess.Popen([
    chrome_exe,
    "--headless=new", f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
    "--force-prefers-reduced-motion", f"--user-data-dir={profile}", "about:blank",
])
try:
    tabs = None
    for _ in range(40):
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
            if tabs:
                break
        except Exception:
            time.sleep(0.5)
    if not tabs:
        sys.exit("CDP não respondeu na porta.")
    ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], suppress_origin=True)
    mid = 0

    def send(method, params=None):
        global mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})

    send("Emulation.setDeviceMetricsOverride",
         {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
    send("Page.enable")
    send("Page.navigate", {"url": URL})
    time.sleep(4)
    if TAB_JS:
        send("Runtime.evaluate", {"expression": TAB_JS})
        time.sleep(2.5)
    chk = send("Runtime.evaluate", {"expression":
        "JSON.stringify({sw: document.documentElement.scrollWidth, iw: innerWidth})"})
    print("overflow-check:", chk.get("result", {}).get("value"))
    shot = send("Page.captureScreenshot", {"format": "png"})
    with open(OUT, "wb") as f:
        f.write(base64.b64decode(shot["data"]))
    print("salvo:", OUT)
finally:
    chrome.kill()
