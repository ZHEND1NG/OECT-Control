from __future__ import annotations

import argparse

from .gui import main as gui_main


def main() -> int:
    parser = argparse.ArgumentParser(description="OECT experiment module")
    parser.add_argument("command", nargs="?", default="gui", choices=["gui"], help="Command to run")
    args = parser.parse_args()
    if args.command == "gui":
        return gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

