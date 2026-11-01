from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.request
import uuid
import zipfile


REPOSITORY = "MULTIGID/AudioLib"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_PACKAGE_BYTES = 500 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    download_url: str
    digest: str
    size: int
    page_url: str
    notes: str


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if match is None:
        raise UpdateError(f"Некоректний номер версії: {value}")
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate: str, current: str) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _create_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if sys.platform != "win32" or not hasattr(ssl, "enum_certificates"):
        return context
    certificates: list[str] = []
    try:
        for certificate, encoding, _trust in ssl.enum_certificates("ROOT"):
            if encoding == "x509_asn":
                certificates.append(ssl.DER_cert_to_PEM_cert(certificate))
        if certificates:
            context.load_verify_locations(cadata="\n".join(certificates))
    except (OSError, ssl.SSLError, ValueError) as error:
        raise UpdateError(
            f"Не вдалося завантажити сертифікати Windows: {error}"
        ) from error
    if not certificates:
        raise UpdateError("У сховищі Windows не знайдено кореневих сертифікатів.")
    return context


class GitHubUpdateManager:
    def __init__(self, current_version: str) -> None:
        self.current_version = current_version
        self._updates_root = Path(tempfile.gettempdir()) / "AudioLibUpdates"
        self._ssl_context: ssl.SSLContext | None = None

    def _context(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            self._ssl_context = _create_ssl_context()
        return self._ssl_context

    @staticmethod
    def _request(url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "AudioLib-Updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def check_latest(self) -> ReleaseInfo | None:
        try:
            with urllib.request.urlopen(
                self._request(LATEST_RELEASE_API),
                timeout=20,
                context=self._context(),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise UpdateError(f"Не вдалося перевірити оновлення: {error}") from error

        if not isinstance(payload, dict):
            raise UpdateError("GitHub повернув некоректні дані релізу.")
        tag = str(payload.get("tag_name", ""))
        version = tag.removeprefix("v")
        _version_tuple(version)
        if not is_newer_version(version, self.current_version):
            return None

        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("У релізі GitHub немає файлів для оновлення.")
        expected_name = f"AudioLib_{version}.zip".casefold()
        asset = next(
            (
                item
                for item in assets
                if isinstance(item, dict)
                and str(item.get("name", "")).casefold() == expected_name
            ),
            None,
        )
        if asset is None:
            raise UpdateError(f"У релізі не знайдено AudioLib_{version}.zip.")

        download_url = str(asset.get("browser_download_url", ""))
        if not download_url.startswith(
            f"https://github.com/{REPOSITORY}/releases/download/"
        ):
            raise UpdateError("GitHub повернув неприпустиму адресу завантаження.")
        digest = str(asset.get("digest", ""))
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise UpdateError("Для релізу відсутня контрольна сума SHA-256.")
        try:
            size = int(asset.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        if size <= 0 or size > MAX_PACKAGE_BYTES:
            raise UpdateError("Розмір пакета оновлення є некоректним.")

        return ReleaseInfo(
            version=version,
            tag=tag,
            download_url=download_url,
            digest=digest.split(":", 1)[1].lower(),
            size=size,
            page_url=str(payload.get("html_url", "")),
            notes=str(payload.get("body", "")),
        )

    def download(
        self,
        release: ReleaseInfo,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        work_dir = self._updates_root / f"{release.version}-{uuid.uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=False)
        temporary = work_dir / "AudioLib_update.zip.part"
        destination = work_dir / f"AudioLib_{release.version}.zip"
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with urllib.request.urlopen(
                self._request(release.download_url),
                timeout=30,
                context=self._context(),
            ) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_PACKAGE_BYTES:
                        raise UpdateError("Пакет оновлення перевищує допустимий розмір.")
                    output.write(chunk)
                    digest.update(chunk)
                    if progress is not None:
                        progress(downloaded, release.size)
            if downloaded != release.size:
                raise UpdateError("Пакет оновлення завантажено не повністю.")
            if digest.hexdigest().lower() != release.digest.lower():
                raise UpdateError("Контрольна сума пакета оновлення не збігається.")
            temporary.replace(destination)
            self._validate_package(destination)
            return destination
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

    @staticmethod
    def _validate_package(package_path: Path) -> None:
        try:
            with zipfile.ZipFile(package_path) as archive:
                infos = archive.infolist()
                if sum(info.file_size for info in infos) > MAX_UNPACKED_BYTES:
                    raise UpdateError("Розпакований пакет оновлення є завеликим.")
                files = {
                    info.filename.replace("\\", "/")
                    for info in infos
                    if not info.is_dir()
                }
                for name in files:
                    path = Path(name)
                    if path.is_absolute() or ".." in path.parts:
                        raise UpdateError("Пакет оновлення містить небезпечний шлях.")
                    if name.casefold().startswith("audiolib/data/"):
                        raise UpdateError("Пакет оновлення не повинен містити дані користувача.")
                if "AudioLib/AudioLib.exe" not in files:
                    raise UpdateError("У пакеті оновлення відсутній AudioLib.exe.")
                if not any(
                    name.startswith("AudioLib/_internal/") for name in files
                ):
                    raise UpdateError("Пакет оновлення не містить службових файлів.")
        except (OSError, zipfile.BadZipFile) as error:
            raise UpdateError(f"Не вдалося перевірити пакет оновлення: {error}") from error

    def prepare_installer(
        self,
        package_path: Path,
        install_dir: Path,
        process_id: int,
    ) -> list[str]:
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            raise UpdateError(
                "Автоматичне встановлення доступне лише у зібраній версії AudioLib."
            )
        package_path = package_path.resolve()
        install_dir = install_dir.resolve()
        work_dir = package_path.parent
        helper_path = work_dir / "apply_update.ps1"
        helper_path.write_text(_POWERSHELL_HELPER, encoding="utf-8-sig")
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(helper_path),
            "-TargetPid",
            str(process_id),
            "-PackagePath",
            str(package_path),
            "-InstallDir",
            str(install_dir),
            "-WorkDir",
            str(work_dir),
        ]

    def cleanup_stale_downloads(self) -> None:
        try:
            entries = list(self._updates_root.iterdir())
        except OSError:
            return
        cutoff = time.time() - 24 * 60 * 60
        for entry in entries:
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass


_POWERSHELL_HELPER = r'''param(
    [Parameter(Mandatory=$true)][int]$TargetPid,
    [Parameter(Mandatory=$true)][string]$PackagePath,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$WorkDir
)
$ErrorActionPreference = "Stop"
$logPath = Join-Path $InstallDir "data\update.log"
$extractDir = Join-Path $WorkDir "extracted"
$backupDir = Join-Path $WorkDir "backup"
$newRoot = Join-Path $extractDir "AudioLib"
$oldExe = Join-Path $InstallDir "AudioLib.exe"
$oldInternal = Join-Path $InstallDir "_internal"
$backupExe = Join-Path $backupDir "AudioLib.exe"
$backupInternal = Join-Path $backupDir "_internal"
$newExe = Join-Path $newRoot "AudioLib.exe"
$newInternal = Join-Path $newRoot "_internal"
$exeBackedUp = $false
$internalBackedUp = $false
$newExeInstalled = $false
$newInternalInstalled = $false

try {
    Wait-Process -Id $TargetPid -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Expand-Archive -LiteralPath $PackagePath -DestinationPath $extractDir -Force
    if (-not (Test-Path -LiteralPath $newExe) -or -not (Test-Path -LiteralPath $newInternal)) {
        throw "The update package has an invalid structure."
    }
    if (Test-Path -LiteralPath $oldExe) {
        Move-Item -LiteralPath $oldExe -Destination $backupExe -Force
        $exeBackedUp = $true
    }
    if (Test-Path -LiteralPath $oldInternal) {
        Move-Item -LiteralPath $oldInternal -Destination $backupInternal -Force
        $internalBackedUp = $true
    }
    Move-Item -LiteralPath $newInternal -Destination $oldInternal -Force
    $newInternalInstalled = $true
    Move-Item -LiteralPath $newExe -Destination $oldExe -Force
    $newExeInstalled = $true
    Start-Process -FilePath $oldExe -WorkingDirectory $InstallDir
    Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PackagePath -Force -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $message = "{0} UPDATE_FAILED {1}" -f (Get-Date -Format s), $_.Exception.Message
    New-Item -ItemType Directory -Path (Split-Path $logPath) -Force | Out-Null
    Add-Content -LiteralPath $logPath -Value $message -Encoding UTF8
    if ($newExeInstalled) {
        Remove-Item -LiteralPath $oldExe -Force -ErrorAction SilentlyContinue
    }
    if ($newInternalInstalled) {
        Remove-Item -LiteralPath $oldInternal -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($exeBackedUp -and (Test-Path -LiteralPath $backupExe)) {
        Move-Item -LiteralPath $backupExe -Destination $oldExe -Force
    }
    if ($internalBackedUp -and (Test-Path -LiteralPath $backupInternal)) {
        Move-Item -LiteralPath $backupInternal -Destination $oldInternal -Force
    }
    if (Test-Path -LiteralPath $oldExe) {
        Start-Process -FilePath $oldExe -WorkingDirectory $InstallDir
    }
    exit 1
}
'''
