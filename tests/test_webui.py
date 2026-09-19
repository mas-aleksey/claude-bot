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
    seen, got, _ = webui.items(path, 0)

    assert seen == path.stat().st_size  # прочитан весь файл, оффсет для следующего кадра
    assert [i["role"] for i in got] == ["user", "tool", "assistant"]
    assert got[0]["text"] == "почини тесты"
    assert got[1]["name"] == "Bash" and "pytest -q" in got[1]["text"]
    assert got[2]["text"] == "готово"


def test_long_tool_argument_comes_with_its_full_text(tmp_path):
    """Короткий шаг остаётся строкой, длинный несёт `full` — из него панель делает
    раскрытие. Переводы строк в `full` живые: `clip` их схлопывает, а команду с heredoc
    читают столбиком."""
    path = tmp_path / "s.jsonl"
    long = "for f in *.py; do\n  echo $f\ndone  # " + "x" * 300
    write(path,
          {"type": "assistant", "message": {"role": "assistant", "content": [
              {"type": "tool_use", "name": "Read", "input": {"file_path": "/p/a.py"}},
              {"type": "tool_use", "name": "Bash", "input": {"command": long}}]}})
    _, got, _ = webui.items(path, 0)

    assert "full" not in got[0]                      # путь и так виден целиком
    assert got[1]["text"].endswith("…")              # строка шага по-прежнему обрезана
    assert got[1]["full"].startswith("for f in *.py; do\n")
    assert len(got[1]["full"]) == min(len(long), webui.FULL_ARG)


def test_run_summary_keeps_only_what_is_known():
    """Итог прогона собирается из `result`: модель, время, цена, токены. Пустые поля
    пропускаются — у местной команды цены нет, и `$0.000` сказал бы неправду."""
    full = webui._stat_line({
        "duration_ms": 72_000,
        "total_cost_usd": 0.0837,
        "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 300,
                  "cache_read_input_tokens": 11_000, "output_tokens": 1400},
    }, "opus")
    assert full == "opus · 1:12 · $0.084 · ↓12.3k · ↑1.4k"

    # Местная команда: ни цены, ни токенов, ни модели — строки нет вовсе, и панель
    # ничего не печатает.
    assert webui._stat_line({"total_cost_usd": 0, "usage": {"output_tokens": 0}}, None) == ""

    # Короткий прогон без цены: секунды и вывод остаются, лишних разделителей нет.
    assert webui._stat_line({"duration_ms": 4200, "usage": {"output_tokens": 950}},
                            "sonnet") == "sonnet · 4с · ↑950"


def test_big_session_arrives_whole(tmp_path):
    """Панель открывает сессию с начала: обрезка по байтам была снята, потому что
    наружу идут только промпты, ответы и строки шагов — на 13.7 МБ транскрипта это
    644 элемента и 0.47 МБ json. Предохранитель остался один, `CHUNK` на кадр."""
    path = tmp_path / "s.jsonl"
    write(path, *[{"type": "user", "message": {"content": f"промпт {i}"}}
                  for i in range(200)])

    off, got, _ = webui.items(path, 0)

    assert off == path.stat().st_size
    assert len(got) == 200
    assert got[0]["text"] == "промпт 0"
    assert got[-1]["text"] == "промпт 199"


def test_items_reads_only_the_tail(tmp_path):
    """Опрос живого запуска: со старого оффсета отдаётся только новое."""
    path = tmp_path / "s.jsonl"
    write(path, *EVENTS)
    seen, _, _ = webui.items(path, 0)

    write(path, *EVENTS, {"type": "assistant", "message": {"role": "assistant",
          "content": [{"type": "text", "text": "и ещё"}]}})
    seen2, tail, _ = webui.items(path, seen)

    assert seen2 == path.stat().st_size
    assert [i["text"] for i in tail] == ["и ещё"]


def test_items_waits_for_a_line_still_being_written(tmp_path):
    """claude дописывает транскрипт, а поток смотрит на него раз в 300 мс. Прочитать
    половину события и сдвинуть на неё оффсет — значит потерять событие целиком."""
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "user", "message": {"content": "\u0440\u0430\u0437"}}\n'
                    '{"type": "user", "mes', encoding="utf-8")
    off, got, _ = webui.items(path, 0)
    assert [i["text"] for i in got] == ["раз"]

    with path.open("a", encoding="utf-8") as f:
        f.write('sage": {"content": "два"}}\n')
    _, tail, _ = webui.items(path, off)
    assert [i["text"] for i in tail] == ["два"]


def test_items_survives_broken_line(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "user", "message": {"content": "раз"}}\nне json\n')
    _, got, _ = webui.items(path, 0)
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


def skill(root, dirname, front=""):
    path = root / dirname / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n\nтело\n", encoding="utf-8")


def test_skills_reads_frontmatter(tmp_path, monkeypatch):
    """Имя идёт из frontmatter, а когда оно негодное — из каталога: набирают скилл
    после слеша, и пробел в имени превратил бы команду в два слова."""
    monkeypatch.setattr(webui, "SKILLS", tmp_path)
    skill(tmp_path, "refine", "name: refine\ndescription: Разбор задачи")
    skill(tmp_path, "sync-repo", "description: без имени")
    skill(tmp_path, "weird", "name: две слова")
    (tmp_path / "черновик").mkdir()  # каталог без SKILL.md

    assert webui.skills() == [
        {"name": "refine", "desc": "Разбор задачи"},
        {"name": "sync-repo", "desc": "без имени"},
        {"name": "weird", "desc": ""},
        *webui.BUILTIN,
    ]


