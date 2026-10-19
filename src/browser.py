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

# Где на странице поля ввода — прямоугольники в css-пикселях вкладки. Сервер шлёт их панели
# сам после перерисовки, а панель решает в момент касания, поднимать ли клавиатуру.
# Спрашивать на касании нельзя: по мобильной сети ответ приходил позже отпускания пальца,
# а клавиатуру iOS поднимает только внутри жеста. Заходит в открытые shadow root и iframe
# своего origin; чужой iframe (капча) не виден — для него у панели кнопка клавиатуры.
EDITS = """(() => { const out = [], skip = /^(button|submit|reset|checkbox|radio|file|image|range|color|hidden)$/i;
  const walk = (root, dx, dy, depth) => {
    if (depth > 4) return;
    for (const e of root.querySelectorAll('*')) {
      if (e.shadowRoot) walk(e.shadowRoot, dx, dy, depth + 1);
      if (e.tagName === 'IFRAME') {
        let d; try { d = e.contentDocument; } catch (_) { d = null; }
        if (d) { const r = e.getBoundingClientRect(); walk(d, dx + r.left, dy + r.top, depth + 1); }
        continue;
      }
      if (!(e.tagName === 'TEXTAREA' || (e.tagName === 'INPUT' && !skip.test(e.type)) || e.isContentEditable)) continue;
      if (e.isContentEditable && e.parentElement && e.parentElement.isContentEditable) continue;
      const r = e.getBoundingClientRect();
      if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > innerHeight) continue;
      out.push([Math.round(r.left + dx), Math.round(r.top + dy), Math.round(r.width), Math.round(r.height)]);
      if (out.length >= 60) return;
    }
  };
  walk(document, 0, 0, 0);
  return out; })()"""

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
        # chrome:// — служебные страницы интерфейса (после входа в Google висит
        # chrome://signin-dice-web-intercept…): вкладками они не являются и не закрываются.
        return [t for t in await r.json() if t.get("type") == "page" and not t.get("url", "").startswith("chrome://")]


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


async def api_new(req: web.Request) -> web.Response:
    """Новая пустая вкладка: шторка переключается на неё, адрес вводят в строке."""
    if not enabled():
        raise web.HTTPNotFound(text="BROWSER=off")
    try:
        async with (aiohttp.ClientSession() as s,
                    s.put(f"{CDP}/json/new?about:blank", timeout=aiohttp.ClientTimeout(total=5)) as r):
            return web.json_response({"id": (await r.json())["id"]})
    except (TimeoutError, aiohttp.ClientError, OSError, KeyError):
        raise web.HTTPServiceUnavailable(text="браузер не отвечает") from None


async def api_close(req: web.Request) -> web.Response:
    """Закрыть вкладку. WhatsApp — нельзя: на ней живёт вход и скилл чтения чатов."""
    if not enabled():
        raise web.HTTPNotFound(text="BROWSER=off")
    tab = req.query.get("tab", "")
    if not TAB_RE.match(tab):
        raise web.HTTPBadRequest(text="нужен tab")
    try:
        t = next((t for t in await _tabs() if t["id"] == tab), None)
        if t is None:
            raise web.HTTPNotFound(text="нет такой вкладки")
        if NO_MOBILE_UA.match(t.get("url", "")):
            raise web.HTTPForbidden(text="вкладку WhatsApp не закрываем")
        async with (aiohttp.ClientSession() as s,
                    s.get(f"{CDP}/json/close/{tab}", timeout=aiohttp.ClientTimeout(total=5)) as r):
            await r.read()
    except (TimeoutError, aiohttp.ClientError, OSError):
        raise web.HTTPServiceUnavailable(text="браузер не отвечает") from None
    return web.json_response({"ok": True})


async def api_log(req: web.Request) -> web.Response:
    """Почему панель закрыла соединение шторки — в лог бота. Соединение пересоздавалось
    каждые 5-10 с, и по логам сервера и Traefik не было видно, кто его рвёт."""
    try:
        data = await req.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="нужен json") from None
    log.info("шторка: %s", json.dumps(data, ensure_ascii=False)[:500])
    return web.json_response({"ok": True})


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
        # Телефону — и экран телефона: с мобильным UA, но десктопным screen 800x600 вкладка
        # выглядела для антибота «смартфоном с экраном ноутбука». Высота — по пропорциям
        # Pixel 8 (412x915), не меньше окна шторки.
        w, h = _clamp(msg.get("w"), 200, MAX_SIDE), _clamp(msg.get("h"), 150, MAX_SIDE)
        mobile = msg.get("mobile") is True
        metrics = {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile}
        if mobile:
            metrics.update(screenWidth=w, screenHeight=max(h, round(w * 915 / 412)))
        return [("Page.stopScreencast", {}),
                ("Emulation.setDeviceMetricsOverride", metrics),
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
        edits_ids: set[int] = set()              # запросы прямоугольников полей
        edits_task: asyncio.Task | None = None
        edits_last: list = []
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

        async def ask_edits() -> None:
            # Не чаще раза в 0.3 с: после перерисовки страница ещё досчитывает раскладку,
            # а во время прокрутки кадров десятки в секунду.
            await asyncio.sleep(0.3)
            edits_ids.add(await send("Runtime.evaluate", {"expression": EDITS, "returnByValue": True}))

        async def from_cdp() -> None:
            nonlocal edits_task
            async for m in cdp:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(m.data)
                if ev.get("id") in edits_ids:
                    edits_ids.discard(ev["id"])
                    v = ev.get("result", {}).get("result", {}).get("value")
                    if isinstance(v, list) and v != edits_last:
                        edits_last[:] = v
                        await ws.send_json({"t": "edits", "r": v})
                    continue
                if ev.get("id") in shots:
                    w, h = shots.pop(ev["id"])
                    if "data" in ev.get("result", {}):
                        await ws.send_json({"t": "frame", "n": None, "data": ev["result"]["data"], "w": w, "h": h})
                    continue
                name, p = ev.get("method"), ev.get("params", {})
                if name == "Page.screencastFrame" and (edits_task is None or edits_task.done()):
                    edits_task = asyncio.create_task(ask_edits())
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
                for method, params in command(msg):
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
            if edits_task:
                edits_task.cancel()
            await cdp.close()     # с сессией уходят и screencast, и подгонка размера
    return ws
