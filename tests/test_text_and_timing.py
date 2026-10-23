from studio.text_utils import clean_text, sentence_start_indices, tokenize
from studio.timing import Boundary, apply_boundaries, estimate_from_speech


def test_clean_text_removes_emoji_and_keeps_paragraphs():
    text = "Привет 🔥 мир!\r\n\r\n\r\nВторой   абзац\nпродолжение 👍"
    assert clean_text(text) == "Привет мир!\n\nВторой абзац продолжение"


def test_tokenize_attaches_standalone_punctuation():
    tokens = tokenize("Это — тест. «Да» ...")
    assert [t.text for t in tokens] == ["Это —", "тест.", "«Да» ..."]


def test_tokenize_paragraphs_and_sentences():
    text = clean_text("Раз два. Три!\n\nЧетыре пять")
    tokens = tokenize(text)
    assert [t.paragraph for t in tokens] == [0, 0, 0, 1, 1]
    assert sentence_start_indices(tokens) == [0, 2, 3]


def test_apply_boundaries_maps_words_and_interpolates_missing():
    text = "Привет, мир! Это 2024 год."
    tokens = tokenize(text)
    boundaries = [
        Boundary("Привет", 0.1, 0.5),
        Boundary(",", 0.5, 0.55),  # пунктуация игнорируется
        Boundary("мир", 0.6, 0.9),
        Boundary("Это", 1.2, 1.4),
        # «2024» синтезатор не прислал — время должно интерполироваться
        Boundary("год", 2.0, 2.3),
    ]
    apply_boundaries(text, tokens, boundaries, total=2.5)
    assert tokens[0].start == 0.1 and tokens[1].start == 0.6
    assert tokens[2].start == 1.2
    assert 1.4 <= tokens[3].start < tokens[4].start == 2.0
    starts = [t.start for t in tokens]
    assert starts == sorted(starts)


def test_apply_boundaries_ignores_far_mismatches():
    text = "один два три четыре"
    tokens = tokenize(text)
    apply_boundaries(text, tokens, [Boundary("один", 0, 0.3), Boundary("пять", 0.4, 0.6), Boundary("два", 0.5, 0.8)], 2.0)
    assert tokens[1].start == 0.5  # «пять» не сдвинул курсор и не сломал разметку


def test_estimate_from_speech_skips_pauses():
    tokens = tokenize("Первое предложение. Второе предложение.")
    estimate_from_speech(tokens, [(0.0, 1.0), (2.0, 3.0)])
    assert tokens[0].start == 0.0
    assert tokens[2].start >= 2.0  # второе предложение начинается после паузы
    assert tokens[-1].end <= 3.0
