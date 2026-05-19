import os
import tarfile
import time
import zipfile
from pathlib import Path

import requests
from kagglehub.clients import build_kaggle_client
from kagglehub.config import set_kaggle_api_token
from kagglesdk.competitions.types.competition_api_service import ApiDownloadDataFilesRequest


COMPETITION = "seizure-prediction"
BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.txt"
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/hy-tmp")).expanduser()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(DOWNLOAD_ROOT / COMPETITION))).expanduser()
ARCHIVE_PATH = OUTPUT_DIR / f"{COMPETITION}.archive"
COMPLETE_MARKER = OUTPUT_DIR / ".complete"
PROXY_URL = os.getenv("KAGGLE_PROXY_URL", "").strip()
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
CHUNK_SIZE = 512 * 1024
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_RETRIES = 8
PROGRESS_BAR_WIDTH = 28


def env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def format_bytes(num_bytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"

    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_progress(downloaded: int, total_size: int | None, started_at: float, resumed_from: int) -> None:
    elapsed = max(time.time() - started_at, 1e-6)
    session_downloaded = max(downloaded - resumed_from, 0)
    speed = session_downloaded / elapsed

    if total_size and total_size > 0:
        ratio = min(downloaded / total_size, 1.0)
        filled = int(PROGRESS_BAR_WIDTH * ratio)
        bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
        remaining_seconds = (total_size - downloaded) / speed if speed > 0 else None
        line = (
            f"\r[{bar}] {ratio * 100:6.2f}%  "
            f"{format_bytes(downloaded)}/{format_bytes(total_size)}  "
            f"{format_bytes(speed)}/s  ETA {format_duration(remaining_seconds)}"
        )
    else:
        pulse = int(elapsed * 3) % PROGRESS_BAR_WIDTH
        bar = "".join("#" if i == pulse else "-" for i in range(PROGRESS_BAR_WIDTH))
        line = (
            f"\r[{bar}]  "
            f"{format_bytes(downloaded)} downloaded  "
            f"{format_bytes(speed)}/s  ETA unknown"
        )

    print(line, end="", flush=True)


def load_token() -> str:
    env_token = os.getenv("KAGGLE_API_TOKEN", "").strip()
    if env_token:
        return env_token

    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            f"Missing token file: {TOKEN_FILE}. Put your Kaggle API token in token.txt "
            "or set KAGGLE_API_TOKEN."
        )

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token.txt is empty.")
    return token


def is_complete() -> bool:
    return COMPLETE_MARKER.exists() and any(
        path.name not in {ARCHIVE_PATH.name, COMPLETE_MARKER.name}
        for path in OUTPUT_DIR.iterdir()
    )


def clear_output_dir() -> None:
    if not OUTPUT_DIR.exists():
        return

    for path in OUTPUT_DIR.iterdir():
        try:
            if path.is_dir():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file():
                        child.unlink()
                    else:
                        child.rmdir()
                path.rmdir()
            else:
                path.unlink()
        except PermissionError as exc:
            raise RuntimeError(
                f"Cannot remove '{path}' because it is currently in use by another process. "
                "Stop the other downloader or close any program using this file, then try again. "
                "If you only want to continue the existing partial download, run without FORCE_DOWNLOAD."
            ) from exc


def prepare_output_dir(force_download: bool) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if force_download:
        clear_output_dir()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def configure_proxy() -> None:
    if not PROXY_URL:
        return

    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    os.environ["http_proxy"] = PROXY_URL
    os.environ["https_proxy"] = PROXY_URL


def get_download_response() -> requests.Response:
    request = ApiDownloadDataFilesRequest()
    request.competition_name = COMPETITION

    with build_kaggle_client() as api_client:
        return api_client.competitions.competition_api_client.download_data_files(request)


