from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Self

from .errors import ArtifactIntegrityError, GraphicsError
from .fonts import find_font_file
from .inputs import load_job, variant_dimensions
from .models import Artifact, BatchResult, GraphicJob
from .qa import inspect_png, validate_svg
from .renderer import EdgeRenderer, RendererProtocol
from .svg import compile_svg
from .templates import compile_graphic

RECIPE_VERSION = "graphics-template-batch.v1.2"
MANIFEST_SCHEMA = "graphics.artifact-manifest.v1"
LOCK_TIMEOUT_SECONDS = 15
# A lock older than this is treated as abandoned even if its pid looks alive
# (pid reuse, or a lock written by another host on a shared output root).
STALE_LOCK_MAX_AGE_SECONDS = 60 * 60
# A live holder refreshes its lock's mtime this often, so runs of any length
# keep their lock. Locks that carry "heartbeat_seconds" are judged by the time
# since the last refresh instead of by their creation time.
LOCK_HEARTBEAT_SECONDS = 30.0
# Missing this many seconds of heartbeats marks a heartbeat lock abandoned
# (for example a crash on another host, where the pid cannot be checked).
HEARTBEAT_STALE_AFTER_SECONDS = 10 * 60
# How long an empty/unparseable lock may exist before it counts as abandoned.
LOCK_WRITE_GRACE_SECONDS = 5
# Reclaiming a stale lock happens under a short-lived "<lock>.reclaim" guard.
# A guard older than this was left by a reclaimer that crashed mid-reclaim.
RECLAIM_GUARD_STALE_SECONDS = 30.0
COMMIT_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _job_fingerprint(job: GraphicJob, renderer: RendererProtocol) -> str:
    font_path = find_font_file(job.brand.font_family)
    if font_path is None:
        raise GraphicsError("validated local font is no longer installed")
    try:
        font_digest = _hash(font_path.read_bytes())
    except OSError as exc:
        raise GraphicsError("validated local font cannot be read") from exc
    payload = {
        "schema_version": "graphics.job.v1",
        "job_id": job.job_id,
        "template": job.template,
        "recipe_version": RECIPE_VERSION,
        "brand": {"colors": dict(job.brand.colors), "font_family": job.brand.font_family},
        "font_sha256": font_digest,
        "variants": list(job.variants),
        "records": list(job.records),
        "assets": {key: asset.sha256 for key, asset in sorted(job.assets.items())},
        "renderer": {"name": renderer.name, "version": renderer.version},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _hash(encoded)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
    os.replace(temporary, path)


def _commit_output_directory(staging: Path, target: Path) -> None:
    # Windows scanners may briefly hold freshly written artifacts during atomic directory commit.
    for attempt in range(len(COMMIT_RETRY_DELAYS) + 1):
        try:
            os.replace(staging, target)
            return
        except PermissionError as exc:
            transient = getattr(exc, "winerror", None) in {5, 32, 33} or exc.errno in {errno.EACCES, errno.EPERM}
            if not transient or attempt == len(COMMIT_RETRY_DELAYS):
                raise
            if target.exists() or target.is_symlink():
                raise GraphicsError("job output appeared during atomic commit; retry to verify replay") from exc
            time.sleep(COMMIT_RETRY_DELAYS[attempt])


def _pid_alive(pid: int) -> bool:
    """Return whether a process with this pid exists on this host."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        # os.kill(pid, 0) would terminate the process on Windows; query it instead.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_lock(path: Path) -> tuple[bytes, dict[str, Any], float] | None:
    """Return (raw bytes, parsed owner, mtime) or None when the lock is gone."""
    try:
        raw = path.read_bytes()
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    owner: dict[str, Any] = {}
    text = raw.decode("utf-8", errors="replace").strip()
    if text.isdigit():  # legacy format: the bare pid
        owner = {"pid": int(text)}
    elif text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            owner = parsed
    return raw, owner, mtime


def _lock_is_stale(owner: dict[str, Any], mtime: float, now: float) -> bool:
    """Decide whether a lock was left behind by a process that no longer runs.

    - Heartbeat locks (current format): stale when not refreshed for
      HEARTBEAT_STALE_AFTER_SECONDS, however long the run has been going.
    - Legacy locks without a heartbeat: stale when older than
      STALE_LOCK_MAX_AGE_SECONDS (covers pid reuse and locks written from
      another host on a shared output directory).
    - Unreadable/empty and older than LOCK_WRITE_GRACE_SECONDS: stale (the
      holder crashed between creating and writing the file).
    - Written on this host by a pid that no longer exists: stale.
    """
    heartbeat = owner.get("heartbeat_seconds")
    if isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool) and heartbeat > 0:
        # The holder refreshes mtime while it runs: only silence counts.
        if now - mtime > HEARTBEAT_STALE_AFTER_SECONDS:
            return True
    else:
        created = owner.get("created_at")
        started = float(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else mtime
        age = now - min(started, mtime)
        if age > STALE_LOCK_MAX_AGE_SECONDS:
            return True
    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return now - mtime > LOCK_WRITE_GRACE_SECONDS
    host = owner.get("host")
    if host is not None and host != socket.gethostname():
        return False
    return not _pid_alive(pid)


def _same_file(fd: int, path: Path) -> bool:
    """True when ``path`` still names the file open as ``fd``."""
    try:
        opened = os.fstat(fd)
        current = os.stat(path)
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


class _JobLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self._stop_heartbeat = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def _owner_record(self) -> bytes:
        record = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_at": time.time(),
            "heartbeat_seconds": LOCK_HEARTBEAT_SECONDS,
        }
        return json.dumps(record, sort_keys=True).encode("utf-8")

    def _touch(self) -> None:
        fd = self.fd
        try:
            if fd is not None and os.utime in os.supports_fd:
                # Touch our own open file, never a lock another run created.
                os.utime(fd)
            else:  # pragma: no cover - Windows has no fd-based utime
                self.path.touch(exist_ok=True)
        except OSError:
            pass

    def _beat(self, interval: float) -> None:
        while not self._stop_heartbeat.wait(interval):
            self._touch()

    def _start_heartbeat(self) -> None:
        self._heartbeat = threading.Thread(
            target=self._beat,
            args=(LOCK_HEARTBEAT_SECONDS,),
            name=f"job-lock-heartbeat:{self.path.name}",
            daemon=True,
        )
        self._heartbeat.start()

    def owns_lock(self) -> bool:
        """True while the lock path is still the file this run created."""
        return self.fd is not None and _same_file(self.fd, self.path)

    def ensure_held(self) -> None:
        """Fail the run if another run took the lock over (never commit then)."""
        if not self.owns_lock():
            raise GraphicsError("the graphics job lock was taken over by another run; output not committed")

    def _acquire_reclaim_guard(self) -> Path | None:
        """Create the reclaim guard with O_EXCL; None when another run holds it."""
        guard = self.path.with_name(f"{self.path.name}.reclaim")
        for _ in range(2):
            try:
                os.close(os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return guard
            except FileExistsError:
                try:
                    age = time.time() - guard.stat().st_mtime
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                if age <= RECLAIM_GUARD_STALE_SECONDS:
                    return None
                try:  # left behind by a reclaimer that crashed mid-reclaim
                    guard.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return None
            except OSError:
                return None
        return None

    def _break_if_stale(self) -> bool:
        """Remove a stale lock. Returns True when the caller should retry at once.

        Reclaimers are serialized by an O_EXCL guard and re-check the lock
        under it, so a lock that another reclaimer already replaced with a
        live one is never touched. The lock is then moved aside and compared
        with what was judged stale; if a live owner released and recreated it
        in that instant, it is linked back. Should even that restore lose a
        race, the holder whose lock was moved notices through ``ensure_held``
        before committing output, so two runs never both commit.
        """
        observed = _read_lock(self.path)
        if observed is None:
            return True
        raw, owner, mtime = observed
        if not _lock_is_stale(owner, mtime, time.time()):
            return False
        guard = self._acquire_reclaim_guard()
        if guard is None:
            return False  # another run is reclaiming; wait for its result
        try:
            current = _read_lock(self.path)
            if current is None:
                return True
            if current[0] != raw or not _lock_is_stale(current[1], current[2], time.time()):
                return True  # replaced since we looked: judge the new lock afresh
            tombstone = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.stale")
            try:
                os.replace(self.path, tombstone)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            try:
                moved = tombstone.read_bytes()
            except OSError:
                moved = raw
            if moved != raw:
                try:
                    os.link(tombstone, self.path)
                except OSError:
                    # Another run already created a new lock; the displaced
                    # holder fails at ensure_held() instead of committing.
                    pass
            try:
                tombstone.unlink(missing_ok=True)
            except OSError:
                pass
            return moved == raw
        finally:
            try:
                guard.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> Self:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, self._owner_record())
                self._start_heartbeat()
                return self
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise GraphicsError("timed out waiting for the same graphics job to finish")
                time.sleep(0.05)
            except OSError as exc:
                raise GraphicsError("could not reserve a graphics job lock") from exc

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop_heartbeat.set()
        if self._heartbeat is not None:
            self._heartbeat.join()
            self._heartbeat = None
        owned = self.owns_lock()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if not owned:
            return  # never delete a lock that another run now holds
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_artifact_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ArtifactIntegrityError("artifact manifest contains an unsafe path")
    try:
        resolved = (root / candidate).resolve(strict=True)
    except OSError as exc:
        raise ArtifactIntegrityError("an artifact listed in the manifest is missing") from exc
    if not resolved.is_relative_to(root.resolve()):
        raise ArtifactIntegrityError("artifact path escapes the job output directory")
    return resolved


def _replay_result(
    target: Path,
    fingerprint: str,
    job: GraphicJob,
    renderer: RendererProtocol,
    started_wall: float,
) -> BatchResult:
    manifest_path = target / "manifest.json"
    if target.is_symlink() or not manifest_path.is_file():
        raise ArtifactIntegrityError("an output directory exists without a valid manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError("existing artifact manifest cannot be decoded") from exc
    if not isinstance(manifest, dict):
        raise ArtifactIntegrityError("existing artifact manifest root is invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("input_sha256") != fingerprint:
        raise GraphicsError("job id already exists with different inputs or renderer version")
    if manifest.get("job_id") != job.job_id:
        raise ArtifactIntegrityError("existing manifest job id does not match the requested job")
    if manifest.get("status") != "accepted":
        raise ArtifactIntegrityError("existing manifest is not an accepted result")
    recipe = manifest.get("recipe")
    if not isinstance(recipe, dict) or recipe != {"id": job.template, "version": RECIPE_VERSION}:
        raise ArtifactIntegrityError("existing manifest recipe does not match the requested job")
    renderer_info = manifest.get("renderer")
    if not isinstance(renderer_info, dict) or renderer_info != {"name": renderer.name, "version": renderer.version}:
        raise ArtifactIntegrityError("existing manifest renderer does not match the requested renderer")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ArtifactIntegrityError("existing manifest has no artifact list")
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        raise ArtifactIntegrityError("existing manifest summary is invalid")
    count = summary.get("accepted_outputs")
    expected_pairs = {(record["id"], variant) for record in job.records for variant in job.variants}
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count != len(expected_pairs)
        or len(artifacts) != count * 2
        or summary.get("accepted_records") != len(job.records)
    ):
        raise ArtifactIntegrityError("existing manifest has an invalid artifact count")
    artifact_pairs: dict[tuple[str, str], set[str]] = {}
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ArtifactIntegrityError("existing manifest contains an invalid artifact entry")
        record_id, variant, role = item.get("record_id"), item.get("variant"), item.get("role")
        if not isinstance(record_id, str) or not isinstance(variant, str) or not isinstance(role, str) or role not in {"editable-svg", "preview-png"}:
            raise ArtifactIntegrityError("existing manifest contains an invalid artifact identity")
        pair = (record_id, variant)
        roles = artifact_pairs.setdefault(pair, set())
        if role in roles:
            raise ArtifactIntegrityError("existing manifest duplicates an artifact role")
        roles.add(role)
        expected_name = "design.svg" if role == "editable-svg" else "preview.png"
        if item["path"] != f"{record_id}/{variant}/{expected_name}":
            raise ArtifactIntegrityError("existing manifest artifact path does not match its identity")
        if pair not in expected_pairs:
            raise ArtifactIntegrityError("existing manifest contains an output that was not requested")
        path = _safe_artifact_path(target, item["path"])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactIntegrityError("an artifact listed in the manifest is missing") from exc
        digest = _hash(data)
        if digest != item.get("sha256"):
            raise ArtifactIntegrityError("an artifact hash does not match its manifest")
        if item.get("bytes") != len(data):
            raise ArtifactIntegrityError("an artifact byte count does not match its manifest")
        width, height = variant_dimensions(variant)
        if item.get("width") != width or item.get("height") != height:
            raise ArtifactIntegrityError("an artifact dimension does not match its selected variant")
    if set(artifact_pairs) != expected_pairs or any(
        roles != {"editable-svg", "preview-png"} for roles in artifact_pairs.values()
    ):
        raise ArtifactIntegrityError("existing manifest output pairs are incomplete")
    return BatchResult(
        "replay", job.job_id, count, 0, None, manifest_path, 0.0,
        round(time.perf_counter() - started_wall, 6),
    )


def _artifact(path: Path, target: Path, record_id: str, variant: str, role: str, width: int, height: int) -> Artifact:
    data = path.read_bytes()
    return Artifact(
        record_id=record_id,
        variant=variant,
        role=role,
        path=path.relative_to(target).as_posix(),
        sha256=_hash(data),
        bytes=len(data),
        width=width,
        height=height,
    )


def _record_output_directory(staging: Path, record_id: str, variant: str) -> Path:
    staging_root = staging.resolve()
    directory = (staging / record_id / variant).resolve()
    if not directory.is_relative_to(staging_root):
        raise GraphicsError("record output path escapes the graphics staging directory")
    return directory


def render_batch(
    spec: str | Path | GraphicJob,
    output_root: str | Path,
    *,
    renderer: RendererProtocol | None = None,
) -> BatchResult:
    job = load_job(spec)
    selected_renderer: RendererProtocol = renderer if renderer is not None else EdgeRenderer()
    fingerprint = _job_fingerprint(job, selected_renderer)
    raw_output_base = Path(output_root).expanduser()
    if raw_output_base.is_symlink():
        raise GraphicsError("output root must not be a symbolic link")
    output_base = raw_output_base.resolve()
    try:
        output_base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GraphicsError("could not create the output root") from exc
    target = output_base / job.job_id
    lock_root = output_base / ".locks"
    try:
        lock_root.mkdir(exist_ok=True)
        if lock_root.is_symlink():
            raise GraphicsError("output lock directory must not be a symbolic link")
    except OSError as exc:
        raise GraphicsError("could not create the output lock directory") from exc
    start_wall = time.perf_counter()
    start_cpu = time.process_time()
    with _JobLock(lock_root / f"{job.job_id}.lock") as job_lock:
        if target.exists() or target.is_symlink():
            return _replay_result(target, fingerprint, job, selected_renderer, start_wall)

        staging = output_base / f".{job.job_id}.{uuid.uuid4().hex}.staging"
        try:
            staging.mkdir()
        except OSError as exc:
            raise GraphicsError("could not create the graphics staging directory") from exc
        artifacts: list[Artifact] = []
        output_count = 0
        try:
            for record in job.records:
                for variant in job.variants:
                    ir = compile_graphic(job, record, variant)
                    svg_data = compile_svg(ir, job)
                    record_dir = _record_output_directory(staging, record["id"], variant)
                    record_dir.mkdir(parents=True, exist_ok=True)
                    svg_path = record_dir / "design.svg"
                    png_path = record_dir / "preview.png"
                    svg_path.write_bytes(svg_data)
                    validate_svg(svg_path, ir, job, dict(record))
                    selected_renderer.render(svg_path, png_path, ir.canvas.width, ir.canvas.height)
                    inspect_png(png_path, ir.canvas.width, ir.canvas.height)
                    artifacts.append(_artifact(svg_path, staging, record["id"], variant, "editable-svg", ir.canvas.width, ir.canvas.height))
                    artifacts.append(_artifact(png_path, staging, record["id"], variant, "preview-png", ir.canvas.width, ir.canvas.height))
                    output_count += 1

            fresh_wall = time.perf_counter() - start_wall
            cpu_seconds = time.process_time() - start_cpu
            manifest = {
                "schema_version": MANIFEST_SCHEMA,
                "job_id": job.job_id,
                "status": "accepted",
                "input_sha256": fingerprint,
                "recipe": {"id": job.template, "version": RECIPE_VERSION},
                "renderer": {"name": selected_renderer.name, "version": selected_renderer.version},
                "summary": {
                    "accepted_records": len(job.records),
                    "accepted_outputs": output_count,
                    "rejected_records": 0,
                    "needs_input": 0,
                    "qa_failures": 0,
                    "replay": False,
                },
                "resources": {
                    "wall_seconds": round(fresh_wall, 6),
                    "controller_cpu_seconds": round(cpu_seconds, 6),
                    "renderer_invocations": output_count,
                },
                "usage": {
                    "model_calls": 0,
                    "monetary_cost": None,
                    "cost_status": "unknown_without_rate_card",
                },
                "artifacts": [
                    {
                        "record_id": item.record_id,
                        "variant": item.variant,
                        "role": item.role,
                        "path": item.path,
                        "sha256": item.sha256,
                        "bytes": item.bytes,
                        "width": item.width,
                        "height": item.height,
                    }
                    for item in artifacts
                ],
            }
            _atomic_json(staging / "manifest.json", manifest)
            if target.exists() or target.is_symlink():
                raise GraphicsError("job output appeared during execution; retry to verify replay")
            job_lock.ensure_held()
            _commit_output_directory(staging, target)
            return BatchResult(
                "accepted", job.job_id, output_count, 0, None, target / "manifest.json",
                round(fresh_wall, 6), 0.0,
            )
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            file_names = [Path(value).name for value in (exc.filename, exc.filename2) if isinstance(value, str)]
            detail = f"errno={exc.errno}, winerror={getattr(exc, 'winerror', None)}, operation={exc.strerror}"
            if file_names:
                detail += f", files={','.join(file_names)}"
            raise GraphicsError(f"graphics output could not be written ({detail})") from exc
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
