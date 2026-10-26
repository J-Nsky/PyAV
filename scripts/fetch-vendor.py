import argparse
import hashlib
import logging
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import time


def get_platform():
    # Allow forcing the target platform so we can fetch e.g. an armv7l build
    # while running on an x86_64 host (cross-compilation).
    forced = os.environ.get("PYAV_VENDOR_PLATFORM")
    if forced:
        return forced

    system = platform.system()
    machine = platform.machine().lower()
    is_arm64 = machine in {"arm64", "aarch64"}
    if system == "Linux":
        prefix = "manylinux-" if platform.libc_ver()[0] == "glibc" else "musllinux-"
        return prefix + machine
    elif system == "Darwin":
        return "macos-arm64" if is_arm64 else "macos-x86_64"
    elif system == "Windows":
        return "windows-aarch64" if is_arm64 else "windows-x86_64"
    else:
        return "unknown"


parser = argparse.ArgumentParser(description="Fetch and extract tarballs")
parser.add_argument("destination_dir")
parser.add_argument("--cache-dir", default="tarballs")
parser.add_argument("--config-file", default=os.path.splitext(__file__)[0] + ".json")
args = parser.parse_args()
logging.basicConfig(level=logging.INFO)

with open(args.config_file) as fp:
    config = json.load(fp)

# ensure destination directory exists
logging.info(f"Creating directory {args.destination_dir}")
if not os.path.exists(args.destination_dir):
    os.makedirs(args.destination_dir)

tarball_url = config["url"].replace("{platform}", get_platform())

# MDB lgpl2 (RC item G4, 2026-10-26): every tarball is pinned by SHA-256 in the config
# file ("sha256": {"<platform>": "<64 hex>"}). No pin for this platform = no download;
# a cached or downloaded file whose SHA-256 differs is refused before it is unpacked.
expected = (config.get("sha256") or {}).get(get_platform(), "")
if not re.fullmatch(r"[0-9a-f]{64}", expected):
    sys.exit(f"fetch-vendor: no SHA-256 pinned for {get_platform()} in {args.config_file} "
             f"(found {expected!r}); refusing to download {tarball_url}")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# download tarball
tarball_name = tarball_url.split("/")[-1]
tarball_file = os.path.join(args.cache_dir, tarball_name)
if not os.path.exists(tarball_file):
    logging.info(f"Downloading {tarball_url}")
    if not os.path.exists(args.cache_dir):
        os.mkdir(args.cache_dir)
    subprocess.check_call(
        ["curl", "--fail", "--location", "--proto", "=https", "--tlsv1.2",
         "--silent", "--show-error", "--output", tarball_file, tarball_url]
    )

got = sha256_of(tarball_file)
if got != expected:
    sys.exit(f"fetch-vendor: {tarball_file} has SHA-256 {got}, the pin is {expected}; refusing to unpack")
logging.info(f"{tarball_name}: SHA-256 {got} = the pin ({os.path.getsize(tarball_file)} bytes)")

logging.info(f"Extracting {tarball_name}")
with tarfile.open(tarball_file) as tar:
    tar.extractall(args.destination_dir, filter="data")

# Some tarball members carry pre-1980 mtimes, which the ZIP format (and thus
# delvewheel's wheel repackaging) cannot represent. Bump any such file to now.
ZIP_EPOCH = 315532800  # 1980-01-01 00:00:00 UTC
now = time.time()
for root, _, files in os.walk(args.destination_dir):
    for name in files:
        path = os.path.join(root, name)
        if os.path.getmtime(path) < ZIP_EPOCH:
            os.utime(path, (now, now))
