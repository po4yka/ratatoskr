"""Bilingual (EN/RU) message catalog for user-facing UI strings.

All user-visible labels, error messages, button captions, and progress
texts live here so that a single ``PREFERRED_LANG`` switch controls the
entire UI language.

Usage::

    from app.core.ui_strings import t

    label = t("tldr", lang="ru")  # -> "TL;DR"
"""

from __future__ import annotations

from app.core.lang import LANG_EN

_STRINGS: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------------
    # Summary card labels (card.py)
    # ------------------------------------------------------------------
    "tldr": {"en": "TL;DR", "ru": "TL;DR"},
    "tldr_ru": {"en": "TL;DR (RU)", "ru": "TL;DR"},
    "key_takeaways": {"en": "Key takeaways", "ru": "Ключевые выводы"},
    "key_stats": {"en": "Key stats", "ru": "Ключевые цифры"},
    "metadata": {"en": "Metadata", "ru": "Метаданные"},
    "tags": {"en": "Tags", "ru": "Теги"},
    "people": {"en": "People", "ru": "Люди"},
    "orgs": {"en": "Orgs", "ru": "Организации"},
    "places": {"en": "Places", "ru": "Места"},
    "summary_ready": {"en": "Summary Ready", "ru": "Резюме готово"},
    "reading_time": {"en": "Reading time", "ru": "Время чтения"},
    "categories": {"en": "Categories", "ru": "Категории"},
    "confidence": {"en": "Confidence", "ru": "Достоверность"},
    "hallucination_risk": {"en": "Hallucination risk", "ru": "Риск галлюцинации"},
    "model": {"en": "Model", "ru": "Модель"},
    "single_pass": {"en": "Single-pass", "ru": "Один проход"},
    "chunked": {"en": "Chunked", "ru": "По частям"},
    "article": {"en": "Article", "ru": "Статья"},
    "interesting_facts": {"en": "Key facts", "ru": "Ключевые факты"},
    "questionable": {"en": "Questionable", "ru": "Спорные утверждения"},
    # ------------------------------------------------------------------
    # Summary section headers (summary_presenter.py)
    # ------------------------------------------------------------------
    "key_ideas": {"en": "Key Ideas", "ru": "Ключевые идеи"},
    "summary_n": {"en": "Summary", "ru": "Резюме"},
    "key_quotes": {"en": "Key Quotes", "ru": "Ключевые цитаты"},
    "highlights": {"en": "Highlights", "ru": "Основные моменты"},
    "questions_answered": {"en": "Questions Answered", "ru": "Ответы на вопросы"},
    "key_points": {"en": "Key Points to Remember", "ru": "Ключевые мысли"},
    "caveats": {"en": "Caveats", "ru": "Оговорки"},
    "critical_analysis": {"en": "Critical Analysis", "ru": "Критический анализ"},
    "perspective_quality": {"en": "Perspective & Quality", "ru": "Перспектива и качество"},
    "topic_classification": {"en": "Topic Classification", "ru": "Классификация тем"},
    "forward_summary_ready": {
        "en": "Forward Summary Ready",
        "ru": "Резюме пересланного сообщения готово",
    },
    "forward_info": {"en": "Forward Info", "ru": "Информация о пересылке"},
    "entities": {"en": "Entities", "ru": "Сущности"},
    "readability": {"en": "Readability", "ru": "Читаемость"},
    "quick_actions": {"en": "Quick Actions:", "ru": "Действия:"},
    # ------------------------------------------------------------------
    # Additional insights (summary_presenter.py)
    # ------------------------------------------------------------------
    "research_highlights": {
        "en": "Additional Research Highlights",
        "ru": "Дополнительные результаты исследования",
    },
    "overview": {"en": "Overview", "ru": "Обзор"},
    "fresh_facts": {"en": "Fresh Facts", "ru": "Новые факты"},
    "open_questions": {"en": "Open Questions", "ru": "Открытые вопросы"},
    "suggested_followup": {"en": "Suggested Follow-up", "ru": "Рекомендуемые источники"},
    "expansion_topics": {"en": "Expansion Topics", "ru": "Темы для углубления"},
    "explore_next": {"en": "What to explore next", "ru": "Что изучить далее"},
    "why_matters": {"en": "Why it matters", "ru": "Почему это важно"},
    "source_hint": {"en": "Source hint", "ru": "Источник"},
    "no_insights": {
        "en": "No additional research insights were available.",
        "ru": "Дополнительных результатов исследования нет.",
    },
    "beyond_article": {"en": "beyond the article", "ru": "за рамками статьи"},
    # ------------------------------------------------------------------
    # Perspective fields (summary_presenter.py)
    # ------------------------------------------------------------------
    "bias": {"en": "Bias", "ru": "Предвзятость"},
    "tone": {"en": "Tone", "ru": "Тон"},
    "evidence": {"en": "Evidence", "ru": "Доказательства"},
    "missing_context": {"en": "Missing Context", "ru": "Недостающий контекст"},
    # ------------------------------------------------------------------
    # Reader-mode progress (notification_formatter.py)
    # ------------------------------------------------------------------
    "processing_domain": {"en": "Processing {domain}...", "ru": "Обработка {domain}..."},
    "extracting_content": {"en": "Extracting content...", "ru": "Извлечение контента..."},
    "content_extracted_analyzing": {
        "en": "Content extracted ({chars} chars, {secs}s). Analyzing...",
        "ru": "Контент извлечен ({chars} симв., {secs}с). Анализируем...",
    },
    "cached_content_analyzing": {
        "en": "Using cached content. Analyzing...",
        "ru": "Используем кэш. Анализируем...",
    },
    "processing_content": {
        "en": "Processing content ({chars} chars)...",
        "ru": "Обработка контента ({chars} симв.)...",
    },
    "detected_lang_analyzing": {
        "en": "Detected language: {lang}. Analyzing...",
        "ru": "Обнаружен язык: {lang}. Анализируем...",
    },
    "preparing_analysis": {"en": "Preparing AI analysis...", "ru": "Подготовка AI-анализа..."},
    "analyzing_ai": {
        "en": "Analyzing with AI ({model})\nContent: {chars} chars\nEst. time: 30-60s...",
        "ru": "Анализ с помощью AI ({model})\nКонтент: {chars} симв.\nОжидание: 30-60с...",
    },
    "analysis_complete": {
        "en": "Analysis complete ({secs}s). Generating summary...",
        "ru": "Анализ завершен ({secs}с). Генерация резюме...",
    },
    "analysis_failed": {
        "en": "AI analysis failed. See details above.",
        "ru": "AI-анализ не удался. Подробности выше.",
    },
    "ai_analysis_done": {
        "en": "AI analysis done ({secs}s). Generating summary...",
        "ru": "AI-анализ завершен ({secs}с). Генерация резюме...",
    },
    "processing_forward": {
        "en": "Processing forwarded post from {title}...",
        "ru": "Обработка пересланного поста из {title}...",
    },
    "detected_lang_sending": {
        "en": "Detected language: {lang}. Sending to model...",
        "ru": "Обнаружен язык: {lang}. Отправляем в модель...",
    },
    # ------------------------------------------------------------------
    # Error messages (notification_formatter.py)
    # ------------------------------------------------------------------
    "err_firecrawl_title": {"en": "Content Extraction Failed", "ru": "Ошибка извлечения контента"},
    "err_firecrawl_body": {
        "en": "I was unable to extract readable content from the provided URL.",
        "ru": "Не удалось извлечь читаемый контент по указанному URL.",
    },
    "err_firecrawl_solutions": {"en": "Possible Solutions", "ru": "Возможные решения"},
    "err_firecrawl_hint_url": {"en": "Try a different URL", "ru": "Попробуйте другой URL"},
    "err_firecrawl_hint_paywall": {
        "en": "Check if the content is publicly accessible (no paywall)",
        "ru": "Проверьте, что контент общедоступен (без paywall)",
    },
    "err_firecrawl_hint_text": {
        "en": "Ensure the URL points to a text-based article",
        "ru": "Убедитесь, что URL ведет на текстовую статью",
    },
    "err_empty_title": {"en": "No Content Found", "ru": "Контент не найден"},
    "err_empty_body": {
        "en": "The extraction process completed, but no meaningful text was found.",
        "ru": "Извлечение завершилось, но значимый текст не найден.",
    },
    "err_empty_causes": {"en": "Common Causes", "ru": "Частые причины"},
    "err_empty_cause_block": {
        "en": "Website blocking automated access",
        "ru": "Сайт блокирует автоматический доступ",
    },
    "err_empty_cause_paywall": {
        "en": "Content behind paywall or login",
        "ru": "Контент за paywall или авторизацией",
    },
    "err_empty_cause_nontext": {
        "en": "Non-text content (images, videos only)",
        "ru": "Нетекстовый контент (только изображения, видео)",
    },
    "err_empty_cause_server": {
        "en": "Temporary server issues at the source",
        "ru": "Временные проблемы сервера источника",
    },
    "err_empty_suggestions": {"en": "Suggestions", "ru": "Рекомендации"},
    "err_empty_hint_url": {"en": "Try a different URL", "ru": "Попробуйте другой URL"},
    "err_empty_hint_private": {
        "en": "Check if the article is readable in a private browser tab",
        "ru": "Проверьте, доступна ли статья в приватной вкладке браузера",
    },
    "err_processing_title": {"en": "Processing Failed", "ru": "Ошибка обработки"},
    "err_processing_body": {
        "en": "I couldn't generate a valid summary despite multiple attempts.",
        "ru": "Не удалось создать корректное резюме после нескольких попыток.",
    },
    "err_processing_what": {"en": "What happened", "ru": "Что произошло"},
    "err_processing_parse": {
        "en": "The AI models returned data that couldn't be parsed",
        "ru": "AI-модели вернули данные, которые не удалось разобрать",
    },
    "err_processing_repair": {
        "en": "Automatic repair attempts were unsuccessful",
        "ru": "Автоматическое восстановление не удалось",
    },
    "err_processing_try": {"en": "Try", "ru": "Попробуйте"},
    "err_processing_hint_retry": {
        "en": "Submit the URL again in a few minutes",
        "ru": "Отправьте URL повторно через несколько минут",
    },
    "err_processing_hint_other": {
        "en": "Try a different article from the same source",
        "ru": "Попробуйте другую статью с того же источника",
    },
    "err_llm_title": {"en": "AI Analysis Failed", "ru": "Ошибка AI-анализа"},
    "err_llm_body": {
        "en": "All AI models failed to process the content despite automatic fallbacks.",
        "ru": "Все AI-модели не смогли обработать контент, несмотря на автоматические fallback-ы.",
    },
    "err_llm_models": {"en": "Models attempted", "ru": "Использованные модели"},
    "err_llm_solutions": {"en": "Possible Solutions", "ru": "Возможные решения"},
    "err_llm_hint_retry": {
        "en": "Try again in a few moments",
        "ru": "Попробуйте снова через некоторое время",
    },
    "err_llm_hint_complex": {
        "en": "The content might be too complex or unusual",
        "ru": "Контент может быть слишком сложным или нестандартным",
    },
    "err_llm_hint_support": {
        "en": "Contact support if this happens repeatedly",
        "ru": "Свяжитесь с поддержкой, если это повторяется",
    },
    "err_unexpected_title": {
        "en": "An unexpected error occurred",
        "ru": "Произошла непредвиденная ошибка",
    },
    "err_unexpected_body": {
        "en": "The system encountered an internal problem while processing your request.",
        "ru": "Система столкнулась с внутренней ошибкой при обработке вашего запроса.",
    },
    "err_unexpected_status": {
        "en": "Please try again in a moment. If the issue persists, try a different URL or contact support.",
        "ru": "Попробуйте снова через мгновение. Если проблема сохраняется -- попробуйте другой URL или свяжитесь с поддержкой.",
    },
    "err_timeout_title": {"en": "Request Timed Out", "ru": "Время ожидания истекло"},
    "err_timeout_default": {
        "en": "The operation took too long to complete.",
        "ru": "Операция заняла слишком много времени.",
    },
    "err_timeout_try": {"en": "Try", "ru": "Попробуйте"},
    "err_timeout_hint_smaller": {
        "en": "Submitting a smaller article",
        "ru": "Отправить статью поменьше",
    },
    "err_timeout_hint_wait": {
        "en": "Waiting a few moments before retrying",
        "ru": "Подождать немного и повторить",
    },
    "err_rate_limit_title": {"en": "Service is Busy", "ru": "Сервис занят"},
    "err_rate_limit_default": {
        "en": "You have reached the rate limit.",
        "ru": "Достигнут лимит запросов.",
    },
    "err_rate_limit_status": {
        "en": "Please wait a minute before sending more requests.",
        "ru": "Пожалуйста, подождите минуту перед отправкой новых запросов.",
    },
    "err_network_title": {"en": "Network Error", "ru": "Ошибка сети"},
    "err_network_default": {"en": "A connection problem occurred.", "ru": "Проблема соединения."},
    "err_network_try": {"en": "Try", "ru": "Попробуйте"},
    "err_network_hint_conn": {
        "en": "Checking your internet connection",
        "ru": "Проверьте подключение к интернету",
    },
    "err_network_hint_retry": {
        "en": "Retrying in a few moments",
        "ru": "Повторите через несколько секунд",
    },
    "err_database_title": {"en": "Database Error", "ru": "Ошибка базы данных"},
    "err_database_default": {
        "en": "An internal storage error occurred.",
        "ru": "Внутренняя ошибка хранилища.",
    },
    "err_database_status": {
        "en": "This is an internal issue. Our team has been notified.",
        "ru": "Это внутренняя проблема. Команда уведомлена.",
    },
    "err_access_denied_title": {"en": "Access Denied", "ru": "Доступ запрещен"},
    "err_access_denied_body": {
        "en": "User ID {uid} is not authorized to use this bot.",
        "ru": "Пользователь с ID {uid} не авторизован для использования бота.",
    },
    "err_access_denied_contact": {
        "en": "If you believe this is an error, please contact the administrator.",
        "ru": "Если вы считаете это ошибкой, свяжитесь с администратором.",
    },
    "err_access_blocked_title": {"en": "Access Blocked", "ru": "Доступ заблокирован"},
    "err_access_blocked_default": {
        "en": "Too many failed access attempts.",
        "ru": "Слишком много неудачных попыток доступа.",
    },
    "err_access_blocked_status": {"en": "Please try again later.", "ru": "Попробуйте позже."},
    "err_message_too_long_title": {"en": "Message Too Long", "ru": "Сообщение слишком длинное"},
    "err_message_too_long_default": {
        "en": "The message exceeds the maximum allowed length.",
        "ru": "Сообщение превышает максимально допустимую длину.",
    },
    "err_message_too_long_hint_split": {
        "en": "Split the content into smaller messages",
        "ru": "Разбейте контент на меньшие сообщения",
    },
    "err_message_too_long_hint_file": {
        "en": "Upload a .txt file with the content instead",
        "ru": "Загрузите .txt файл с контентом",
    },
    "err_no_urls_title": {"en": "No URLs Found", "ru": "URL не найдены"},
    "err_no_urls_default": {
        "en": "I couldn't find any valid URLs to process.",
        "ru": "Не удалось найти валидные URL для обработки.",
    },
    "err_no_urls_hint_http": {
        "en": "Ensuring URLs start with http:// or https://",
        "ru": "Убедитесь, что URL начинается с http:// или https://",
    },
    "err_no_urls_hint_typo": {
        "en": "Checking for typos in the address",
        "ru": "Проверьте адрес на опечатки",
    },
    "err_generic_title": {"en": "Error Occurred", "ru": "Произошла ошибка"},
    "err_generic_default": {"en": "Unknown error", "ru": "Неизвестная ошибка"},
    "error_id": {"en": "Error ID", "ru": "ID ошибки"},
    "details": {"en": "Details", "ru": "Подробности"},
    "reason": {"en": "Reason", "ru": "Причина"},
    "status": {"en": "Status", "ru": "Статус"},
    "try_label": {"en": "Try", "ru": "Попробуйте"},
    "suggestions": {"en": "Suggestions", "ru": "Рекомендации"},
    # ------------------------------------------------------------------
    # Button labels (actions.py)
    # ------------------------------------------------------------------
    "btn_more": {"en": "More", "ru": "Ещё"},
    "btn_pdf": {"en": "PDF", "ru": "PDF"},
    "btn_md": {"en": "MD", "ru": "MD"},
    "btn_html": {"en": "HTML", "ru": "HTML"},
    "btn_json": {"en": "JSON", "ru": "JSON"},
    "btn_save": {"en": "Save", "ru": "Сохранить"},
    "btn_similar": {"en": "Similar", "ru": "Похожие"},
    "btn_ask": {"en": "Ask", "ru": "Спросить"},
    # ------------------------------------------------------------------
    # Callback responses (callback_handler.py)
    # ------------------------------------------------------------------
    "cb_generating_summary": {
        "en": "Generating full summary...",
        "ru": "Генерация полного резюме...",
    },
    "cb_export_generating": {
        "en": "Generating {fmt} export...",
        "ru": "Генерация {fmt} экспорта...",
    },
    "cb_export_failed": {
        "en": "Export failed. Summary not found or export error. Error ID: {cid}",
        "ru": "Ошибка экспорта. Резюме не найдено или ошибка экспорта. ID ошибки: {cid}",
    },
    "cb_summary_not_found": {"en": "Summary not found.", "ru": "Резюме не найдено."},
    "cb_no_similar": {"en": "No similar summaries found.", "ru": "Похожие резюме не найдены."},
    "cb_finding_similar": {
        "en": "Finding similar summaries for: {title}...",
        "ru": "Поиск похожих резюме: {title}...",
    },
    "cb_saved": {"en": "Summary saved to favorites.", "ru": "Резюме добавлено в избранное."},
    "cb_removed": {"en": "Summary removed from favorites.", "ru": "Резюме удалено из избранного."},
    "cb_feedback_thanks": {
        "en": "Thanks for your {rating} feedback! This helps improve summarization quality.",
        "ru": "Спасибо за {rating} отзыв! Это помогает улучшить качество резюме.",
    },
    "cb_feedback_positive": {"en": "positive", "ru": "положительный"},
    "cb_feedback_negative": {"en": "negative", "ru": "отрицательный"},
    "cb_translation_already_ru": {
        "en": "This summary is already in Russian.",
        "ru": "Это резюме уже на русском.",
    },
    "cb_translation_processing": {
        "en": "Translation feature request received. Processing...",
        "ru": "Запрос на перевод получен. Обрабатываем...",
    },
    "cb_no_details": {
        "en": "No additional details available.",
        "ru": "Дополнительных сведений нет.",
    },
    "cb_search_unavailable": {
        "en": "Search service is currently unavailable.",
        "ru": "Сервис поиска временно недоступен.",
    },
    "cb_not_enough_info": {
        "en": "Not enough information to perform similarity search.",
        "ru": "Недостаточно информации для поиска похожих.",
    },
    "cb_followup_prompt": {
        "en": "Ask a follow-up question about this summary. I will answer using the stored summary and source context.",
        "ru": "Задайте уточняющий вопрос по этому резюме. Я отвечу, используя сохраненное резюме и исходный контекст.",
    },
    "cb_followup_continue": {
        "en": "Ask another follow-up, or send /cancel to exit follow-up mode.",
        "ru": "Задайте следующий вопрос или отправьте /cancel для выхода из режима уточнений.",
    },
    "cb_followup_unavailable": {
        "en": "Follow-up Q&A is temporarily unavailable.",
        "ru": "Режим уточняющих вопросов временно недоступен.",
    },
    "cb_followup_no_answer": {
        "en": "I could not generate a grounded answer from the stored summary and source.",
        "ru": "Не удалось сформировать обоснованный ответ на основе сохраненных резюме и источника.",
    },
    "cb_followup_thinking": {
        "en": "Thinking...",
        "ru": "Думаю...",
    },
    "cb_timeout": {
        "en": "Operation timed out. Please try again.",
        "ru": "Превышено время ожидания. Попробуйте снова.",
    },
    "cb_retrying": {
        "en": "Retrying summarization...",
        "ru": "Повторная попытка суммаризации...",
    },
    "cb_retry_url_not_found": {
        "en": "Could not find the original URL for this request. Please send the URL again.",
        "ru": "Не удалось найти исходный URL для этого запроса. Отправьте URL повторно.",
    },
    "cb_retry_unavailable": {
        "en": "Retry is temporarily unavailable. Please send the URL again.",
        "ru": "Повторная попытка временно недоступна. Отправьте URL повторно.",
    },
    # ------------------------------------------------------------------
    # Callback handler -- "More" section headers
    # ------------------------------------------------------------------
    "more_long_summary": {"en": "Long summary", "ru": "Развернутое резюме"},
    "more_research_highlights": {"en": "Research highlights", "ru": "Результаты исследования"},
    "more_answered_questions": {"en": "Answered questions", "ru": "Ответы на вопросы"},
    "more_tags": {"en": "Tags", "ru": "Теги"},
    "more_entities": {"en": "Entities", "ru": "Сущности"},
    # ------------------------------------------------------------------
    # Router messages (message_router.py)
    # ------------------------------------------------------------------
    "fallback_prompt": {
        "en": "Send a URL, forward a channel post, or upload a .txt / .md file.",
        "ru": "Отправьте URL, перешлите пост из канала или загрузите файл .txt / .md.",
    },
    "concurrent_ops_limit": {
        "en": "Too many concurrent operations. Please wait for your previous requests to complete.",
        "ru": "Слишком много одновременных операций. Пожалуйста, дождитесь завершения предыдущих запросов.",
    },
    # ------------------------------------------------------------------
    # Progress formatter (single_url_progress_formatter.py)
    # ------------------------------------------------------------------
    "progress_extracting_content": {
        "en": "Content Extraction",
        "ru": "Извлечение контента",
    },
    "progress_extracting": {"en": "Extracting...", "ru": "Извлекаем..."},
    "progress_ai_analysis": {"en": "AI Analysis", "ru": "AI-анализ"},
    "progress_content": {"en": "Content", "ru": "Контент"},
    "progress_lang": {"en": "Language", "ru": "Язык"},
    "progress_tier": {"en": "Category", "ru": "Категория"},
    "progress_model": {"en": "Model", "ru": "Модель"},
    "progress_status_processing": {
        "en": "Status: Processing with smart fallbacks",
        "ru": "Статус: Обработка с автоматическими fallback-ами",
    },
    "progress_analyzing": {"en": "Analyzing...", "ru": "Анализируем..."},
    "progress_retrying": {"en": "Retrying with fallback...", "ru": "Повтор с fallback-моделью..."},
    "progress_enriching": {"en": "Generating insights...", "ru": "Генерация инсайтов..."},
    "progress_processing": {"en": "Processing...", "ru": "Обработка..."},
    "progress_analysis_complete": {"en": "Analysis Complete", "ru": "Анализ завершен"},
    "progress_summary_generated": {"en": "Summary generated", "ru": "Резюме сгенерировано"},
    "progress_analysis_failed": {"en": "Analysis Failed", "ru": "Анализ не удался"},
    "progress_error": {"en": "Error", "ru": "Ошибка"},
    "progress_error_id": {"en": "Error ID", "ru": "ID ошибки"},
    "progress_youtube_processing": {
        "en": "YouTube Video Processing",
        "ru": "Обработка видео YouTube",
    },
    "progress_video_complete": {
        "en": "Video Processing Complete",
        "ru": "Обработка видео завершена",
    },
    "progress_video_failed": {"en": "Video Processing Failed", "ru": "Ошибка обработки видео"},
    "progress_transcript_ready": {"en": "Transcript ready", "ru": "Транскрипт готов"},
    "progress_total": {"en": "Total", "ru": "Всего"},
    # ------------------------------------------------------------------
    # Data formatter (data_formatter.py)
    # ------------------------------------------------------------------
    "yes": {"en": "Yes", "ru": "Да"},
    "no": {"en": "No", "ru": "Нет"},
    "source": {"en": "Source", "ru": "Источник"},
    "score": {"en": "Score", "ru": "Оценка"},
    "level": {"en": "Level", "ru": "Уровень"},
    # ------------------------------------------------------------------
    # Custom article (summary_presenter.py)
    # ------------------------------------------------------------------
    "key_highlights": {"en": "Key Highlights", "ru": "Ключевые моменты"},
    # ------------------------------------------------------------------
    # Related reads
    # ------------------------------------------------------------------
    "related_header": {"en": "Related reads:", "ru": "Связанные статьи:"},
    "cb_related_not_found": {"en": "Summary not found.", "ru": "Резюме не найдено."},
    # ------------------------------------------------------------------
    # No-answer placeholder
    # ------------------------------------------------------------------
    "no_answer": {"en": "(No answer provided)", "ru": "(Ответ не предоставлен)"},
}


def t(key: str, lang: str = LANG_EN) -> str:
    """Look up a UI string by *key* for the given *lang*.

    Falls back to English, then to the raw key itself.
    """
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    return entry.get(lang, entry.get(LANG_EN, key))
