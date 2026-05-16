from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PHYSIONET_BASE = "https://physionet.org/files/siena-scalp-eeg/1.0.0"
S3_BASE = "https://physionet-open.s3.amazonaws.com/siena-scalp-eeg/1.0.0"
ROOT_FILES = (
    "LICENSE.txt",
    "RECORDS",
    "SHA256SUMS.txt",
    "subject_info.csv",
)


@dataclass
class DownloadTask:
    rel_path: str
    dst: Path
    kind: str


@dataclass
class DownloadResult:
    rel_path: str
    dst: Path
    kind: str
    used_url: str
    resumed: bool
    bytes_written: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the full SIENA dataset with parallel workers and resumable .part files."
    )
    parser.add_argument(
        "--data-source",
        default="siena",
        choices=["siena"],
        help="Dataset name used to build the default output path.",
    )
    parser.add_argument(
        "--base-dir",
        default="/hy-tmp/data/raw",
        help="Base directory used when --output-root is not set.",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Full output path. Defaults to <base-dir>/<data-source>.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of parallel download workers.",
    )
    parser.add_argument(
        "--chunk-size-mb",
        type=int,
        default=8,
        help="Chunk size per read in MiB.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=120,
        help="Per-request timeout in seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retries per URL before falling back to the next mirror.",
    )
    parser.add_argument(
        "--retry-wait-sec",
        type=float,
        default=2.0,
        help="Seconds to wait between retries.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files even if the final file already exists.",
    )
    return parser.parse_args()


def resolve_output_root(args: argparse.Namespace) -> Path:
    if args.output_root:
        return Path(args.output_root)
    return Path(args.base_dir) / args.data_source


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def local_file_ok(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def parse_records(records_path: Path) -> list[str]:
    return [line.strip() for line in read_text(records_path).splitlines() if line.strip()]


def subject_ids_from_records(records: Iterable[str]) -> list[str]:
    return sorted({rel.split("/")[0] for rel in records})


def candidate_urls(rel_path: str) -> list[str]:
    rel_url = rel_path.replace("\\", "/")
    return [f"{S3_BASE}/{rel_url}", f"{PHYSIONET_BASE}/{rel_url}"]


def parse_expected_size(status: int, headers: object, start_offset: int) -> int | None:
    content_range = headers.get("Content-Range")
    if content_range:
        match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
        if match and match.group(3) != "*":
            return int(match.group(3))

    content_length = headers.get("Content-Length")
    if content_length is None:
        return None

    try:
        length = int(content_length)
    except ValueError:
        return None

    if status == 206:
        return start_offset + length
    return length


def download_file(
    url: str,
    dst: Path,
    chunk_size: int,
    timeout_sec: int,
    overwrite: bool,
) -> tuple[int, bool]:
    ensure_parent(dst)
    tmp = dst.with_suffix(dst.suffix + ".part")

    if overwrite:
        if dst.exists():
            dst.unlink()
        if tmp.exists():
            tmp.unlink()

    while True:
        start_offset = tmp.stat().st_size if tmp.exists() else 0
        headers: dict[str, str] = {}
        if start_offset > 0:
            headers["Range"] = f"bytes={start_offset}-"

        request = Request(url, headers=headers)
        try:
            response = urlopen(request, timeout=timeout_sec)
        except HTTPError as exc:
            if exc.code == 416 and tmp.exists():
                tmp.unlink()
                continue
            raise

        with response:
            status = getattr(response, "status", response.getcode())
            resume_ok = start_offset > 0 and status == 206
            write_mode = "ab"

            if start_offset > 0 and status != 206:
                if tmp.exists():
                    tmp.unlink()
                start_offset = 0
                write_mode = "wb"

            expected_size = parse_expected_size(status, response.headers, start_offset)

            with tmp.open(write_mode) as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)

        final_size = tmp.stat().st_size
        if expected_size is not None and final_size != expected_size:
            raise RuntimeError(
                f"size mismatch for {dst}: expected {expected_size} bytes, got {final_size} bytes"
            )
        if final_size <= 0:
            raise RuntimeError(f"downloaded empty file: {dst}")

        tmp.replace(dst)
        return final_size, resume_ok


