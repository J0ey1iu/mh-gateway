"""Text extraction for uploaded document files.

Every extractor is **stdlib-only** and model-agnostic: the extracted text is
exactly what a model sees when it reads an attachment via the
``read_attachment`` tool.  There is deliberately no dependency on document
libraries — docx/pptx are ZIP+XML, and .msg is an OLE2 compound file, all of
which the standard library can read.

Extending to a new format = add one :class:`FileExtractor` subclass and one
line in :data:`_EXTRACTORS`.  No other layer changes.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class UnsupportedFormatError(ValueError):
    """Raised when a file's content cannot be extracted as text."""


class FileExtractor(Protocol):
    """Extract human-readable text from raw file bytes."""

    extensions: set[str]

    def extract(self, data: bytes) -> str:
        """Return the extracted text (may be empty). Raises
        :class:`UnsupportedFormatError` when the bytes are not this format."""
        ...


# ── Text files (txt / md) ─────────────────────────────────────────────────────


class TextFileExtractor:
    extensions = {
        "txt",
        "md",
        "log",
        "csv",
        "json",
        "yaml",
        "yml",
        "xml",
        "html",
        "ini",
        "conf",
    }

    def extract(self, data: bytes) -> str:
        # BOM-aware UTF-8 first, then common fallbacks; latin-1 never fails.
        for encoding in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
            try:
                return data.decode(encoding).strip()
            except (UnicodeDecodeError, ValueError):
                continue
        return data.decode("utf-8", errors="replace").strip()


# ── DOCX (Word) ───────────────────────────────────────────────────────────────


