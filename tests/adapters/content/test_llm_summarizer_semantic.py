import pytest

from app.adapters.content import llm_summarizer_semantic as semantic_module
from app.adapters.content.llm_summarizer_semantic import LLMSemanticHelper


@pytest.mark.asyncio
async def test_enrich_with_rag_fields_generates_missing_semantic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = LLMSemanticHelper()

    async def fake_tfidf(content_text: str, topn: int) -> list[str]:
        return [f"kw{i}" for i in range(25)]

    monkeypatch.setattr(helper, "_extract_keywords_tfidf_async", fake_tfidf)

    summary = {
        "summary_1000": "This is a long enough summary sentence for semantic boosters.",
        "summary_250": "Another useful semantic sentence appears here.",
        "tldr": "Short version.",
        "topic_tags": ["#AI", " ML "],
        "key_ideas": ["Idea", "idea"],
    }
    content = " ".join(f"word{i}" for i in range(310))

    result = await helper.enrich_with_rag_fields(
        summary,
        content_text=content,
        chosen_lang="en",
        req_id=42,
    )

    assert result["language"] == "en"
    assert result["article_id"] == "42"
    assert result["semantic_boosters"][0].startswith("This is a long enough")
    assert result["query_expansion_keywords"][:3] == ["Idea", "AI", "ML"]
    assert len(result["query_expansion_keywords"]) == 28
    assert len(result["semantic_chunks"]) == 3
    assert result["semantic_chunks"][0]["topics"] == ["AI", "ML"]


@pytest.mark.asyncio
async def test_enrich_with_rag_fields_trims_existing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = LLMSemanticHelper()
    summary = {
        "language": "ru",
        "article_id": "existing",
        "semantic_boosters": [str(i) for i in range(20)],
        "query_expansion_keywords": [str(i) for i in range(35)],
        "semantic_chunks": [{"text": "existing"}],
    }

    result = await helper.enrich_with_rag_fields(
        summary,
        content_text="body",
        chosen_lang=None,
        req_id=99,
    )

    assert result["language"] == "ru"
    assert result["article_id"] == "existing"
    assert result["semantic_boosters"] == [str(i) for i in range(15)]
    assert result["query_expansion_keywords"] == [str(i) for i in range(30)]
    assert result["semantic_chunks"] == [{"text": "existing"}]


@pytest.mark.asyncio
async def test_keyword_generation_handles_empty_and_tfidf_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = LLMSemanticHelper()

    assert helper._extract_keywords_simple("") == []
    assert helper._extract_keywords_simple("the 123 and tiny useful useful другой") == [
        "useful",
        "tiny",
        "другой",
    ]

    def failing_tfidf(content_text: str, topn: int) -> list[str]:
        raise RuntimeError("bad")

    monkeypatch.setattr(semantic_module, "extract_keywords_tfidf", failing_tfidf)
    assert await helper._extract_keywords_tfidf_async("content", 3) == []
    assert await helper._extract_keywords_tfidf_async("   ", 3) == []


def test_semantic_chunks_and_local_summary_edge_cases() -> None:
    helper = LLMSemanticHelper()

    assert helper._build_semantic_chunks("", topics=[], article_id=None, language=None) == []

    chunk = helper._build_semantic_chunks(
        "First sentence. Second sentence. " + " ".join(["keyword"] * 120),
        topics=["topic"],
        article_id="a1",
        language="en",
        target_words=50,
    )[0]

    assert chunk["article_id"] == "a1"
    assert chunk["local_summary"].startswith("First sentence. Second sentence.")
    assert chunk["local_keywords"] == ["keyword", "sentence", "first", "second"]
    assert helper._extract_local_summary("single " * 100)
