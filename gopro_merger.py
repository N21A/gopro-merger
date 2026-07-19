#!/usr/bin/env python3
"""
Merge GoPro chapter files and compress them to HEVC/H.265 with FFmpeg.

Examples:
    gopro-merger "D:\\GoPro\\D2S1"
    gopro-merger "D:\\GoPro\\D2S1" --quality 24
    gopro-merger "D:\\GoPro\\D2S1" --resolution 1080p
    gopro-merger "D:\\GoPro\\D2S1" --speed fast
    gopro-merger "D:\\GoPro\\D2S1" --encoder cpu
    gopro-merger "D:\\GoPro\\D2S1" --combine-all
    gopro-merger "D:\\GoPro\\D2S1" --delete-originals

Without a folder argument, a graphical folder picker is opened when Tkinter is
available.

Default behaviour:
- Groups GoPro chapter files belonging to the same recording, for example:
    GX010021.MP4, GX020021.MP4, ... -> one output
- If no GoPro naming pattern is found, combines all MP4 files in filename order.
- Encodes directly to HEVC/H.265 without making a large intermediate file.
- Uses NVIDIA NVENC and CUDA/NVDEC hardware decoding when available.
- Falls back automatically to NVENC with software decoding, then CPU libx265.
- Uses a balanced speed profile by default for faster encoding without giving up
  sensible compression efficiency.
- Can downscale to 2160p, 1440p, 1080p, or 720p while preserving aspect ratio.
- Uses GPU-accelerated CUDA scaling when the full NVIDIA path is available.
- Copies audio and GoPro telemetry/data streams where supported.
- Deletes .LRV and .THM sidecar files after successful processing.
- Keeps every original MP4 unless --delete-originals is supplied.
- With --delete-originals, validates the output and asks for explicit
  confirmation before permanently deleting eligible source MP4 files.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version
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
MIN_VALID_OUTPUT_BYTES = 1024

# NVENC profiles trade compression efficiency for throughput. The default
# deliberately avoids the slower p5 + multipass combination while retaining
# spatial adaptive quantisation and B-frames for sensible compression.
NVENC_SPEED_PROFILES = {
    "quality": {
        "preset": "p5",
        "multipass": "qres",
        "spatial_aq": "1",
        "temporal_aq": "1",
        "bframes": "3",
    },
    "balanced": {
        "preset": "p3",
        "multipass": "disabled",
        "spatial_aq": "1",
        "temporal_aq": "0",
        "bframes": "3",
    },
    "fast": {
        "preset": "p2",
        "multipass": "disabled",
        "spatial_aq": "1",
        "temporal_aq": "0",
        "bframes": "3",
    },
    "maximum": {
        "preset": "p1",
        "multipass": "disabled",
        "spatial_aq": "0",
        "temporal_aq": "0",
        "bframes": "0",
    },
}

CPU_SPEED_PROFILES = {
    "quality": "slow",
    "balanced": "medium",
    "fast": "fast",
    "maximum": "veryfast",
}

# Resolution presets use the output height while FFmpeg calculates an even width
# that preserves the source display aspect ratio. "original" performs no scaling.
RESOLUTION_HEIGHTS: Dict[str, Optional[int]] = {
    "original": None,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
}


def package_version() -> str:
    """Return the installed package version, or a development label."""
    try:
        return version("gopro-merger")
    except PackageNotFoundError:
        return "development"


def natural_key(path: Path) -> List[object]:
    """Sort filenames naturally: file2 before file10."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


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
            f"{name} was not found in PATH. Open a terminal and confirm that "
            f"'{name} -version' works."
        )
    return executable


def companion_executable(primary: str, companion: str) -> Optional[str]:
    """Find a companion executable beside FFmpeg or elsewhere on PATH."""
    primary_path = Path(primary)
    suffix = primary_path.suffix
    beside_primary = primary_path.with_name(companion + suffix)
    if beside_primary.is_file():
        return str(beside_primary)
    return shutil.which(companion)


def ffmpeg_has_encoder(ffmpeg: str, encoder: str) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return encoder.lower() in result.stdout.lower()


def ffmpeg_has_hwaccel(ffmpeg: str, hwaccel: str) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-hwaccels"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    available = {line.strip().lower() for line in result.stdout.splitlines()}
    return hwaccel.lower() in available


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


def build_groups(
    files: Sequence[Path],
    combine_all: bool,
    folder_name: str,
) -> Dict[str, List[Path]]:
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


