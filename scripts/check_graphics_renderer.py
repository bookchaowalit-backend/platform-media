"""Optional Windows renderer acceptance: missing prerequisites never count as a pass."""

import os
import sys
import unittest
from pathlib import Path


def main() -> int:
    for name in (
        "BOOK_MEDIA_SKIP_RENDERER_TESTS",
        "BOOK_MEDIA_GRAPHICS_UPDATE_GOLDENS",
    ):
        if os.environ.get(name) == "1":
            print(f"Renderer acceptance requires {name} to be unset or 0", file=sys.stderr)
            return 1
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(
        str(root / "tests"), pattern="test_graphics_renderer.py"
    )
    if suite.countTestCases() == 0:
        print("Renderer acceptance found no integration cases", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(
            f"Renderer acceptance UNAVAILABLE: {len(result.skipped)} skipped cases; "
            "this is not a passed renderer gate.",
            file=sys.stderr,
        )
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
