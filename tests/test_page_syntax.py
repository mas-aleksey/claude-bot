"""Страница целиком лежит в строке PAGE, и опечатка в её JS не видна ни ruff, ни тестам:
образ соберётся, endpoint ответит 200, а панели просто не появятся. Ловим синтаксис
через node — он есть в базовом образе, потому что на нём работает claude-cli.
"""

import json
import re
import shutil
import subprocess

import pytest

import webui


def slice_out(tag: str) -> str:
    return webui.PAGE.split(f"<{tag}>")[1].split(f"</{tag}>")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_page_script_parses(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(slice_out("script"), encoding="utf-8")
    done = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_grid_size_matches_between_css_and_script():
    """Сетку рисует CSS, а шаг перетаскивания считает скрипт по своим COLS и ROWS.
    Разъедутся — панель будет прыгать не туда, и заметить это можно только мышью."""
    cols, rows = re.search(r"COLS = (\d+), ROWS = (\d+)", slice_out("script")).groups()
    css = slice_out("style")
    assert f"grid-template-columns:repeat({cols}," in css
    assert f"grid-template-rows:repeat({rows}," in css


def test_page_has_both_halves():
    """Разбор по тегам молча отдал бы пустую строку, и проверка выше стала бы холостой."""
    assert "function drawPane" in slice_out("script")
    assert "grid-column:var(--c,1)" in slice_out("style")


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_search_spans_markup_and_line_breaks(tmp_path):
    """Поиск идёт по плоскому тексту панели: фраза, разорванная тегом или переносом,
    обязана находиться — ровно этого не умел прежний поиск по отдельным узлам."""
    body = slice_out("script").split("// --- find:begin ---")[1].split("// --- find:end ---")[0]
    js = tmp_path / "find.js"
    js.write_text(body + """
console.log(JSON.stringify([
  hits('функция linkify чинит', 'функция linkify'),
  hits('строка один\\nстрока два', 'один строка'),
  hits('цена 5$ за (штуку)', '5$ за (штуку)'),
  hits('Слово и слово', 'СЛОВО'),
  hits('что угодно', '   '),
]));""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got == [[[0, 15]], [[7, 18]], [[5, 18]], [[0, 5], [8, 13]], []]
