#!/usr/bin/env python3

import os
import sys
import time
import logging
import argparse
import subprocess
from pathlib import Path
from urllib.parse import urljoin

import requests


# ============================================================
# CONFIG
# ============================================================

class Config:
    def __init__(self, config_file=None):
        self.dashcam_ip = os.getenv("DASHCAM_IP", "http://192.168.1.33")
        self.download_dir = os.getenv("DOWNLOAD_DIR", "./dashcam")

        if config_file and Path(config_file).exists():
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(config_file)

            if "dashcam" in cfg:
                self.dashcam_ip = cfg["dashcam"].get("ip", self.dashcam_ip)

            if "general" in cfg:
                self.download_dir = cfg["general"].get("download_dir", self.download_dir)

        if not self.dashcam_ip.startswith("http"):
            self.dashcam_ip = "http://" + self.dashcam_ip


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("dashcam-v2")


# ============================================================
# DASHCAM CLIENT
# ============================================================

class DashcamClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 DashcamTransporterV2"
        })

        # warm-up (important for firmware cookie quirks)
        try:
            self.session.get(self.base_url, timeout=5)
        except Exception:
            pass

    def list_files(self):
        folders = [
            ("/DCIM/Movie", "video"),
            ("/DCIM/Movie/RO", "video"),
            ("/DCIM/Movie/Parking", "video"),
            ("/DCIM/Photo", "photo"),
        ]

        files = []

        for folder, ftype in folders:
            url = self.base_url + folder

            try:
                r = self.session.get(url, timeout=15)
                if r.status_code != 200:
                    continue

                import re
                matches = re.findall(
                    r'href=["\']?([^"\'>]+\.(?:mp4|MP4|jpg|JPG|jpeg|JPEG)(?:\?[^"\'>]*)?)',
                    r.text,
                    re.IGNORECASE
                )

                for m in matches:
                    full = urljoin(url + "/", m)
                    name = m.split("?")[0].split("/")[-1]

                    files.append({
                        "name": name,
                        "url": full,
                        "type": ftype
                    })

            except Exception as e:
                logger.warning(f"List failed {folder}: {e}")

        # dedupe
        seen = set()
        out = []
        for f in files:
            if f["name"] not in seen:
                seen.add(f["name"])
                out.append(f)

        logger.info(f"Found {len(out)} files")
        return out

    def delete(self, file):
        try:
            self.session.get(file["url"] + "?del=1", timeout=10)
        except Exception:
            pass


# ============================================================
# RESILIENT DOWNLOADER
# ============================================================

class ResilientDownloader:
    def __init__(self, session):
        self.session = session

    def download(self, url: str, dest: Path, max_retries=5):
        dest.parent.mkdir(parents=True, exist_ok=True)

        part = dest.with_suffix(".part")

        for attempt in range(1, max_retries + 1):

            try:
                downloaded = part.stat().st_size if part.exists() else 0

                headers = {}
                if downloaded > 0:
                    headers["Range"] = f"bytes={downloaded}-"
                    logger.info(f"Resuming at {downloaded} bytes")

                with self.session.get(
                    url,
                    stream=True,
                    headers=headers,
                    timeout=(10, 300),
                    allow_redirects=True
                ) as r:

                    r.raise_for_status()

                    # If server ignores Range
                    if downloaded > 0 and r.status_code == 200:
                        logger.warning("Server ignored Range → restarting")
                        downloaded = 0
                        part.unlink(missing_ok=True)

                    # 🔥 IMPORTANT FIX: raw stream drain (NOT iter_content)
                    r.raw.decode_content = True

                    mode = "ab" if downloaded > 0 else "wb"

                    with open(part, mode) as f:
                        while True:
                            chunk = r.raw.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)

                final_size = part.stat().st_size

                # sanity check (detect truncation like 32KB bug)
                if final_size < 1000:
                    raise IOError(f"Suspiciously small download: {final_size} bytes")

                part.rename(dest)

                logger.info(f"OK: {dest.name} ({final_size} bytes)")
                return True

            except Exception as e:
                logger.warning(f"Attempt {attempt}/{max_retries} failed: {e}")

                time.sleep(min(2 ** attempt, 30))

        # ====================================================
        # CURL FALLBACK
        # ====================================================

        logger.warning("Falling back to curl for reliability")

        try:
            subprocess.run([
                "curl",
                "-L",
                "--fail",
                "--retry", "5",
                "--retry-delay", "2",
                "-o", str(dest),
                url
            ], check=True)

            logger.info(f"curl success: {dest.name}")
            return True

        except Exception as e:
            logger.error(f"curl fallback failed: {e}")
            return False


# ============================================================
# ORCHESTRATOR
# ============================================================

class Transporter:
    def __init__(self, config, onlydelete=False):
        self.client = DashcamClient(config.dashcam_ip)
        self.downloader = ResilientDownloader(self.client.session)
        self.root = Path(config.download_dir)
        self.onlydelete = onlydelete

    def run(self):
        logger.info("Dashcam Transporter v2 starting")
        logger.info(f"Target: {self.client.base_url}")

        files = self.client.list_files()

        if not files:
            logger.info("No files found")
            return

        if self.onlydelete:
            logger.info("Delete-only mode enabled")

            deleted = 0

            for f in files:
                logger.info(f"Deleting {f['name']}")
                self.client.delete(f)
                deleted += 1

            logger.info(f"Deleted {deleted} files")
            return

        for f in files:
            dest = self.root / f["type"] / f["name"]

            if dest.exists():
                logger.info(f"Skip existing: {f['name']}")
                self.client.delete(f)
                continue

            ok = self.downloader.download(f["url"], dest)

            if ok:
                self.client.delete(f)
            else:
                logger.error(f"FAILED: {f['name']}")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument(
    "--onlydelete",
    action="store_true",
    help="Delete all files from the dashcam without downloading them."
    )
    
    args = parser.parse_args()

    config = Config(args.config)
    t = Transporter(config, onlydelete=args.onlydelete)

    try:
        t.run()
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
