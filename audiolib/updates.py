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
LATEST_MANIFEST_URL = (
    f"https://github.com/{REPOSITORY}/releases/latest/download/update.json"
)
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
    if sys.platform == "win32":
        try:
            import truststore

            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            pass
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

    def _release_from_manifest(self, payload: object) -> ReleaseInfo | None:
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise UpdateError("GitHub повернув некоректний маніфест оновлення.")
        version = str(payload.get("version", ""))
        _version_tuple(version)
        tag = str(payload.get("tag", f"v{version}"))
        if tag != f"v{version}":
            raise UpdateError("Версія в маніфесті оновлення не збігається з тегом.")
        if not is_newer_version(version, self.current_version):
            return None
        download_url = str(payload.get("download_url", ""))
        expected_url = (
            f"https://github.com/{REPOSITORY}/releases/download/"
            f"{tag}/AudioLib_{version}.zip"
        )
        if download_url != expected_url:
            raise UpdateError("Маніфест містить неприпустиму адресу завантаження.")
        digest = str(payload.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError("Маніфест не містить коректної суми SHA-256.")
        try:
            size = int(payload.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        if size <= 0 or size > MAX_PACKAGE_BYTES:
            raise UpdateError("Розмір пакета в маніфесті є некоректним.")
        page_url = str(payload.get("page_url", ""))
        if page_url != f"https://github.com/{REPOSITORY}/releases/tag/{tag}":
            raise UpdateError("Маніфест містить неприпустиму адресу релізу.")
        return ReleaseInfo(
            version=version,
            tag=tag,
            download_url=download_url,
            digest=digest,
            size=size,
            page_url=page_url,
            notes=str(payload.get("notes", "")),
        )

    def _check_latest_manifest(self) -> ReleaseInfo | None:
        # GitHub can briefly cache the /latest redirect after a new release.
        # A unique query keeps clients from accepting the previous release's
        # otherwise valid manifest and missing an available update.
        manifest_url = f"{LATEST_MANIFEST_URL}?check={time.time_ns()}"
        with urllib.request.urlopen(
            self._request(manifest_url),
            timeout=20,
            context=self._context(),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return self._release_from_manifest(payload)

    def _check_latest_api(self) -> ReleaseInfo | None:
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

    def check_latest(self) -> ReleaseInfo | None:
        try:
            return self._check_latest_manifest()
        except (OSError, ValueError, urllib.error.URLError, UpdateError) as manifest_error:
            try:
                return self._check_latest_api()
            except UpdateError as api_error:
                raise UpdateError(
                    "Не вдалося перевірити оновлення через маніфест або GitHub API: "
                    f"{manifest_error}; {api_error}"
                ) from api_error

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
                    normalized = name.casefold()
                    if normalized.startswith("audiolib/data/") or normalized.startswith("data/"):
                        raise UpdateError("Пакет оновлення не повинен містити дані користувача.")
                wrapped = (
                    "AudioLib/AudioLib.exe" in files
                    and any(name.startswith("AudioLib/_internal/") for name in files)
                )
                flat = (
                    "AudioLib.exe" in files
                    and any(name.startswith("_internal/") for name in files)
                )
                if not wrapped and not flat:
                    raise UpdateError("У пакеті оновлення відсутній AudioLib.exe.")
        except (OSError, zipfile.BadZipFile) as error:
            raise UpdateError(f"Не вдалося перевірити пакет оновлення: {error}") from error

    def prepare_installer(
        self,
        package_path: Path,
        install_dir: Path,
        process_id: int,
        progress: Callable[[int, int], None] | None = None,
        language: str = "uk",
    ) -> list[str]:
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            raise UpdateError(
                "Автоматичне встановлення доступне лише у зібраній версії AudioLib."
            )
        package_path = package_path.resolve()
        install_dir = install_dir.resolve()
        work_dir = package_path.parent
        extract_dir = work_dir / "extracted"
        shutil.rmtree(extract_dir, ignore_errors=True)
        self._validate_package(package_path)
        try:
            with zipfile.ZipFile(package_path) as archive:
                infos = archive.infolist()
                total = sum(info.file_size for info in infos if not info.is_dir())
                completed = 0
                if progress is not None:
                    progress(0, total)
                for info in infos:
                    relative = Path(info.filename.replace("\\", "/"))
                    destination = extract_dir.joinpath(*relative.parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            completed += len(chunk)
                            if progress is not None:
                                progress(completed, total)
            wrapped_root = extract_dir / "AudioLib"
            new_root = wrapped_root if (wrapped_root / "AudioLib.exe").is_file() else extract_dir
            if not (new_root / "AudioLib.exe").is_file() or not (new_root / "_internal").is_dir():
                raise UpdateError("Пакет оновлення має некоректну структуру.")
        except Exception:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise
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
            "-Language",
            "en" if language == "en" else "uk",
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
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [ValidateSet("uk", "en")][string]$Language = "uk"
)
$ErrorActionPreference = "Stop"
$logPath = Join-Path $InstallDir "data\update.log"
$extractDir = Join-Path $WorkDir "extracted"
$backupDir = Join-Path $WorkDir "backup"
$readyFile = Join-Path $WorkDir "startup_ready"
$oldExe = Join-Path $InstallDir "AudioLib.exe"
$oldInternal = Join-Path $InstallDir "_internal"
$backupExe = Join-Path $backupDir "AudioLib.exe"
$backupInternal = Join-Path $backupDir "_internal"
$exeBackedUp = $false
$internalBackedUp = $false
$newExeInstalled = $false
$newInternalInstalled = $false
$startedProcess = $null
$form = $null

function LocalText([string]$Ukrainian, [string]$English) {
    if ($Language -eq "en") { return $English }
    return $Ukrainian
}

function Refresh-Window {
    if ($null -ne $form -and -not $form.IsDisposed) {
        [System.Windows.Forms.Application]::DoEvents()
    }
}

function Set-UpdateStatus([string]$Text, [string]$Detail) {
    if ($null -ne $script:statusLabel) { $script:statusLabel.Text = $Text }
    if ($null -ne $script:detailLabel) { $script:detailLabel.Text = $Detail }
    Refresh-Window
}

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "AudioLib"
    $form.Size = New-Object System.Drawing.Size(430, 248)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedSingle"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.ControlBox = $false
    $form.TopMost = $true
    $form.BackColor = [System.Drawing.Color]::FromArgb(17, 24, 39)

    $picture = New-Object System.Windows.Forms.PictureBox
    $picture.Location = New-Object System.Drawing.Point(173, 18)
    $picture.Size = New-Object System.Drawing.Size(72, 72)
    $picture.SizeMode = "Zoom"
    if (Test-Path -LiteralPath $oldExe) {
        $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($oldExe)
        if ($null -ne $icon) {
            $picture.Image = $icon.ToBitmap()
            $icon.Dispose()
        }
    }
    $form.Controls.Add($picture)

    $titleLabel = New-Object System.Windows.Forms.Label
    $titleLabel.Text = "AudioLib"
    $titleLabel.ForeColor = [System.Drawing.Color]::FromArgb(248, 250, 252)
    $titleLabel.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 16)
    $titleLabel.TextAlign = "MiddleCenter"
    $titleLabel.Location = New-Object System.Drawing.Point(20, 92)
    $titleLabel.Size = New-Object System.Drawing.Size(378, 30)
    $form.Controls.Add($titleLabel)

    $script:statusLabel = New-Object System.Windows.Forms.Label
    $script:statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(248, 250, 252)
    $script:statusLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $script:statusLabel.TextAlign = "MiddleCenter"
    $script:statusLabel.Location = New-Object System.Drawing.Point(20, 125)
    $script:statusLabel.Size = New-Object System.Drawing.Size(378, 24)
    $form.Controls.Add($script:statusLabel)

    $script:detailLabel = New-Object System.Windows.Forms.Label
    $script:detailLabel.ForeColor = [System.Drawing.Color]::FromArgb(156, 163, 175)
    $script:detailLabel.Font = New-Object System.Drawing.Font("Segoe UI", 8)
    $script:detailLabel.TextAlign = "MiddleCenter"
    $script:detailLabel.Location = New-Object System.Drawing.Point(20, 148)
    $script:detailLabel.Size = New-Object System.Drawing.Size(378, 22)
    $form.Controls.Add($script:detailLabel)

    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Style = "Marquee"
    $progressBar.MarqueeAnimationSpeed = 25
    $progressBar.Location = New-Object System.Drawing.Point(34, 180)
    $progressBar.Size = New-Object System.Drawing.Size(350, 8)
    $form.Controls.Add($progressBar)

    Set-UpdateStatus (LocalText "Завершення роботи AudioLib…" "Closing AudioLib…") (LocalText "Підготовка до встановлення оновлення" "Preparing to install the update")
    $form.Show()
    Refresh-Window

    while ($null -ne (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue)) {
        Refresh-Window
        Start-Sleep -Milliseconds 80
    }

    Set-UpdateStatus (LocalText "Встановлення оновлення…" "Installing update…") (LocalText "Заміна файлів програми" "Replacing application files")
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $wrappedRoot = Join-Path $extractDir "AudioLib"
    if (Test-Path -LiteralPath (Join-Path $wrappedRoot "AudioLib.exe")) {
        $newRoot = $wrappedRoot
    }
    else {
        $newRoot = $extractDir
    }
    $newExe = Join-Path $newRoot "AudioLib.exe"
    $newInternal = Join-Path $newRoot "_internal"
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

    Set-UpdateStatus (LocalText "Запуск нової версії…" "Starting the new version…") (LocalText "AudioLib відкриється автоматично" "AudioLib will open automatically")
    Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue
    $env:AUDIOLIB_UPDATE_READY = $readyFile
    try {
        $startedProcess = Start-Process -FilePath $oldExe -WorkingDirectory $InstallDir -PassThru
    }
    finally {
        Remove-Item Env:AUDIOLIB_UPDATE_READY -ErrorAction SilentlyContinue
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not (Test-Path -LiteralPath $readyFile)) {
        if ($startedProcess.HasExited) {
            throw (LocalText "Нова версія AudioLib завершила роботу під час запуску." "The new AudioLib version exited during startup.")
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            $message = "{0} UPDATE_START_CONFIRMATION_TIMEOUT" -f (Get-Date -Format s)
            New-Item -ItemType Directory -Path (Split-Path $logPath) -Force | Out-Null
            Add-Content -LiteralPath $logPath -Value $message -Encoding UTF8
            throw (LocalText "Нова версія AudioLib не підтвердила успішний запуск. Виконується повернення до попередньої версії." "The new AudioLib version did not confirm a successful startup. Rolling back to the previous version.")
        }
        Refresh-Window
        Start-Sleep -Milliseconds 80
    }
    if ($null -ne $form -and -not $form.IsDisposed) { $form.Close() }
    Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PackagePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $message = "{0} UPDATE_FAILED {1}" -f (Get-Date -Format s), $_.Exception.Message
    New-Item -ItemType Directory -Path (Split-Path $logPath) -Force | Out-Null
    Add-Content -LiteralPath $logPath -Value $message -Encoding UTF8
    if ($null -ne $startedProcess -and -not $startedProcess.HasExited) {
        Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue
        $startedProcess.WaitForExit(5000) | Out-Null
    }
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
    if ($null -ne $form -and -not $form.IsDisposed) {
        Set-UpdateStatus (LocalText "Не вдалося встановити оновлення" "Update installation failed") $_.Exception.Message
        $progressBar.Style = "Blocks"
        $progressBar.Value = 0
        Refresh-Window
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            (LocalText "Оновлення AudioLib" "AudioLib Update"),
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
        $form.Close()
    }
    exit 1
}
'''