def download_with_resume(url: str, archive_path: Path, total_size: int | None, resumable: bool) -> None:
    attempts = 0
    allow_resume = env_flag("RESUME_DOWNLOAD")

    while True:
        existing_size = archive_path.stat().st_size if archive_path.exists() else 0
        headers = {}
        mode = "wb"

        if existing_size and not allow_resume:
            print(f"Discarding existing archive ({format_bytes(existing_size)}); starting a clean download.")
            archive_path.unlink()
            existing_size = 0
        elif total_size is not None and existing_size == total_size:
            print(f"Existing archive already has expected size: {format_bytes(existing_size)}")
            return
        elif total_size is not None and existing_size > total_size:
            print(
                f"Existing archive is larger than expected "
                f"({format_bytes(existing_size)} > {format_bytes(total_size)}); restarting."
            )
            archive_path.unlink()
            existing_size = 0

        if existing_size:
            if not resumable:
                raise RuntimeError(
                    "The server does not support HTTP range requests, so this partial archive cannot be resumed."
                )
            headers["Range"] = f"bytes={existing_size}-"
            mode = "ab"
            print(f"Resuming download from {existing_size} bytes...")
        else:
            print("Starting download...")

        try:
            started_at = time.time()
            with requests.get(
                url,
                stream=True,
                headers=headers,
                proxies=PROXIES,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            ) as response:
                response.raise_for_status()

                if existing_size and response.status_code != 206:
                    raise RuntimeError(
                        f"Expected HTTP 206 for resume, got {response.status_code}. "
                        "Delete the partial archive or set FORCE_DOWNLOAD=1 to restart."
                    )

                with archive_path.open(mode) as fh:
                    bytes_written = existing_size
                    last_rendered_at = 0.0
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        bytes_written += len(chunk)
                        now = time.time()
                        if now - last_rendered_at >= 0.25:
                            render_progress(bytes_written, total_size, started_at, existing_size)
                            last_rendered_at = now

                render_progress(bytes_written, total_size, started_at, existing_size)
                print()

            final_size = archive_path.stat().st_size
            if total_size is not None and final_size != total_size:
                if final_size < total_size:
                    print(f"Download paused at {final_size}/{total_size} bytes, retrying...")
                    attempts += 1
                    if attempts > MAX_RETRIES:
                        raise RuntimeError("Download did not finish after multiple resume attempts.")
                    time.sleep(min(attempts, 5))
                    continue

                raise RuntimeError(
                    f"Downloaded file is larger than expected: {final_size} > {total_size}."
                )

            return
        except (requests.RequestException, OSError) as exc:
            attempts += 1
            if attempts > MAX_RETRIES:
                raise RuntimeError("Download failed after multiple retries.") from exc
            print(f"Download interrupted ({exc}). Retrying {attempts}/{MAX_RETRIES}...")
            time.sleep(min(attempts, 5))


def extract_archive(archive_path: Path, output_dir: Path) -> None:
    validate_archive(archive_path)
    print("Extracting archive...")
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as archive:
            archive.extractall(output_dir)
        return

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as archive:
            archive.extractall(output_dir)
        return

    raise ValueError(f"Unsupported archive type: {archive_path}")


def validate_archive(archive_path: Path) -> None:
    print("Validating archive...")
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as archive:
            for member in archive:
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                while extracted.read(CHUNK_SIZE):
                    pass
        return

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as archive:
            bad_member = archive.testzip()
        if bad_member is not None:
            raise RuntimeError(f"Archive validation failed at member: {bad_member}")
        return

    raise ValueError(f"Unsupported archive type: {archive_path}")


def mark_complete() -> None:
    COMPLETE_MARKER.write_text("ok\n", encoding="utf-8")


def main() -> None:
    set_kaggle_api_token(load_token())
    configure_proxy()
    force_download = env_flag("FORCE_DOWNLOAD")
    prepare_output_dir(force_download)

    if is_complete() and not force_download:
        print("Competition files already exist in:", OUTPUT_DIR)
        print("Set FORCE_DOWNLOAD=1 to replace the existing download.")
        return

    try:
        response = get_download_response()
        download_url = response.url
        total_size = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else None
        resumable = response.headers.get("Accept-Ranges", "").lower() == "bytes"

        download_with_resume(download_url, ARCHIVE_PATH, total_size, resumable)
        extract_archive(ARCHIVE_PATH, OUTPUT_DIR)
        ARCHIVE_PATH.unlink(missing_ok=True)
        mark_complete()
    except Exception as exc:
        raise RuntimeError(
            "Download failed. Make sure you are logged into Kaggle, "
            "have accepted the competition rules on the Kaggle website, "
            "and that your network can access Kaggle."
        ) from exc

    print("Competition files downloaded to:", OUTPUT_DIR)


if __name__ == "__main__":
    main()
