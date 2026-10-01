<img src="https://www.upload.ee/image/19807445/2026-10-02_031006.png" border="0" alt="2026-10-02_031006.png" />

# ECOMTECH Startup Manager

Windows startup management utility by ECOMTECH.

## Current version

Version 1.1.1 (from `VERSION.txt`).

## Project contents

- `main.py` — main application
- `run.bat` — quick launcher
- `install_and_run.bat` — dependency installation and launcher
- `build_exe.bat` — Windows executable build script
- `make_icon.py` — icon generation helper
- `requirements.txt` — runtime dependencies
- `requirements-build.txt` — build dependencies
- `assets/` — application assets
- `legacy_scripts/` — legacy scripts retained for reference

## Run from source

```bat
pip install -r requirements.txt
python main.py
```

Or use:

```bat
run.bat
```

## Build Windows EXE

```bat
pip install -r requirements-build.txt
build_exe.bat
```

## Notes

This project is intended for Windows. Review the startup entries and permissions requested by the application before applying changes.

## License

See `LICENSE`.
