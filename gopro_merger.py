#!/usr/bin/env python3
"""
Merge GoPro chapter files and compress them to HEVC/H.265 with FFmpeg.

Examples:
    python gopro_merger.py "D:\\GoPro\\D2S1"
    python gopro_merger.py "D:\\GoPro\\D2S1" --quality 24
    python gopro_merger.py "D:\\GoPro\\D2S1" --encoder cpu
    python gopro_merger.py "D:\\GoPro\\D2S1" --combine-all

Without a folder argument, a Windows folder picker is opened.

Default behaviour:
- Groups GoPro chapter files belonging to the same recording, for example:
    GX010021.MP4, GX020021.MP4, ... -> one output
- If no GoPro naming pattern is found, combines all MP4 files in filename order.
- Encodes directly to HEVC/H.265 without making a huge intermediate file.
- Uses NVIDIA NVENC when available; otherwise uses libx265 on the CPU.
- Deletes .LRV and .THM sidecar files after successful processing.
- Keeps every original MP4.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Newer GoPro naming, e.g. GX010021.MP4 or GH020123.MP4
NEW_GOPRO_RE = re.compile(
    r"^(?P<prefix>G[A-Z])(?P<chapter>\d{2})(?P<clip>\d{4})\.MP4$",
    re.IGNORECASE,
)

# Older GoPro naming, e.g. GOPR1234.MP4 followed by GP011234.MP4
OLD_FIRST_RE = re.compile(r"^GOPR(?P<clip>\d{4})\.MP4$", re.IGNORECASE)
OLD_CHAPTER_RE = re.compile(
    r"^GP(?P<chapter>\d{2})(?P<clip>\d{4})\.MP4$",
    re.IGNORECASE,
)

OUTPUT_DIR_NAME = "processed"
OUTPUT_MARKERS = ("_merged_hevc", "_compressed_hevc")
SIDECAR_EXTENSIONS = {".lrv", ".thm"}


def natural_key(path: Path) -> List[object]:
    """Sort filenames naturally: file2 before file10."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", path.name)]


def choose_folder() -> Optional[Path]:
    """Open a simple folder picker when no path was supplied."""
    try:
        from tkinter import Tk, filedialog
    except ImportError:
        return None

    root = Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    selected = filedialog.askdirectory(title="Select a GoPro video folder")
    root.destroy()
    return Path(selected) if selected else None


