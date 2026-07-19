# gopro-merger

A Python tool that automatically groups, combines, and compresses GoPro video chapters using FFmpeg.

## Features

- Detects newer GoPro chapter names such as `GX010021.MP4`, `GX020021.MP4`, etc.
- Supports older `GOPR1234.MP4` / `GP011234.MP4` naming.
- Merges each recording into one continuous MP4.
- Compresses video to HEVC/H.265.
- Uses NVIDIA NVENC when available, with automatic CPU fallback via `libx265`.
- Copies audio and GoPro telemetry/data streams where supported.
- Removes `.LRV` and `.THM` sidecar files after successful processing.
- Keeps the original MP4 files by default.
- Can optionally delete original MP4 files after successful processing and explicit confirmation.
- Opens a graphical folder picker when run without a folder argument and Tkinter is available.
- Works on Windows, macOS, and Linux.

## Dependencies

### Required

- Python 3.9 or newer.
- FFmpeg installed and available in your system `PATH`.

The script only uses modules from the Python standard library, so no `pip` packages are required.

### Optional

- Tkinter, for the graphical folder picker.
  - Tkinter is normally included with Python on Windows and macOS.
  - Some Linux distributions package it separately.
  - If Tkinter is unavailable, provide the source folder as a command-line argument instead.
- An NVIDIA GPU, compatible driver, and an FFmpeg build containing `hevc_nvenc` for hardware-accelerated encoding.
  - If NVENC is unavailable or fails in automatic mode, the script falls back to CPU encoding with `libx265`.

## Installation

### Windows

#### 1. Install Python

Install Python 3.9 or newer from the Microsoft Store or the official Python installer.

Confirm that Python is available:

```powershell
python --version
```

On some systems, use:

```powershell
py --version
```

#### 2. Install FFmpeg

Using `winget`:

```powershell
winget install Gyan.FFmpeg
```

Alternatively, using Chocolatey:

```powershell
choco install ffmpeg
```

Confirm that FFmpeg is available:

```powershell
ffmpeg -version
```

### macOS

Install Python and FFmpeg using Homebrew:

```bash
brew install python ffmpeg
```

Confirm both are available:

```bash
python3 --version
ffmpeg -version
```

### Linux

Package names vary by distribution.

#### Ubuntu or Debian

```bash
sudo apt update
sudo apt install python3 ffmpeg
```

Install Tkinter as well if you want the graphical folder picker:

```bash
sudo apt install python3-tk
```

#### Fedora

```bash
sudo dnf install python3 ffmpeg
```

Install Tkinter as well if required:

```bash
sudo dnf install python3-tkinter
```

#### Arch Linux

```bash
sudo pacman -S python ffmpeg tk
```

Confirm Python and FFmpeg are available:

```bash
python3 --version
ffmpeg -version
```

## Usage

### Windows

Open the graphical folder picker:

```powershell
python gopro_merger.py
```

Process a specific folder:

```powershell
python gopro_merger.py "D:\GoPro\D2S1"
```

If `python` is not recognised, use the Python launcher:

```powershell
py gopro_merger.py "D:\GoPro\D2S1"
```

### macOS and Linux

Open the graphical folder picker, if Tkinter is available:

```bash
python3 gopro_merger.py
```

Process a specific folder:

```bash
python3 gopro_merger.py "/path/to/GoPro/D2S1"
```

For example:

```bash
python3 gopro_merger.py "$HOME/Videos/GoPro/D2S1"
```

Outputs are written to a `processed` directory inside the selected source directory.

## Options

The examples below use `python`. On macOS and Linux, use `python3` instead where required.

### Choose the compression quality

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --quality 26
```

Lower values give better quality and larger files. The default is `26`.

### Force NVIDIA encoding

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --encoder nvenc
```

This requires:

- an NVIDIA GPU that supports HEVC encoding;
- a compatible NVIDIA driver;
- an FFmpeg build containing the `hevc_nvenc` encoder.

Check whether your FFmpeg build provides it on Windows:

```powershell
ffmpeg -hide_banner -encoders | findstr hevc_nvenc
```

On macOS and Linux:

```bash
ffmpeg -hide_banner -encoders | grep hevc_nvenc
```

### Force CPU encoding

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --encoder cpu
```

### Keep `.LRV` and `.THM` sidecar files

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --keep-sidecars
```

### Combine every MP4 in the folder into one output

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --combine-all
```

Use this only when the files belong together and use compatible stream formats.

### Overwrite an existing output

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --overwrite
```

### Delete original MP4 files after successful processing

```bash
python gopro_merger.py "/path/to/GoPro/D2S1" --delete-originals
```

The script lists every original file eligible for deletion, shows their combined size, and requires you to type `DELETE` before anything is removed.

## Safety

Original MP4 files are kept unless `--delete-originals` is supplied.

When `--delete-originals` is used:

- deletion only happens after FFmpeg completes successfully and creates a non-empty output;
- only source files encoded successfully during the current run are eligible;
- files belonging to failed recordings are kept;
- files belonging to outputs skipped because they already existed are kept;
- you must type `DELETE` exactly when prompted;
- deleted files are permanently removed rather than moved to the Recycle Bin or Trash.

Check each merged output before confirming deletion of important source footage.

## Notes

- HEVC/H.265 offers smaller files than many H.264 workflows, but encoding is lossy.
- CPU encoding with `libx265` is normally slower than NVIDIA NVENC.
- Playback support for HEVC varies by operating system, application, and installed codecs.
- GoPro telemetry preservation depends on the streams present in the source files and support in the installed FFmpeg build.