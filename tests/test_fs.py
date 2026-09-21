"""Дерево файлов и редактор: границы корней и защита от затирания.

Оба места нетривиальны. Путь приходит из браузера и открывается на запись, а скиллы в
`/root/.claude/skills` лежат симлинками наружу — правило «симлинк за корень отклоняем»
сделало бы нередактируемым ровно то, что правят чаще всего.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import files
import sessions
import webui


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Два корня: проект и конфиг. Ровно то, что видит панель в песочнице."""
    projects, config = tmp_path / "projects", tmp_path / "config"
    (projects / "demo").mkdir(parents=True)
    config.mkdir()
    monkeypatch.setattr(sessions, "PROJECTS_DIR", projects)
    monkeypatch.setattr(files, "FILE_ROOTS", [config, tmp_path / "нет-такого"])
    return projects / "demo", config


@pytest.fixture
async def client(roots):
    c = TestClient(TestServer(webui.build()))
    await c.start_server()
    yield c
    await c.close()


def test_roots_skip_missing_paths(roots):
    demo, config = roots
    assert [str(p) for p in files.roots()] == [str(demo), str(config)]


def test_path_outside_roots_is_refused(roots):
    with pytest.raises(web.HTTPBadRequest):
        files.inside("/etc/passwd")


def test_traversal_out_of_a_root_is_refused(roots):
    demo, _ = roots
    with pytest.raises(web.HTTPBadRequest):
        files.inside(str(demo / ".." / ".." / ".." / "etc"))


def test_symlink_into_another_root_opens(roots):
    """Так собран каждый скилл: `/root/.claude/skills/x -> /opt/skills/20-*/x`.
    Цель лежит в другом корне, поэтому путь обязан пройти."""
    demo, config = roots
    (demo / "SKILL.md").write_text("текст")
    (config / "skills").mkdir()
    (config / "skills" / "demo").symlink_to(demo)
    assert files.inside(str(config / "skills" / "demo" / "SKILL.md")).read_text() == "текст"


def test_symlink_out_of_all_roots_is_refused(roots):
    _, config = roots
    (config / "escape").symlink_to("/etc")
    with pytest.raises(web.HTTPBadRequest):
        files.inside(str(config / "escape" / "passwd"))


def test_entries_put_directories_first(roots):
    demo, _ = roots
    (demo / "b.txt").write_text("x")
    (demo / "a").mkdir()
    (demo / ".hidden").write_text("y")  # скрытые отдаём все, прячет их панель
    assert [e["name"] for e in files._entries(demo)] == ["a", ".hidden", "b.txt"]


async def test_listing_answers_with_entries(client, roots):
    demo, _ = roots
    (demo / "a.txt").write_text("x")
    res = await client.get("/api/files", params={"path": str(demo)})
    assert res.status == 200
    assert [e["name"] for e in (await res.json())["entries"]] == ["a.txt"]


async def test_listing_outside_roots_is_400(client):
    assert (await client.get("/api/files", params={"path": "/etc"})).status == 400


async def test_big_file_comes_without_text(client, roots):
    demo, _ = roots
    (demo / "big.log").write_bytes(b"a" * (files.MAX_EDIT + 1))
    body = await (await client.get("/api/file", params={"path": str(demo / "big.log")})).json()
    assert body["why"] and "text" not in body


async def test_binary_file_comes_without_text(client, roots):
    demo, _ = roots
    (demo / "a.bin").write_bytes(b"\x89PNG\x00\x1a\n")
    body = await (await client.get("/api/file", params={"path": str(demo / "a.bin")})).json()
    assert body["why"] and "text" not in body


async def test_save_writes_and_returns_new_version(client, roots):
    demo, _ = roots
    f = demo / "a.txt"
    f.write_text("было")
    read = await (await client.get("/api/file", params={"path": str(f)})).json()
    res = await client.post("/api/file", json={"path": str(f), "text": "стало",
                                               "version": read["version"]})
    assert res.status == 200
    assert f.read_text() == "стало"
    assert (await res.json())["version"] != read["version"]


