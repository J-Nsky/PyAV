"""The test this fork's CI runs on every built wheel (instead of upstream's tests,
which encode with libx264).

1. The licence guard: all seven FFmpeg libraries say "LGPL version 2.1 or later",
   no --enable-gpl / nonfree / version3, the only --enable-lib* is libdav1d, no
   encoder at all, and the libraries bundled into the wheel are only FFmpeg's
   seven, dav1d and zlib.
2. What My Dancing Book needs: open, decode, resample (as faster-whisper's
   decode_audio does) and stream-copy remux of the synthetic clips in
   scripts/lgpl-matrix/.

Run with the installed wheel, from any folder but the source tree:
    python scripts/lgpl-check.py
"""

import importlib.metadata
import os
import re
import sys
import tempfile

import av

LICENSE = "LGPL version 2.1 or later"
LIBRARIES = ("avcodec", "avdevice", "avfilter", "avformat", "avutil", "swresample", "swscale")
BUNDLED = re.compile(
    r"^(lib)?(avcodec|avdevice|avfilter|avformat|avutil|swresample|swscale|dav1d|z|zlib1)([-.]|$)", re.I
)
DECODERS = ("h264", "hevc", "vp9", "libdav1d", "aac", "mp3float", "opus")
MUXERS = ("mp4", "mov", "webm", "matroska")
MATRIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lgpl-matrix")
REMUX_FORMATS = {".mp4": "mp4", ".mov": "mov", ".webm": "webm"}

problems: list[str] = []


def licence_guard() -> None:
    version = importlib.metadata.version("av")
    print(f"av {version} from {os.path.dirname(av.__file__)}")
    if "+lgpl" not in version:
        problems.append(f"version {version} has no +lgpl label")

    meta = getattr(av, "library_meta", None) or av._core.library_meta
    for name in LIBRARIES:
        info = meta.get("lib" + name)
        if not info:
            problems.append(f"lib{name}: no library meta")
            continue
        if info.get("license") != LICENSE:
            problems.append(f"lib{name}: licence {info.get('license')!r}")
        words = str(info.get("configuration", "")).split()
        for flag in ("--enable-gpl", "--enable-nonfree", "--enable-version3"):
            if flag in words:
                problems.append(f"lib{name}: configured with {flag}")
        libs = {w for w in words if w.startswith("--enable-lib")}
        if libs != {"--enable-libdav1d"}:
            problems.append(f"lib{name}: --enable-lib* = {sorted(libs)}")

    encoders = []
    for name in sorted(av.codecs_available):
        try:
            av.Codec(name, "w")
        except Exception:
            continue
        encoders.append(name)
    if encoders:
        problems.append(f"encoders present: {encoders}")
    for name in DECODERS:
        try:
            av.Codec(name, "r")
        except Exception as exc:
            problems.append(f"decoder {name} missing: {exc}")
    for name in MUXERS:
        if name not in av.formats_available:
            problems.append(f"format {name} missing")
    print(f"{len(av.codecs_available)} codecs, {len(encoders)} encoders")

    package = os.path.dirname(av.__file__)
    folders = [os.path.join(package, ".dylibs"), package.rstrip(os.sep) + ".libs"]
    bundled = []
    for folder in folders:
        if os.path.isdir(folder):
            bundled += [f for f in os.listdir(folder) if not f.startswith(".")]
    print("bundled:", " ".join(sorted(bundled)) or "(none)")
    if not bundled:
        problems.append(f"no bundled libraries found in {folders}")
    for name in bundled:
        if not BUNDLED.search(name):
            problems.append(f"bundled library not allowed: {name}")


def remux(src: str, fmt: str, dst: str) -> None:
    """Stream copy as My Dancing Book's share export does (no encoder lookup)."""
    with av.open(src) as inp:
        keep = [s for s in inp.streams if s.type in ("video", "audio")]
        with av.open(dst, "w", format=fmt, options={"fflags": "+bitexact"}) as out:
            mapping = {s.index: out.add_stream_from_template(s, opaque=True) for s in keep}
            for pkt in inp.demux(keep):
                if pkt.dts is None:
                    continue
                pkt.stream = mapping[pkt.stream.index]
                out.mux(pkt)
    with av.open(src) as a, av.open(dst) as b:
        before = [(s.type, s.codec_context.name) for s in a.streams if s.type in ("video", "audio")]
        after = [(s.type, s.codec_context.name) for s in b.streams]
        if before != after:
            raise ValueError(f"streams {before} became {after}")


def matrix() -> None:
    clips = sorted(f for f in os.listdir(MATRIX) if not f.startswith("."))
    if len(clips) < 10:
        problems.append(f"only {len(clips)} clips in {MATRIX}")
    with tempfile.TemporaryDirectory() as tmp:
        for name in clips:
            path = os.path.join(MATRIX, name)
            row = [name]
            try:
                with av.open(path, metadata_errors="ignore") as c:
                    row.append("+".join(s.codec_context.name for s in c.streams))
                    if c.streams.video:
                        frame = next(c.decode(video=0))
                        row.append(f"{frame.width}x{frame.height}")
                with av.open(path, metadata_errors="ignore") as c:
                    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                    samples = 0
                    for frame in c.decode(audio=0):
                        for out in resampler.resample(frame):
                            samples += out.samples
                    for out in resampler.resample(None):
                        samples += out.samples
                    if samples < 16000 * 1.5:
                        raise ValueError(f"only {samples} samples of audio")
                    row.append(f"audio {samples / 16000:.2f} s")
                ext = os.path.splitext(name)[1]
                if ext in REMUX_FORMATS:
                    remux(path, REMUX_FORMATS[ext], os.path.join(tmp, "copy" + ext))
                    row.append("remux ok")
                print("  " + " | ".join(row))
            except Exception as exc:
                problems.append(f"{name}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    licence_guard()
    matrix()
    if problems:
        print("LGPL check FAILED:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("LGPL check passed")