def download_with_fallback(task: DownloadTask, args: argparse.Namespace) -> DownloadResult:
    last_error: Exception | None = None
    chunk_size = max(1, args.chunk_size_mb) * 1024 * 1024

    for url in candidate_urls(task.rel_path):
        for attempt in range(1, max(1, args.retries) + 1):
            try:
                bytes_written, resumed = download_file(
                    url=url,
                    dst=task.dst,
                    chunk_size=chunk_size,
                    timeout_sec=max(1, args.timeout_sec),
                    overwrite=args.overwrite,
                )
                return DownloadResult(
                    rel_path=task.rel_path,
                    dst=task.dst,
                    kind=task.kind,
                    used_url=url,
                    resumed=resumed,
                    bytes_written=bytes_written,
                )
            except (HTTPError, URLError, OSError, RuntimeError) as exc:
                last_error = exc
                is_last_try = attempt >= max(1, args.retries)
                if not is_last_try:
                    time.sleep(max(0.0, args.retry_wait_sec))

    if last_error is None:
        raise RuntimeError(f"no candidate URL available for {task.rel_path}")
    raise RuntimeError(f"failed to download {task.rel_path}: {last_error}")


def download_task_batch(tasks: list[DownloadTask], args: argparse.Namespace) -> tuple[int, int, list[str]]:
    if not tasks:
        return 0, 0, []

    completed = 0
    resumed = 0
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_to_task = {executor.submit(download_with_fallback, task, args): task for task in tasks}
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                completed += 1
                resumed += 1 if result.resumed else 0
                mode = "resume" if result.resumed else "download"
                print(f"[{mode}-{result.kind}] {result.rel_path} <- {result.used_url}")
            except Exception as exc:
                failures.append(f"{task.kind}: {task.rel_path} | {exc}")
                print(f"[failed-{task.kind}] {task.rel_path} | {exc}", file=sys.stderr)

    return completed, resumed, failures


def sync_root_files(output_root: Path, args: argparse.Namespace) -> None:
    print(f"[root] downloading metadata into {output_root}")
    tasks: list[DownloadTask] = []
    for name in ROOT_FILES:
        dst = output_root / name
        if local_file_ok(dst) and not args.overwrite:
            print(f"[skip-root] {name}")
            continue
        tasks.append(DownloadTask(rel_path=name, dst=dst, kind="root"))

    completed, resumed, failures = download_task_batch(tasks, args)
    print(f"[root-summary] completed={completed} resumed={resumed} failed={len(failures)}")
    if failures:
        raise RuntimeError("metadata download failed")


def build_seizure_list_tasks(output_root: Path, subjects: Iterable[str], args: argparse.Namespace) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for subject in subjects:
        rel = f"{subject}/Seizures-list-{subject}.txt"
        dst = output_root / subject / f"Seizures-list-{subject}.txt"
        if local_file_ok(dst) and not args.overwrite:
            continue
        tasks.append(DownloadTask(rel_path=rel, dst=dst, kind="seizure-list"))
    return tasks


def build_edf_tasks(output_root: Path, records: Iterable[str], args: argparse.Namespace) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []
    for rel in records:
        dst = output_root / Path(rel)
        if local_file_ok(dst) and not args.overwrite:
            continue
        tasks.append(DownloadTask(rel_path=rel, dst=dst, kind="edf"))
    return tasks


def main() -> int:
    args = parse_args()
    output_root = resolve_output_root(args)
    output_root.mkdir(parents=True, exist_ok=True)

    print(f"Output root: {output_root}")
    print(f"Workers: {max(1, args.workers)}")
    print(f"Overwrite: {args.overwrite}")
    print("")

    sync_root_files(output_root, args)

    records_path = output_root / "RECORDS"
    if not records_path.exists():
        print(f"Missing RECORDS file after metadata download: {records_path}", file=sys.stderr)
        return 1

    records = parse_records(records_path)
    subjects = subject_ids_from_records(records)

    seizure_tasks = build_seizure_list_tasks(output_root, subjects, args)
    edf_tasks = build_edf_tasks(output_root, records, args)

    print(f"Subjects in RECORDS: {len(subjects)}")
    print(f"EDF files in RECORDS: {len(records)}")
    print(f"Pending seizure lists: {len(seizure_tasks)}")
    print(f"Pending EDF files: {len(edf_tasks)}")
    print("")

    seizure_completed, seizure_resumed, seizure_failures = download_task_batch(seizure_tasks, args)
    edf_completed, edf_resumed, edf_failures = download_task_batch(edf_tasks, args)

    total_failures = seizure_failures + edf_failures
    print("")
    print("Download summary")
    print(f"  output_root: {output_root}")
    print(f"  total_subjects: {len(subjects)}")
    print(f"  total_edf_in_records: {len(records)}")
    print(f"  seizure_lists_downloaded: {seizure_completed}")
    print(f"  seizure_lists_resumed: {seizure_resumed}")
    print(f"  edf_downloaded: {edf_completed}")
    print(f"  edf_resumed: {edf_resumed}")
    print(f"  failed_downloads: {len(total_failures)}")

    if total_failures:
        print("")
        print("Failed downloads")
        for item in total_failures:
            print(f"  {item}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
