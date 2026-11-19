"""Documents by reference (design note section 4).

``BORG_DOCS_ROOT`` (``.env``) names the one folder documents may be read
from. A request names documents relative to it, as
``{"folder": "<relative path>"}`` or ``{"files": ["<relative path>", ...]}``.

Rules, all checked before any read:

- The requested string must be relative. Absolute paths, drive-relative
  (``C:x.txt``), root-relative (``\\x.txt``), UNC (``\\\\server\\share``) and
  device prefixes (``\\\\?\\``, ``\\\\.\\``) are refused before joining.
- Every file, including each file found by listing a folder, is resolved
  with ``resolve(strict=True)`` (following symlinks and junctions) and must
  lie inside the resolved root (case-insensitive on Windows).
- Open, then verify: ``os.fstat`` of the open handle must match the
  ``os.stat`` taken at the check (device and inode), so a file swapped
  between check and open is refused. Limit: a hard link inside the root to a
  file outside it cannot be told apart.
- ``.txt`` and ``.md`` only, read as UTF-8 with replacement. Other files are
  listed in the manifest as unsupported and never read.
- The root itself is refused when unset, empty, ``.``, missing, or when it
  is, or contains, the application directory (which holds ``.env`` and the
  database).

Every refusal is ``BRT-LANE-001`` and names the requested relative path only,
never the resolved absolute path.

A document longer than ``lane_part_chars`` is split into parts (``doc_003#2``),
cut at a line break where one falls in the second half of the part; nothing
is cut off silently.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import LaneError

DOCS_ROOT_ENV = "BORG_DOCS_ROOT"
SUPPORTED_TYPES = ("txt", "md")
_APP_DIR = Path(__file__).resolve().parents[2]
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _refuse(message: str) -> LaneError:
    return LaneError("BRT-LANE-001", message)


@dataclass(frozen=True)
class DocPart:
    part_id: str        # "doc_001" for a whole document, "doc_001#2" for a part
    doc_id: str
    text: str


@dataclass
class Document:
    doc_id: str
    name: str           # the path relative to the root, with "/" separators
    type: str
    size: int           # bytes on disk
    text: str
    parts: list[DocPart] = field(default_factory=list)


@dataclass
class LoadedDocuments:
    documents: list[Document]
    unsupported: list[dict]     # {"name", "type", "size"}; never read

    def __post_init__(self) -> None:
        self.part_index: dict[str, DocPart] = {
            p.part_id: p for d in self.documents for p in d.parts
        }
        self.doc_index: dict[str, Document] = {d.doc_id: d for d in self.documents}

    def manifest(self, mode: str = "names") -> list[dict]:
        """What the planner may see: ids, types, sizes and (unless ``ids``) names."""
        rows: list[dict] = []
        for d in self.documents:
            row = {"id": d.doc_id, "type": d.type, "size": d.size, "parts": len(d.parts),
                   "chars": len(d.text)}
            if mode != "ids":
                row["name"] = d.name
            rows.append(row)
        for u in self.unsupported:
            row = {"type": u["type"], "size": u["size"], "status": "unsupported, not read"}
            if mode != "ids":
                row["name"] = u["name"]
            rows.append(row)
        return rows


# -- the root ---------------------------------------------------------------------

def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _within(path: Path, root: Path) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def docs_root() -> Path:
    """The resolved documents root.

    Raises:
        LaneError: BRT-LANE-001 when the root is unset or unsafe.
    """
    raw = os.getenv(DOCS_ROOT_ENV)
    if raw is None or not raw.strip():
        raise _refuse(f"{DOCS_ROOT_ENV} is not set, so no document can be read")
    raw = raw.strip()
    if raw in (".", "./", ".\\"):
        raise _refuse(f"{DOCS_ROOT_ENV} may not be '.'; name a folder that holds only documents")
    try:
        root = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise _refuse(f"{DOCS_ROOT_ENV} does not name an existing folder") from None
    if not root.is_dir():
        raise _refuse(f"{DOCS_ROOT_ENV} does not name a folder")
    if _within(_APP_DIR, root):
        raise _refuse(
            f"{DOCS_ROOT_ENV} is, or contains, the application folder (which holds .env "
            "and the database); name a folder of documents only"
        )
    return root


# -- one path -------------------------------------------------------------------------

def _check_request_string(rel: object) -> str:
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise _refuse("a document path must be a non-empty relative path")
    s = rel.strip()
    if s.startswith(("\\", "/")):
        raise _refuse(f"'{s}' is not a relative path")
    if _DRIVE_RE.match(s) or PureWindowsPath(s).drive or PureWindowsPath(s).is_absolute():
        raise _refuse(f"'{s}' is not a relative path")
    if PurePosixPath(s).is_absolute():
        raise _refuse(f"'{s}' is not a relative path")
    return s


def _resolve_inside(root: Path, rel: str) -> Path:
    try:
        resolved = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        raise _refuse(f"'{rel}' was not found under {DOCS_ROOT_ENV}") from None
    if not _within(resolved, root):
        raise _refuse(f"'{rel}' resolves outside {DOCS_ROOT_ENV}")
    return resolved


def check_path(root: Path, rel: str) -> tuple[Path, os.stat_result]:
    """Check one requested file path. Returns (resolved path, its stat).

    Raises:
        LaneError: BRT-LANE-001.
    """
    rel = _check_request_string(rel)
    resolved = _resolve_inside(root, rel)
    if not resolved.is_file():
        raise _refuse(f"'{rel}' is not a file")
    try:
        return resolved, os.stat(resolved)
    except OSError:
        raise _refuse(f"'{rel}' could not be read") from None


def read_verified(resolved: Path, expected: os.stat_result, rel: str) -> bytes:
    """Open the checked file and confirm it is still the file that was checked.

    Raises:
        LaneError: BRT-LANE-001 when it changed between check and open.
    """
    try:
        with open(resolved, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
                raise _refuse(f"'{rel}' changed between the check and the read")
            return fh.read()
    except LaneError:
        raise
    except OSError:
        raise _refuse(f"'{rel}' could not be read") from None


def _rel_name(path: Path, root: Path) -> str:
    return os.path.relpath(str(path), str(root)).replace("\\", "/")


def _list_folder(root: Path, rel: str) -> list[str]:
    """Every file under a requested folder, as relative names. Each entry,
    file or folder, is resolved and must stay inside the root."""
    rel = _check_request_string(rel)
    start = _resolve_inside(root, rel)
    if not start.is_dir():
        raise _refuse(f"'{rel}' is not a folder")
    names: list[str] = []
    seen: set[str] = set()
    pending = [(start, Path(root / rel))]
    while pending:
        real_dir, shown_dir = pending.pop()
        if _norm(real_dir) in seen:
            continue
        seen.add(_norm(real_dir))
        try:
            entries = sorted(os.scandir(real_dir), key=lambda e: e.name)
        except OSError:
            raise _refuse(f"'{_rel_name(shown_dir, root)}' could not be listed") from None
        for entry in entries:
            shown = shown_dir / entry.name
            shown_rel = _rel_name(shown, root)
            try:
                real = Path(entry.path).resolve(strict=True)
            except (OSError, RuntimeError):
                raise _refuse(f"'{shown_rel}' could not be resolved") from None
            if not _within(real, root):
                raise _refuse(f"'{shown_rel}' resolves outside {DOCS_ROOT_ENV}")
            if real.is_dir():
                pending.append((real, shown))
            elif real.is_file():
                names.append(shown_rel)
    return sorted(names)


# -- splitting ----------------------------------------------------------------------------

def split_text(text: str, part_chars: int) -> list[str]:
    """Cut ``text`` into pieces of at most ``part_chars``; prefer a line break
    in the second half of each piece. Every character lands in one piece."""
    if part_chars < 1 or len(text) <= part_chars:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > part_chars:
        cut = rest.rfind("\n", part_chars // 2, part_chars)
        cut = cut + 1 if cut != -1 else part_chars
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


# -- the request ------------------------------------------------------------------------

def load_documents(request: object, part_chars: int = 8000) -> LoadedDocuments:
    """Check and read the documents a request names.

    Raises:
        LaneError: BRT-LANE-001 for any refused path, unsafe root or unreadable file.
    """
    if not isinstance(request, dict):
        raise _refuse('documents must be {"folder": "<path>"} or {"files": ["<path>", ...]}')
    folder, files = request.get("folder"), request.get("files")
    if (folder is None) == (files is None):
        raise _refuse('give exactly one of "folder" or "files"')
    root = docs_root()
    if folder is not None:
        names = _list_folder(root, folder)
    else:
        if not isinstance(files, list) or not files:
            raise _refuse('"files" must be a non-empty list of relative paths')
        names = [_check_request_string(f) for f in files]

    documents: list[Document] = []
    unsupported: list[dict] = []
    seen: set[str] = set()
    for rel in names:
        resolved, stat = check_path(root, rel)
        if _norm(resolved) in seen:
            continue
        seen.add(_norm(resolved))
        suffix = resolved.suffix.lower().lstrip(".")
        if suffix not in SUPPORTED_TYPES:
            unsupported.append({"name": rel.replace("\\", "/"), "type": suffix or "none",
                                "size": stat.st_size})
            continue
        data = read_verified(resolved, stat, rel)
        text = data.decode("utf-8", errors="replace")
        doc_id = f"doc_{len(documents) + 1:03d}"
        pieces = split_text(text, part_chars)
        if len(pieces) == 1:
            parts = [DocPart(part_id=doc_id, doc_id=doc_id, text=text)]
        else:
            parts = [
                DocPart(part_id=f"{doc_id}#{i}", doc_id=doc_id, text=piece)
                for i, piece in enumerate(pieces, start=1)
            ]
        documents.append(Document(
            doc_id=doc_id, name=rel.replace("\\", "/"), type=suffix,
            size=stat.st_size, text=text, parts=parts,
        ))
    return LoadedDocuments(documents=documents, unsupported=unsupported)
