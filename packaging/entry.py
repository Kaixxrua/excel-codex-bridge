"""PyInstaller entry point for the release builds (excel-codex.exe, excel-codex)."""

from excel_codex_bridge.cli import main

raise SystemExit(main())
