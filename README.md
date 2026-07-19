# GoPro Merger

A Python tool that automatically groups, combines, and compresses GoPro video chapters using FFmpeg.

## Features

- Detects newer GoPro chapter names such as `GX010021.MP4`, `GX020021.MP4`, etc.
- Supports older `GOPR1234.MP4` / `GP011234.MP4` naming.
- Merges each recording into one continuous MP4.
- Compresses to HEVC/H.265.
- Uses NVIDIA NVENC when available, with automatic CPU fallback via `libx265`.
- Copies audio and GoPro telemetry/data streams where supported.
- Removes `.LRV` and `.THM` sidecar files after successful processing.
- Keeps the original MP4 files.
- Opens a folder picker when run without a folder argument.

## Requirements

- Python 3.9 or newer.
- FFmpeg installed and available in `PATH`.

Check FFmpeg from Command Prompt or PowerShell:

```powershell
ffmpeg -version
```

## Usage

Open a folder picker:

```powershell
python gopro_merger.py
```

Process a specific folder:

```powershell
python gopro_merger.py "D:\GoPro\D2S1"
```

Outputs are written to a `processed` folder inside the selected source folder.

## Options

Choose the compression quality:

```powershell
python gopro_merger.py "D:\GoPro\D2S1" --quality 26
```

Lower values give better quality and larger files. The default is `26`.

Force NVIDIA encoding:

```powershell
python gopro_merger.py "D:\GoPro\D2S1" --encoder nvenc
```

Force CPU encoding:

```powershell
python gopro_merger.py "D:\GoPro\D2S1" --encoder cpu
```

Keep `.LRV` and `.THM` sidecar files:

```powershell
python gopro_merger.py "D:\GoPro\D2S1" --keep-sidecars
```

Combine every MP4 in the folder into one output:

```powershell
python gopro_merger.py "D:\GoPro\D2S1" --combine-all
```

Overwrite an existing output:

```powershell
python gopro_merger.py "D:\GoPro\D2S1" --overwrite
```

## Safety

The script does not delete original MP4 files. Check each merged output before manually removing any source footage.
