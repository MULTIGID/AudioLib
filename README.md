# AudioLib

<a href="README.md"><kbd>English</kbd></a>
<a href="README.uk.md"><kbd>Українська</kbd></a>

AudioLib is a local audio library for Windows with categories, global hotkeys, system-tray controls, and optional audio routing to a virtual microphone through VB-CABLE.

Current version: **2.8.2**.

## Features

- categories with custom images and track counters;
- complete English and Ukrainian interface localization;
- a dedicated Favorites section;
- category and audio-recording search;
- import of common audio formats;
- playback, pause, seeking, and volume controls;
- non-destructive loudness normalization during playback without modifying source files;
- custom global hotkeys for recordings in Favorites;
- a configurable global Stop hotkey;
- playback controls in a compact system-tray window;
- library backup and restore;
- saved window size and interface preferences;
- optional routing of microphone input and audio recordings to VB-CABLE;
- selection of the physical microphone, output device, and latency/stability mode;
- automatic recovery of Windows audio routing after an unexpected shutdown;
- background update checks and secure in-app installation from GitHub Releases without replacing the local library.

All images, recordings, database files, and settings are stored locally in the `data` directory and are never sent to the developer.

## Ready-to-use Windows build

Download the latest ZIP archive from [Releases](../../releases/latest), extract it to a regular folder, and run `AudioLib.exe`.

Do not run the application directly from inside the ZIP archive.

## Running from source

Windows 10/11 and Python 3.11 or newer are required.

1. Download or clone the repository.
2. Run `install.bat` once.
3. Use `launch.bat` to start the application afterward.

The `.venv` environment and the `data` directory are created locally and are not committed to Git.

## Building the EXE

After installing the dependencies, run `build_exe.bat`.

The packaged application will be created in `dist/AudioLib`. The `build` and `dist` directories and release ZIP archives are excluded from the repository; publish the finished archive through GitHub Releases.

## Virtual microphone

Virtual-microphone support is optional. AudioLib uses the official [VB-CABLE](https://vb-audio.com/Cable/) driver by VB-Audio and does not bundle that driver in this repository.

1. Open `Settings` → `Virtual microphone`.
2. Install VB-CABLE with the provided button and approve the Windows prompt.
3. Restart the computer after installation.
4. Select the physical microphone in AudioLib settings.
5. Enable the virtual microphone when needed and use the playback controls in the corresponding table column.

AudioLib does not enable the virtual microphone automatically without the user's permission. The corresponding setting is disabled by default.

## User data

- library database: `data/library.sqlite3`;
- category images and audio recordings: `data/people/`;
- local preferences: JSON files in `data/`.

For a manual backup, copy the entire `data` directory. You can also use the built-in backup and restore features.

The `data` directory is excluded through `.gitignore`, so personal recordings and settings are never included in the repository.

## Project structure

- `main.py` — application entry point;
- `audiolib/app.py` — main interface and settings;
- `audiolib/storage.py` — library database and files;
- `audiolib/player.py` — local audio playback;
- `audiolib/loudness.py` — loudness analysis and normalization;
- `audiolib/hotkeys.py` — global hotkeys;
- `audiolib/virtual_audio.py` — VB-CABLE integration;
- `audiolib/audio_endpoints.py` — Windows audio routing;
- `audiolib/library_tools.py` — backup and restore;
- `audiolib/updates.py` — GitHub Release checks, package verification, and safe application updates;
- `build_hooks/` — PyInstaller packaging configuration;
- `assets/` — application icons.

## Author

MULTIGID.

## License

No license has been selected yet. Until a license is added, standard copyright restrictions apply.
