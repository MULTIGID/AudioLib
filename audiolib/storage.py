from __future__ import annotations

import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from mutagen import File as MutagenFile


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".wma", ".flac", ".ogg"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    photo_path: str | None


@dataclass(frozen=True)
class Track:
    id: int
    person_id: int
    title: str
    file_path: str
    source_name: str
    file_size: int | None
    duration_ms: int | None
    created_at: str
    loudness_db: float | None = None
    normalization_gain: float | None = None


class LibraryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.people_dir = self.data_dir / "people"
        self.db_path = self.data_dir / "library.sqlite3"
        self.people_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE,
                photo_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                title TEXT NOT NULL COLLATE NOCASE,
                file_path TEXT NOT NULL UNIQUE,
                source_name TEXT NOT NULL,
                file_size INTEGER,
                duration_ms INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_tracks_person ON tracks(person_id);
            CREATE TABLE IF NOT EXISTS favorites (
                track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
                hotkey TEXT,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self._ensure_track_columns()
        self._ensure_favorite_columns()
        self.connection.commit()

    def _ensure_track_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(tracks)").fetchall()
        }
        if "file_size" not in columns:
            self.connection.execute("ALTER TABLE tracks ADD COLUMN file_size INTEGER")
        if "duration_ms" not in columns:
            self.connection.execute("ALTER TABLE tracks ADD COLUMN duration_ms INTEGER")
        if "loudness_db" not in columns:
            self.connection.execute("ALTER TABLE tracks ADD COLUMN loudness_db REAL")
        if "normalization_gain" not in columns:
            self.connection.execute("ALTER TABLE tracks ADD COLUMN normalization_gain REAL")

    def _ensure_favorite_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(favorites)").fetchall()
        }
        if "hotkey" not in columns:
            self.connection.execute("ALTER TABLE favorites ADD COLUMN hotkey TEXT")
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_favorites_hotkey "
            "ON favorites(hotkey COLLATE NOCASE) WHERE hotkey IS NOT NULL AND hotkey <> ''"
        )

    def close(self) -> None:
        self.connection.close()

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def absolute(self, relative_path: str | None) -> Path | None:
        if not relative_path:
            return None
        return self.root / Path(relative_path)

    def list_people(self, query: str = "") -> list[Person]:
        if query.strip():
            rows = self.connection.execute(
                "SELECT id, name, photo_path FROM people WHERE name LIKE ? ORDER BY name",
                (f"%{query.strip()}%",),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT id, name, photo_path FROM people ORDER BY name"
            ).fetchall()
        return [Person(**dict(row)) for row in rows]

    def get_person(self, person_id: int) -> Person | None:
        row = self.connection.execute(
            "SELECT id, name, photo_path FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        return Person(**dict(row)) if row else None

    def track_counts_by_person(self) -> dict[int, int]:
        rows = self.connection.execute(
            "SELECT person_id, COUNT(*) AS count FROM tracks GROUP BY person_id"
        ).fetchall()
        return {int(row["person_id"]): int(row["count"]) for row in rows}

    def add_person(self, name: str, photo_source: Path | None = None) -> Person:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Ім'я не може бути порожнім")
        cursor = self.connection.execute("INSERT INTO people(name) VALUES (?)", (clean_name,))
        person_id = int(cursor.lastrowid)
        self.connection.commit()
        if photo_source:
            self.set_person_photo(person_id, photo_source)
        person = self.get_person(person_id)
        if person is None:
            raise RuntimeError("Не вдалося створити людину")
        return person

    def rename_person(self, person_id: int, name: str) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Ім'я не може бути порожнім")
        self.connection.execute("UPDATE people SET name = ? WHERE id = ?", (clean_name, person_id))
        self.connection.commit()

    def set_person_photo(self, person_id: int, source: Path) -> None:
        if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("Оберіть підтримуваний файл зображення")
        person_dir = self.people_dir / str(person_id)
        person_dir.mkdir(parents=True, exist_ok=True)
        old = self.get_person(person_id)
        target = person_dir / f"photo{source.suffix.lower()}"
        shutil.copy2(source, target)
        if old and old.photo_path:
            old_path = self.absolute(old.photo_path)
            if old_path and old_path != target:
                old_path.unlink(missing_ok=True)
        self.connection.execute(
            "UPDATE people SET photo_path = ? WHERE id = ?",
            (self._relative(target), person_id),
        )
        self.connection.commit()

    def delete_person(self, person_id: int) -> None:
        self.connection.execute("DELETE FROM people WHERE id = ?", (person_id,))
        self.connection.commit()
        person_dir = self.people_dir / str(person_id)
        if person_dir.exists():
            shutil.rmtree(person_dir)

    def _read_metadata(self, path: Path) -> tuple[int, int]:
        try:
            file_size = path.stat().st_size
        except OSError:
            return 0, 0
        duration_ms = 0
        try:
            audio = MutagenFile(path)
            if audio is not None and getattr(audio, "info", None) is not None:
                duration_ms = max(0, int(float(audio.info.length) * 1000))
        except Exception:
            pass
        return file_size, duration_ms

    def refresh_missing_metadata(self) -> int:
        """Fill legacy metadata without making ordinary list queries touch files."""
        rows = self.connection.execute(
            "SELECT id, file_path FROM tracks "
            "WHERE file_size IS NULL OR duration_ms IS NULL"
        ).fetchall()
        for row in rows:
            path = self.absolute(str(row["file_path"]))
            file_size, duration_ms = self._read_metadata(path) if path else (0, 0)
            self.connection.execute(
                "UPDATE tracks SET file_size = ?, duration_ms = ? WHERE id = ?",
                (file_size, duration_ms, int(row["id"])),
            )
        if rows:
            self.connection.commit()
        return len(rows)

    def list_tracks(
        self,
        person_id: int,
        query: str = "",
        sort_by: str = "title",
        descending: bool = False,
    ) -> list[Track]:
        sort_columns = {
            "title": "title COLLATE NOCASE",
            "source": "source_name COLLATE NOCASE",
            "duration": "duration_ms",
            "size": "file_size",
            "added": "created_at",
        }
        sort_column = sort_columns.get(sort_by, sort_columns["title"])
        direction = "DESC" if descending else "ASC"
        selected_columns = (
            "id, person_id, title, file_path, source_name, "
            "file_size, duration_ms, created_at, loudness_db, normalization_gain"
        )
        params: tuple[object, ...]
        if query.strip():
            sql = (
                f"SELECT {selected_columns} FROM tracks "
                "WHERE person_id = ? AND (title LIKE ? OR source_name LIKE ?) "
                f"ORDER BY ({sort_column} IS NULL), {sort_column} {direction}, title COLLATE NOCASE ASC"
            )
            pattern = f"%{query.strip()}%"
            params = (person_id, pattern, pattern)
        else:
            sql = (
                f"SELECT {selected_columns} FROM tracks WHERE person_id = ? "
                f"ORDER BY ({sort_column} IS NULL), {sort_column} {direction}, title COLLATE NOCASE ASC"
            )
            params = (person_id,)
        return [Track(**dict(row)) for row in self.connection.execute(sql, params).fetchall()]

    def get_track(self, track_id: int) -> Track | None:
        row = self.connection.execute(
            "SELECT id, person_id, title, file_path, source_name, "
            "file_size, duration_ms, created_at, loudness_db, normalization_gain "
            "FROM tracks WHERE id = ?",
            (track_id,),
        ).fetchone()
        return Track(**dict(row)) if row else None

    def favorite_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM favorites").fetchone()
        return int(row["count"] if row else 0)

    def favorite_track_ids(self) -> set[int]:
        return {
            int(row["track_id"])
            for row in self.connection.execute("SELECT track_id FROM favorites").fetchall()
        }

    def favorite_hotkeys(self) -> dict[int, str]:
        rows = self.connection.execute(
            "SELECT track_id, hotkey FROM favorites "
            "WHERE hotkey IS NOT NULL AND hotkey <> ''"
        ).fetchall()
        return {int(row["track_id"]): str(row["hotkey"]) for row in rows}

    def set_favorite_hotkey(self, track_id: int, hotkey: str | None) -> None:
        exists = self.connection.execute(
            "SELECT 1 FROM favorites WHERE track_id = ?", (track_id,)
        ).fetchone()
        if exists is None:
            raise ValueError("Гарячу клавішу можна призначити лише запису з Favorites")
        clean_hotkey = hotkey.strip() if hotkey else None
        try:
            self.connection.execute(
                "UPDATE favorites SET hotkey = ? WHERE track_id = ?",
                (clean_hotkey, track_id),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            raise ValueError("Ця гаряча клавіша вже призначена іншому запису") from error

    def set_favorite(self, track_id: int, favorite: bool, limit: int = 15) -> bool:
        track = self.get_track(track_id)
        if track is None:
            raise ValueError("Аудіозапис не знайдено")
        exists = self.connection.execute(
            "SELECT 1 FROM favorites WHERE track_id = ?", (track_id,)
        ).fetchone() is not None
        if favorite and not exists:
            if self.favorite_count() >= limit:
                raise ValueError(f"У Favorites можна додати не більше {limit} записів")
            self.connection.execute(
                "INSERT INTO favorites(track_id) VALUES (?)", (track_id,)
            )
        elif not favorite and exists:
            self.connection.execute("DELETE FROM favorites WHERE track_id = ?", (track_id,))
        self.connection.commit()
        return favorite

    def list_favorites(
        self,
        query: str = "",
        sort_by: str = "title",
        descending: bool = False,
    ) -> list[Track]:
        sort_columns = {
            "title": "t.title COLLATE NOCASE",
            "source": "t.source_name COLLATE NOCASE",
            "duration": "t.duration_ms",
            "size": "t.file_size",
            "added": "f.added_at",
        }
        sort_column = sort_columns.get(sort_by, sort_columns["title"])
        direction = "DESC" if descending else "ASC"
        selected_columns = (
            "t.id, t.person_id, t.title, t.file_path, t.source_name, "
            "t.file_size, t.duration_ms, t.created_at, "
            "t.loudness_db, t.normalization_gain"
        )
        params: tuple[object, ...] = ()
        where = ""
        if query.strip():
            where = "WHERE t.title LIKE ? OR t.source_name LIKE ?"
            pattern = f"%{query.strip()}%"
            params = (pattern, pattern)
        sql = (
            f"SELECT {selected_columns} FROM tracks t "
            "JOIN favorites f ON f.track_id = t.id "
            f"{where} ORDER BY ({sort_column} IS NULL), {sort_column} {direction}, "
            "t.title COLLATE NOCASE ASC"
        )
        return [Track(**dict(row)) for row in self.connection.execute(sql, params).fetchall()]

    def add_tracks(self, person_id: int, sources: Iterable[Path]) -> list[Track]:
        audio_dir = self.people_dir / str(person_id) / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        added: list[Track] = []
        for source in sources:
            if not source.is_file() or source.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            unique_name = f"{uuid.uuid4().hex}{source.suffix.lower()}"
            target = audio_dir / unique_name
            shutil.copy2(source, target)
            file_size, duration_ms = self._read_metadata(target)
            cursor = self.connection.execute(
                "INSERT INTO tracks(person_id, title, file_path, source_name, file_size, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (person_id, source.stem, self._relative(target), source.name, file_size, duration_ms),
            )
            track_id = int(cursor.lastrowid)
            created_at = self.connection.execute(
                "SELECT created_at FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()["created_at"]
            added.append(
                Track(
                    track_id,
                    person_id,
                    source.stem,
                    self._relative(target),
                    source.name,
                    file_size,
                    duration_ms,
                    str(created_at),
                    None,
                    None,
                )
            )
        self.connection.commit()
        return added

    def set_track_normalization(
        self, track_id: int, loudness_db: float, normalization_gain: float
    ) -> None:
        self.connection.execute(
            "UPDATE tracks SET loudness_db = ?, normalization_gain = ? WHERE id = ?",
            (float(loudness_db), float(normalization_gain), track_id),
        )
        self.connection.commit()

    def rename_track(self, track_id: int, title: str) -> None:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Назва не може бути порожньою")
        self.connection.execute("UPDATE tracks SET title = ? WHERE id = ?", (clean_title, track_id))
        self.connection.commit()

    def delete_track(self, track_id: int) -> None:
        track = self.get_track(track_id)
        if not track:
            return
        self.connection.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        self.connection.commit()
        path = self.absolute(track.file_path)
        if path:
            path.unlink(missing_ok=True)
