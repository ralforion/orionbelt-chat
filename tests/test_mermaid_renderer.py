"""Tests for src.mermaid_renderer."""

from unittest.mock import MagicMock

from src.mermaid_renderer import extract_mermaid_from_tool_results, is_mermaid


class TestIsMermaid:
    def test_er_diagram(self):
        assert is_mermaid("erDiagram\n  CUSTOMER ||--o{ ORDER : places") is True

    def test_er_diagram_with_init(self):
        text = "%%{init: {'theme': 'default'}}%%\nerDiagram\n  A ||--o{ B : has"
        assert is_mermaid(text) is True

    def test_flowchart(self):
        assert is_mermaid("flowchart LR\n  A --> B") is True

    def test_graph(self):
        assert is_mermaid("graph TD\n  A --> B") is True

    def test_sequence_diagram(self):
        assert is_mermaid("sequenceDiagram\n  Alice->>Bob: Hello") is True

    def test_class_diagram(self):
        assert is_mermaid("classDiagram\n  Animal <|-- Duck") is True

    def test_gantt(self):
        assert (
            is_mermaid("gantt\n  title A Gantt\n  section A\n  task1 :a1, 2024-01-01, 30d") is True
        )

    def test_pie(self):
        assert is_mermaid('pie title Pets\n  "Dogs" : 386\n  "Cats" : 85') is True

    def test_plain_text(self):
        assert is_mermaid("just some text") is False

    def test_json(self):
        assert is_mermaid('{"data": [1, 2, 3]}') is False

    def test_sql(self):
        assert is_mermaid("SELECT * FROM users") is False

    def test_leading_whitespace(self):
        assert is_mermaid("  \n  erDiagram\n  A ||--o{ B : has") is True


class TestExtractMermaidFromToolResults:
    def _make_messages(self, contents):
        """Create mock messages with ToolReturnPart-like parts."""
        messages = []
        for content in contents:
            part = MagicMock()
            type(part).__name__ = "ToolReturnPart"
            part.content = content
            part.tool_name = "test_tool"
            msg = MagicMock()
            msg.parts = [part]
            messages.append(msg)
        return messages

    def test_finds_mermaid_string(self):
        msgs = self._make_messages(["erDiagram\n  A ||--o{ B : has"])
        result = extract_mermaid_from_tool_results(msgs)
        assert len(result) == 1
        assert result[0].startswith("erDiagram")

    def test_finds_mermaid_in_dict(self):
        msgs = self._make_messages([{"content": "erDiagram\n  X ||--o{ Y : links"}])
        result = extract_mermaid_from_tool_results(msgs)
        assert len(result) == 1

    def test_finds_mermaid_in_diagram_key(self):
        msgs = self._make_messages([{"diagram": "flowchart LR\n  A --> B"}])
        result = extract_mermaid_from_tool_results(msgs)
        assert len(result) == 1

    def test_no_mermaid(self):
        msgs = self._make_messages(["Just a plain result", {"data": "some data"}])
        result = extract_mermaid_from_tool_results(msgs)
        assert result == []

    def test_empty_messages(self):
        assert extract_mermaid_from_tool_results([]) == []

    def test_multiple_diagrams(self):
        msgs = self._make_messages(
            [
                "erDiagram\n  A ||--o{ B : has",
                "flowchart LR\n  X --> Y",
            ]
        )
        result = extract_mermaid_from_tool_results(msgs)
        assert len(result) == 2

    def test_skips_non_tool_parts(self):
        part = MagicMock()
        type(part).__name__ = "TextPart"
        part.content = "erDiagram\n  A ||--o{ B : has"
        msg = MagicMock()
        msg.parts = [part]
        result = extract_mermaid_from_tool_results([msg])
        assert result == []
