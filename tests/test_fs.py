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
    """Два корня: `/projects` целиком и конфиг. Ровно то, что видит панель в песочнице —
    проект отдельным корнем не стоит, в него заходят из дерева."""
    projects, config = tmp_path / "projects", tmp_path / "config"
    (projects / "demo").mkdir(parents=True)
    config.mkdir()
    monkeypatch.setattr(sessions, "PROJECTS_DIR", projects)
    monkeypatch.setattr(files, "FILE_ROOTS", [projects, config, tmp_path / "нет-такого"])
    return projects / "demo", config


@pytest.fixture
async def client(roots):
    c = TestClient(TestServer(webui.build()))
    await c.start_server()
    yield c
    await c.close()


def test_roots_skip_missing_paths(roots):
    demo, config = roots
    assert [str(p) for p in files.roots()] == [str(demo.parent), str(config)]


async def test_projects_root_opens_itself(client, roots):
    """Сам `/projects` открывается и перечисляет проекты. Раньше он отдавал «путь вне
    корней»: корнями были проекты по отдельности, и родитель в список не попадал."""
    demo, _ = roots
    r = await client.get(f"/api/files?path={demo.parent}")
    assert r.status == 200
    listing = await r.json()
    assert [e["name"] for e in listing["entries"]] == ["demo"]
    assert listing["entries"][0]["dir"] is True


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


async def test_rm_takes_a_file_and_an_empty_dir(client, roots):
    demo, _ = roots
    (demo / "мусор.txt").write_text("x")
    (demo / "пусто").mkdir()

    assert (await client.post("/api/rm", json={"path": str(demo / "мусор.txt")})).status == 200
    assert (await client.post("/api/rm", json={"path": str(demo / "пусто")})).status == 200
    assert not (demo / "мусор.txt").exists()
    assert not (demo / "пусто").exists()


async def test_rm_refuses_a_dir_with_anything_inside(client, roots):
    """Рекурсии у кнопки нет намеренно: промах мышью не должен стоить дерева."""
    demo, _ = roots
    (demo / "проект").mkdir()
    (demo / "проект" / "main.py").write_text("print(1)")

    r = await client.post("/api/rm", json={"path": str(demo / "проект")})
    assert r.status == 400
    assert "не пуста" in await r.text()
    assert (demo / "проект" / "main.py").exists()


async def test_rm_refuses_the_root_itself(client, roots):
    demo, _ = roots
    r = await client.post("/api/rm", json={"path": str(demo.parent)})
    assert r.status == 400
    assert demo.parent.is_dir()


async def test_rm_outside_roots_is_refused(client, tmp_path):
    посторонний = tmp_path / "чужое.txt"
    посторонний.write_text("x")
    r = await client.post("/api/rm", json={"path": str(посторонний)})
    assert r.status == 400
    assert посторонний.exists()


async def test_image_is_not_read_as_text(client, roots):
    """Картинка приходит типом, а не содержимым: в редактор она не идёт, её тянет `<img>`
    отдельным запросом. Раньше сюда попадал отказ «не текст в utf-8»."""
    demo, _ = roots
    (demo / "снимок.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    f = await (await client.get("/api/file", params={"path": str(demo / "снимок.png")})).json()
    assert f["image"] == "image/png"
    assert "text" not in f and "why" not in f


async def test_raw_serves_the_bytes_with_its_type(client, roots):
    demo, _ = roots
    body = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
    (demo / "снимок.png").write_bytes(body)

    r = await client.get("/api/raw", params={"path": str(demo / "снимок.png")})
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/png"
    assert await r.read() == body


async def test_raw_outside_roots_is_refused(client, tmp_path):
    чужое = tmp_path / "чужое.png"
    чужое.write_bytes(b"x")
    assert (await client.get("/api/raw", params={"path": str(чужое)})).status == 400


async def test_big_image_still_opens(client, roots):
    """Потолок редактора в 1 МБ на картинки не распространяется: фото с телефона крупнее,
    а смотреть его это не мешает."""
    demo, _ = roots
    (demo / "фото.jpg").write_bytes(b"\xff\xd8" + b"\x00" * (2 << 20))

    f = await (await client.get("/api/file", params={"path": str(demo / "фото.jpg")})).json()
    assert f["image"] == "image/jpeg"


async def test_rm_takes_the_link_and_spares_its_target(client, roots):
    """Симлинк снимается сам. Так собран каждый скилл: `/root/.claude/skills/x` ведёт в
    `/opt/skills/*`, и «удалить ссылку» не должно значить «удалить скилл»."""
    demo, config = roots
    (demo / "SKILL.md").write_text("текст")
    (config / "skills").mkdir()
    (config / "skills" / "demo").symlink_to(demo)

    r = await client.post("/api/rm", json={"path": str(config / "skills" / "demo")})
    assert r.status == 200
    assert not (config / "skills" / "demo").is_symlink()
    assert (demo / "SKILL.md").read_text() == "текст"   # цель на месте


async def test_rename_keeps_the_directory(client, roots):
    demo, _ = roots
    (demo / "было.md").write_text("текст")

    r = await client.post("/api/mv", json={"path": str(demo / "было.md"), "name": "стало.md"})
    assert r.status == 200
    assert (await r.json())["path"] == str(demo / "стало.md")
    assert (demo / "стало.md").read_text() == "текст"
    assert not (demo / "было.md").exists()


async def test_rename_refuses_a_taken_name(client, roots):
    demo, _ = roots
    (demo / "было.md").write_text("а")
    (demo / "занято.md").write_text("б")

    r = await client.post("/api/mv", json={"path": str(demo / "было.md"), "name": "занято.md"})
    assert r.status == 400
    assert (demo / "было.md").read_text() == "а"
    assert (demo / "занято.md").read_text() == "б"


async def test_rename_cannot_escape_the_directory(client, roots):
    demo, config = roots
    (demo / "было.md").write_text("текст")

    r = await client.post("/api/mv",
                          json={"path": str(demo / "было.md"), "name": "../../беглец"})
    assert r.status == 200          # имя чистится, а не отклоняется
    assert (demo / "было.md").exists() is False
    assert sorted(p.name for p in config.iterdir()) == []   # наружу ничего не уехало
    assert [p.name for p in demo.iterdir()] == ["беглец"]


async def test_dotfiles_keep_their_dot(client, roots):
    """`.env` и `.gitignore` — обычные имена. Прежний санитайзер срезал точку в начале,
    и файл молча создавался под другим именем."""
    demo, _ = roots
    r = await client.post("/api/new", json={"dir": str(demo), "name": ".env"})
    assert r.status == 200
    assert (await r.json())["path"] == str(demo / ".env")
    assert (demo / ".env").is_file()

    assert files._filename(".") == "file"
    assert files._filename("..") == "file"
