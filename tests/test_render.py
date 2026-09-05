"""Единственная нетривиальная логика в проекте — накопление событий и обрезка под 4096."""

import render


def _events(n_tools: int = 3):
    yield {"type": "system", "subtype": "init", "session_id": "sess-1"}
    for i in range(n_tools):
        yield {
            "type": "assistant",
            "session_id": "sess-1",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": f"/x/{i}.py"}}
                ]
            },
        }
    yield {
        "type": "result",
        "subtype": "success",
        "session_id": "sess-1",
        "result": "готово",
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_basic():
    run = render.Run("почини баг", "localhome")
    for ev in _events():
        run.feed(ev)
    out = run.text()
    assert run.session_id == "sess-1"
    assert run.done and not run.error
    assert "готово" in out
    assert "📖 Read: /x/2.py" in out
    assert "3 вызовов" in out and "$0.012" in out


def test_tail_and_limit():
    run = render.Run("x" * 200, "p")
    for ev in _events(400):
        run.feed(ev)
    out = run.text()
    assert len(out) <= render.LIMIT
    assert "готово" in out  # финальный текст переживает обрезку
    assert "ещё" in out


def test_error_event():
    run = render.Run("сломай", "p")
    run.feed({"type": "_bot", "kind": "error", "rc": 1, "text": "boom"})
    assert "❌" in run.text() and "boom" in run.text()


def test_result_is_error():
    run = render.Run("x", "p")
    run.feed({"type": "result", "is_error": True, "result": "нет такого файла"})
    out = run.text()
    assert "❌" in out and "нет такого файла" in out


def test_long_result_split():
    run = render.Run("дай отчёт", "p")
    body = "\n".join(f"строка {i} " + "x" * 80 for i in range(200))
    run.feed({"type": "result", "result": body})
    first, extra = run.parts()
    assert extra, "длинный результат должен уехать в доп. сообщения"
    assert all(len(c) <= render.LIMIT for c in [first, *extra])
    # Ничего не потеряно: склейка кусков даёт исходный текст без «…».
    joined = first[first.index("строка 0"):] + "\n" + "\n".join(extra)
    assert "…" not in joined
    assert joined.count("\n".join(body.split("\n")[-1:])) == 1
    for i in (0, 100, 199):
        assert f"строка {i} " in joined


def test_short_result_stays_single():
    run = render.Run("x", "p")
    for ev in _events():
        run.feed(ev)
    first, extra = run.parts()
    assert extra == [] and first == run.text()


def test_split_line_longer_than_limit():
    chunks = render.split("a" * 10000, 4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == "a" * 10000


def test_long_error_split():
    run = render.Run("сломай", "p")
    run.feed({"type": "result", "is_error": True, "result": "boom\n" * 2000})
    first, extra = run.parts()
    assert "❌" in first and extra
    assert all(len(c) <= render.LIMIT for c in [first, *extra])


def test_esc_neutralises_claude_output():
    """Вывод claude идёт внутрь наших <pre>/<code> — сырые < и & ломают parse_mode=HTML."""
    out = render.esc('if a<b & c>d: raise Error("x")')
    assert "<" not in out and ">" not in out
    assert "&lt;b" in out and "&amp;" in out


def test_plain_strips_only_our_tags():
    assert render.plain("❌ <b>ошибка</b>\n<pre>a &lt; b</pre>") == "❌ ошибка\na < b"


def test_esc_plain_round_trip():
    """app.py:407 — ошибка в <pre>; после фолбэка должен вернуться исходный текст."""
    err = 'if a<b & c>d: "quoted"'
    assert render.plain(f"❌ <b>ошибка</b>\n<pre>{render.esc(err)}</pre>") == f"❌ ошибка\n{err}"


def test_md_basics():
    assert render.md("**жирный**") == "<b>жирный</b>"
    assert render.md("*курсив*") == "<i>курсив</i>"
    assert render.md("## Заголовок") == "<b>Заголовок</b>"
    assert render.md("`a < b`") == "<code>a &lt; b</code>"
    assert render.md("[док](https://x.io)") == '<a href="https://x.io">док</a>'


def test_md_escapes_and_keeps_code_literal():
    # `<` из диффа не должен стать тегом, а разметка внутри кода — разметкой
    assert render.md("diff: <div> & co") == "diff: &lt;div&gt; &amp; co"
    assert render.md("`**x**`") == "<code>**x**</code>"
    assert render.md("```py\nx = 1 < 2\n```") == "<pre>x = 1 &lt; 2\n</pre>"
    # незакрытый fence (обрезанный вывод) не роняет конвертер
    assert render.md("```\nhalf").startswith("<pre>")


def test_md_leaves_stray_stars_alone():
    assert render.md("2 * 3 * 4") == "2 * 3 * 4"
    assert render.md("a**b") == "a**b"


def test_result_rendered_as_html():
    run = render.Run("p", "proj")
    run.feed({"type": "result", "result": "**итог**: `ok`"})
    assert "<b>итог</b>" in run.text() and "<code>ok</code>" in run.text()


NARROW = "| колонка | значение |\n| --- | --- |\n| **жирный** | `код` |\n| обычный | 42 |"
WIDE = (
    "| файл | что изменилось | почему |\n| --- | --- | --- |\n"
    "| `render.py` | добавлен конвертер `md()` | Telegram не понимает markdown |"
)


def test_table_narrow_becomes_pre():
    out = render.md(NARROW)
    assert out.startswith("<pre>") and out.endswith("</pre>")
    # колонки выровнены, разделитель строк таблицы убран, маркеры срезаны
    assert "колонка  значение" in out
    assert "---" not in out and "**" not in out and "`" not in out
    assert "жирный" in out and "код" in out


def test_table_wide_wraps_inside_cells():
    out = render.md(WIDE)
    assert out.startswith("<pre>")  # сжали колонки, а не развернули в список
    assert all(len(ln) <= render.TABLE_WIDTH + 4 for ln in out.split("\n"))
    assert "добавлен" in out and "конвертер" in out


def test_table_falls_back_to_list_when_columns_dont_fit():
    # 6 колонок: даже по MIN_COL на каждую — шире порога, переносом не спасти
    head = "| a | b | c | d | e | f |"
    out = render.md(f"{head}\n| --- | --- | --- | --- | --- | --- |\n" + "| длинное значение " * 6 + "|")
    assert "<pre>" not in out and out.startswith("▪ a:")


def test_table_escapes_and_survives_ragged_rows():
    assert "&lt;div&gt;" in render.md("| tag | x |\n| --- | --- |\n| <div> | a & b |")
    assert "<pre>" in render.md("| a | b | c |\n| --- | --- | --- |\n| 1 |")


def test_table_inside_fence_untouched():
    out = render.md("```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```")
    assert "| --- | --- |" in out  # код-блок не переписываем


def test_long_table_split_repeats_header():
    run = render.Run("p", "x")
    run.feed({"type": "result", "result": "| кол | знач |\n| --- | --- |\n" + "| ряд | 42 |\n" * 900})
    first, extra = run.parts()
    chunks = [first, *extra]
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert all("кол" in c for c in chunks)  # шапка в каждом куске, а не сырые пайпы в хвосте
    assert not any("| ряд |" in c for c in chunks)
