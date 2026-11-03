from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from .errors import GraphicsError


def find_edge() -> Path:
    configured = os.environ.get("BOOK_MEDIA_EDGE_PATH")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
        raise GraphicsError("BOOK_MEDIA_EDGE_PATH does not point to an Edge executable")
    for command in ("msedge", "microsoft-edge", "microsoft-edge-stable"):
        located = shutil.which(command)
        if located:
            return Path(located).resolve()
    candidates = (
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise GraphicsError("Microsoft Edge is required for local PNG preview rendering")


def _edge_version(executable: Path) -> str:
    try:
        import winreg

        for key_name in (r"Software\Microsoft\Edge\BLBeacon", r"Software\WOW6432Node\Microsoft\Edge\BLBeacon"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:  # type: ignore[attr-defined]  # Windows-only API; CI type-checks for Linux
                    value, _ = winreg.QueryValueEx(key, "version")  # type: ignore[attr-defined]  # Windows-only API; CI type-checks for Linux
                    if value:
                        return str(value)
            except OSError:
                continue
    except ImportError:
        pass
    for args in (("--version",), ("-version",)):
        try:
            result = subprocess.run(
                [str(executable), *args], capture_output=True, text=True, timeout=5, check=False
            )
            output = (result.stdout or result.stderr).strip()
            if result.returncode == 0 and output:
                return output[-120:]
        except (OSError, subprocess.TimeoutExpired):
            continue
    stat = executable.stat()
    return f"binary-mtime-{stat.st_mtime_ns}"


def _run_edge(
    args: list[str],
    timeout_seconds: int,
    *,
    screenshot_path: Path,
    profile_path: Path,
) -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise GraphicsError("Windows PowerShell is required to supervise the local Edge renderer")
    arguments = subprocess.list2cmdline(args[1:])

    def ps_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def stop_profiled_edge_processes() -> None:
        cleanup_script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            f"$profile={ps_literal(str(profile_path.resolve()))}; "
            "$matching=@(Get-CimInstance -ClassName Win32_Process -Filter \"Name='msedge.exe'\" "
            "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) }); "
            "foreach ($item in $matching) { Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        cleanup_encoded = base64.b64encode(cleanup_script.encode("utf-16le")).decode("ascii")
        try:
            subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", cleanup_encoded],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    script = (
        "$ErrorActionPreference='Stop'; "
        "$complete=$false; $lastLength=-1; $stable=0; $remaining=@(); $edge_exit=1; "
        f"$edge={ps_literal(args[0])}; "
        f"$arguments={ps_literal(arguments)}; "
        f"$preview={ps_literal(str(screenshot_path.resolve()))}; "
        f"$profile={ps_literal(str(profile_path.resolve()))}; "
        "function Get-SessionEdge { @(Get-CimInstance -ClassName Win32_Process -Filter \"Name='msedge.exe'\" "
        "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) }) }; "
        "try { "
        "$process=Start-Process -FilePath $edge -ArgumentList $arguments "
        "-WindowStyle Hidden -PassThru -Wait; $edge_exit=$process.ExitCode; "
        "$deadline=[DateTime]::UtcNow.AddSeconds(45); "
        "while ([DateTime]::UtcNow -lt $deadline) { "
        "if (Test-Path -LiteralPath $preview) { "
        "$length=(Get-Item -LiteralPath $preview).Length; "
        "if ($length -ge 64 -and $length -eq $lastLength) { $stable++; if ($stable -ge 2) { break } } "
        "else { $stable=0 }; $lastLength=$length }; "
        "Start-Sleep -Milliseconds 50 }; "
        "$complete=(Test-Path -LiteralPath $preview) -and ((Get-Item -LiteralPath $preview).Length -ge 64) -and ($stable -ge 2) "
        "} finally { "
        "$matching=Get-SessionEdge; foreach ($item in $matching) { "
        "Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "$cleanupDeadline=[DateTime]::UtcNow.AddSeconds(5); "
        "do { $remaining=Get-SessionEdge; if ($remaining.Count -eq 0) { break }; "
        "Start-Sleep -Milliseconds 50 } while ([DateTime]::UtcNow -lt $cleanupDeadline) }; "
        "$report=@{complete=$complete; bytes=$lastLength; edge_exit=$edge_exit; remaining=$remaining.Count}; "
        "[Console]::Out.WriteLine('BOOK_MEDIA_GRAPHICS='+($report | ConvertTo-Json -Compress)); "
        "if (-not $complete -or $remaining.Count -gt 0) { exit 70 }; exit $process.ExitCode"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    command = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
        tree_terminated = False
        if taskkill:
            try:
                result = subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=False,
                )
                tree_terminated = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not tree_terminated:
            stop_profiled_edge_processes()
            if process.poll() is None:
                process.kill()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stop_profiled_edge_processes()
            process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=stdout or exc.output,
            stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class EdgeRenderer:
    name = "Microsoft Edge Headless"

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable or find_edge()
        if not self.executable.is_file():
            raise GraphicsError("configured Edge executable is unavailable")
        self.version = _edge_version(self.executable)

    def render(self, svg_path: Path, png_path: Path, width: int, height: int) -> None:
        svg = svg_path.read_text(encoding="utf-8")
        html = (
            '<!doctype html><html><head><meta charset="utf-8"><style>'
            f'html,body{{margin:0;padding:0;width:{width}px;height:{height}px;overflow:hidden;background:#fff}}'
            f'svg{{display:block;width:{width}px;height:{height}px}}</style></head><body>{svg}</body></html>'
        )
        png_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="book-media-edge-") as temp_dir:
            temp = Path(temp_dir)
            html_path = temp / "render.html"
            profile_path = temp / "profile"
            rendered_png = temp / "preview.png"
            html_path.write_text(html, encoding="utf-8", newline="")
            png_path.unlink(missing_ok=True)
            args = [
                str(self.executable),
                "--headless=new",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--user-data-dir={profile_path}",
                f"--window-size={width},{height}",
                f"--screenshot={rendered_png.resolve()}",
                Path(html_path).as_uri(),
            ]
            try:
                result = _run_edge(args, 60, screenshot_path=rendered_png, profile_path=profile_path)
            except subprocess.TimeoutExpired as exc:
                raise GraphicsError("local SVG rasterization timed out") from exc
            except GraphicsError:
                raise
            except OSError as exc:
                raise GraphicsError("local Edge rasterization could not start") from exc
            output_exists = rendered_png.is_file()
            output_bytes = rendered_png.stat().st_size if output_exists else 0
            if result.returncode != 0 or not output_exists or output_bytes < 64:
                details = (result.stderr or result.stdout).strip()[-500:]
                suffix = f": {details}" if details else ""
                raise GraphicsError(
                    "local Edge rasterization failed "
                    f"(exit={result.returncode}, screenshot_exists={output_exists}, bytes={output_bytes}){suffix}"
                )
            try:
                shutil.copyfile(rendered_png, png_path)
            except OSError as exc:
                raise GraphicsError("could not move the rendered PNG into the output directory") from exc


class RendererProtocol(Protocol):
    name: str
    version: str

    def render(self, svg_path: Path, png_path: Path, width: int, height: int) -> None:
        ...
