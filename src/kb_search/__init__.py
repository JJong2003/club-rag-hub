"""검색 핵심. 웹 질의응답 Lambda와 MCP 도구 Lambda가 같이 씁니다.

    import kb_search
    kb_search.list_sources(["ana"])
    kb_search.search("동아리방에서 전기난로를 써도 되나요?", ["ana"])
    kb_search.read_document("school/kb/electric-heater__a004.md", ["ana"])

형식은 노션 '웹 챗봇 설계 (AWS) > 인터페이스 약속 > kb_search'를 따릅니다. 자세한 설명은 core.py에 있습니다.
"""

from .core import InvalidRequest, list_sources, read_document, search

__all__ = ["InvalidRequest", "list_sources", "read_document", "search"]
