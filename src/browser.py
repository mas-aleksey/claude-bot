"""Общий браузер в шторке панели: картинка вкладки и ввод в неё, через CDP.

Chromium из `browser.sh` слушает DevTools на 127.0.0.1:9222 — в том же netns, что и бот,
поэтому ходить к нему отсюда можно напрямую, без сокета для ssh-туннеля. Панель держит
один websocket на открытую шторку, сервер — одну CDP-сессию на этот websocket:

- кадры: `Page.startScreencast` → `Page.screencastFrame` (jpeg base64) → панель рисует
  на canvas и подтверждает, только тогда сервер шлёт `screencastFrameAck`. Без
  подтверждений Chromium перестаёт слать кадры сам — медленный телефон не копит очередь;
- ввод: клики, колесо, клавиши и текст из панели → `Input.dispatch*` / `Input.insertText`;
- размер: вкладка подгоняется под шторку `Emulation.setDeviceMetricsOverride`. Подгонка
  живёт ровно столько, сколько CDP-сессия: закрыли шторку — сессия ушла, и Chromium
  вернул вкладке прежний размер сам, claude её не увидит перекошенной.

Трансляция идёт, только пока websocket открыт: закрытая шторка не стоит кадров.
"""

import asyncio
import json
import logging
import os
import re

import aiohttp
from aiohttp import web

log = logging.getLogger("claude_bot.browser")

CDP = "http://127.0.0.1:9222"
# id вкладки у Chromium — 32 hex. Попадает в адрес CDP, поэтому проверяется.
TAB_RE = re.compile(r"[0-9A-F]{32}\Z")
# Кадр не крупнее экрана шторки: дальше это лишние байты, глазом разницы нет.
MAX_SIDE = 2000
QUALITY = 80

PROBE = """(() => { let d = document, x = %d, y = %d, e = null;
  for (let i = 0; i < 8; i++) {
    e = d.elementFromPoint(x, y);
    while (e && e.shadowRoot) { const s = e.shadowRoot.elementFromPoint(x, y); if (!s || s === e) break; e = s; }
    if (!e || e.tagName !== 'IFRAME') break;
    let inner; try { inner = e.contentDocument; } catch (_) { inner = null; }
    if (!inner) return false;
    const r = e.getBoundingClientRect(); x -= r.left; y -= r.top; d = inner;
  }
  if (!e) return false;
  if (e.isContentEditable || e.tagName === 'TEXTAREA') return true;
  const l = e.closest && e.closest('label'); if (l && l.control) e = l.control;
  return e.tagName === 'INPUT' && !/^(button|submit|reset|checkbox|radio|file|image|range|color|hidden)$/i.test(e.type);
})()"""

# Мобильный UA — только на время просмотра с телефона и только в сессии шторки: Google и
# Википедия выбирают вёрстку по UA и Client Hints, а не по ширине, и на 390px отдавали
# десктоп. Подмена снимается вместе с CDP-сессией, claude после шторки видит прежний UA.
# WhatsApp — никогда: на мобильный UA он отвечает «скачайте приложение».
NO_MOBILE_UA = re.compile(r"^https://web\.whatsapp\.com/")


def mobile_ua(major: str) -> dict:
    return {"userAgent": f"Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                         f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Mobile Safari/537.36",
            "platform": "Linux armv8l",
            "userAgentMetadata": {
                "brands": [{"brand": "Chromium", "version": major}, {"brand": "Not:A-Brand", "version": "99"}],
                "fullVersion": f"{major}.0.0.0", "platform": "Android", "platformVersion": "14.0.0",
                "architecture": "", "model": "Pixel 8", "mobile": True}}


def enabled() -> bool:
    return os.environ.get("BROWSER") != "off"


async def _tabs() -> list[dict]:
    async with (aiohttp.ClientSession() as s,
                s.get(f"{CDP}/json/list", timeout=aiohttp.ClientTimeout(total=3)) as r):
        return [t for t in await r.json() if t.get("type") == "page"]