def test_skills_of_project_join_and_win(tmp_path, monkeypatch):
    """Скиллы проекта видны в подсказке и перекрывают общие по имени — так же, как их
    разрешает сам claude."""
    monkeypatch.setattr(webui, "SKILLS", tmp_path / "home")
    skill(tmp_path / "home", "refine", "name: refine\ndescription: общий")
    project = tmp_path / "proj"
    skill(project / ".claude" / "skills", "bw", "name: bw\ndescription: .env через vault")
    skill(project / ".claude" / "skills", "refine", "name: refine\ndescription: проектный")

    assert webui.skills(str(project)) == [
        {"name": "bw", "desc": ".env через vault"},
        {"name": "refine", "desc": "проектный"},
        *webui.BUILTIN,
    ]


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
    _, got, _ = webui.items(path, 0)

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
    _, got, _ = webui.items(path, 0)

    assert got == [{"role": "user", "text": "/refine RP-3945 + HANDOFF.md"}]


def test_slash_command_without_args(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content":
          "<command-name>/end</command-name>"}})
    _, got, _ = webui.items(path, 0)
    assert got == [{"role": "user", "text": "/end"}]


def test_empty_injected_context_is_skipped(tmp_path):
    """Пустой список блоков — служебное событие, пометка о нём была бы шумом."""
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content": []}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "content": "лог"}]}})
    _, got, _ = webui.items(path, 0)
    assert got == []


def test_task_notification_is_a_note_not_my_message(tmp_path):
    """Уведомление о фоновой задаче стояло в панели под подписью «ты», хотя человек не
    писал ни строки. Поймано на живой сессии."""
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content":
          "<task-notification>\n<task-id>bcsebz5dc</task-id>\n"
          "<summary>Background command \"sleep 30; cat out\" completed (exit code 0)</summary>\n"
          "</task-notification>"}})
    _, got, _ = webui.items(path, 0)

    assert got[0]["role"] == "note"
    assert got[0]["text"].startswith("фоновая задача завершилась: Background command")


def test_local_command_stdout_is_a_note(tmp_path):
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user",
          "content": "<local-command-stdout>Goodbye!</local-command-stdout>"}})
    _, got, _ = webui.items(path, 0)
    assert got == [{"role": "note", "text": "вывод локальной команды: Goodbye!"}]


def test_caveat_before_command_still_shows_the_command(tmp_path):
    """Преамбула клиента идёт в том же сообщении, что и настоящая команда."""
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user", "content":
          "<local-command-caveat>Caveat: messages below were generated by the user"
          "</local-command-caveat>\n<command-name>/clear</command-name>"}})
    _, got, _ = webui.items(path, 0)
    assert got == [{"role": "user", "text": "/clear"}]


def test_unknown_tag_stays_my_message(tmp_path):
    """Неизвестный тег не прячем: лучше лишнее в панели, чем потерянное сообщение."""
    path = tmp_path / "s.jsonl"
    write(path, {"type": "user", "message": {"role": "user",
          "content": "<important>посмотри вот это</important>"}})
    _, got, _ = webui.items(path, 0)
    assert got == [{"role": "user", "text": "<important>посмотри вот это</important>"}]


def test_ctx_takes_last_assistant_usage(tmp_path, monkeypatch):
    """Контекст считается по последнему ответу, а не суммой по сессии: после /compact
    он падает, и сумма показывала бы давно истёкший максимум."""
    path = tmp_path / "s.jsonl"
    def ev(read, out):
        return json.dumps({"type": "assistant", "message": {
            "model": "claude-opus-5", "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 8,
                      "cache_read_input_tokens": read, "output_tokens": out}}})
    path.write_text("\n".join([ev(90_000, 100), ev(1_000, 90)]) + "\n", encoding="utf-8")

    monkeypatch.setattr(webui.store, "get", lambda key, default=None: None)
    assert webui.items(path, 0)[2] == {"used": 1100, "window": webui.DEFAULT_WINDOW,
                                       "guess": True}

    # Окно, записанное runner-ом после прогона, перебивает оценку.
    monkeypatch.setattr(webui.store, "get", lambda key, default=None: "1000000")
    assert webui.items(path, 0)[2] == {"used": 1100, "window": 1_000_000, "guess": False}

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert webui.items(empty, 0)[2] is None


def test_ctx_of_reads_the_whole_session(tmp_path, monkeypatch):
    """`/status` в Telegram считает контекст своим проходом, а не `items`.

    `items` останавливается на `CHUNK` элементов и у длинной сессии вернул бы контекст
    её начала. Порог тут занижен до двух — писать в тест три тысячи событий незачем.
    """
    path = tmp_path / "s.jsonl"

    def ev(read):
        return json.dumps({"type": "assistant", "message": {
            "model": "claude-opus-5", "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": read, "output_tokens": 0}}})

    path.write_text("\n".join([ev(90_000), ev(500), ev(1_000)]) + "\n", encoding="utf-8")
    monkeypatch.setattr(webui.store, "get", lambda key, default=None: None)
    monkeypatch.setattr(webui, "CHUNK", 2)

    assert webui.items(path, 0)[2]["used"] == 500      # успел дойти только до второго
    assert webui.ctx_of(path)["used"] == 1_000         # а тут последний ответ

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert webui.ctx_of(empty) is None
