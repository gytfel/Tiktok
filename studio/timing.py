"""Тайминги слов: сопоставление меток синтезатора с текстом и оценка без меток."""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from .text_utils import Token, has_word_chars

# Насколько далеко вперёд (в символах) можно искать слово из метки синтезатора.
_MAX_LOOKAHEAD = 80


@dataclass
class Boundary:
    """Метка слова от синтезатора речи: текст и время звучания в секундах."""

    text: str
    start: float
    end: float


def apply_boundaries(text: str, tokens: list[Token], boundaries: list[Boundary], total: float) -> None:
    """Проставляет start/end токенам по меткам синтезатора. Слова без меток
    получают интерполированное время."""
    if not tokens:
        return
    lower = text.lower()
    starts = [t.start_char for t in tokens]
    cursor = 0
    for b in boundaries:
        word = b.text.strip().lower()
        if not word or not has_word_chars(word):
            continue
        pos = lower.find(word, cursor)
        if pos < 0 or pos - cursor > _MAX_LOOKAHEAD:
            continue
        cursor = pos + len(word)
        idx = max(0, bisect.bisect_right(starts, pos) - 1)
        tok = tokens[idx]
        if tok.start is None:
            tok.start, tok.end = b.start, b.end
        else:
            tok.end = max(tok.end or b.end, b.end)
    fill_missing(tokens, 0.0, total)


def fill_missing(tokens: list[Token], span_start: float, span_end: float) -> None:
    """Интерполирует время для слов без таймингов пропорционально их длине."""
    n = len(tokens)
    i = 0
    while i < n:
        if tokens[i].start is not None:
            i += 1
            continue
        j = i
        while j < n and tokens[j].start is None:
            j += 1
        left = tokens[i - 1].end if i > 0 else span_start
        right = tokens[j].start if j < n else span_end
        if left is None:
            left = span_start
        if right is None or right < left:
            right = left + 0.3 * (j - i)
        weights = [_weight(t) for t in tokens[i:j]]
        total_w = sum(weights) or 1.0
        t = left
        for k, w in zip(range(i, j), weights):
            dur = (right - left) * w / total_w
            tokens[k].start = t
            tokens[k].end = t + dur
            t += dur
        i = j
    # Гарантируем монотонность времени.
    prev = span_start
    for tok in tokens:
        tok.start = max(tok.start, prev)
        tok.end = max(tok.end, tok.start + 0.05)
        prev = tok.start


def estimate_from_speech(tokens: list[Token], speech_spans: list[tuple[float, float]]) -> None:
    """Раскладывает слова по участкам речи (без пауз), когда синтезатор не дал меток."""
    spans = [s for s in speech_spans if s[1] - s[0] > 0.05]
    if not tokens or not spans:
        return
    speech_total = sum(e - s for s, e in spans)
    weights = [_weight(t) for t in tokens]
    total_w = sum(weights) or 1.0

    def to_time(speech_pos: float, is_start: bool) -> float:
        # Начало слова на стыке участков относим к следующему участку, конец — к текущему.
        acc = 0.0
        for s, e in spans:
            length = e - s
            if speech_pos < acc + length - 1e-9 or (not is_start and speech_pos <= acc + length + 1e-9):
                return s + (speech_pos - acc)
            acc += length
        return spans[-1][1]

    pos = 0.0
    for tok, w in zip(tokens, weights):
        dur = speech_total * w / total_w
        tok.start = to_time(pos, True)
        tok.end = to_time(pos + dur * 0.92, False)
        pos += dur


def _weight(tok: Token) -> float:
    letters = sum(1 for ch in tok.text if ch.isalnum())
    w = letters + 1.0
    if tok.ends_sentence:
        w += 3.0
    elif tok.ends_clause:
        w += 1.5
    return w
