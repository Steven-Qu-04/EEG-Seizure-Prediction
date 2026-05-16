from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def local_file_ok(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def copy_if_needed(src: Path, dst: Path, overwrite: bool = False) -> bool:
    if not src.exists():
        return False
    if dst.exists() and not overwrite:
        src_size = src.stat().st_size
        dst_size = dst.stat().st_size
        if src_size == dst_size and dst_size > 0:
            return False
    ensure_parent(dst)
    shutil.copy2(src, dst)
    return True


def download_file(url: str, dst: Path, chunk_size: int = 1024 * 1024) -> bool:
    ensure_parent(dst)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        with urlopen(url) as resp, tmp.open("wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
        if tmp.stat().st_size <= 0:
            raise RuntimeError("downloaded empty file")
        tmp.replace(dst)
        return True
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def candidate_urls(rel_path: str) -> list[str]:
    rel_url = rel_path.replace("\\", "/")
    return [f"{S3_BASE}/{rel_url}", f"{PHYSIONET_BASE}/{rel_url}"]


def download_with_fallback(rel_path: str, dst: Path) -> str:
    last_error: Exception | None = None
    for url in candidate_urls(rel_path):
        try:
            download_file(url, dst)
            return url
        except (HTTPError, URLError, RuntimeError) as exc:
            last_error = exc
    if last_error is None:
        raise RuntimeError(f"no candidate url for {rel_path}")
    raise RuntimeError(f"failed to download {rel_path}: {last_error}")


def run_download_task(task: DownloadTask) -> DownloadResult:
    used_url = download_with_fallback(task.rel_path, task.dst)
    return DownloadResult(
        rel_path=task.rel_path,
        dst=task.dst,
        kind=task.kind,
        used_url=used_url,
    )


def parse_records(records_path: Path) -> list[str]:
    return [line.strip() for line in read_text(records_path).splitlines() if line.strip()]


def subject_ids_from_records(records: Iterable[str]) -> list[str]:
    return sorted({rel.split("/")[0] for rel in records})


def sync_root_files(source_root: Path, target_root: Path, verbose: bool = True) -> None:
    for name in ROOT_FILES:
        src = source_root / name
        dst = target_root / name
        if src.exists():
            changed = copy_if_needed(src, dst)
            if verbose:
                print(f"[copy-root] {name} -> {'copied' if changed else 'skipped'}")
        elif not local_file_ok(dst):
            used = download_with_fallback(name, dst)
            if verbose:
                print(f"[download-root] {name} <- {used}")


def sync_seizure_lists(
    source_root: Path,
    target_root: Path,
    subjects: Iterable[str],
    workers: int,
    verbose: bool = True,
) -> None:
    pending: list[DownloadTask] = []
    for subject in subjects:
        rel = f"{subject}/Seizures-list-{subject}.txt"
        src = source_root / subject / f"Seizures-list-{subject}.txt"
        dst = target_root / subject / f"Seizures-list-{subject}.txt"
        if src.exists():
            changed = copy_if_needed(src, dst)
            if verbose:
                print(f"[copy-seizure-list] {rel} -> {'copied' if changed else 'skipped'}")
        elif not local_file_ok(dst):
            pending.append(DownloadTask(rel_path=rel, dst=dst, kind="seizure-list"))

    if verbose and pending:
        print(f"[queue-seizure-list] {len(pending)} files with {max(1, workers)} workers")
    download_tasks_in_parallel(pending, workers=workers, verbose=verbose)


def download_tasks_in_parallel(tasks: list[DownloadTask], workers: int, verbose: bool = True) -> tuple[int, list[str]]:
    if not tasks:
        return 0, []

    completed = 0
    failures: list[str] = []
    max_workers = max(1, workers)
    if verbose:
        print(f"[parallel-download] {len(tasks)} tasks with {max_workers} workers")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {executor.submit(run_download_task, task): task for task in tasks}
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                completed += 1
                if verbose:
                    print(f"[download-{result.kind}] {result.rel_path} <- {result.used_url}")
            except Exception as exc:
                failures.append(f"{task.kind}: {task.rel_path} | {exc}")
                if verbose:
                    print(f"[failed-{task.kind}] {task.rel_path} | {exc}", file=sys.stderr)
    return completed, failures


def sync_edf_files(
    source_root: Path,
    target_root: Path,
    records: Iterable[str],
    workers: int,
    verbose: bool = True,
) -> None:
    total = 0
    copied = 0
    downloaded = 0
    skipped = 0
    pending: list[DownloadTask] = []

    for rel in records:
        total += 1
        src = source_root / Path(rel)
        dst = target_root / Path(rel)

        if src.exists():
            changed = copy_if_needed(src, dst)
            if changed:
                copied += 1
                if verbose:
                    print(f"[copy-edf] {rel}")
            else:
                skipped += 1
                if verbose:
                    print(f"[skip-edf] {rel}")
            continue

        if local_file_ok(dst):
            skipped += 1
            if verbose:
                print(f"[skip-edf] {rel}")
            continue

        pending.append(DownloadTask(rel_path=rel, dst=dst, kind="edf"))

    if verbose and pending:
        print(f"[queue-edf] {len(pending)} files with {max(1, workers)} workers")
    downloaded, failures = download_tasks_in_parallel(pending, workers=workers, verbose=verbose)

    print("")
    print("Sync summary")
    print(f"  total_edf: {total}")
    print(f"  copied_from_local: {copied}")
    print(f"  downloaded_missing: {downloaded}")
    print(f"  skipped_existing: {skipped}")
    print(f"  failed_downloads: {len(failures)}")
    if failures:
        print("")
        print("Failed downloads")
        for item in failures:
            print(f"  {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy existing SIENA files to a target directory and download the remaining files."
    )
    parser.add_argument(
        "--source-root",
        default="data/raw/siena",
        help="Current local SIENA dataset root in the workspace.",
    )
    parser.add_argument(
        "--target-root",
        default=r"F:\data\siena",
        help="Target SIENA dataset root on the destination drive.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel download workers.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_root = Path(args.source_root)
    target_root = Path(args.target_root)

    records_path = source_root / "RECORDS"
    if not records_path.exists():
        print(f"Missing RECORDS file: {records_path}", file=sys.stderr)
        return 1

    records = parse_records(records_path)
    subjects = subject_ids_from_records(records)

    print(f"Source root: {source_root.resolve()}")
    print(f"Target root: {target_root}")
    print(f"Subjects in RECORDS: {len(subjects)}")
    print(f"EDF files in RECORDS: {len(records)}")
    print("")

    sync_root_files(source_root, target_root)
    sync_seizure_lists(source_root, target_root, subjects, workers=args.workers)
    sync_edf_files(source_root, target_root, records, workers=args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
