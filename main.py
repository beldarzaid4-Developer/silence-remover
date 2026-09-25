"""
main.py
Entry point. Run with `python3 -m app.main` during development, or this is
what PyInstaller points at for the packaged .app.

Supports `--selftest` for a headless, no-GUI end-to-end verification run
(used by CI to prove the packaged .app actually works on real hardware).
"""
import sys


def _entry():
    if "--selftest" in sys.argv:
        from app.selftest import main as selftest_main
        sys.exit(selftest_main())
    else:
        from app.gui import main as gui_main
        gui_main()


if __name__ == "__main__":
    _entry()
