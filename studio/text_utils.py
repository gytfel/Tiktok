"""Подготовка текста: очистка, разбиение на слова, предложения и абзацы."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Эмодзи и пиктограммы: голос их не читает, а libass не умеет рисовать цветные эмодзи.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U000020E3"
    "]+",
    flags=re.UNICODE,
)
_WORD_CHAR_RE = re.compile(r"[\w]", re.UNICODE)
_SENTENCE_END_RE = re.compile(r"[.!?…]+[»\"')\]]*$")
_CLAUSE_END_RE = re.compile(r"[,;:—–-]+[»\"')\]]*$")


@dataclass
class Token:
    """Слово исходного текста вместе с позицией и таймингом."""

    text: str
    start_char: int
    end_char: int
    paragraph: int = 0
    start: float | None = None  # секунды
    end: float | None = None

    @property
    def ends_sentence(self) -> bool:
        return bool(_SENTENCE_END_RE.search(self.text))

    @property
    def ends_clause(self) -> bool:
        return bool(_CLAUSE_END_RE.search(self.text))


def clean_text(text: str) -> str:
    """Убирает эмодзи, лишние пробелы и пустые строки, сохраняя абзацы."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[  ]+", " ", text)
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    lines = []
    for p in paragraphs:
        # Одиночные переносы строк внутри абзаца превращаем в пробелы.
        p = " ".join(p.split("\n")).strip()
        if p:
            lines.append(p)
    return "\n\n".join(lines)


def has_word_chars(s: str) -> bool:
    return bool(_WORD_CHAR_RE.search(s))


def tokenize(text: str) -> list[Token]:
    """Разбивает текст на слова. Отдельно стоящая пунктуация (например «—»)
    приклеивается к соседнему слову, чтобы не появляться в субтитрах одна."""
    tokens: list[Token] = []
    paragraph = 0
    last_end = 0
    pending_prefix: tuple[str, int] | None = None
    for m in re.finditer(r"\S+", text):
        if "\n\n" in text[last_end:m.start()] and tokens:
            paragraph += 1
        last_end = m.end()
        word = m.group(0)
        if not has_word_chars(word):
            if tokens and tokens[-1].paragraph == paragraph:
                prev = tokens[-1]
                prev.text = f"{prev.text} {word}"
                prev.end_char = m.end()
            elif pending_prefix is None:
                pending_prefix = (word, m.start())
            continue
        start = m.start()
        if pending_prefix is not None:
            word = f"{pending_prefix[0]} {word}"
            start = pending_prefix[1]
            pending_prefix = None
        tokens.append(Token(text=word, start_char=start, end_char=m.end(), paragraph=paragraph))
    return tokens


def sentence_start_indices(tokens: list[Token]) -> list[int]:
    """Индексы слов, с которых начинаются предложения (и абзацы)."""
    if not tokens:
        return []
    starts = [0]
    for i in range(1, len(tokens)):
        if tokens[i - 1].ends_sentence or tokens[i].paragraph != tokens[i - 1].paragraph:
            starts.append(i)
    return starts


def estimate_duration(text: str, rate_percent: int = 0) -> float:
    """Грубая оценка длительности озвучки (около 14 символов в секунду)."""
    chars = len(clean_text(text))
    speed = 14.0 * (1 + rate_percent / 100.0)
    return chars / max(speed, 1.0)
