"""On-disk layout, staging directories, and atomic publication.

Layout under the data directory::

    projects/<project>/original.blend        uploaded bytes, never modified
    projects/<project>/baseline.json         protection baseline from the upload
    projects/<project>/revisions/<rev>/      model.blend + inspection.json + fingerprints.json (immutable)
    projects/<project>/renders/<job>/        <view>[--<pass>].png + cameras.json (job = render or dry-run edit)
    projects/<project>/references/           reference images (<ref>.png) + index.json, owned by the user
    projects/<project>/exports/<artifact>.stl
    staging/<job-or-upload>/                 private working directories
    logs/jobs/<job>.log                      bounded raw Blender logs (local only)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import NotFound

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def check_id(value: str) -> str:
    if not _SAFE_ID.match(value):
        raise NotFound("Unknown identifier", details={"id": value[:80]})
    return value


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


class Storage:
    def __init__(self, settings: Settings):
        self.root = settings.data_dir.resolve()
        self.projects = settings.projects_dir
        self.staging = settings.staging_dir
        self.logs = settings.logs_dir / "jobs"

    def init(self) -> None:
        for d in (self.root, self.projects, self.staging, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    # -- paths --------------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        return self.projects / check_id(project_id)

    def original_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "original.blend"

    def baseline_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "baseline.json"

    def revision_dir(self, project_id: str, revision_id: str) -> Path:
        return self.project_dir(project_id) / "revisions" / check_id(revision_id)

    def revision_model(self, project_id: str, revision_id: str) -> Path:
        return self.revision_dir(project_id, revision_id) / "model.blend"

    def revision_inspection(self, project_id: str, revision_id: str) -> Path:
        return self.revision_dir(project_id, revision_id) / "inspection.json"

    def revision_fingerprints(self, project_id: str, revision_id: str) -> Path:
        return self.revision_dir(project_id, revision_id) / "fingerprints.json"

    def references_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "references"

    def reference_path(self, project_id: str, reference_id: str) -> Path:
        return self.references_dir(project_id) / f"{check_id(reference_id)}.png"

    def render_dir(self, project_id: str, job_id: str) -> Path:
        return self.project_dir(project_id) / "renders" / check_id(job_id)

    def exports_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "exports"

    def job_log(self, job_id: str) -> Path:
        return self.logs / f"{check_id(job_id)}.log"

    def new_staging(self, name: str) -> Path:
        path = self.staging / check_id(name)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        os.chmod(path, 0o700)
        return path

    def discard_staging(self, name: str) -> None:
        shutil.rmtree(self.staging / check_id(name), ignore_errors=True)

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def resolve_artifact(self, rel_path: str) -> Path:
        """Resolve a stored artifact path, refusing anything outside the data dir."""
        if not rel_path or rel_path.startswith("/") or "\\" in rel_path:
            raise NotFound("Artifact file is unavailable")
        parts = rel_path.split("/")
        if any(p in ("", ".", "..") or not _SAFE_NAME.match(p) for p in parts):
            raise NotFound("Artifact file is unavailable")
        path = (self.root / rel_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise NotFound("Artifact file is unavailable")
        return path

    # -- publication --------------------------------------------------------

    @staticmethod
    def publish_dir(staged: Path, final: Path) -> None:
        """Atomically move a fully written staging directory into place."""
        for item in staged.rglob("*"):
            if item.is_file():
                fsync_file(item)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise FileExistsError(f"refusing to overwrite published directory {final.name}")
        os.rename(staged, final)
        fsync_dir(final.parent)

    @staticmethod
    def publish_file(staged: Path, final: Path) -> None:
        fsync_file(staged)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise FileExistsError(f"refusing to overwrite published file {final.name}")
        os.rename(staged, final)
        fsync_dir(final.parent)

    @staticmethod
    def unpublish(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)

    # -- recovery -----------------------------------------------------------

    def clear_staging(self) -> int:
        count = 0
        for child in self.staging.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            count += 1
        return count

    def reconcile(self, project_ids: set[str], revision_ids: set[str], artifact_paths: set[str]) -> list[str]:
        """Remove published files that no committed database row references."""
        removed: list[str] = []
        for pdir in self.projects.iterdir():
            if not pdir.is_dir():
                continue
            if pdir.name not in project_ids:
                shutil.rmtree(pdir, ignore_errors=True)
                removed.append(self.relative_safe(pdir))
                continue
            revs = pdir / "revisions"
            if revs.is_dir():
                for rdir in revs.iterdir():
                    if rdir.name not in revision_ids:
                        shutil.rmtree(rdir, ignore_errors=True)
                        removed.append(self.relative_safe(rdir))
            for sub in ("renders", "exports"):
                base = pdir / sub
                if not base.is_dir():
                    continue
                for f in base.rglob("*"):
                    if f.is_file() and f.name != "cameras.json" and self.relative_safe(f) not in artifact_paths:
                        f.unlink(missing_ok=True)
                        removed.append(self.relative_safe(f))
                for d in sorted((d for d in base.rglob("*") if d.is_dir()), reverse=True):
                    if not any(p.name != "cameras.json" for p in d.iterdir()):
                        shutil.rmtree(d, ignore_errors=True)
        return removed

    def relative_safe(self, path: Path) -> str:
        try:
            return self.relative(path)
        except ValueError:
            return path.name
