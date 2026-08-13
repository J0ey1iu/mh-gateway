"""Attachment support for the gateway (shared engine).

The gateway owns the attachment *contract*: upload/download APIs, the
attachment tools the model calls to pull file content into context, and
stdlib text extraction for docx/pptx/md/txt/msg.  Actual file storage is a
deployment concern — each app supplies its own
:class:`~mh_gateway.adapters.AttachmentStore` implementation.
"""

from mh_gateway.attachments.extractors import (
    FileExtractor,
    UnsupportedFormatError,
    extract_attachment_text,
    get_extractor,
)

__all__ = [
    "FileExtractor",
    "UnsupportedFormatError",
    "extract_attachment_text",
    "get_extractor",
]
