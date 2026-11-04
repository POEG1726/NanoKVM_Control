"""Fetch a completed test batch; never notify or change the run stage here."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import uuid


class ArtifactError(RuntimeError):
    pass


def _name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", value)
            or ".." in value or value.endswith(".")
            or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}):
        raise ArtifactError("Unsafe artifact basename")
    return value


def _descriptor(item):
    name = _name(item.get("name"))
    size = item.get("size")
    digest = item.get("sha256")
    if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ArtifactError("Invalid artifact size/hash")
    return {"name": name, "size": size, "sha256": digest.lower()}


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _matches(path, descriptor):
    return path.is_file() and path.stat().st_size == descriptor["size"] and _hash(path) == descriptor["sha256"]


def _snapshot(target, remote):
    # UTF-8 JSON content and raw-byte size/hash are captured from the same read.
    quoted = "'" + str(remote).replace("'", "''") + "'"
    code = (f"$b=[IO.File]::ReadAllBytes({quoted}); "
            "[pscustomobject]@{content=[Text.Encoding]::UTF8.GetString($b);size=$b.Length;"
            "sha256=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($b)).ToLowerInvariant()} | ConvertTo-Json -Compress")
    snapshot = json.loads(target.ps(code))
    obj = json.loads(snapshot["content"].lstrip("\ufeff"))
    descriptor = _descriptor({"name": remote.name, "size": snapshot["size"], "sha256": snapshot["sha256"]})
    return obj, descriptor


def _download(target, folder, local, descriptor):
    final = local / descriptor["name"]
    if final.exists():
        if not _matches(final, descriptor):
            raise ArtifactError("Existing original differs: " + descriptor["name"])
        return final
    temporary = local / (descriptor["name"] + "." + uuid.uuid4().hex + ".part")
    target.copy(folder / descriptor["name"], temporary)
    if not _matches(temporary, descriptor):
        raise ArtifactError("Transferred artifact failed verification: " + descriptor["name"])
    try:
        # Same-directory hard link publishes atomically and refuses overwrites.
        os.link(temporary, final)
    except FileExistsError:
        if not _matches(final, descriptor):
            raise ArtifactError("Concurrent original differs: " + descriptor["name"])
    temporary.unlink()
    return final


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("runs", "rows"):
            if isinstance(value.get(key), list):
                return value[key]
    raise ArtifactError("Summary/result has no run rows")


def _count_rows(rows, count, source):
    if len(rows) != count:
        raise ArtifactError(source + " has a different run count")
    indexes = [row.get("index", row.get("run", row.get("run_index"))) for row in rows]
    if any(type(i) not in (int, str) or not str(i).isdigit() for i in indexes) or sorted(map(int, indexes)) != list(range(1, count + 1)):
        raise ArtifactError(source + " run indexes do not match completed runs")


def pull_test_artifacts(target, run, kind, *, allow_partial=False):
    """Return a verified local receipt only after the full ten-run batch arrives.

    Target only needs root, ps(code), copy(remote,local). No SSH/network work
    happens except through that supplied object, so the entire path is fakeable.
    """
    if kind != "vt3":
        raise ArtifactError("Only finite VT3 batches have this transfer contract")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run.name):
        raise ArtifactError("Invalid run id")
    root = PureWindowsPath(target.root)
    if not root.is_absolute() or root.drive.startswith("\\") or ".." in root.parts or root == PureWindowsPath(root.anchor):
        raise ArtifactError("Invalid target root")
    folder = root / "runs" / run.name
    manifest_name = f"test-{kind}-artifacts.json"
    manifest, manifest_descriptor = _snapshot(target, folder / manifest_name)
    complete = manifest.get("status") == "complete"
    if (manifest.get("schema_version") != 1 or manifest.get("run_id") != run.name
            or manifest.get("kind") != kind
            or (not complete and not (allow_partial and manifest.get("status") == "partial"))):
        raise ArtifactError("Batch manifest is not complete for this run")
    count = manifest.get("completed_count")
    if (type(manifest.get("expected_count")) is not int or manifest["expected_count"] != 10
            or type(count) is not int or not 0 <= count <= 10
            or type(manifest.get("run_count")) is not int or manifest["run_count"] != count
            or (complete and count != 10)):
        raise ArtifactError("Manifest completed/expected counts are inconsistent")
    if "folder" in manifest and PureWindowsPath(manifest["folder"]) != folder:
        raise ArtifactError("Manifest folder is outside the selected run")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ArtifactError("Manifest files are missing")
    descriptors = [_descriptor(f) for f in files]
    by_name = {d["name"]: d for d in descriptors}
    if len({n.casefold() for n in by_name}) != len(descriptors):
        raise ArtifactError("Duplicate artifact names")
    logs = {f"vt3_run{i}.log" for i in range(1, count + 1)}
    actual_logs = {d["name"] for d in descriptors if d["name"].lower().endswith(".log")}
    required = logs | {"test-vt3-summary.json", "test-vt3-summary.csv", "test-vt3-config.json"}
    if actual_logs != logs or not required.issubset(by_name):
        raise ArtifactError("Manifest lacks ten logs or summaries/config")
    result_name = "test-vt3-result.json"
    result, result_descriptor = _snapshot(target, folder / result_name)
    expected_statuses = {"finished"} if complete else {"failed", "partial"}
    if result.get("run_id") != run.name or result.get("kind") != kind or result.get("status") not in expected_statuses:
        raise ArtifactError("Batch result has not finished")
    _count_rows(_rows(result), count, "result")
    binding = _descriptor(result.get("manifest", {}))
    if binding != manifest_descriptor:
        raise ArtifactError("Result does not bind the downloaded artifact manifest")
    if manifest_name in by_name or result_name in by_name:
        raise ArtifactError("Manifest/result must not recursively include themselves")
    local = Path(run.path) / "test-artifacts" / kind
    if not complete:
        local = local / ("partial-" + manifest_descriptor["sha256"][:16])
    local.mkdir(parents=True, exist_ok=True)
    if not local.resolve().is_relative_to(Path(run.path).resolve()):
        raise ArtifactError("Local artifact directory escapes the selected run")
    all_descriptors = [manifest_descriptor, result_descriptor, *descriptors]
    for descriptor in all_descriptors:
        _download(target, folder, local, descriptor)
    if _read_json(local / manifest_name) != manifest or _read_json(local / result_name) != result:
        raise ArtifactError("Manifest/result content changed during transfer")
    summary = _read_json(local / "test-vt3-summary.json")
    _count_rows(_rows(summary), count, "JSON summary")
    with (local / "test-vt3-summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        _count_rows(list(csv.DictReader(stream)), count, "CSV summary")
    # Reject rotation after downloads, not just inconsistent first snapshots.
    for name, expected in ((manifest_name, manifest_descriptor), (result_name, result_descriptor)):
        _, current = _snapshot(target, folder / name)
        if current != expected:
            raise ArtifactError("Remote batch changed during transfer")
    receipt_path = Path(run.path) / "received-tests.json"
    if not complete:
        receipt_path = Path(run.path) / ("received-tests-partial-" + manifest_descriptor["sha256"][:16] + ".json")
    receipt = {
        "schema_version": 1, "run_id": run.name, "kind": kind,
        "complete_batch": complete, "run_count": count, "expected_count": 10,
        "manifest_sha256": manifest_descriptor["sha256"],
        "result_sha256": result_descriptor["sha256"],
        "evidence_complete": summary.get("evidence_complete") if isinstance(summary, dict) else None,
        "files": all_descriptors, "local_folder": str(local.resolve()),
        "receipt_path": str(receipt_path.resolve()),
    }
    if receipt_path.exists():
        previous = _read_json(receipt_path)
        if {k:v for k,v in previous.items() if k != "received_utc"} != receipt:
            raise ArtifactError("Existing receipt binds a different batch")
        return previous
    receipt["received_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    temporary = receipt_path.with_name(receipt_path.name + "." + uuid.uuid4().hex + ".part")
    temporary.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.link(temporary, receipt_path)
    except FileExistsError:
        if _read_json(receipt_path) != receipt:
            raise ArtifactError("Concurrent receipt differs")
    temporary.unlink()
    return receipt
