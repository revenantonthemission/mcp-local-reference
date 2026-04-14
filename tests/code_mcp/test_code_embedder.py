"""Tests for incremental vector embedding."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_mcp.models import CodeSymbol


def make_symbol(
    name: str = "func",
    text: str = "def func():\n    return 42\n    # padding to exceed MIN_EMBED_CHARS threshold",
    **kwargs,
) -> CodeSymbol:
    return CodeSymbol(
        name=name,
        symbol_type=kwargs.get("symbol_type", "function_definition"),
        text=text,
        start_line=kwargs.get("start_line", 1),
        end_line=kwargs.get("end_line", 3),
        language=kwargs.get("language", "python"),
        file_id=kwargs.get("file_id", 0),
        signature=kwargs.get("signature", f"def {name}():"),
    )


@pytest.fixture()
def embedder():
    """Return a CodeEmbedder with mocked model and DB."""
    from code_mcp.code_embedder import CodeEmbedder

    e = CodeEmbedder.__new__(CodeEmbedder)
    e.model_name = "test-model"
    e._device = "cpu"
    e._backend = "torch"
    e._embedding_dim = 4

    # Mock the model to return fixed-size vectors matching input length
    mock_model = MagicMock()

    def fake_encode(texts, **kwargs):
        result = MagicMock()
        result.tolist.return_value = [[0.1, 0.2, 0.3, 0.4]] * len(texts)
        return result

    mock_model.encode.side_effect = fake_encode
    e._model = mock_model

    # Mock LanceDB
    e._db = MagicMock()

    return e


class TestIncrementalEmbedding:
    def test_creates_table_when_not_exists(self, embedder) -> None:
        embedder._db.table_names.return_value = []

        file_symbols = [(1, [make_symbol(name="func_a")])]
        embedder.add_symbols_batch(file_symbols)

        embedder._db.create_table.assert_called_once()
        args = embedder._db.create_table.call_args
        assert args[0][0] == "code_chunks"

    def test_deletes_then_adds_when_table_exists(self, embedder) -> None:
        mock_table = MagicMock()
        embedder._db.table_names.return_value = ["code_chunks"]
        embedder._db.open_table.return_value = mock_table

        file_symbols = [(1, [make_symbol(name="func_a")]), (2, [make_symbol(name="func_b")])]
        embedder.add_symbols_batch(file_symbols)

        # Should delete old data for file_ids 1 and 2
        mock_table.delete.assert_called_once()
        delete_expr = mock_table.delete.call_args[0][0]
        assert "1" in delete_expr
        assert "2" in delete_expr

        # Should add new data
        mock_table.add.assert_called_once()

    def test_does_not_overwrite_entire_table(self, embedder) -> None:
        """Ensure create_table with mode='overwrite' is NOT called when table exists."""
        embedder._db.table_names.return_value = ["code_chunks"]
        embedder._db.open_table.return_value = MagicMock()

        file_symbols = [(1, [make_symbol()])]
        embedder.add_symbols_batch(file_symbols)

        # create_table should NOT be called (we use open_table + delete + add instead)
        embedder._db.create_table.assert_not_called()
