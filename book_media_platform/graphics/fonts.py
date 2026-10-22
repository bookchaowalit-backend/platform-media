from __future__ import annotations

import os
import platform
from pathlib import Path

from .errors import GraphicsError

FONT_FILES: dict[str, tuple[str, ...]] = {
    "Arial": ("arial.ttf", "Arial.ttf", "ArialMT.ttf"),
    "Tahoma": ("tahoma.ttf", "Tahoma.ttf"),
    "Leelawadee UI": ("LeelawUI.ttf", "LeelawUI-Bold.ttf", "LeelawadeeUI.ttf"),
    "DejaVu Sans": ("DejaVuSans.ttf",),
}


FONT_DIRS_ENV = "BOOK_MEDIA_FONT_DIRS"


def _configured_font_roots() -> list[Path]:
    """Return absolute directories from ``BOOK_MEDIA_FONT_DIRS``.

    The variable uses the platform path separator. Relative entries are ignored
    so that font discovery never depends on the current working directory.
    """

    raw = os.environ.get(FONT_DIRS_ENV, "")
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry).expanduser()
        if path.is_absolute():
            roots.append(path)
    return roots


def _font_roots() -> tuple[Path, ...]:
    roots: list[Path] = _configured_font_roots()
    if os.name == "nt":
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        roots.extend((windows / "Fonts", Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"))
    elif platform.system() == "Darwin":
        roots.extend((Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"))
    else:
        roots.extend(
            (
                Path("/usr/share/fonts/truetype/msttcorefonts"),
                Path("/usr/share/fonts/truetype/dejavu"),
                Path("/usr/share/fonts/truetype/tlwg"),
                Path("/usr/share/fonts/dejavu-sans-fonts"),
                Path("/usr/share/fonts/TTF"),
                Path("/usr/local/share/fonts"),
                Path.home() / ".local/share/fonts",
            )
        )
    return tuple(root for root in roots if root.is_dir())


def find_font_file(family: str) -> Path | None:
    allowed = FONT_FILES.get(family)
    if allowed is None:
        return None
    candidates = {item.casefold() for item in allowed}
    for root in _font_roots():
        try:
            for item in root.iterdir():
                if item.is_file() and item.name.casefold() in candidates:
                    return item
        except OSError:
            continue
    return None


def require_font(family: str) -> Path:
    path = find_font_file(family)
    if path is None:
        if family not in FONT_FILES:
            raise GraphicsError(f"unsupported font family: {family}")
        raise GraphicsError(f"required local font is not installed: {family}")
    return path
