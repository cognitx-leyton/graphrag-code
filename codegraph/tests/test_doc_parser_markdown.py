"""Tests for markdown extraction in :mod:`codegraph.doc_parser`."""
from __future__ import annotations

from pathlib import Path

import pytest

from codegraph.doc_parser import extract_markdown
from codegraph.schema import DocumentNode, DocumentSectionNode

FIXTURES = Path(__file__).parent / "fixtures" / "markdown"
CONCEPTS = FIXTURES / "concepts.md"
DECISIONS = FIXTURES / "decisions.md"
EMPTY = FIXTURES / "empty.md"
NO_HEADINGS = FIXTURES / "no-headings.md"


# ── Basic extraction ──────────────────────────────────────────────────


def test_extract_markdown_basic():
    doc, sections = extract_markdown(CONCEPTS, "docs/concepts.md")
    assert isinstance(doc, DocumentNode)
    assert doc.file_type == "markdown"
    assert doc.path == "docs/concepts.md"
    assert doc.loc > 0


def test_extract_markdown_sections():
    doc, sections = extract_markdown(CONCEPTS, "docs/concepts.md")
    # concepts.md has 3 headings: Graph Indexing, Incremental Update, Schema Migration
    assert len(sections) == 3
    for sec in sections:
        assert isinstance(sec, DocumentSectionNode)
        assert sec.heading
        assert len(sec.text_sample) <= 500


def test_extract_markdown_section_index_sequential():
    _, sections = extract_markdown(CONCEPTS, "docs/concepts.md")
    indices = [s.section_index for s in sections]
    assert indices == list(range(len(sections)))


# ── ID format ─────────────────────────────────────────────────────────


def test_extract_markdown_id_format():
    doc, sections = extract_markdown(CONCEPTS, "docs/concepts.md")
    assert doc.id.startswith("doc:default:")
    for sec in sections:
        assert sec.id.startswith("docsec:default:")


def test_extract_markdown_repo_name():
    doc, sections = extract_markdown(CONCEPTS, "docs/concepts.md", repo_name="myrepo")
    assert "myrepo" in doc.id
    for sec in sections:
        assert "myrepo" in sec.id


# ── Edge cases ────────────────────────────────────────────────────────


def test_extract_markdown_no_headings():
    doc, sections = extract_markdown(NO_HEADINGS, "docs/no-headings.md")
    assert doc.loc > 0
    # Should return a single untitled section for non-empty content
    assert len(sections) == 1
    assert sections[0].heading == "(untitled)"


def test_extract_markdown_empty_file():
    doc, sections = extract_markdown(EMPTY, "docs/empty.md")
    assert doc.loc == 0
    assert sections == []


def test_extract_markdown_text_sample_limit():
    _, sections = extract_markdown(DECISIONS, "docs/decisions.md")
    for sec in sections:
        assert len(sec.text_sample) <= 500


def test_extract_markdown_section_paths_match_doc():
    doc, sections = extract_markdown(CONCEPTS, "docs/concepts.md")
    for sec in sections:
        assert sec.path == doc.path


def test_extract_markdown_size_guard(tmp_path):
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (50_000_001))
    with pytest.raises(ValueError, match="exceeds 50 MB"):
        extract_markdown(big, "big.md")


def test_extract_markdown_fenced_code_block_ignored(tmp_path):
    """Headings inside fenced code blocks should not be treated as sections."""
    md = tmp_path / "fenced.md"
    md.write_text(
        "# Real Heading\n\n"
        "Some text.\n\n"
        "```markdown\n"
        "# Fake Heading Inside Fence\n"
        "```\n\n"
        "## Another Real Heading\n\n"
        "More text.\n",
        encoding="utf-8",
    )
    doc, sections = extract_markdown(md, "fenced.md")
    headings = [s.heading for s in sections]
    assert "Real Heading" in headings
    assert "Another Real Heading" in headings
    assert "Fake Heading Inside Fence" not in headings
    assert len(sections) == 2
