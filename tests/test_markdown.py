"""Разметка ответа рисуется своими регулярками, а не библиотекой, поэтому проверяется.

Скрипт страницы целиком в node не запустить — он с первой строки трогает document и
localStorage. Поэтому берём только блок между маркерами плюс строку с `esc`, от которой
он зависит.
"""

import json
import shutil
import subprocess

import pytest

import webui

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="нужен node")


def md_module() -> str:
    js = webui.PAGE.split("<script>")[1].split("</script>")[0]
    esc = next(line for line in js.splitlines() if line.startswith("const esc ="))
    body = js.split("// --- md:begin ---")[1].split("// --- md:end ---")[0]
    return f"{esc}\n{body}\nconsole.log(JSON.stringify(JSON.parse(process.argv[2]).map(md)));"


@pytest.fixture(scope="module")
def render(tmp_path_factory):
    path = tmp_path_factory.mktemp("md") / "md.js"
    path.write_text(md_module(), encoding="utf-8")

    def run(*sources: str) -> list[str]:
        done = subprocess.run(["node", str(path), json.dumps(list(sources))],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    return run


def test_script_tag_appears_once_in_page():
    """Тесты режут страницу по `<script>`. Второе вхождение — и они молча проверяют
    обрезок: именно так уже случилось с этим словом внутри комментария."""
    assert webui.PAGE.count("<script>") == 1


def test_headings_bold_and_code(render):
    got = render("## Итог", "текст **жирный** и `код`")
    assert got[0] == "<h2>Итог</h2>"
    assert got[1] == "<p>текст <b>жирный</b> и <code>код</code></p>"


def test_lists(render):
    ul, ol = render("- раз\n- два", "1. раз\n2. два")
    assert ul == "<ul><li>раз</li><li>два</li></ul>"
    assert ol == "<ol><li>раз</li><li>два</li></ol>"


def test_fenced_code_keeps_text_and_escapes_it(render):
    (got,) = render("до\n```python\nif a < b:\n    print('<b>')\n```\nпосле")
    assert "<pre><code>if a &lt; b:\n    print('&lt;b&gt;')</code></pre>" in got
    assert got.startswith("<p>до</p>")
    assert got.endswith("<p>после</p>")


def test_table(render):
    (got,) = render("| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert got == "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"


def test_link_gets_noopener(render):
    (got,) = render("[док](https://example.org/x)")
    assert got == '<p><a href="https://example.org/x" target="_blank" rel="noopener">док</a></p>'


@pytest.mark.parametrize("src,forbidden", [
    ("<img src=x onerror=alert(1)>", "<img"),
    ("<iframe src=evil></iframe>", "<iframe"),
    ("текст <b>уже тег</b>", "<b>уже"),
])
def test_html_from_claude_never_survives(render, src, forbidden):
    """Экранирование идёт до вставки своих тегов — это единственная защита здесь."""
    (got,) = render(src)
    assert forbidden not in got
    assert "&lt;" in got


def test_plain_text_survives_untouched(render):
    (got,) = render("обычная строка без разметки")
    assert got == "<p>обычная строка без разметки</p>"


def test_bare_url_becomes_link(render):
    got = render("см. https://example.org/x, дальше текст",
                 "[док](https://example.org/x)",
                 "`https://example.org/x`")
    assert got[0] == ('<p>см. <a href="https://example.org/x" target="_blank" rel="noopener">'
                      'https://example.org/x</a>, дальше текст</p>')
    # Готовая ссылка из markdown не заворачивается второй раз, а url в `коде` остаётся
    # кодом — он там как текст команды, а не как адрес.
    assert got[1].count("<a href=") == 1
    assert got[2] == "<p><code>https://example.org/x</code></p>"


def test_loose_text_next_to_block_is_wrapped(render):
    """Голый текстовый узел рядом со списком образует анонимный блок, а WebKit в нём не
    красит ::highlight — попадание поиска оставалось невидимым при верном счётчике."""
    (got,) = render("итог первой строкой\n\nдальше:\n- раз\n- два\n\nхвост")
    # Пустая строка внутри абзаца остаётся переносом: вид тот же, что был, меняется
    # только то, что голого текста прямо в .body больше нет.
    assert got == ("<p>итог первой строкой<br><br>дальше:</p><ul><li>раз</li><li>два</li></ul>"
                   "<p>хвост</p>")
