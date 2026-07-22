"""Sentence-boundary detection in sanitize_summary_text.

Regression cover for a defect that shipped to a user: trimming a truncated
tail cut back to the last ``.``, which also ends abbreviations, so real
content was silently destroyed mid-sentence.

This function runs on every field of every summary, so both directions matter
-- under-trimming leaves a slightly ragged tail, over-trimming loses meaning.
"""

from __future__ import annotations

import pytest

from app.adapters.external.formatting.text_processor import (
    TextProcessorImpl,
    _last_sentence_boundary,
)


def _sanitize(text: str) -> str:
    return TextProcessorImpl(response_sender=None).sanitize_summary_text(text)  # type: ignore[arg-type]


# ── Content that must survive intact ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # The exact production regression.
        "Утверждение о 500 МБ на 50 тыс. корутин vs 10 ГБ на 50 тыс. потоков"
        " приведено без указания источника",
        "Выручка выросла на 5 млн. руб. за квартал по данным отчёта",
        "Встреча назначена на 5 г. в конференц-зале второго этажа",
        "Показатели т.е. итоговые значения приведены в таблице ниже",
        "The API costs approx. 5 USD per 1k tokens for summarisation runs",
        "Use a queue e.g. Redis or RabbitMQ to decouple the workers",
        "Acme Inc. reported steady growth across all regions this year",
        "Значение pi равно 3.14 и используется в расчётах орбиты",
        "Источник habr.com содержит подробный разбор темы корутин",
    ],
    ids=[
        "ru-tys",
        "ru-mln-rub",
        "ru-god",
        "ru-te",
        "en-approx",
        "en-eg",
        "en-inc",
        "decimal",
        "domain",
    ],
)
def test_abbreviations_and_dots_are_not_treated_as_sentence_ends(text: str) -> None:
    """Only a trailing period may be added -- no content may be removed."""
    assert _sanitize(text).rstrip(".") == text.rstrip(".")


# ── Genuine truncation must still be trimmed ─────────────────────────────────


def test_truncated_tail_after_a_real_sentence_is_trimmed() -> None:
    """The original intent must not regress into a no-op."""
    assert _sanitize("Первое предложение закончено. Второе оборвалось на полус") == (
        "Первое предложение закончено."
    )


def test_truncated_tail_is_trimmed_in_english() -> None:
    assert _sanitize("This sentence is complete. This one was cut mid-wor") == (
        "This sentence is complete."
    )


def test_text_without_any_boundary_gets_a_period_rather_than_being_cut() -> None:
    """No qualifying boundary -> leave the text, just terminate it."""
    assert _sanitize("одно незаконченное предложение без точек") == (
        "одно незаконченное предложение без точек."
    )


# ── Boundary helper directly ─────────────────────────────────────────────────


def test_unambiguous_terminators_are_accepted_anywhere() -> None:
    assert _last_sentence_boundary("Готово! Дальше текст") == len("Готово")
    assert _last_sentence_boundary("Правда? Затем ещё") == len("Правда")


def test_abbreviation_period_is_rejected() -> None:
    assert _last_sentence_boundary("на 50 тыс. корутин") == -1


def test_period_followed_by_capital_is_accepted() -> None:
    text = "Конец предложения. Начало следующего"
    assert _last_sentence_boundary(text) == text.index(".")
