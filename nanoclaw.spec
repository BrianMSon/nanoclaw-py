# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run_nanoclaw.py'],
    pathex=['src', '.venv/Lib/site-packages'],
    binaries=[],
    datas=[],
    hiddenimports=['nanoclaw', 'nanoclaw.bot', 'nanoclaw.agent', 'nanoclaw.config', 'nanoclaw.db', 'nanoclaw.memory', 'nanoclaw.conversations', 'nanoclaw.scheduler', 'claude_agent_sdk', 'mcp', 'mcp.client', 'mcp.client.stdio', 'mcp.types', 'mcp.os.win32', 'mcp.os.win32.utilities', 'pywintypes', 'win32api', 'win32event', 'aiosqlite', 'apscheduler', 'apscheduler.schedulers.asyncio', 'apscheduler.triggers.interval', 'croniter', 'dotenv', 'telegram', 'telegram.ext', 'httpx', 'httpcore', 'h11', 'anyio', 'anyio._backends._asyncio', 'sniffio'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='nanoclaw',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
