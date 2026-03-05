"""PyInstaller entry point for nanoclaw."""
from nanoclaw.__main__ import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
