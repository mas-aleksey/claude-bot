"""Подстраховка на все тесты: живой `/data/bot.db` трогать нельзя.

`sessions.title` читает имя сессии из `store`, а через него проходят все списки — то
есть в базу теперь лезут и тесты, которые про `store` ничего не знают. Без этой
фикстуры они создавали бы таблицы в боевом файле бота. Кто подменяет `DB_PATH` сам,
делает это после и ничего не теряет.
"""

import threading

import pytest

import store


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "store.db"))
    monkeypatch.setattr(store, "_local", threading.local())


@pytest.fixture(autouse=True)
def _no_api_key(tmp_path, monkeypatch):
    """Ключ API из окружения или живого settings.json перевёл бы тесты подписки в режим
    ключа: бот в песочнице и сам ходит по ключу."""
    import account
    monkeypatch.setattr(account, "SETTINGS", str(tmp_path / "settings.json"))
    for k in account.KEY_VARS:
        monkeypatch.delenv(k, raising=False)
