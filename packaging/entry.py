"""PyInstaller entry point for the Windows release (excel-codex.exe)."""

from excel_codex_bridge.cli import main

raise SystemExit(main())
