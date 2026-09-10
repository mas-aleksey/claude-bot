"""Разбор транскрипта и проверка клиентских параметров. Оба места нетривиальны:
парсер молча пропускает незнакомое, а id сессии подставляется в имя файла."""

import json

import pytest
from aiohttp import web

import sessions
import webui


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path)
    return tmp_path


def write(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def transcript_for(transcripts, cwd, sid, *events):
    path = transcripts / sessions._slug(str(cwd)) / f"{sid}.jsonl"
    write(path, *events)
    return path


EVENTS = [
    {"type": "user", "message": {"role": "user", "content": "почини тесты"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "длинные мысли, в читалку не идут"}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "1 failed"}]}},
    {"type": "attachment", "attachment": {"type": "output_style"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "готово"}]}},
]


def test_items_keeps_conversation_drops_noise(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, *EVENTS)
    seen, got = webui.items(path, 0)

    assert seen == 6  # прочитаны все строки, оффсет для следующего опроса
    assert [i["role"] for i in got] == ["user", "tool", "assistant"]
    assert got[0]["text"] == "почини тесты"
    assert got[1]["name"] == "Bash" and "pytest -q" in got[1]["text"]
    assert got[2]["text"] == "готово"


def test_items_reads_only_the_tail(tmp_path):
    """Опрос живого запуска: со старого оффсета отдаётся только новое."""
    path = tmp_path / "s.jsonl"
    write(path, *EVENTS)
    seen, _ = webui.items(path, 0)

    write(path, *EVENTS, {"type": "assistant", "message": {"role": "assistant",
          "content": [{"type": "text", "text": "и ещё"}]}})
    seen2, tail = webui.items(path, seen)

    assert seen2 == 7
    assert [i["text"] for i in tail] == ["и ещё"]


def test_items_survives_broken_line(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "user", "message": {"content": "раз"}}\nне json\n')
    _, got = webui.items(path, 0)
    assert [i["text"] for i in got] == ["раз"]


@pytest.mark.parametrize("sid", ["../../etc/passwd", "a/b", "", "..", "nope$"])
def test_transcript_rejects_bad_id(transcripts, tmp_path, sid):
    with pytest.raises(web.HTTPBadRequest):
        webui.transcript(str(tmp_path), sid)


def test_transcript_missing_file_is_not_an_error(transcripts, tmp_path):
    """Новая сессия: id уже есть, файла ещё нет. Это нормальное состояние."""
    path = webui.transcript(str(tmp_path), "7b53843c-b9b5-43be-aedd-0ef5c0f376b4")
    assert not path.exists()


def test_transcript_found(transcripts, tmp_path):
    sid = "7b53843c-b9b5-43be-aedd-0ef5c0f376b4"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    want = transcript_for(transcripts, cwd, sid, *EVENTS)
    assert webui.transcript(str(cwd), sid) == want


@pytest.mark.parametrize("raw,want", [("5", 5), (None, 0), ("", 0), ("-3", 0), ("абв", 0)])
def test_int_never_raises(raw, want):
    assert webui._int(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("", []),
    ("one=https://one.example", [{"name": "one", "url": "https://one.example"}]),
    ("  a = https://a  , b=https://b ",
     [{"name": "a", "url": "https://a"}, {"name": "b", "url": "https://b"}]),
    ("сломано,=https://x,y=", []),  # без имени или без url запись выбрасывается
])
def test_peers_parsing(monkeypatch, raw, want):
    monkeypatch.setenv("WEB_PEERS", raw)
    assert webui.peers() == want


SKILL_BODY = "Ты исследуешь ЧУЖОЙ репозиторий через GitLab REST API. " * 20


def test_skill_body_is_collapsed_not_shown_as_answer(tmp_path):
    """Тело скилла приходит user-сообщением со списком блоков. Раньше текстовый блок
    считался ответом claude всегда, и скилл вываливался в панель его словами."""
    path = tmp_path / "s.jsonl"
    write(path,
          {"type": "user", "message": {"role": "user", "content": "/refine задачу"}},
          {"type": "user", "message": {"role": "user", "content": [
              {"type": "text", "text": SKILL_BODY}]}},
          {"type": "assistant", "message": {"role": "assistant", "content": [
              {"type": "text", "text": "разобрал"}]}})
    _, got = webui.items(path, 0)

    assert [i["role"] for i in got] == ["user", "note", "assistant"]
    assert SKILL_BODY not in got[1]["text"]
    assert "подставлен контекст" in got[1]["text"]
    assert str(len(SKILL_BODY)) in got[1]["text"]  # объём виден, содержимое нет


def test_slash_command_wrapper_becomes_one_line(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content":
          "<command-message>refine</command-message>\n"
          "<command-name>/refine</command-name>\n"
          "<command-args>RP-3945 + HANDOFF.md</command-args>"}})
    _, got = webui.items(path, 0)

    assert got == [{"role": "user", "text": "/refine RP-3945 + HANDOFF.md"}]


def test_slash_command_without_args(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content":
          "<command-name>/end</command-name>"}})
    _, got = webui.items(path, 0)
    assert got == [{"role": "user", "text": "/end"}]


def test_empty_injected_context_is_skipped(tmp_path):
    """Пустой список блоков — служебное событие, пометка о нём была бы шумом."""
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content": []}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "content": "лог"}]}})
    _, got = webui.items(path, 0)
    assert got == []
