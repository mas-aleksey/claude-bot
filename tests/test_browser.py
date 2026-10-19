"""Разбор команд шторки браузера: из того, что шлёт панель, в вызовы CDP. Живого Chromium
тут нет — проверяется ровно та часть, которая решает, что уйдёт во вкладку."""

import browser


def test_click_keeps_coordinates_and_button():
    [(m, p)] = browser.command({"t": "mouse", "type": "mousePressed", "x": 10, "y": 20, "button": "left", "clicks": 1})
    assert m == "Input.dispatchMouseEvent"
    assert (p["type"], p["x"], p["y"], p["button"], p["clickCount"]) == ("mousePressed", 10, 20, "left", 1)


def test_unknown_mouse_type_and_garbage_are_dropped():
    assert browser.command({"t": "mouse", "type": "mouseDragged"}) == []
    assert browser.command({"t": "eval", "code": "alert(1)"}) == []
    assert browser.command({}) == []


def test_coordinates_are_clamped_not_trusted():
    [(_, p)] = browser.command({"t": "wheel", "x": "abc", "y": -5, "dy": 10**9})
    assert (p["x"], p["y"], p["deltaY"]) == (0, 0, 5000)


def test_enter_carries_text_other_keys_do_not():
    enter = browser.command({"t": "key", "key": "Enter", "code": "Enter", "vk": 13})
    assert enter[0][1]["type"] == "keyDown" and enter[0][1]["text"] == "\r"
    back = browser.command({"t": "key", "key": "Backspace", "code": "Backspace", "vk": 8})
    assert back[0][1]["type"] == "rawKeyDown" and "text" not in back[0][1]
    assert [c[1]["type"] for c in back] == ["rawKeyDown", "keyUp"]


def test_bare_host_becomes_https():
    [(m, p)] = browser.command({"t": "nav", "url": " example.com "})
    assert (m, p["url"]) == ("Page.navigate", "https://example.com")
    [(_, p)] = browser.command({"t": "nav", "url": "http://x.local/a"})
    assert p["url"] == "http://x.local/a"


def test_size_overrides_viewport_and_restarts_screencast():
    calls = browser.command({"t": "size", "w": 99999, "h": 10})
    assert [m for m, _ in calls] == ["Page.stopScreencast", "Emulation.setDeviceMetricsOverride",
                                     "Page.startScreencast"]
    assert (calls[1][1]["width"], calls[1][1]["height"]) == (browser.MAX_SIDE, 150)
    assert calls[1][1]["mobile"] is False and calls[1][1]["deviceScaleFactor"] == 1


def test_phone_gets_mobile_layout_only_for_a_real_true():
    calls = dict(browser.command({"t": "size", "w": 390, "h": 600, "mobile": True}))
    assert calls["Emulation.setDeviceMetricsOverride"]["mobile"] is True
    junk = dict(browser.command({"t": "size", "w": 390, "h": 600, "mobile": "yes"}))
    assert junk["Emulation.setDeviceMetricsOverride"]["mobile"] is False
