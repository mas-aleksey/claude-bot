"""Файл из чата: что именно уходит в промпт."""

import asyncio
import os

import pytest

os.environ.setdefault("TG_BOT_TOKEN", "x")  # app читает env на импорте

import app


class FakeFile:
    def __init__(self, file_name=None, uid="uid1"):
        self.file_name = file_name
        self.file_unique_id = uid


class FakeMessage:
    """Ровно те поля Message, которых касается on_file."""

    def __init__(self, document=None, photo=None, caption=None,
                 media_group_id=None, message_id=1):
        self.document, self.photo, self.caption = document, photo, caption
        self.media_group_id, self.message_id = media_group_id, message_id
        self.replies: list[str] = []
        self.downloaded: list[tuple] = []
        self.bot = self

    async def download(self, file, dest):
        self.downloaded.append((file, dest))
        dest.write_bytes(b"payload")

    async def answer(self, text, **_):
        self.replies.append(text)


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(app, "login", None)
    sent = []
    # handle() — весь путь промпта до claude; тут проверяем только его вход.
    monkeypatch.setattr(app, "handle", lambda msg, prompt: sent.append(prompt) or _noop())
    return tmp_path / "inbox", sent


async def _noop():
    return None


async def test_caption_and_path_both_reach_prompt(inbox):
    dirpath, sent = inbox
    msg = FakeMessage(document=FakeFile("report.pdf"), caption="что тут не так?")
    await app.on_file(msg)
    prompt = sent[0]
    assert "что тут не так?" in prompt
    assert str(dirpath) in prompt
    assert prompt.endswith("report.pdf")


async def test_file_without_caption_sends_path_alone(inbox):
    _, sent = inbox
    await app.on_file(FakeMessage(document=FakeFile("a.txt")))
    assert sent[0].endswith("a.txt")
    assert "\n" not in sent[0]  # пустой подписи не остаётся дырки


async def test_photo_takes_largest_size(inbox):
    _, sent = inbox
    small, large = FakeFile(uid="s"), FakeFile(uid="l")
    msg = FakeMessage(photo=[small, large])
    await app.on_file(msg)
    assert msg.downloaded[0][0] is large
    assert sent[0].endswith("l.jpg")  # у фото нет file_name — имя из uid


async def test_path_traversal_in_file_name_stays_in_inbox(inbox):
    dirpath, sent = inbox
    await app.on_file(FakeMessage(document=FakeFile("../../etc/passwd")))
    assert sent[0].startswith(str(dirpath))
    assert ".." not in sent[0]


async def test_file_during_login_is_refused(inbox, monkeypatch):
    _, sent = inbox
    monkeypatch.setattr(app, "login", object())
    msg = FakeMessage(document=FakeFile("a.txt"))
    await app.on_file(msg)
    assert not sent
    assert "логин" in msg.replies[0]


async def test_album_becomes_one_prompt(inbox, monkeypatch):
    """Альбом Telegram — это N сообщений с общим media_group_id и подписью ровно на
    одном. Без склейки каждое давало свой прогон, и контекст доставался одному файлу."""
    monkeypatch.setattr(app, "ALBUM_WAIT", 0.01)
    _, sent = inbox
    album = [
        FakeMessage(document=FakeFile("b.txt"), media_group_id="g1", message_id=2),
        FakeMessage(document=FakeFile("a.txt"), media_group_id="g1", message_id=1,
                    caption="разбери эти три"),
        FakeMessage(document=FakeFile("c.txt"), media_group_id="g1", message_id=3),
    ]
    await asyncio.gather(*(app.on_file(m) for m in album))

    assert len(sent) == 1, sent
    lines = sent[0].splitlines()
    assert lines[0] == "разбери эти три"
    # По message_id, а не по порядку прихода: задачи апдейтов идут параллельно.
    assert [x.split("-")[-1] for x in lines[2:]] == ["a.txt", "b.txt", "c.txt"]
    assert not app.albums   # группа снята, иначе следующий альбом дописался бы к ней


async def test_second_album_is_not_glued_to_the_first(inbox, monkeypatch):
    monkeypatch.setattr(app, "ALBUM_WAIT", 0.01)
    _, sent = inbox
    await app.on_file(FakeMessage(document=FakeFile("a.txt"), media_group_id="g1"))
    await app.on_file(FakeMessage(document=FakeFile("b.txt"), media_group_id="g2"))
    assert len(sent) == 2
    assert sent[0].endswith("a.txt") and sent[1].endswith("b.txt")