async def test_save_with_stale_version_is_409(client, roots):
    """Claude правит те же файлы. Без сверки версии правка человека затёрла бы его."""
    demo, _ = roots
    f = demo / "a.txt"
    f.write_text("было")
    read = await (await client.get("/api/file", params={"path": str(f)})).json()
    f.write_text("правка claude")
    res = await client.post("/api/file", json={"path": str(f), "text": "стало",
                                               "version": read["version"]})
    assert res.status == 409
    assert f.read_text() == "правка claude"


async def test_save_to_readonly_mount_answers_400(client, roots, monkeypatch):
    """`/root/.claude/CLAUDE.md` в песочнице примонтирован `:ro`. Панель обязана
    показать текст системы, а не молча сделать вид, что сохранила."""
    demo, _ = roots
    f = demo / "a.txt"
    f.write_text("было")
    read = await (await client.get("/api/file", params={"path": str(f)})).json()

    def boom(*a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(files.Path, "write_text", boom)
    res = await client.post("/api/file", json={"path": str(f), "text": "стало",
                                               "version": read["version"]})
    assert res.status == 400
    assert "Read-only" in await res.text()


async def test_new_file_opens_empty_and_shows_up_in_listing(client, roots):
    demo, _ = roots
    r = await client.post("/api/new", json={"dir": str(demo), "name": "заметка.md"})
    assert r.status == 200
    made = await r.json()
    assert made["path"] == str(demo / "заметка.md")
    assert (demo / "заметка.md").read_text() == ""
    # Версия отдаётся сразу — редактор сохраняет поверх без лишнего чтения.
    assert made["version"]

    listing = await (await client.get(f"/api/files?path={demo}")).json()
    assert "заметка.md" in [e["name"] for e in listing["entries"]]


async def test_new_file_refuses_to_overwrite(client, roots):
    demo, _ = roots
    (demo / "есть.txt").write_text("важное", encoding="utf-8")
    r = await client.post("/api/new", json={"dir": str(demo), "name": "есть.txt"})
    assert r.status == 400
    assert "уже есть" in await r.text()
    assert (demo / "есть.txt").read_text() == "важное"   # не тронут


async def test_new_file_stays_inside_roots(client, roots):
    """Имя чистится `_filename`, поэтому каталоги из него не выходят, а сам каталог
    проверяется `inside` — снаружи корней создать нечего."""
    demo, _ = roots
    r = await client.post("/api/new", json={"dir": "/etc", "name": "passwd2"})
    assert r.status == 400

    r = await client.post("/api/new", json={"dir": str(demo), "name": "../беглец"})
    assert r.status == 200
    assert (demo / "беглец").is_file()          # имя схлопнулось в своё же
    assert not (demo.parent / "беглец").exists()


async def test_new_folder_opens_in_the_tree(client, roots):
    demo, _ = roots
    r = await client.post("/api/new", json={"dir": str(demo), "name": "черновики",
                                            "folder": True})
    assert r.status == 200
    made = await r.json()
    assert made == {"path": str(demo / "черновики"), "dir": True}   # версии у папки нет
    assert (demo / "черновики").is_dir()

    # В неё сразу можно зайти — панель так и делает после создания.
    listing = await (await client.get(f"/api/files?path={demo / 'черновики'}")).json()
    assert listing["entries"] == []


async def test_new_folder_refuses_an_occupied_name(client, roots):
    demo, _ = roots
    (demo / "занято").write_text("файл, не папка", encoding="utf-8")
    r = await client.post("/api/new", json={"dir": str(demo), "name": "занято",
                                            "folder": True})
    assert r.status == 400
    assert "уже есть" in await r.text()
    assert (demo / "занято").is_file()   # не подменили файл папкой
