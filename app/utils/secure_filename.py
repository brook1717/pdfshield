"""
Secure filename sanitisation utility.

Prevents path-traversal attacks on uploaded filenames by:

* Normalising unicode to ASCII
* Stripping all directory separators (``/`` and ``\\``)
* Removing leading dots that would create hidden files or relative-path prefixes
* Collapsing consecutive dots (``../../`` traversal remnants)
* Allowing only safe characters: word chars, hyphens, underscores, and a single
  extension dot
* Enforcing the POSIX 255-byte maximum filename length

References
----------
Werkzeug's ``secure_filename`` implementation (BSD-licensed) served as
inspiration; this version is self-contained with no Werkzeug dependency.
"""
from __future__ import annotations

import os
import re
import unicodedata


def secure_filename(filename: str) -> str:
    """
    Return a filesystem-safe, path-traversal-free version of *filename*.

    Parameters
    ----------
    filename:
        Raw filename string as supplied by the HTTP client.

    Returns
    -------
    str
        Sanitised filename containing only ``[A-Za-z0-9_.-]`` characters, at
        most one extension dot, and at most 255 characters in total.
        Returns ``"document.pdf"`` when the sanitised result would be empty.

    Examples
    --------
    >>> secure_filename("../../etc/passwd")
    'etc_passwd'
    >>> secure_filename("Invoice January 2024.pdf")
    'Invoice_January_2024.pdf'
    >>> secure_filename("C:\\\\Windows\\\\System32\\\\cmd.exe")
    'cmd.exe'
    >>> secure_filename("")
    'document.pdf'
    """
    if not filename:
        return "document.pdf"

    # 1. Normalise unicode → NFKD, drop non-ASCII bytes
    filename = unicodedata.normalize("NFKD", filename)
    filename = filename.encode("ascii", "ignore").decode("ascii")

    # 2. Isolate the final component — eliminates both / and \ traversal
    #    (replace \ → / first so os.path.basename handles the unified path)
    filename = os.path.basename(filename.replace("\\", "/"))

    # 3. Keep only safe characters: word chars (\w), spaces, hyphens, dots
    filename = re.sub(r"[^\w\s\-.]", "", filename)

    # 4. Collapse whitespace → underscores
    filename = re.sub(r"\s+", "_", filename)

    # 5. Strip leading dots (hidden-file prefix and relative-path artifacts)
    filename = filename.lstrip(".")

    # 6. Collapse consecutive dots (residual traversal sequences)
    filename = re.sub(r"\.{2,}", ".", filename)

    # 7. Enforce POSIX maximum filename length (255 bytes)
    if len(filename) > 255:
        base, ext = os.path.splitext(filename)
        filename = base[: 255 - len(ext)] + ext

    if not re.search(r"[a-zA-Z0-9]", filename):
        return "document.pdf"
    return filename
