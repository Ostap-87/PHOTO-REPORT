"""Разбор ТЗ из простого текста (.txt) и Markdown (.md).

Каждая строка с маркером («1.», «1.2», «—», «а)») — отдельный абзац; строка,
начинающаяся со строчной буквы, продолжает предыдущий абзац (перенос строки).
В Markdown дополнительно: заголовки «#», жирные строки «**…**», вложенные списки
по отступу и таблицы «| … |».
"""

from __future__ import annotations

import os
import re

from .common import (Para, TableBlock, build_items, classify_marker, decode_text, esc, looks_like_caps_heading,
                     one_line, src_para, src_row, table_html, title_from_filename)

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_BOLD_LINE = re.compile(r"^(?:\*\*|__)(.+?)(?:\*\*|__)[:.]?$")
_MD_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def strip_md(s: str) -> str:
    """Убирает разметку Markdown из строки (жирный, код, ссылки, цитаты)."""
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"^\s*>\s?", "", s)
    s = re.sub(r"(?<![\w*])\*(\S[^*]*?\S|\S)\*(?![\w*])", r"\1", s)
    return s.strip()


def _split_md_row(line: str) -> list[str]:
    t = line.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    return [strip_md(c.strip()) for c in t.split("|")]


def parse_text(data: bytes, filename: str) -> dict:
    """Разбор .txt / .md → словарь parse_tz."""
    text = decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
    ext = os.path.splitext(filename or "")[1].lower()
    is_md = ext in (".md", ".markdown") or bool(re.search(r"^#{1,6}\s", text, re.M))
    lines = text.split("\n")

    blocks: list = []        # Para | (TableBlock, html)
    cur: Para | None = None  # абзац, который ещё может продолжиться
    p_index = 0
    t_index = 0

    def new_para(t: str, **kw) -> Para:
        nonlocal p_index
        p = Para(text=t, src=src_para(p_index), **kw)
        p_index += 1
        blocks.append(p)
        return p

    i = 0
    while i < len(lines):
        raw = lines[i].replace("\t", "    ")
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            cur = None
            i += 1
            continue
        # Таблица «| a | b |»
        if stripped.startswith("|") and stripped.count("|") >= 2:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                ln = lines[i].strip()
                if not _MD_TABLE_SEP.match(ln):
                    rows.append(_split_md_row(ln))
                i += 1
            width = max(len(r) for r in rows) if rows else 0
            rows = [r + [""] * (width - len(r)) for r in rows]
            tb = TableBlock(rows=rows, srcs=[src_row(t_index, r) for r in range(len(rows))],
                            merged=[sum(1 for c in r if c) == 1 and width > 1 for r in rows])
            blocks.append((tb, table_html(rows, t_index)))
            t_index += 1
            cur = None
            continue
        indent_spaces = len(raw) - len(raw.lstrip(" "))
        indent = indent_spaces // 2 if indent_spaces >= 2 else 0
        if is_md:
            m = _MD_HEADING.match(stripped)
            if m:
                new_para(strip_md(m.group(2)), heading=len(m.group(1)), bold=True)
                cur = None
                i += 1
                continue
            mb = _MD_BOLD_LINE.match(stripped)
            if mb and not classify_marker(strip_md(stripped)):
                new_para(strip_md(mb.group(1)), bold=True)
                cur = None
                i += 1
                continue
            stripped = strip_md(stripped)
        mk = classify_marker(stripped)
        if mk is not None:
            cur = new_para(stripped, indent=indent)
        elif looks_like_caps_heading(stripped) and not (cur and cur.heading):
            new_para(stripped, bold=True)
            cur = None
        elif cur is not None and not cur.heading and not cur.bold and (
                is_md or re.match(r"^[a-zа-яё(«\"]", stripped) or cur.text.rstrip().endswith((",", "-", "—", "–"))):
            cur.text = f"{cur.text} {stripped}"
        else:
            cur = new_para(stripped)
        i += 1

    stream: list = []
    html = ['<div class="tz-doc tz-text">']
    for b in blocks:
        if isinstance(b, Para):
            stream.append(b)
            ref = b.src["ref"]
            if b.heading:
                tag = f"h{min(6, b.heading + 1)}"
                html.append(f'<{tag} class="tz-h" data-src="{ref}">{esc(one_line(b.text))}</{tag}>')
            else:
                style = f' style="margin-left:{1.5 * b.indent:.1f}em"' if b.indent else ""
                inner = esc(one_line(b.text))
                if b.bold:
                    inner = f"<b>{inner}</b>"
                html.append(f'<p class="tz-p" data-src="{ref}"{style}>{inner}</p>')
        else:
            tb, thtml = b
            stream.append(tb)
            html.append(thtml)
    html.append("</div>")
    title, items = build_items(stream, title_from_filename(filename))
    warnings = [] if items else ["В тексте не найдено пунктов: добавьте их вручную."]
    return {"title": title, "kind": "txt", "items": items, "html": "\n".join(html), "pages": [],
            "warnings": warnings}