async def api_tabs(req: web.Request) -> web.Response:
    """Вкладки общего браузера. 404 — браузера нет, шторка прячет вкладку «браузер»."""
    if not enabled():
        raise web.HTTPNotFound(text="BROWSER=off")
    try:
        tabs = await _tabs()
    except (TimeoutError, aiohttp.ClientError, OSError):
        raise web.HTTPNotFound(text="браузер не отвечает") from None
    return web.json_response([{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")}
                              for t in tabs])


def _clamp(v, lo, hi) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return lo


# Что панель шлёт → чем это становится в CDP. Разбор отдельно от сокета: это единственная
# логика с ветвлениями, она и проверяется тестом, без живого Chromium.
def command(msg: dict) -> list[tuple[str, dict]]:
    t = msg.get("t")
    x, y = _clamp(msg.get("x"), 0, 10000), _clamp(msg.get("y"), 0, 10000)
    mods = _clamp(msg.get("mods"), 0, 15)
    if t == "mouse" and msg.get("type") in ("mousePressed", "mouseReleased", "mouseMoved"):
        button = msg.get("button") if msg.get("button") in ("left", "right", "middle") else "none"
        return [("Input.dispatchMouseEvent", {
            "type": msg["type"], "x": x, "y": y, "button": button, "modifiers": mods,
            "clickCount": _clamp(msg.get("clicks"), 0, 3)})]
    if t == "wheel":
        return [("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y, "modifiers": mods,
            "deltaX": _clamp(msg.get("dx"), -5000, 5000), "deltaY": _clamp(msg.get("dy"), -5000, 5000)})]
    if t == "text" and isinstance(msg.get("text"), str):
        return [("Input.insertText", {"text": msg["text"][:10000]})]
    if t == "key" and isinstance(msg.get("key"), str):
        # Только клавиши без печатного символа (Enter, Backspace, стрелки) и сочетания:
        # сам текст приходит отдельно через `text`, иначе буква печаталась бы дважды.
        base = {"key": msg["key"][:32], "code": str(msg.get("code", ""))[:32],
                "windowsVirtualKeyCode": _clamp(msg.get("vk"), 0, 255), "modifiers": mods}
        down = dict(base, type="rawKeyDown")
        if msg["key"] == "Enter":
            down = dict(base, type="keyDown", text="\r")   # без text форма не отправляется
        return [("Input.dispatchKeyEvent", down), ("Input.dispatchKeyEvent", dict(base, type="keyUp"))]
    if t == "nav" and isinstance(msg.get("url"), str):
        url = msg["url"].strip()
        if not re.match(r"[a-z][a-z0-9+.-]*:", url):
            url = "https://" + url
        return [("Page.navigate", {"url": url[:4000]})]
    if t == "probe":
        # Что под пальцем: поле ввода или нет. Панель поднимает экранную клавиатуру только
        # над полем — иначе она вылезала на каждое касание и прокрутку. Спрашиваем на
        # касании, ответ нужен к отпусканию: фокус для клавиатуры iOS ставится только
        # внутри жеста, позже уже нельзя. Заходит в shadow DOM и iframe своего origin;
        # чужой iframe (капча) — «не знаю», для него у панели кнопка клавиатуры.
        return [("Runtime.evaluate", {"returnByValue": True, "expression": PROBE % (x, y)})]
    if t == "back":
        return [("Runtime.evaluate", {"expression": "history.back()"})]
    if t == "reload":
        return [("Page.reload", {})]
    if t == "size":
        # Телефону — мобильная вёрстка: десктопная страница шириной 390px мелкая.
        # Плотность пикселей не передаём: screencast отдаёт кадры в css-пикселях при любой
        # (проверено: dpr 3 → кадр 390x600, хотя captureScreenshot даёт 1170x1800), а
        # досылать резкий кадр скриншотом нельзя — снимок посреди трансляции на миг
        # переключает вкладку в физические пиксели, кадры начинают чередоваться 390 и 1170
        # по ширине вместе с метаданными, и клики уезжают. Чёткость добирает QUALITY.
        # stop перед start обязателен: второй startScreencast поверх идущего Chromium молча
        # игнорирует, и после смены размера кадры не приходили вовсе.
        w, h = _clamp(msg.get("w"), 200, MAX_SIDE), _clamp(msg.get("h"), 150, MAX_SIDE)
        return [("Page.stopScreencast", {}),
                ("Emulation.setDeviceMetricsOverride",
                 {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": msg.get("mobile") is True}),
                ("Page.startScreencast",
                 {"format": "jpeg", "quality": QUALITY, "maxWidth": w, "maxHeight": h})]
    return []


async def api_ws(req: web.Request) -> web.WebSocketResponse:
    if not enabled():
        raise web.HTTPNotFound(text="BROWSER=off")
    tab = req.query.get("tab", "")
    if not TAB_RE.match(tab):
        raise web.HTTPBadRequest(text="нужен tab")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(req)
    async with aiohttp.ClientSession() as s:
        try:
            cdp = await s.ws_connect(f"ws://127.0.0.1:9222/devtools/page/{tab}", max_msg_size=0)
        except (aiohttp.ClientError, OSError) as e:
            await ws.send_json({"t": "gone", "why": str(e)[:200]})
            await ws.close()
            return ws
        seq = 0
        shots: dict[int, tuple[int, int]] = {}   # id запроса скриншота → css-размер кадра
        probes: dict[int, object] = {}           # id запроса пробы → id касания в панели
        ua_major = ""
        mobile_on = False
        try:
            async with s.get(f"{CDP}/json/version") as r:
                ua_major = re.search(r"/(\d+)\.", (await r.json()).get("Browser", "")).group(1)
        except (aiohttp.ClientError, AttributeError, OSError):
            pass
        page_url = next((t.get("url", "") for t in await _tabs() if t["id"] == tab), "")

        async def send(method: str, params: dict) -> int:
            nonlocal seq
            seq += 1
            await cdp.send_json({"id": seq, "method": method, "params": params})
            return seq

        async def from_cdp() -> None:
            async for m in cdp:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(m.data)
                if ev.get("id") in probes:
                    touch = probes.pop(ev["id"])
                    v = ev.get("result", {}).get("result", {}).get("value")
                    await ws.send_json({"t": "probe", "id": touch, "edit": v is True})
                    continue
                if ev.get("id") in shots:
                    w, h = shots.pop(ev["id"])
                    if "data" in ev.get("result", {}):
                        await ws.send_json({"t": "frame", "n": None, "data": ev["result"]["data"], "w": w, "h": h})
                    continue
                name, p = ev.get("method"), ev.get("params", {})
                if name == "Page.screencastFrame":
                    await ws.send_json({"t": "frame", "n": p["sessionId"], "data": p["data"],
                                        "w": p["metadata"]["deviceWidth"], "h": p["metadata"]["deviceHeight"]})
                elif name == "Page.frameNavigated" and not p.get("frame", {}).get("parentId"):
                    await ws.send_json({"t": "url", "url": p["frame"].get("url", "")})
                elif name in ("Inspector.detached", "Inspector.targetCrashed"):
                    break
            if not ws.closed:
                await ws.send_json({"t": "gone", "why": "вкладка закрыта"})
                await ws.close()

        reader = asyncio.create_task(from_cdp())
        await send("Page.enable", {})
        try:
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                msg = json.loads(m.data)
                if msg.get("t") == "ack":
                    await send("Page.screencastFrameAck", {"sessionId": _clamp(msg.get("n"), 0, 2**31)})
                    continue
                # Режим внутри одной сессии не меняется (тач-экран остаётся тач-экраном), а
                # снимает подмену закрытие сессии — отдельной ветки «выключить» не нужно.
                if (msg.get("t") == "size" and msg.get("mobile") is True and not mobile_on
                        and ua_major and not NO_MOBILE_UA.match(page_url)):
                    mobile_on = True
                    await send("Emulation.setUserAgentOverride", mobile_ua(ua_major))
                cmds = command(msg)
                if msg.get("t") == "probe" and cmds:
                    method, params = cmds[0]
                    probes[await send(method, params)] = msg.get("id")
                    continue
                for method, params in cmds:
                    await send(method, params)
                # Screencast шлёт кадр только на перерисовку: к неподвижной странице (главная
                # Google) шторка подключалась и показывала серое поле — 0 кадров за 5 с.
                # Первый кадр — скриншотом. Плотность всегда 1, поэтому снимок не сбивает
                # размеры трансляции (сбивал при dpr 3, см. command("size")).
                if msg.get("t") == "size":
                    shots[await send("Page.captureScreenshot", {"format": "jpeg", "quality": QUALITY})] = (
                        _clamp(msg.get("w"), 200, MAX_SIDE), _clamp(msg.get("h"), 150, MAX_SIDE))
        except (aiohttp.ClientError, ConnectionResetError, json.JSONDecodeError) as e:
            log.info("browser ws: %s", e)
        finally:
            reader.cancel()
            await cdp.close()     # с сессией уходят и screencast, и подгонка размера
    return ws
