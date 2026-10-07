"""How an original document is returned to an officer's browser.

The bytes are untrusted input from an applicant. They are never rendered as HTML: the type comes from sniffing the bytes (not
from the client), only a short list of passive types is shown inline, everything else is a download, and `nosniff` stops the
browser from guessing. The response is never cached, never framed, and carries a restrictive CSP.
"""

from __future__ import annotations

from fastapi import Response

from onboarding.service import ViewedDocument

DOCUMENT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
}
# A PDF viewer cannot run inside a sandboxed response, so PDFs keep the other restrictions but not `sandbox`.
PDF_CSP = "default-src 'none'; frame-ancestors 'none'"


def document_response(doc: ViewedDocument) -> Response:
    # A neutral file name: the applicant's own file name was never kept (it can contain personal data).
    name = doc.doc_type
    headers = dict(DOCUMENT_HEADERS)
    if doc.inline:
        media_type = doc.content_type
        headers["Content-Disposition"] = f'inline; filename="{name}"'
        if media_type == "application/pdf":
            headers["Content-Security-Policy"] = PDF_CSP
    else:
        media_type = "application/octet-stream"
        headers["Content-Disposition"] = f'attachment; filename="{name}.bin"'
    return Response(content=doc.content, media_type=media_type, headers=headers)