def executable_or_exit(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(
            f"{name} was not found in PATH. Open Command Prompt and confirm that "
            f"'{name} -version' works."
        )
    return executable


def ffmpeg_has_encoder(ffmpeg: str, encoder: str) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return encoder.lower() in result.stdout.lower()


def parse_gopro_name(path: Path) -> Optional[Tuple[str, int]]:
    """Return (recording key, chapter number), or None for non-GoPro names."""
    match = NEW_GOPRO_RE.match(path.name)
    if match:
        prefix = match.group("prefix").upper()
        clip = match.group("clip")
        chapter = int(match.group("chapter"))
        return f"{prefix}{clip}", chapter

    match = OLD_FIRST_RE.match(path.name)
    if match:
        return f"GOPR{match.group('clip')}", 0

    match = OLD_CHAPTER_RE.match(path.name)
    if match:
        return f"GOPR{match.group('clip')}", int(match.group("chapter"))

    return None


def discover_mp4_files(folder: Path) -> List[Path]:
    files = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        lower_name = path.stem.lower()
        if any(marker in lower_name for marker in OUTPUT_MARKERS):
            continue
        files.append(path)
    return sorted(files, key=natural_key)


def build_groups(files: Sequence[Path], combine_all: bool, folder_name: str) -> Dict[str, List[Path]]:
    if combine_all:
        return {folder_name: sorted(files, key=natural_key)}

    grouped: Dict[str, List[Tuple[int, Path]]] = defaultdict(list)
    unmatched: List[Path] = []

    for path in files:
        parsed = parse_gopro_name(path)
        if parsed is None:
            unmatched.append(path)
            continue
        key, chapter = parsed
        grouped[key].append((chapter, path))

    # If there are no recognisable GoPro names, treat the folder as one sequence.
    if not grouped:
        return {folder_name: sorted(unmatched, key=natural_key)}

    result: Dict[str, List[Path]] = {}
    for key, items in grouped.items():
        result[key] = [path for _, path in sorted(items, key=lambda item: item[0])]

    if unmatched:
        print("\nSkipped non-GoPro MP4 files:")
        for path in unmatched:
            print(f"  - {path.name}")
        print("Use --combine-all if those files should be included as well.")

    return result


def concat_line(path: Path) -> str:
    """Quote an absolute path for an FFmpeg concat-demuxer list."""
    value = path.resolve().as_posix()
    value = value.replace("'", "'\\''")
    return f"file '{value}'\n"


def make_concat_file(folder: Path, files: Sequence[Path]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".ffconcat.txt",
        prefix="gopro_merger_",
        dir=folder,
        delete=False,
        newline="\n",
    )
    try:
        for path in files:
            handle.write(concat_line(path))
    finally:
        handle.close()
    return Path(handle.name)


def encoder_args(encoder: str, quality: int) -> List[str]:
    if encoder == "nvenc":
        return [
            "-c:v", "hevc_nvenc",
            "-preset", "p5",
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(quality),
            "-b:v", "0",
            "-multipass", "qres",
            "-spatial_aq", "1",
            "-temporal_aq", "1",
        ]

    return [
        "-c:v", "libx265",
        "-preset", "medium",
        "-crf", str(quality),
    ]


def run_ffmpeg(
    ffmpeg: str,
    concat_file: Path,
    output: Path,
    encoder: str,
    quality: int,
    overwrite: bool,
) -> int:
    command = [
        ffmpeg,
        "-hide_banner",
        "-y" if overwrite else "-n",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        # Keep the main video, optional audio, and optional GoPro telemetry/data stream.
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:d?",
        *encoder_args(encoder, quality),
        "-c:a", "copy",
        "-c:d", "copy",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-tag:v", "hvc1",
        "-movflags", "+faststart",
        str(output),
    ]

    print("\nRunning FFmpeg:\n  " + " ".join(f'"{part}"' if " " in part else part for part in command))
    return subprocess.run(command, check=False).returncode


def sidecar_files(folder: Path) -> List[Path]:
    return sorted(
        [path for path in folder.iterdir()
         if path.is_file() and path.suffix.lower() in SIDECAR_EXTENSIONS],
        key=natural_key,
    )


def delete_sidecars(files: Iterable[Path]) -> Tuple[int, int]:
    deleted = 0
    failed = 0
    for path in files:
        try:
            path.unlink()
            print(f"Deleted sidecar: {path.name}")
            deleted += 1
        except OSError as exc:
            print(f"Could not delete {path.name}: {exc}")
            failed += 1
    return deleted, failed


def safe_output_name(group_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", group_name).strip("._-")
    return safe or "gopro_video"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gopro_merger.py",
        description="Merge GoPro chapter files, compress to HEVC, and remove .LRV/.THM sidecars."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        type=Path,
        help="Folder containing the GoPro files. A folder picker opens if omitted.",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=26,
        choices=range(18, 36),
        metavar="18-35",
        help="HEVC quality value. Lower = better/larger; higher = smaller. Default: 26.",
    )
    parser.add_argument(
        "--encoder",
        choices=("auto", "nvenc", "cpu"),
        default="auto",
        help="auto uses NVIDIA NVENC if available, then falls back to CPU. Default: auto.",
    )
    parser.add_argument(
        "--combine-all",
        action="store_true",
        help="Combine every MP4 in the folder into one output instead of grouping GoPro chapters.",
    )
    parser.add_argument(
        "--keep-sidecars", "--keep-junk",
        dest="keep_sidecars",
        action="store_true",
        help="Keep .LRV and .THM sidecar files instead of deleting them.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing output file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    used_picker = args.folder is None
    folder = args.folder or choose_folder()

    if folder is None:
        print("No folder selected.")
        return 1

    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a valid folder: {folder}")
        return 1

    try:
        ffmpeg = executable_or_exit("ffmpeg")
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    files = discover_mp4_files(folder)
    if not files:
        print(f"No MP4 files found in: {folder}")
        return 1

    groups = build_groups(files, args.combine_all, folder.name)
    output_dir = folder / OUTPUT_DIR_NAME
    output_dir.mkdir(exist_ok=True)

    nvenc_available = ffmpeg_has_encoder(ffmpeg, "hevc_nvenc")
    if args.encoder == "nvenc" and not nvenc_available:
        print("ERROR: This FFmpeg build does not provide the hevc_nvenc encoder.")
        return 1

    requested_encoder = args.encoder
    if requested_encoder == "auto":
        selected_encoder = "nvenc" if nvenc_available else "cpu"
    else:
        selected_encoder = requested_encoder

    print(f"\nFolder: {folder}")
    print(f"MP4 files found: {len(files)}")
    print(f"Recording groups: {len(groups)}")
    print(f"Encoder: {'NVIDIA NVENC' if selected_encoder == 'nvenc' else 'CPU libx265'}")
    print(f"Quality: {args.quality}")
    print(f"Outputs: {output_dir}")

    successful_outputs: List[Path] = []
    failed_groups: List[str] = []

    for group_name, group_files in groups.items():
        if not group_files:
            continue

        output = output_dir / f"{safe_output_name(group_name)}_merged_hevc.mp4"
        if output.exists() and not args.overwrite:
            print(f"\nSkipping {group_name}: output already exists: {output.name}")
            successful_outputs.append(output)
            continue

        print(f"\n=== {group_name} ===")
        for path in group_files:
            print(f"  {path.name}")

        concat_file = make_concat_file(folder, group_files)
        try:
            return_code = run_ffmpeg(
                ffmpeg=ffmpeg,
                concat_file=concat_file,
                output=output,
                encoder=selected_encoder,
                quality=args.quality,
                overwrite=args.overwrite,
            )

            # An installed FFmpeg can advertise NVENC even when the local NVIDIA
            # driver/GPU cannot use it. In auto mode, retry once with libx265.
            if return_code != 0 and requested_encoder == "auto" and selected_encoder == "nvenc":
                print("\nNVENC failed. Retrying this recording with CPU libx265...")
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass
                return_code = run_ffmpeg(
                    ffmpeg=ffmpeg,
                    concat_file=concat_file,
                    output=output,
                    encoder="cpu",
                    quality=args.quality,
                    overwrite=True,
                )

            if return_code == 0 and output.exists() and output.stat().st_size > 0:
                successful_outputs.append(output)
                print(f"\nCreated: {output}")
            else:
                failed_groups.append(group_name)
                print(f"\nFAILED: {group_name}")
        finally:
            try:
                concat_file.unlink()
            except OSError:
                pass

    # Delete only .LRV/.THM sidecars, and only after at least one output succeeded.
    deleted = failed_deletes = 0
    if not args.keep_sidecars:
        junk = sidecar_files(folder)
        if junk and successful_outputs:
            print("\nRemoving .LRV and .THM sidecar files...")
            deleted, failed_deletes = delete_sidecars(junk)
        elif junk:
            print("\nNo sidecar files were deleted because no output was completed successfully.")

    print("\n=== Finished ===")
    print(f"Successful output files: {len(successful_outputs)}")
    for output in successful_outputs:
        print(f"  - {output}")
    print(f"Sidecar files deleted: {deleted}")
    if failed_deletes:
        print(f"Sidecar deletion failures: {failed_deletes}")
    if failed_groups:
        print("Failed groups:")
        for name in failed_groups:
            print(f"  - {name}")
        return_code = 2
    else:
        return_code = 0

    print("\nOriginal MP4 files were not deleted.")

    if used_picker:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass

    return return_code


if __name__ == "__main__":
    sys.exit(main())