def encoder_args(encoder: str, quality: int, speed: str) -> List[str]:
    """Return encoder arguments for the selected speed/quality trade-off."""
    if encoder == "nvenc":
        profile = NVENC_SPEED_PROFILES[speed]
        return [
            "-c:v", "hevc_nvenc",
            "-preset", profile["preset"],
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(quality),
            "-b:v", "0",
            "-multipass", profile["multipass"],
            "-spatial_aq", profile["spatial_aq"],
            "-temporal_aq", profile["temporal_aq"],
            "-bf", profile["bframes"],
        ]

    return [
        "-c:v", "libx265",
        "-preset", CPU_SPEED_PROFILES[speed],
        "-crf", str(quality),
    ]


def video_filter_args(
    resolution: str,
    encoder: str,
    hardware_decode: bool,
) -> List[str]:
    """Return an aspect-ratio-preserving scale filter for the selected path."""
    target_height = RESOLUTION_HEIGHTS[resolution]
    if target_height is None:
        return []

    if encoder == "nvenc" and hardware_decode:
        # Keep frames in GPU memory and scale them with CUDA before NVENC.
        video_filter = (
            f"scale_cuda=-2:{target_height}:"
            "format=yuv420p:interp_algo=lanczos"
        )
    else:
        # Software scaling is used for CPU encoding and NVENC software decode.
        video_filter = (
            f"scale=-2:{target_height}:flags=lanczos,"
            "format=yuv420p"
        )

    return ["-vf", video_filter]


def run_ffmpeg(
    ffmpeg: str,
    concat_file: Path,
    output: Path,
    encoder: str,
    quality: int,
    speed: str,
    resolution: str,
    overwrite: bool,
    hardware_decode: bool,
) -> int:
    command = [
        ffmpeg,
        "-hide_banner",
        "-y" if overwrite else "-n",
    ]

    # Keep decoded frames in GPU memory and feed them directly to NVENC. If the
    # local driver/GPU cannot use this path, the caller retries with software
    # decoding before falling back to CPU encoding.
    if encoder == "nvenc" and hardware_decode:
        command.extend([
            "-hwaccel", "cuda",
            "-hwaccel_output_format", "cuda",
        ])

    command.extend([
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        # Keep the main video, optional audio, and optional GoPro telemetry/data.
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:d?",
        *video_filter_args(resolution, encoder, hardware_decode),
        *encoder_args(encoder, quality, speed),
        "-c:a", "copy",
        "-c:d", "copy",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-tag:v", "hvc1",
        "-movflags", "+faststart",
        str(output),
    ])

    print(
        "\nRunning FFmpeg:\n  "
        + " ".join(f'"{part}"' if " " in part else part for part in command)
    )
    return subprocess.run(command, check=False).returncode