class DocxExtractor:
    extensions = {"docx"}

    def extract(self, data: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                xml_bytes = zf.read("word/document.xml")
        except (zipfile.BadZipFile, KeyError) as exc:
            raise UnsupportedFormatError(f"not a valid .docx file: {exc}") from exc
        root = ET.fromstring(xml_bytes)
        paragraphs: list[str] = []
        for p in root.iter(_W + "p"):
            texts = [t.text or "" for t in p.iter(_W + "t")]
            paragraphs.append("".join(texts))
        return "\n".join(paragraphs).strip()


# ── PPTX (PowerPoint) ─────────────────────────────────────────────────────────


class PptxExtractor:
    extensions = {"pptx"}

    def extract(self, data: bytes) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                slide_names = sorted(
                    n
                    for n in zf.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
                )
                if not slide_names:
                    raise UnsupportedFormatError(
                        "not a valid .pptx file (no slides found)"
                    )
                slides: list[str] = []
                for name in slide_names:
                    root = ET.fromstring(zf.read(name))
                    paragraphs: list[str] = []
                    for p in root.iter(_A + "p"):
                        runs = [t.text or "" for t in p.iter(_A + "t")]
                        paragraphs.append("".join(runs))
                    slides.append(
                        f"[Slide {len(slides) + 1}]\n" + "\n".join(paragraphs)
                    )
        except zipfile.BadZipFile as exc:
            raise UnsupportedFormatError(f"not a valid .pptx file: {exc}") from exc
        return "\n\n".join(slides).strip()


# ── MSG (Outlook) — minimal OLE2 compound file reader ─────────────────────────


class _Ole2Reader:
    """Reads a named top-level stream out of an OLE2 (Compound File Binary)
    document.  Enough for .msg files: the message body lives in the ``Body``
    stream as UTF-16LE text."""

    _FREESECT = 0xFFFFFFFF
    _ENDOFCHAIN = 0xFFFFFFFE

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise UnsupportedFormatError("not an OLE2 compound file (.msg)")
        self._data = data
        sector_shift = struct.unpack_from("<H", data, 30)[0]
        self._sector_size = 1 << sector_shift
        self._mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        self._num_fat_sectors = struct.unpack_from("<I", data, 44)[0]
        self._first_dir_sector = struct.unpack_from("<I", data, 48)[0]
        self._mini_stream_cutoff = struct.unpack_from("<I", data, 56)[0]
        self._first_minifat_sector = struct.unpack_from("<I", data, 60)[0]
        self._num_minifat_sectors = struct.unpack_from("<I", data, 64)[0]

        # DIFAT: 109 header entries + chained DIFAT sectors.
        difat = list(struct.unpack_from("<109I", data, 76))
        cur = struct.unpack_from("<I", data, 68)[0]
        while cur not in (self._ENDOFCHAIN, self._FREESECT):
            off = self._sector_offset(cur)
            entries = struct.unpack_from(
                f"<{self._sector_size // 4 - 1}I", self._data, off
            )
            difat.extend(entries)
            nxt = struct.unpack_from("<I", self._data, off + self._sector_size - 4)[0]
            if nxt in (self._ENDOFCHAIN, self._FREESECT):
                break
            cur = nxt

        fat_sectors = [s for s in difat if s not in (self._FREESECT, self._ENDOFCHAIN)][
            : self._num_fat_sectors
        ]
        self._fat: list[int] = []
        for fs in fat_sectors:
            off = self._sector_offset(fs)
            self._fat.extend(
                struct.unpack_from(f"<{self._sector_size // 4}I", self._data, off)
            )

        # MiniFAT (only used when streams are below the mini-stream cutoff).
        self._minifat: list[int] = []
        if self._num_minifat_sectors and self._first_minifat_sector not in (
            self._ENDOFCHAIN,
            self._FREESECT,
        ):
            for ms in self._chain_sectors(self._first_minifat_sector):
                off = self._sector_offset(ms)
                self._minifat.extend(
                    struct.unpack_from(f"<{self._sector_size // 4}I", self._data, off)
                )

    def _sector_offset(self, sector: int) -> int:
        return 512 + sector * self._sector_size

    def _chain_sectors(self, start: int) -> list[int]:
        sectors: list[int] = []
        cur = start
        while cur not in (self._ENDOFCHAIN, self._FREESECT):
            if cur >= len(self._fat):
                raise UnsupportedFormatError("OLE2 FAT chain out of range")
            sectors.append(cur)
            cur = self._fat[cur]
            if len(sectors) > 1_000_000:  # guard against corrupt chains
                raise UnsupportedFormatError("OLE2 FAT chain too long")
        return sectors

    def _read_chain(self, start: int) -> bytes:
        size = self._sector_size
        return b"".join(
            self._data[self._sector_offset(s) : self._sector_offset(s) + size]
            for s in self._chain_sectors(start)
        )

    def _directory_entries(self) -> list[tuple[str, int, int, int]]:
        raw = self._read_chain(self._first_dir_sector)
        entries: list[tuple[str, int, int, int]] = []
        for i in range(len(raw) // 128):
            off = i * 128
            obj_type = raw[off + 66]
            if obj_type == 0:  # empty slot
                continue
            # Name length (bytes 64-66) includes the trailing NUL; decode
            # everything before it.  A byte-pattern split is unreliable
            # (a name ending in NUL merges with the terminator).
            name_len = struct.unpack_from("<H", raw, off + 64)[0]
            if name_len < 2 or name_len > 64:
                continue
            name = raw[off : off + name_len - 2].decode("utf-16-le", errors="replace")
            start_sector = struct.unpack_from("<I", raw, off + 116)[0]
            size = struct.unpack_from("<Q", raw, off + 120)[0]
            entries.append((name, obj_type, start_sector, size))
        return entries

    def read_stream(self, name: str) -> bytes | None:
        root: tuple[int, int] | None = None
        target: tuple[int, int] | None = None
        for entry_name, obj_type, start, size in self._directory_entries():
            if obj_type == 5:  # root storage
                root = (start, size)
            elif entry_name == name and obj_type == 2:  # stream
                target = (start, size)
        if target is None:
            return None
        start, size = target
        if size < self._mini_stream_cutoff and root is not None:
            # Mini stream: the root entry's chain holds the mini sectors,
            # chained by the miniFAT.
            mini_start, _ = root
            mini_bytes = self._read_chain(mini_start)
            ms = self._mini_sector_size
            chain: list[int] = []
            cur = start
            while cur not in (self._ENDOFCHAIN, self._FREESECT):
                if cur >= len(self._minifat):
                    break
                chain.append(cur)
                cur = self._minifat[cur]
            out = b"".join(mini_bytes[cur * ms : (cur + 1) * ms] for cur in chain)
            return out[:size]
        chain = self._chain_sectors(start)
        out = b"".join(
            self._data[
                self._sector_offset(s) : self._sector_offset(s) + self._sector_size
            ]
            for s in chain
        )
        return out[:size]


class MsgExtractor:
    extensions = {"msg"}

    def extract(self, data: bytes) -> str:
        reader = _Ole2Reader(data)
        body = reader.read_stream("Body")
        if body is None:
            raise UnsupportedFormatError(
                "no Body stream in .msg file — the message may be "
                "HTML/RTF-only, which is not supported yet"
            )
        text = body.decode("utf-16-le", errors="replace")
        # OLE streams are zero-padded to sector size; collapse NULs and
        # normalise line endings.
        text = text.replace("\x00", "")
        text = re.sub(r"\r\n?", "\n", text).strip()
        if not text:
            raise UnsupportedFormatError("empty Body stream in .msg file")
        return text


# ── Registry ──────────────────────────────────────────────────────────────────


_EXTRACTORS: list[FileExtractor] = [
    TextFileExtractor(),
    DocxExtractor(),
    PptxExtractor(),
    MsgExtractor(),
]


def get_extractor(
    filename: str, extra: tuple[FileExtractor, ...] | list[FileExtractor] = ()
) -> FileExtractor | None:
    """Return the extractor for *filename*'s extension, or ``None``.

    *extra* holds deployment-injected extractors (``GatewayAdapters.
    attachment_extractors``): they are consulted **before** the built-in
    defaults, so an app can override a default format or add new formats
    without touching the gateway.
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    for extractor in (*extra, *_EXTRACTORS):
        if ext in extractor.extensions:
            return extractor
    return None


def extract_attachment_text(
    filename: str,
    data: bytes,
    extra: tuple[FileExtractor, ...] | list[FileExtractor] = (),
) -> str:
    """Extract text from *data* for *filename*. Raises
    :class:`UnsupportedFormatError` when the extension has no extractor or
    the bytes are not the expected format. *extra* are deployment-injected
    extractors consulted before the built-in defaults."""
    extractor = get_extractor(filename, extra=extra)
    if extractor is None:
        raise UnsupportedFormatError(
            f"unsupported file type: {Path(filename).suffix or '(none)'}"
        )
    return extractor.extract(data)
