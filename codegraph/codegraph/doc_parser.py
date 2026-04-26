"""Deterministic document extraction (PDF + Markdown).

Extracts text from PDF and Markdown files and produces :class:`DocumentNode` /
:class:`DocumentSectionNode` instances for loading into Neo4j.
PDF sections are derived from the PDF outline (bookmarks) when available,
or fall back to one section per page.  Markdown sections are derived from
heading hierarchy (``#`` through ``######``).

PDF extraction requires the ``[docs]`` extra: ``pip install "codegraph[docs]"``.
Markdown extraction has no extra dependencies.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .schema import DocumentNode, DocumentSectionNode

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_MAX_FILE_SIZE = 50_000_000  # 50 MB


def extract_pdf(
    path: Path,
    rel: str,
    repo_name: str = "default",
) -> tuple[DocumentNode, list[DocumentSectionNode]]:
    """Extract text from *path* and return a document node + sections.

    Parameters
    ----------
    path:
        Absolute path to the PDF file on disk.
    rel:
        Repo-relative path (used as the node ``path`` field).
    repo_name:
        Repository namespace for multi-repo indexing.

    Returns
    -------
    tuple
        ``(DocumentNode, list[DocumentSectionNode])``

    Raises
    ------
    ImportError
        If ``pypdf`` is not installed.
    ValueError
        If the file exceeds the 50 MB size guard.
    """
    if pypdf is None:
        raise ImportError(
            "Install the [docs] extra: pip install 'codegraph[docs]'"
        )

    if path.stat().st_size > _MAX_FILE_SIZE:
        raise ValueError(
            f"{rel}: file exceeds 50 MB size limit "
            f"({path.stat().st_size / 1_000_000:.1f} MB)"
        )

    try:
        reader = pypdf.PdfReader(path)
    except Exception as exc:
        log.warning("Cannot read PDF %s: %s", rel, exc)
        return (
            DocumentNode(
                path=rel, file_type="pdf", loc=0,
                extracted_at=datetime.now(timezone.utc).isoformat(),
                repo=repo_name,
            ),
            [],
        )

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            log.warning("Encrypted PDF %s — returning empty document", rel)
            return (
                DocumentNode(
                    path=rel, file_type="pdf", loc=0,
                    extracted_at=datetime.now(timezone.utc).isoformat(),
                    repo=repo_name,
                ),
                [],
            )

    # Extract per-page text.
    page_texts: list[str] = []
    for page in reader.pages:
        try:
            page_texts.append(page.extract_text() or "")
        except Exception:
            page_texts.append("")

    full_text = "\n".join(page_texts)
    loc = len(full_text)
    extracted_at = datetime.now(timezone.utc).isoformat()

    # Try outline-based sections first.
    sections = _sections_from_outline(reader, page_texts, rel, repo_name)
    if not sections:
        sections = _sections_from_pages(page_texts, rel, repo_name)

    doc = DocumentNode(
        path=rel,
        file_type="pdf",
        loc=loc,
        extracted_at=extracted_at,
        repo=repo_name,
    )
    return doc, sections


def _sections_from_outline(
    reader: "pypdf.PdfReader",
    page_texts: list[str],
    rel: str,
    repo_name: str,
) -> list[DocumentSectionNode]:
    """Derive sections from PDF outline/bookmarks."""
    try:
        outline = reader.outline
    except Exception:
        return []

    if not outline:
        return []

    # Flatten nested outline into (title, page_index) pairs.
    entries: list[tuple[str, int]] = []
    _flatten_outline(outline, reader, entries)

    if not entries:
        return []

    sections: list[DocumentSectionNode] = []
    for idx, (title, page_idx) in enumerate(entries):
        # Collect text from this bookmark's page.
        text = page_texts[page_idx] if page_idx < len(page_texts) else ""
        sections.append(DocumentSectionNode(
            path=rel,
            heading=title,
            section_index=idx,
            text_sample=text[:500],
            repo=repo_name,
        ))
    return sections


def _flatten_outline(
    items: list,
    reader: "pypdf.PdfReader",
    out: list[tuple[str, int]],
) -> None:
    """Recursively flatten a nested PDF outline into (title, page_index) pairs."""
    for item in items:
        if isinstance(item, list):
            _flatten_outline(item, reader, out)
        else:
            try:
                title = str(item.title)
                page_idx = reader.get_destination_page_number(item)
                out.append((title, page_idx))
            except Exception:
                continue


def _sections_from_pages(
    page_texts: list[str],
    rel: str,
    repo_name: str,
) -> list[DocumentSectionNode]:
    """One section per page (fallback when no outline is available)."""
    sections: list[DocumentSectionNode] = []
    seq = 0
    for idx, text in enumerate(page_texts):
        if not text.strip():
            continue
        sections.append(DocumentSectionNode(
            path=rel,
            heading=f"Page {idx + 1}",
            section_index=seq,
            text_sample=text[:500],
            repo=repo_name,
        ))
        seq += 1
    return sections


# ── Markdown extraction ─────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FENCED_CODE_RE = re.compile(r"^```[^\n]*\n.*?^```", re.MULTILINE | re.DOTALL)


def extract_markdown(
    path: Path,
    rel: str,
    repo_name: str = "default",
) -> tuple[DocumentNode, list[DocumentSectionNode]]:
    """Extract sections from a Markdown file using heading hierarchy.

    Parameters
    ----------
    path:
        Absolute path to the ``.md`` file on disk.
    rel:
        Repo-relative path (used as the node ``path`` field).
    repo_name:
        Repository namespace for multi-repo indexing.

    Returns
    -------
    tuple
        ``(DocumentNode, list[DocumentSectionNode])``

    Raises
    ------
    ValueError
        If the file exceeds the 50 MB size guard.
    """
    if path.stat().st_size > _MAX_FILE_SIZE:
        raise ValueError(
            f"{rel}: file exceeds 50 MB size limit "
            f"({path.stat().st_size / 1_000_000:.1f} MB)"
        )

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Cannot read markdown %s: %s", rel, exc)
        return (
            DocumentNode(
                path=rel, file_type="markdown", loc=0,
                extracted_at=datetime.now(timezone.utc).isoformat(),
                repo=repo_name,
            ),
            [],
        )

    loc = len(content)
    extracted_at = datetime.now(timezone.utc).isoformat()

    # Replace fenced code blocks with same-length whitespace so that
    # heading regex positions stay valid for slicing the original content.
    defenced = _FENCED_CODE_RE.sub(lambda m: " " * len(m.group(0)), content)
    headings = list(_HEADING_RE.finditer(defenced))
    sections: list[DocumentSectionNode] = []

    if not headings:
        # No headings — return single section with full text sample if non-empty.
        if content.strip():
            sections.append(DocumentSectionNode(
                path=rel,
                heading="(untitled)",
                section_index=0,
                text_sample=content[:500],
                repo=repo_name,
            ))
    else:
        for idx, match in enumerate(headings):
            heading_text = match.group(2).strip()
            # Section content: text from after this heading to the start of the
            # next heading (or end of file).
            start = match.end()
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(content)
            section_content = content[start:end].strip()
            sections.append(DocumentSectionNode(
                path=rel,
                heading=heading_text,
                section_index=idx,
                text_sample=section_content[:500],
                repo=repo_name,
            ))

    doc = DocumentNode(
        path=rel,
        file_type="markdown",
        loc=loc,
        extracted_at=extracted_at,
        repo=repo_name,
    )
    return doc, sections