def probe_media(ffprobe: Optional[str], path: Path) -> Optional[Tuple[float, List[str]]]:
    """Return duration and stream types, or None when probing is unavailable."""
    if not ffprobe:
        return None

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout)
        duration = float(payload.get("format", {}).get("duration", 0.0))
        stream_types = [
            stream.get("codec_type", "")
            for stream in payload.get("streams", [])
            if stream.get("codec_type")
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    return duration, stream_types


def probe_video_dimensions(
    ffprobe: Optional[str],
    path: Path,
) -> Optional[Tuple[int, int]]:
    """Return the first video stream's width and height when available."""
    if not ffprobe:
        return None

    result = subprocess.run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    try:
        streams = json.loads(result.stdout).get("streams", [])
        if not streams:
            return None
        width = int(streams[0]["width"])
        height = int(streams[0]["height"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    return width, height


def validate_output(
    ffprobe: Optional[str],
    output: Path,
    sources: Sequence[Path],
    resolution: str,
) -> bool:
    """Validate file size, video presence, and duration before declaring success."""
    try:
        if not output.is_file() or output.stat().st_size < MIN_VALID_OUTPUT_BYTES:
            print("Output validation failed: output file is missing or empty.")
            return False
    except OSError as exc:
        print(f"Output validation failed: {exc}")
        return False

    output_probe = probe_media(ffprobe, output)
    if output_probe is None:
        if ffprobe:
            print("WARNING: ffprobe could not validate the output; using file-size validation only.")
        return True

    output_duration, output_streams = output_probe
    if "video" not in output_streams or output_duration <= 0:
        print("Output validation failed: no valid video stream or duration was found.")
        return False

    dimensions = probe_video_dimensions(ffprobe, output)
    target_height = RESOLUTION_HEIGHTS[resolution]
    if target_height is not None and dimensions is not None:
        width, height = dimensions
        if height != target_height:
            print(
                "Output validation failed: expected "
                f"{resolution} output but received {width}x{height}."
            )
            return False
        print(f"Validated output resolution: {width}x{height} ({resolution}).")
    elif dimensions is not None:
        print(f"Validated output resolution: {dimensions[0]}x{dimensions[1]} (original).")

    source_durations: List[float] = []
    for source in sources:
        source_probe = probe_media(ffprobe, source)
        if source_probe is None or source_probe[0] <= 0:
            source_durations = []
            break
        source_durations.append(source_probe[0])

    if source_durations:
        expected_duration = sum(source_durations)
        tolerance = max(3.0, expected_duration * 0.01)
        difference = abs(output_duration - expected_duration)
        if difference > tolerance:
            print(
                "Output validation failed: duration differs from the source sequence "
                f"by {difference:.2f} seconds."
            )
            return False
        print(
            f"Validated duration: {output_duration:.2f}s "
            f"(expected approximately {expected_duration:.2f}s)."
        )
    else:
        print(f"Validated output video duration: {output_duration:.2f}s.")

    return True


def total_file_size(files: Iterable[Path]) -> int:
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def print_compression_summary(sources: Sequence[Path], output: Path) -> None:
    source_size = total_file_size(sources)
    try:
        output_size = output.stat().st_size
    except OSError:
        return

    print(f"Source size: {format_bytes(source_size)}")
    print(f"Output size: {format_bytes(output_size)}")
    if source_size > 0:
        reduction = (1.0 - (output_size / source_size)) * 100.0
        if reduction >= 0:
            print(f"Space reduction: {reduction:.1f}%")
        else:
            print(f"Output is {-reduction:.1f}% larger than the sources.")


def encoding_attempts(
    selected_encoder: str,
    requested_encoder: str,
    cuda_available: bool,
    disable_hw_decode: bool,
) -> List[Tuple[str, str, bool]]:
    """Build a safe ordered list of hardware/software fallback attempts."""
    if selected_encoder == "cpu":
        return [("CPU libx265 encoding", "cpu", False)]

    attempts: List[Tuple[str, str, bool]] = []
    if cuda_available and not disable_hw_decode:
        attempts.append(("NVIDIA NVENC with CUDA/NVDEC hardware decoding", "nvenc", True))

    attempts.append(("NVIDIA NVENC with software decoding", "nvenc", False))

    if requested_encoder == "auto":
        attempts.append(("CPU libx265 fallback", "cpu", False))

    return attempts


def sidecar_files(folder: Path) -> List[Path]:
    return sorted(
        [
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in SIDECAR_EXTENSIONS
        ],
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


def unique_paths(files: Iterable[Path]) -> List[Path]:
    """Return paths in their original order with duplicates removed."""
    seen = set()
    result: List[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def format_bytes(size: int) -> str:
    """Format a byte count using binary units."""
    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def confirm_original_deletion(files: Sequence[Path]) -> bool:
    """Require an explicit confirmation before permanently deleting MP4 files."""
    total_size = total_file_size(files)

    print("\nWARNING: original MP4 deletion was requested.")
    print("The following source files will be permanently deleted, not moved to the Recycle Bin or Trash:")
    for path in files:
        print(f"  - {path.name}")
    print(f"Files: {len(files)} | Total size: {format_bytes(total_size)}")
    print("Only files from recordings successfully encoded and validated during this run are listed.")

    try:
        response = input("Type DELETE to confirm, or press Enter to keep the originals: ")
    except EOFError:
        return False
    return response.strip() == "DELETE"


def delete_originals(files: Iterable[Path]) -> Tuple[int, int]:
    """Permanently delete original MP4 files after confirmation."""
    deleted = 0
    failed = 0
    for path in files:
        try:
            path.unlink()
            print(f"Deleted original: {path.name}")
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
        prog="gopro-merger",
        description="Merge GoPro chapters, compress to HEVC, and remove .LRV/.THM sidecars.",
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
        "--resolution",
        choices=tuple(RESOLUTION_HEIGHTS),
        default="original",
        help=(
            "Output resolution. Preserves aspect ratio and calculates an even "
            "width automatically. Default: original."
        ),
    )
    parser.add_argument(
        "--speed",
        choices=("quality", "balanced", "fast", "maximum"),
        default="balanced",
        help=(
            "Encoding speed/compression trade-off. balanced is faster than the old "
            "default while retaining sensible compression. Default: balanced."
        ),
    )
    parser.add_argument(
        "--encoder",
        choices=("auto", "nvenc", "cpu"),
        default="auto",
        help="auto uses NVIDIA NVENC if available, then falls back to CPU. Default: auto.",
    )
    parser.add_argument(
        "--no-hw-decode",
        action="store_true",
        help="Disable CUDA/NVDEC hardware decoding while still allowing NVENC encoding.",
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
    parser.add_argument(
        "--delete-originals",
        action="store_true",
        help=(
            "After successful processing, ask for confirmation before permanently "
            "deleting source MP4 files encoded and validated during this run."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version()}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    used_picker = args.folder is None
    folder = args.folder or choose_folder()

    if folder is None:
        print("No folder selected, or Tkinter is unavailable.")
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

    ffprobe = companion_executable(ffmpeg, "ffprobe")
    files = discover_mp4_files(folder)
    if not files:
        print(f"No MP4 files found in: {folder}")
        return 1

    groups = build_groups(files, args.combine_all, folder.name)
    output_dir = folder / OUTPUT_DIR_NAME
    output_dir.mkdir(exist_ok=True)

    nvenc_available = ffmpeg_has_encoder(ffmpeg, "hevc_nvenc")
    cuda_available = ffmpeg_has_hwaccel(ffmpeg, "cuda")
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
    if selected_encoder == "nvenc":
        hw_decode_enabled = cuda_available and not args.no_hw_decode
        print(f"Hardware decoding: {'CUDA/NVDEC' if hw_decode_enabled else 'software decode'}")
    print(f"Speed profile: {args.speed}")
    print(f"Resolution: {args.resolution}")
    print(f"Quality: {args.quality}")
    print(f"Output validation: {'ffprobe duration and stream checks' if ffprobe else 'file-size check only'}")
    print(f"Outputs: {output_dir}")

    successful_outputs: List[Path] = []
    deletable_originals: List[Path] = []
    failed_groups: List[str] = []

    for group_name, group_files in groups.items():
        if not group_files:
            continue

        resolution_suffix = (
            "" if args.resolution == "original" else f"_{args.resolution}"
        )
        output = output_dir / (
            f"{safe_output_name(group_name)}_merged_hevc"
            f"{resolution_suffix}.mp4"
        )
        if output.exists() and not args.overwrite:
            print(f"\nSkipping {group_name}: output already exists: {output.name}")
            print("Originals for this skipped recording will not be deleted.")
            successful_outputs.append(output)
            continue

        print(f"\n=== {group_name} ===")
        for path in group_files:
            print(f"  {path.name}")

        concat_file = make_concat_file(folder, group_files)
        attempts = encoding_attempts(
            selected_encoder=selected_encoder,
            requested_encoder=requested_encoder,
            cuda_available=cuda_available,
            disable_hw_decode=args.no_hw_decode,
        )

        group_succeeded = False
        group_start = time.monotonic()
        try:
            for attempt_number, (label, encoder, hardware_decode) in enumerate(attempts, start=1):
                print(f"\nEncoding attempt {attempt_number}/{len(attempts)}: {label}")

                if attempt_number > 1:
                    try:
                        output.unlink(missing_ok=True)
                    except OSError:
                        pass

                return_code = run_ffmpeg(
                    ffmpeg=ffmpeg,
                    concat_file=concat_file,
                    output=output,
                    encoder=encoder,
                    quality=args.quality,
                    speed=args.speed,
                    resolution=args.resolution,
                    overwrite=args.overwrite or attempt_number > 1,
                    hardware_decode=hardware_decode,
                )

                if return_code != 0:
                    print(f"\n{label} failed.")
                    continue

                if not validate_output(
                    ffprobe, output, group_files, args.resolution
                ):
                    print(f"\n{label} created an output that did not pass validation.")
                    continue

                group_succeeded = True
                successful_outputs.append(output)
                deletable_originals.extend(group_files)
                elapsed = time.monotonic() - group_start
                print(f"\nCreated and validated: {output}")
                print(f"Encoding time: {elapsed / 60.0:.1f} minutes")
                print_compression_summary(group_files, output)
                break

            if not group_succeeded:
                failed_groups.append(group_name)
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass
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

    originals_deleted = original_delete_failures = 0
    if args.delete_originals:
        candidates = unique_paths(deletable_originals)
        if candidates:
            if confirm_original_deletion(candidates):
                print("\nDeleting confirmed original MP4 files...")
                originals_deleted, original_delete_failures = delete_originals(candidates)
            else:
                print("\nOriginal MP4 deletion cancelled. All source videos were kept.")
        else:
            print(
                "\nNo original MP4 files are eligible for deletion because no recording "
                "was successfully encoded and validated during this run."
            )

    print("\n=== Finished ===")
    print(f"Successful output files: {len(successful_outputs)}")
    for output in successful_outputs:
        print(f"  - {output}")
    print(f"Sidecar files deleted: {deleted}")
    if failed_deletes:
        print(f"Sidecar deletion failures: {failed_deletes}")
    print(f"Original MP4 files deleted: {originals_deleted}")
    if original_delete_failures:
        print(f"Original MP4 deletion failures: {original_delete_failures}")
    if failed_groups:
        print("Failed groups:")
        for name in failed_groups:
            print(f"  - {name}")
        return_code = 2
    else:
        return_code = 0

    if not args.delete_originals:
        print("\nOriginal MP4 files were not deleted. Use --delete-originals to request deletion.")
    elif originals_deleted == 0:
        print("\nNo original MP4 files were deleted.")

    if used_picker:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass

    return return_code


if __name__ == "__main__":
    sys.exit(main())
