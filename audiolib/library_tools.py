from __future__ import annotations

from datetime import datetime
import json
import shutil
import sqlite3
import uuid
import zipfile
from pathlib import Path
from typing import Callable


class LibraryToolError(RuntimeError):
    pass


ProgressCallback = Callable[[str, int, int], None]


def _report_progress(
    callback: ProgressCallback | None,
    stage: str,
    completed: int,
    total: int,
) -> None:
    if callback is not None:
        callback(stage, completed, total)


def create_backup(
    root: Path,
    destination: Path | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    data_dir = root / "data"
    database = data_dir / "library.sqlite3"
    if not database.is_file():
        raise LibraryToolError("Базу бібліотеки не знайдено")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if destination is None:
        backup_dir = root / "Backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"AudioLib_backup_{stamp}.zip"
    else:
        destination = destination.with_suffix(".zip")
        destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        files = [
            path
            for path in data_dir.rglob("*")
            if path.is_file() and path.name not in {"audio_routing.log"}
        ]
        total = sum(path.stat().st_size for path in files)
        completed = 0
        _report_progress(progress, "preparing", completed, total)
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "backup_manifest.json",
                json.dumps({"format": 1, "created_at": datetime.now().isoformat()}),
            )
            for path in files:
                archive_name = (
                    Path("data") / path.relative_to(data_dir)
                ).as_posix()
                info = zipfile.ZipInfo.from_file(path, archive_name)
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                    while chunk := input_stream.read(1024 * 1024):
                        output_stream.write(chunk)
                        completed += len(chunk)
                        _report_progress(progress, "copying", completed, total)
        _report_progress(progress, "finalizing", total, total)
        temporary.replace(destination)
    except (OSError, zipfile.BadZipFile) as error:
        temporary.unlink(missing_ok=True)
        raise LibraryToolError(f"Не вдалося створити резервну копію: {error}") from error
    return destination


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members: list[zipfile.ZipInfo] = []
    for item in archive.infolist():
        path = Path(item.filename.replace("\\", "/"))
        if item.is_dir() or path.name == "backup_manifest.json":
            continue
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "data":
            raise LibraryToolError("Архів не є резервною копією AudioLib")
        members.append(item)
    if not any(Path(item.filename).as_posix() == "data/library.sqlite3" for item in members):
        raise LibraryToolError("В архіві немає бази бібліотеки")
    return members


def restore_backup(
    root: Path,
    source: Path,
    progress: ProgressCallback | None = None,
) -> None:
    if not source.is_file():
        raise LibraryToolError("Файл резервної копії не знайдено")
    stage = root / f".restore_{uuid.uuid4().hex}"
    previous = root / f".data_before_restore_{uuid.uuid4().hex}"
    data_dir = root / "data"
    try:
        stage.mkdir()
        with zipfile.ZipFile(source) as archive:
            members = _safe_members(archive)
            total = sum(item.file_size for item in members)
            completed = 0
            _report_progress(progress, "preparing", completed, total)
            for item in members:
                relative = Path(item.filename).relative_to("data")
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as input_stream, destination.open("wb") as output_stream:
                    while chunk := input_stream.read(1024 * 1024):
                        output_stream.write(chunk)
                        completed += len(chunk)
                        _report_progress(progress, "copying", completed, total)
        _report_progress(progress, "verifying", 0, 0)
        database = stage / "library.sqlite3"
        with sqlite3.connect(database) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).casefold() != "ok":
                raise LibraryToolError("Перевірка бази даних у копії завершилася помилкою")
        _report_progress(progress, "applying", 0, 0)
        if data_dir.exists():
            data_dir.replace(previous)
        shutil.copytree(stage, data_dir)
        shutil.rmtree(previous, ignore_errors=True)
        _report_progress(progress, "finalizing", total, total)
    except Exception as error:
        if previous.exists():
            shutil.rmtree(data_dir, ignore_errors=True)
            try:
                previous.replace(data_dir)
            except OSError:
                shutil.copytree(previous, data_dir)
        if isinstance(error, LibraryToolError):
            raise
        raise LibraryToolError(f"Не вдалося відновити бібліотеку: {error}") from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)
