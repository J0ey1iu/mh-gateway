"""Unit tests for stdlib text extraction of uploaded documents.

docx/pptx fixtures are minimal ZIP+XML files; msg fixtures are generated
OLE2 compound files (regular-chain and mini-stream variants) built in-memory
so the tests run without any third-party document library.
"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from mh_gateway.attachments import (
    UnsupportedFormatError,
    extract_attachment_text,
    get_extractor,
)

# ── OLE2 (Compound File Binary) writer for .msg fixtures ─────────────────────

_SECTOR = 512
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE


def _make_ole_regular(streams: dict[str, bytes]) -> bytes:
    """OLE2 with regular FAT chains only (stream size >= mini cutoff 4096).

    Layout: header(512) | sector0=FAT | sector1=dir | data sectors...
    """
    padded = {n: s + b"\x00" * (-len(s) % _SECTOR) for n, s in streams.items()}
    num_data_sectors = sum(len(s) // _SECTOR for s in padded.values())
    data_start = 2
    data_blob = b""
    entries: list[bytes] = []
    next_sector = data_start
    fat = [_FREESECT] * (data_start + num_data_sectors)
    fat[0] = _ENDOFCHAIN  # FAT sector
    fat[1] = _ENDOFCHAIN  # directory sector
    for name in streams:
        size = len(padded[name])
        nsectors = size // _SECTOR
        start = next_sector
        for i in range(nsectors):
            idx = next_sector + i
            fat[idx] = next_sector + i + 1 if i < nsectors - 1 else _ENDOFCHAIN
        next_sector += nsectors
        data_blob += padded[name]
        entries.append(_ole_entry(name, 2, start, size))
    root = _ole_entry("Root Entry", 5, _FREESECT, 0)
    dir_blob = b"".join([root] + entries)
    dir_blob += b"\x00" * (-len(dir_blob) % _SECTOR)

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[30:32] = struct.pack("<H", 9)
    header[32:34] = struct.pack("<H", 6)
    header[44:48] = struct.pack("<I", 1)
    header[48:52] = struct.pack("<I", 1)  # dir sector
    header[56:60] = struct.pack("<I", 4096)  # mini cutoff
    header[60:64] = struct.pack("<I", _ENDOFCHAIN)  # no miniFAT
    header[64:68] = struct.pack("<I", 0)
    header[68:72] = struct.pack("<I", _ENDOFCHAIN)  # no DIFAT chain
    header[72:76] = struct.pack("<I", 0)
    difat = [_FREESECT] * 109
    difat[0] = 0  # FAT at sector 0
    header[76:76 + 109 * 4] = struct.pack("<109I", *difat)
    fat_blob = struct.pack(f"<{len(fat)}I", *fat)
    fat_blob += b"\x00" * (-len(fat_blob) % _SECTOR)
    return bytes(header) + fat_blob + dir_blob + data_blob


def _make_ole_mini(name: str, body: bytes) -> bytes:
    """OLE2 where the stream lives in the mini stream (< 4096 bytes).

    Layout: sector0=FAT | sector1=dir | sector2=miniFAT | 3..=mini container.
    """
    container = body + b"\x00" * (-len(body) % 64)
    container += b"\x00" * (-len(container) % _SECTOR)
    while len(container) < 4096:
        container += b"\x00" * _SECTOR
    num_cont = len(container) // _SECTOR
    data_start = 3
    fat = [_FREESECT] * (data_start + num_cont)
    fat[0] = _ENDOFCHAIN
    fat[1] = _ENDOFCHAIN
    fat[2] = _ENDOFCHAIN  # miniFAT sector
    for i in range(num_cont):
        idx = data_start + i
        fat[idx] = idx + 1 if i < num_cont - 1 else _ENDOFCHAIN

    n_mini = (len(body) + 63) // 64
    minifat = [_FREESECT] * 128
    for i in range(n_mini):
        minifat[i] = i + 1 if i < n_mini - 1 else _ENDOFCHAIN
    minifat_blob = struct.pack("<128I", *minifat)

    root = _ole_entry("Root Entry", 5, data_start, len(container))
    stream = _ole_entry(name, 2, 0, len(body))  # start = mini sector index
    dir_blob = root + stream
    dir_blob += b"\x00" * (-len(dir_blob) % _SECTOR)

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[30:32] = struct.pack("<H", 9)
    header[32:34] = struct.pack("<H", 6)
    header[44:48] = struct.pack("<I", 1)
    header[48:52] = struct.pack("<I", 1)  # dir sector
    header[56:60] = struct.pack("<I", 4096)
    header[60:64] = struct.pack("<I", 2)  # first miniFAT sector
    header[64:68] = struct.pack("<I", 1)  # num miniFAT sectors
    header[68:72] = struct.pack("<I", _ENDOFCHAIN)
    header[72:76] = struct.pack("<I", 0)
    difat = [_FREESECT] * 109
    difat[0] = 0
    header[76:76 + 109 * 4] = struct.pack("<109I", *difat)
    fat_blob = struct.pack(f"<{len(fat)}I", *fat)
    fat_blob += b"\x00" * (-len(fat_blob) % _SECTOR)
    return bytes(header) + fat_blob + dir_blob + minifat_blob + container


def _ole_entry(name: str, obj_type: int, start: int, size: int) -> bytes:
    e = bytearray(128)
    raw = (name + "\x00").encode("utf-16-le")
    e[0 : len(raw)] = raw
    e[64:66] = struct.pack("<H", len(raw))
    e[66] = obj_type
    e[116:120] = struct.pack("<I", start)
    e[120:128] = struct.pack("<Q", size)
    return bytes(e)


def make_msg_body(text: str) -> bytes:
    """A small .msg (mini-stream) with the given UTF-16LE body text."""
    return _make_ole_mini("Body", text.encode("utf-16-le"))


def make_msg_body_large(text: str) -> bytes:
    """A .msg whose Body stream uses a regular FAT chain (>= 4096 bytes)."""
    body = text.encode("utf-16-le")
    body += b"\x00" * (4096 - len(body))
    return _make_ole_regular({"Body": body})


# ── Office fixtures ───────────────────────────────────────────────────────────


def make_docx(paragraphs: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        body = "".join(
            f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
        )
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            f"{body}</w:body></w:document>",
        )
    return buf.getvalue()


def make_pptx(slides: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for i, paragraphs in enumerate(slides, 1):
            body = "".join(
                f'<a:p><a:r><a:t>{p}</a:t></a:r></a:p>' for p in paragraphs
            )
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/'
                'presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/'
                f'drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>{body}'
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )
    return buf.getvalue()


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestTextExtraction:
    def test_txt_plain(self) -> None:
        assert extract_attachment_text("notes.txt", "hello world".encode()) == "hello world"

    def test_txt_utf8_bom(self) -> None:
        assert extract_attachment_text("a.txt", "你好".encode("utf-8-sig")) == "你好"

    def test_markdown(self) -> None:
        md = "# Title\n\n**bold** text".encode("utf-8")
        assert extract_attachment_text("README.md", md) == "# Title\n\n**bold** text"

    def test_unknown_extension_raises(self) -> None:
        with pytest.raises(UnsupportedFormatError):
            extract_attachment_text("archive.bin", b"\x00\x01\x02")


class TestDocxExtraction:
    def test_docx_paragraphs(self) -> None:
        text = extract_attachment_text("report.docx", make_docx(["First line", "第二行"]))
        assert text == "First line\n第二行"

    def test_docx_empty(self) -> None:
        assert extract_attachment_text("empty.docx", make_docx([])) == ""

    def test_docx_not_zip_raises(self) -> None:
        with pytest.raises(UnsupportedFormatError):
            extract_attachment_text("bad.docx", b"not a zip")


class TestPptxExtraction:
    def test_pptx_slides(self) -> None:
        text = extract_attachment_text(
            "deck.pptx", make_pptx([["Slide one title"], ["Slide two", "details"]])
        )
        assert "[Slide 1]\nSlide one title" in text
        assert "[Slide 2]\nSlide two\ndetails" in text

    def test_pptx_missing_slides_raises(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
        with pytest.raises(UnsupportedFormatError):
            extract_attachment_text("empty.pptx", buf.getvalue())


class TestMsgExtraction:
    def test_msg_mini_stream(self) -> None:
        text = extract_attachment_text(
            "mail.msg", make_msg_body("Subject: hi\n\nHello from Outlook 你好")
        )
        assert "Hello from Outlook 你好" in text
        assert "Subject: hi" in text

    def test_msg_regular_chain(self) -> None:
        text = extract_attachment_text(
            "big.msg", make_msg_body_large("Body text " + "y" * 100)
        )
        assert "Body text" in text

    def test_msg_multi_sector_stream(self) -> None:
        body = "长内容" * 2000  # > 4096 bytes UTF-16LE → multi-sector chain
        text = extract_attachment_text("big.msg", make_msg_body_large(body))
        assert "长内容" in text

    def test_msg_not_ole_raises(self) -> None:
        with pytest.raises(UnsupportedFormatError):
            extract_attachment_text("mail.msg", b"not an ole file at all")

    def test_msg_missing_body_stream_raises(self) -> None:
        data = _make_ole_regular({"Other": b"\x00" * 4096})
        with pytest.raises(UnsupportedFormatError):
            extract_attachment_text("no-body.msg", data)


    def test_injected_extractor_does_not_break_defaults_for_other_formats(
        self,
    ) -> None:
        """注入自定义 docx 解析器后，txt 等未覆盖格式仍走内置默认。"""
        class CustomDocx:
            extensions = {"docx"}

            def extract(self, data: bytes) -> str:
                return "CUSTOM"

        # docx 被覆盖，txt/md 不受影响
        assert (
            extract_attachment_text("a.docx", b"x", extra=[CustomDocx()]) == "CUSTOM"
        )
        assert (
            extract_attachment_text("a.txt", b"plain text", extra=[CustomDocx()])
            == "plain text"
        )
        assert (
            extract_attachment_text("a.md", b"# md", extra=[CustomDocx()]) == "# md"
        )


class TestRegistry:
    def test_get_extractor_by_extension(self) -> None:
        assert get_extractor("a.docx") is not None
        assert get_extractor("a.PPTX") is not None  # case-insensitive
        assert get_extractor("a.md") is not None
        assert get_extractor("a.msg") is not None
        assert get_extractor("a.unknown") is None

    def test_empty_extra_falls_back_to_defaults(self) -> None:
        """应用侧不注入（None/空列表）时，内置默认解析器照常可用。"""
        assert get_extractor("a.docx", extra=[]) is not None
        assert get_extractor("a.txt", extra=()) is not None
        assert (
            extract_attachment_text("a.docx", make_docx(["default path"]), extra=[])
            == "default path"
        )

    def test_injected_extractor_adds_new_format(self) -> None:
        class XlsxExtractor:
            extensions = {"xlsx"}

            def extract(self, data: bytes) -> str:
                return data.decode("utf-8").replace(",", " | ")

        xlsx = XlsxExtractor()
        assert get_extractor("a.xlsx") is None  # 不在默认集
        assert get_extractor("a.xlsx", extra=[xlsx]) is xlsx
        assert (
            extract_attachment_text("a.xlsx", b"a,b", extra=[xlsx]) == "a | b"
        )

    def test_injected_extractor_overrides_default(self) -> None:
        class WeirdDocx:
            extensions = {"docx"}

            def extract(self, data: bytes) -> str:
                return "custom docx reader"

        custom = WeirdDocx()
        # 同扩展名：应用注入的优先于 gateway 内置默认
        assert get_extractor("a.docx", extra=[custom]) is custom
        assert (
            extract_attachment_text("a.docx", b"anything", extra=[custom])
            == "custom docx reader"
        )
        # 不注入时仍走默认实现
        assert get_extractor("a.docx") is not custom

    def test_injected_extractor_ordering_preserved(self) -> None:
        class ExtA:
            extensions = {"xyz"}

            def extract(self, data: bytes) -> str:
                return "A"

        class ExtB:
            extensions = {"xyz"}

            def extract(self, data: bytes) -> str:
                return "B"

        # 列表顺序即优先级（第一个命中生效）
        assert get_extractor("a.xyz", extra=[ExtA(), ExtB()]).extract(b"") == "A"
