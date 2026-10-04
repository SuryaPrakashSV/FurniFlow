#!/usr/bin/env python3
"""Read-only Wrike status capture. Python 3.9+. Run sample before all.
Standalone: reuses validated HTTP, workflow and baseline-reading helpers from
wrike_task_snapshots v3. Does not import or change that file or its baselines.
All outputs are local; no Snowflake connection. See --help and README.txt.
"""


import argparse

import csv

import getpass

import hashlib

import io

import json

import os

import random

import re

import sys

import tempfile

import time

import uuid

import warnings

from contextlib import contextmanager

from datetime import datetime, timezone

from email.utils import parsedate_to_datetime

from http.client import HTTPException

from pathlib import Path

from urllib.error import HTTPError, URLError

from urllib.parse import quote, urlencode

from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = ("www.wrike.com", "app-eu.wrike.com")

SCOPE = "account_tasks_including_subtasks_no_filters_v1"

SNAPSHOT_FIELDS_V1 = [
    "task_id", "title", "status", "custom_status_id", "observed_at",
    "source_updated_at", "super_task_ids", "account_id", "run_id",
]

LABEL_FIELDS = ["custom_status_name", "workflow_id", "workflow_name",
                "workflow_status_group", "status_name_resolution", "workflow_observed_at"]

SNAPSHOT_FIELDS_V2 = SNAPSHOT_FIELDS_V1 + LABEL_FIELDS

DETAIL_FIELDS = [
    "general_status", "workflow_status", "task_updated_at",
    "general_status_changed_at", "workflow_status_changed_at", "status_change_time_source",
    "general_status_last_change_observed_at", "workflow_status_last_change_observed_at",
    "task_created_at", "task_completed_at", "task_type", "importance",
    "custom_item_type_id", "custom_fields_json", "additional_status_fields_json",
]

FRONT_FIELDS = ["task_id", "title", "general_status", "workflow_status", "custom_status_id",
                "task_updated_at", "observed_at", "general_status_last_change_observed_at",
                "workflow_status_last_change_observed_at"]

SNAPSHOT_FIELDS = FRONT_FIELDS + [key for key in SNAPSHOT_FIELDS_V2 + DETAIL_FIELDS if key not in FRONT_FIELDS]

SNAPSHOT_SCHEMAS = {1: SNAPSHOT_FIELDS_V1, 2: SNAPSHOT_FIELDS_V2, 3: SNAPSHOT_FIELDS}

CATALOG_FIELDS = ["workflow_id", "workflow_name", "custom_status_id", "custom_status_name",
                  "workflow_status_group", "workflow_hidden", "status_hidden", "scope",
                  "space_id", "workflow_observed_at"]

class SnapshotError(Exception):
    """Safe, non-secret diagnostic suitable for a summary or console."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status

def utcnow():
    return datetime.now(timezone.utc)

def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

def parse_timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise SnapshotError("Invalid observation timestamp in previous snapshot.") from exc

def csv_encode(value):
    """Reversible protection against spreadsheet formula interpretation."""
    value = str(value)
    if value.startswith(("'", "\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value

def csv_decode(value):
    if value.startswith("'") and csv_encode(value[1:]) == value:
        return value[1:]
    return value

def atomic_write(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

def write_csv(path, fields, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: csv_encode(row.get(key, "")) for key in fields})
    atomic_write(path, buffer.getvalue().encode("utf-8"))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class WrikeClient:
    def __init__(self, token, host="www.wrike.com", opener=None, sleep=time.sleep,
                 now=utcnow, max_retries=6, pace=0.25):
        if host not in HOSTS:
            raise SnapshotError("Unsupported API host.")
        if not token or any(character.isspace() for character in token):
            raise SnapshotError("Enter a nonempty token without whitespace.")
        self._token = token
        self.host = host
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.now = now
        self.max_retries = max_retries
        self.pace = pace
        self.events = []
        self.log_path = None

    def log(self, event):
        event = dict(event, at=timestamp(self.now()))
        self.events.append(event)
        if self.log_path:
            with Path(self.log_path).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
                stream.flush()

    def retry_delay(self, retry_after, attempt):
        server_delay = None
        if retry_after:
            try:
                server_delay = float(retry_after)
            except (TypeError, ValueError):
                try:
                    date = parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    server_delay = (date - self.now()).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    pass
        if server_delay is not None and server_delay >= 0:
            if server_delay > 300:
                raise SnapshotError("Wrike requested a Retry-After delay longer than 300 seconds; retry this capture later.")
            return max(server_delay, 1.0)
        return min(120.0, 5.0 * (2 ** attempt)) + random.uniform(0, 1)

    def get(self, endpoint, params=None):
        space_workflows = re.fullmatch(r"/spaces/[A-Za-z0-9_.:%=-]+/workflows", endpoint)
        entity_lookup = re.fullmatch(r"/(tasks|folders)/[A-Za-z0-9_.:%=,\-]+", endpoint)
        if endpoint not in ("/account", "/contacts", "/tasks", "/folders", "/workflows", "/spaces") and not (space_workflows or entity_lookup):
            raise SnapshotError("Unsupported read endpoint.")
        query = "?" + urlencode(params) if params else ""
        request = Request(
            "https://" + self.host + "/api/v4" + endpoint + query,
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"},
            method="GET",
        )
        for attempt in range(self.max_retries + 1):
            if self.pace:
                self.sleep(self.pace)
            event = {"endpoint": endpoint, "attempt": attempt + 1}
            retry_after = None
            try:
                with self.opener.open(request, timeout=90) as response:
                    status = response.getcode()
                    raw = response.read(64 * 1024 * 1024 + 1)
                if status != 200:
                    self.log(dict(event, http_status=status, outcome="unexpected_response"))
                    raise SnapshotError("Wrike returned an unexpected HTTP response.")
                if len(raw) > 64 * 1024 * 1024:
                    self.log(dict(event, http_status=status, outcome="oversized_response"))
                    raise SnapshotError("Wrike response exceeded the 64 MiB page limit; capture is incomplete.")
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("object required")
                except (ValueError, UnicodeError) as exc:
                    self.log(dict(event, http_status=status, outcome="invalid_json"))
                    raise SnapshotError("Wrike returned invalid JSON; capture is incomplete.") from exc
                self.log(dict(event, http_status=status, outcome="success"))
                return value
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                event.update(http_status=status, outcome="http_error")
                retryable = status in (429, 500, 502, 503, 504)
                message = "Wrike HTTP %s on %s." % (status, endpoint)
                if status in (401, 403):
                    message += " Check token validity, account host and read permissions."
                elif status == 429:
                    message += " Rate limiting persisted; retry later."
            except (URLError, TimeoutError, ConnectionError, OSError, HTTPException):
                # Deliberately do not log str(exc), URLs, headers or response bodies.
                event.update(outcome="network_error")
                status = None
                retryable = True
                message = "Network request failed on " + endpoint + "."
            if not retryable or attempt >= self.max_retries:
                self.log(event)
                raise SnapshotError(message, http_status=status)
            try:
                delay = self.retry_delay(retry_after, attempt)
            except SnapshotError as exc:
                self.log(dict(event, outcome=event["outcome"] + "_retry_deferred"))
                exc.http_status = status
                raise
            self.log(dict(event, retry_in_seconds=round(delay, 3)))
            print("%s Retrying in %.1f seconds." % (message, delay), file=sys.stderr, flush=True)
            remaining = delay
            while remaining > 0:
                chunk = min(remaining, 30.0)
                self.sleep(chunk)
                remaining -= chunk
        raise SnapshotError("Request retry limit reached.")

def only_identity(response, label):
    records = response.get("data")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise SnapshotError("Could not establish a single " + label + " for comparison.")
    identity = records[0].get("id")
    if not isinstance(identity, str) or not identity or identity != identity.strip():
        raise SnapshotError("Wrike returned an invalid " + label + " ID.")
    return identity

def catalog_records(response, scope, space_id, observed_at):
    """Parse one workflow response completely before admitting its entries."""
    if response.get("kind") != "workflows" or not isinstance(response.get("data"), list):
        raise SnapshotError("Workflow response has an invalid kind or data collection.")
    if response.get("nextPageToken"):
        raise SnapshotError("Workflow response unexpectedly indicates more pages; catalog coverage is incomplete.")
    records, workflow_ids = [], set()
    for workflow in response["data"]:
        if not isinstance(workflow, dict):
            raise SnapshotError("Invalid workflow object in catalog.")
        workflow_id, name = workflow.get("id"), workflow.get("name")
        statuses = workflow.get("customStatuses")
        if not isinstance(workflow_id, str) or not workflow_id.strip() or not isinstance(name, str) or not name.strip():
            raise SnapshotError("Workflow catalog contains a missing ID or name.")
        if not isinstance(statuses, list):
            raise SnapshotError("Workflow catalog contains an invalid customStatuses list.")
        workflow_ids.add(workflow_id)
        for status in statuses:
            if not isinstance(status, dict):
                raise SnapshotError("Invalid custom status object in catalog.")
            status_id, status_name = status.get("id"), status.get("name")
            group = status.get("group", "")
            if not isinstance(status_id, str) or not status_id.strip() or not isinstance(status_name, str) or not status_name.strip():
                raise SnapshotError("Workflow catalog contains a missing custom status ID or name.")
            if not isinstance(group, str):
                raise SnapshotError("Workflow catalog contains an invalid status group.")
            def boolean_text(value):
                if value is None:
                    return ""
                if not isinstance(value, bool):
                    raise SnapshotError("Workflow catalog contains an invalid hidden flag.")
                return str(value).lower()
            records.append({
                "workflow_id": workflow_id, "workflow_name": name,
                "custom_status_id": status_id, "custom_status_name": status_name,
                "workflow_status_group": group,
                "workflow_hidden": boolean_text(workflow.get("hidden")),
                "status_hidden": boolean_text(status.get("hidden")),
                "scope": scope, "space_id": space_id, "workflow_observed_at": observed_at,
            })
    return records, workflow_ids

def fetch_workflow_catalog(client, now=utcnow):
    catalog, workflow_ids = [], set()
    report = {"status": "COMPLETE", "errors": [], "spaces_returned": 0,
              "space_lookups_attempted": 0, "space_lookups_succeeded": 0,
              "account_lookup_succeeded": False}

    def record_error(endpoint, exc):
        report["errors"].append({"endpoint": endpoint, "message": str(exc), "http_status": exc.http_status})
        report["status"] = "PARTIAL"

    def read_workflows(endpoint, scope, space_id=""):
        response = client.get(endpoint)
        records, ids = catalog_records(response, scope, space_id, timestamp(now()))
        catalog.extend(records)
        workflow_ids.update(ids)

    try:
        read_workflows("/workflows", "ACCOUNT")
        report["account_lookup_succeeded"] = True
    except SnapshotError as exc:
        record_error("/workflows", exc)
        # Do not continue hammering the API after exhausted rate/server retries.
        if exc.http_status == 401 or exc.http_status == 429 or (exc.http_status and exc.http_status >= 500):
            report["remaining_lookups_skipped"] = True
            return catalog, report
    try:
        response = client.get("/spaces", {"withArchived": "true"})
        spaces = response.get("data")
        if response.get("kind") != "spaces" or not isinstance(spaces, list) or response.get("nextPageToken"):
            raise SnapshotError("Space response is invalid or unexpectedly paginated; space workflow coverage is incomplete.")
        space_ids = set()
        for space in spaces:
            space_id = space.get("id") if isinstance(space, dict) else None
            if not isinstance(space_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:=\-]{1,256}", space_id):
                raise SnapshotError("Space response contains an invalid space ID.")
            space_ids.add(space_id)
        report["spaces_returned"] = len(space_ids)
    except SnapshotError as exc:
        record_error("/spaces", exc)
        space_ids = set()
    for number, space_id in enumerate(sorted(space_ids), 1):
        endpoint = "/spaces/" + quote(space_id, safe="") + "/workflows"
        report["space_lookups_attempted"] += 1
        try:
            read_workflows(endpoint, "SPACE", space_id)
            report["space_lookups_succeeded"] += 1
        except SnapshotError as exc:
            record_error(endpoint, exc)
            if exc.http_status == 401 or exc.http_status == 429 or (exc.http_status and exc.http_status >= 500):
                report["remaining_lookups_skipped"] = number < len(space_ids)
                break
        print("Workflow lookup: space %d of %d." % (number, len(space_ids)), flush=True)
    # Preserve conflicting entries so the mapper can reject ambiguity; remove exact duplicates.
    unique = {tuple(row[key] for key in CATALOG_FIELDS if key != "workflow_observed_at"): row for row in catalog}
    catalog = sorted(unique.values(), key=lambda row: (row["workflow_id"], row["custom_status_id"], row["scope"], row["space_id"]))
    report["workflow_count"] = len(workflow_ids)
    report["custom_status_id_count"] = len({row["custom_status_id"] for row in catalog})
    report["distinct_status_name_count"] = len({row["custom_status_name"] for row in catalog})
    return catalog, report

def previous_complete(output):
    candidates = []
    for path in (output / "runs").glob("*/summary.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError("An existing run summary is unreadable; check the local runs folder before continuing.") from exc
        if not isinstance(manifest, dict) or manifest.get("status") not in ("RUNNING", "INCOMPLETE", "COMPLETE"):
            raise SnapshotError("An existing run summary has an invalid status.")
        if manifest["status"] != "COMPLETE":
            continue
        if manifest.get("schema_version") not in SNAPSHOT_SCHEMAS or manifest.get("csv_encoding") != "apostrophe_v1":
            raise SnapshotError("A completed snapshot uses an unsupported schema or CSV encoding.")
        candidates.append((parse_timestamp(manifest.get("finished_at")), path, manifest))
    if not candidates:
        return None, {}
    _, summary_path, manifest = max(candidates, key=lambda entry: (entry[0], str(entry[1])))
    try:
        name = manifest["files"]["snapshot"]
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".csv"):
            raise SnapshotError("Invalid previous snapshot filename.")
        snapshot_path = summary_path.parent / name
        if snapshot_path.resolve().parent != summary_path.parent.resolve():
            raise SnapshotError("Previous snapshot is outside its run folder.")
        data = snapshot_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["snapshot_sha256"]:
            raise SnapshotError("The previous complete snapshot was edited or corrupted; its checksum does not match.")
        reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
        expected_fields = SNAPSHOT_SCHEMAS[manifest["schema_version"]]
        if reader.fieldnames != expected_fields:
            raise SnapshotError("Previous snapshot columns do not match this script.")
        rows = {}
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise SnapshotError("Previous snapshot has a malformed CSV row.")
            row = {key: csv_decode(value) for key, value in raw.items()}
            if manifest["schema_version"] == 1:
                row.update({key: "" for key in LABEL_FIELDS})
                row["status_name_resolution"] = "NOT_CAPTURED_V1"
            if manifest["schema_version"] < 3:
                row.update({key: "" for key in DETAIL_FIELDS})
                # Aliases of fields actually captured then; no historical lookup.
                row["general_status"] = row["status"]
                row["workflow_status"] = row["custom_status_name"]
                row["task_updated_at"] = row["source_updated_at"]
                row["status_change_time_source"] = UNKNOWN_STATUS_TIME
            task_id = row["task_id"]
            if not task_id or task_id in rows or not row["status"]:
                raise SnapshotError("Previous snapshot has invalid or duplicate task IDs or missing statuses.")
            if row["run_id"] != manifest["run_id"] or row["account_id"] != manifest["identity"]["account_id"]:
                raise SnapshotError("Previous snapshot identity does not match its summary.")
            parse_timestamp(row["observed_at"])
            for key in ("general_status_last_change_observed_at", "workflow_status_last_change_observed_at"):
                if row[key] and parse_timestamp(row[key]) > parse_timestamp(row["observed_at"]):
                    raise SnapshotError("Previous snapshot has a status detection time later than its observation.")
            rows[task_id] = row
        if len(rows) != manifest["counts"]["unique_tasks"]:
            raise SnapshotError("Previous snapshot row count does not match its summary.")
    except (KeyError, OSError, UnicodeError, csv.Error, TypeError) as exc:
        raise SnapshotError("Could not validate the previous complete snapshot; no new baseline will be committed.") from exc
    return manifest, rows


# New project/task capture layer. The helpers above preserve the existing
# snapshot CSV encoding, checksums and hidden-token authentication approach.
GENERAL_GROUPS = {"Active", "Completed", "Deferred", "Cancelled"}
UNKNOWN_STATUS_TIME = "NOT_AVAILABLE_FROM_THIS_TASK_SNAPSHOT"
TASK_FIELDS = ["parentIds", "superTaskIds", "superParentIds", "customItemTypeId"]
FOLDER_FIELDS = ["superParentIds", "customItemTypeId"]
ENTITY_FIELDS = [
    "entity_type", "entity_id", "task_id", "project_id", "title",
    "general_status", "workflow_status", "custom_status_id",
    "workflow_id", "workflow_name", "workflow_status_group",
    "general_status_source", "raw_status", "raw_status_source",
    "status_resolution", "updated_at", "observed_at", "workflow_observed_at",
    "parent_ids_json", "super_task_ids_json", "super_parent_ids_json",
    "project_ids_json", "project_mapping", "custom_item_type_id",
    "account_id", "run_id",
]
LINK_FIELDS = ["task_id", "task_title", "task_general_status", "task_workflow_status",
               "task_custom_status_id", "task_observed_at", "project_id", "project_title",
               "project_general_status", "project_workflow_status", "project_custom_status_id",
               "project_observed_at", "relationship", "mapping_status"]
DIFFERENCE_FIELDS = ["task_id", "previous_title", "current_title", "previous_general_status",
                     "current_general_status", "previous_custom_status_id", "current_custom_status_id",
                     "previous_workflow_status", "current_workflow_status", "general_status_changed",
                     "workflow_status_id_changed", "custom_status_comparable", "result",
                     "previous_observed_at", "current_observed_at"]
csv.field_size_limit(16 * 1024 * 1024)


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:=\-]{1,256}", value):
        raise SnapshotError("Missing or invalid entity ID in API response.")
    return value


def optional_text(record, key):
    value = record.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SnapshotError("Invalid text field: " + key)
    return value


def ids_field(record, key):
    value = record.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise SnapshotError("Invalid relationship list: " + key)
    return [valid_id(item) for item in value]


def record_entry(record, observed, account_id):
    if not isinstance(record, dict):
        raise SnapshotError("Invalid entity object.")
    valid_id(record.get("id"))
    if not isinstance(record.get("title"), str):
        raise SnapshotError("Entity title is missing or invalid.")
    if record.get("accountId") not in (None, account_id):
        raise SnapshotError("Mixed account IDs in API response.")
    return {"data": record, "observed_at": observed}


def fingerprint(record):
    # Ignore unrelated metadata changes, but reject conflicting status/parent
    # observations for one logical ID during pagination.
    project = record.get("project")
    status_object = project if isinstance(project, dict) else record
    return json.dumps({"status": status_object.get("status"),
                       "customStatusId": status_object.get("customStatusId"),
                       "is_project": isinstance(project, dict),
                       "parentIds": sorted(ids_field(record, "parentIds")),
                       "superParentIds": sorted(ids_field(record, "superParentIds")),
                       "superTaskIds": sorted(ids_field(record, "superTaskIds"))}, sort_keys=True)


def collect(client, endpoint, params, account_id, destination, report, raw_path):
    query = dict(params)
    seen_tokens = set()
    report.update(status="RUNNING", pages=0, records=0, duplicates=0, unique=0)
    expected_kind = "tasks" if endpoint == "/tasks" else "folders"
    with raw_path.open("w", encoding="utf-8") as raw_file:
        while True:
            response = client.get(endpoint, query)
            data = response.get("data")
            if response.get("kind") != expected_kind or not isinstance(data, list):
                raise SnapshotError("Unexpected collection response for " + endpoint)
            observed = timestamp(utcnow())
            report["pages"] += 1
            before = len(destination)
            for record in data:
                entry = record_entry(record, observed, account_id)
                entity_id = record["id"]
                if endpoint == "/folders" and not isinstance(record.get("project"), dict):
                    raise SnapshotError("A projects-only request returned a non-project.")
                report["records"] += 1
                if entity_id in destination:
                    report["duplicates"] += 1
                    if fingerprint(destination[entity_id]["data"]) != fingerprint(record):
                        raise SnapshotError("Conflicting repeated ID during pagination: " + entity_id)
                    continue
                destination[entity_id] = entry
                raw_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                report["unique"] = len(destination)
            raw_file.flush()
            print("%s: page %d; %d unique IDs" % (endpoint, report["pages"], len(destination)), flush=True)
            token = response.get("nextPageToken")
            if token is None or token == "":
                break
            if not isinstance(token, str) or not token.strip() or token in seen_tokens:
                raise SnapshotError("Invalid/repeated nextPageToken on " + endpoint)
            if len(destination) == before:
                raise SnapshotError("No pagination progress on " + endpoint)
            seen_tokens.add(token)
            query = dict(params, nextPageToken=token)
    report["status"] = "COMPLETE"


def lookup(client, kind, requested, account_id, issues):
    """Read exact IDs; preserve missing/error IDs, never silently resample."""
    result = {}
    requested = sorted(set(valid_id(x) for x in requested))
    for start in range(0, len(requested), 100):
        batch = requested[start:start + 100]
        endpoint = "/" + kind + "/" + ",".join(quote(x, safe="") for x in batch)
        try:
            response = client.get(endpoint, {"fields": '["customItemTypeId"]'})
        except SnapshotError as exc:
            if exc.http_status not in (403, 404):
                raise
            if len(batch) > 1:
                for entity_id in batch:
                    result.update(lookup(client, kind, [entity_id], account_id, issues))
            else:
                issues.append({"kind": kind, "id": batch[0], "reason": "HTTP_%s_NOT_RETRIEVABLE" % exc.http_status})
            continue
        if response.get("kind") != kind or not isinstance(response.get("data"), list) or response.get("nextPageToken"):
            raise SnapshotError("Unexpected exact-ID response for " + kind)
        seen = set()
        observed = timestamp(utcnow())
        for record in response["data"]:
            entry = record_entry(record, observed, account_id)
            entity_id = record["id"]
            if entity_id not in batch or entity_id in seen:
                raise SnapshotError("Unexpected or duplicate exact-ID result.")
            seen.add(entity_id)
            result[entity_id] = entry
        for entity_id in sorted(set(batch) - seen):
            issues.append({"kind": kind, "id": entity_id, "reason": "NOT_RETURNED_NOT_PROOF_OF_DELETION"})
    return result


def make_catalog_index(catalog):
    index = {}
    for item in catalog:
        identity = tuple(item[k] for k in ("workflow_id", "workflow_name", "custom_status_name", "workflow_status_group"))
        index.setdefault(item["custom_status_id"], {})[identity] = item
    return index


def normalize(entry, entity_type, index, account_id, run_id):
    record = entry["data"]
    source = record if entity_type == "task" else record.get("project")
    if not isinstance(source, dict):
        raise SnapshotError("A requested project no longer has project metadata.")
    raw_status = optional_text(source, "status")
    custom_id = optional_text(source, "customStatusId")
    row = dict.fromkeys(ENTITY_FIELDS, "")
    entity_id = valid_id(record["id"])
    row.update(entity_type=entity_type, entity_id=entity_id, title=record["title"],
               custom_status_id=custom_id, raw_status=raw_status,
               raw_status_source="task.status" if entity_type == "task" else "project.status",
               updated_at=optional_text(record, "updatedDate"), observed_at=entry["observed_at"],
               custom_item_type_id=optional_text(record, "customItemTypeId"),
               account_id=account_id, run_id=run_id)
    row[entity_type + "_id"] = entity_id
    for output, key in (("parent_ids_json", "parentIds"), ("super_task_ids_json", "superTaskIds"),
                        ("super_parent_ids_json", "superParentIds")):
        row[output] = json.dumps(ids_field(record, key)) if key in record else ""
    issues = []
    candidates = index.get(custom_id, {})
    if not custom_id:
        issues.append("MISSING_CUSTOM_STATUS_ID")
    elif not candidates:
        issues.append("UNRESOLVED_WORKFLOW_STATUS_ID")
    elif len(candidates) != 1:
        issues.append("CONFLICTING_WORKFLOW_DEFINITIONS")
    else:
        item = next(iter(candidates.values()))
        row.update(workflow_status=item["custom_status_name"], workflow_id=item["workflow_id"],
                   workflow_name=item["workflow_name"], workflow_status_group=item["workflow_status_group"],
                   workflow_observed_at=item["workflow_observed_at"])
    group = row["workflow_status_group"]
    if entity_type == "task":
        # Preserve the task's API general status even when catalog disagrees.
        row["general_status"] = raw_status
        row["general_status_source"] = "task.status" if raw_status else "NOT_RETURNED"
    elif group in GENERAL_GROUPS:
        # Legacy project.status values (e.g. Green) are retained separately.
        # A project's general lifecycle group comes from its actual custom ID.
        row["general_status"] = group
        row["general_status_source"] = "workflow.customStatuses.group"
    elif raw_status in GENERAL_GROUPS:
        row["general_status"] = raw_status
        row["general_status_source"] = "project.status"
    else:
        row["general_status_source"] = "NOT_RESOLVED"
    if row["general_status"] not in GENERAL_GROUPS:
        issues.append("GENERAL_STATUS_MISSING_OR_UNRECOGNIZED")
    if group and group not in GENERAL_GROUPS:
        issues.append("UNRECOGNIZED_WORKFLOW_GROUP")
    if raw_status in GENERAL_GROUPS and group in GENERAL_GROUPS and raw_status != group:
        issues.append("GENERAL_AND_WORKFLOW_GROUP_DISAGREE")
    row["status_resolution"] = ";".join(issues) if issues else "RESOLVED"
    return row


def parent_refs(kind, entry):
    record = entry["data"]
    refs = {( "folders", x) for key in ("parentIds", "superParentIds") for x in ids_field(record, key)}
    if kind == "tasks":
        refs.update(("tasks", x) for x in ids_field(record, "superTaskIds"))
    return refs


def resolve_ancestors(client, tasks, projects, account_id, root_ids, issues):
    """Traverse all visible parent paths, including inherited subtask parents."""
    nodes = {("tasks", key): value for key, value in tasks.items()}
    nodes.update({("folders", key): value for key, value in projects.items()})
    attempted = set(nodes)
    frontier = set()
    for value in tasks.values():
        frontier.update(parent_refs("tasks", value))
    expanded = set()
    while frontier:
        missing = frontier - attempted - {("folders", value) for value in root_ids}
        for kind in ("tasks", "folders"):
            ids = sorted(key for k, key in missing if k == kind)
            if ids:
                print("Resolving %d %s in parent paths..." % (len(ids), kind), flush=True)
                retrieved = lookup(client, kind, ids, account_id, issues)
                nodes.update({(kind, key): value for key, value in retrieved.items()})
                attempted.update((kind, key) for key in ids)
        next_frontier = set()
        for ref in frontier - expanded:
            if ref in nodes:
                next_frontier.update(parent_refs(ref[0], nodes[ref]))
        expanded.update(frontier)
        frontier = next_frontier - expanded
    return nodes


def project_membership(task_entry, nodes, root_ids):
    # Per-task DFS detects cycles without treating shared ancestors as cycles.
    found, errors, done, active = set(), set(), set(), set()
    stack = [(ref, False) for ref in parent_refs("tasks", task_entry)]
    if "parentIds" not in task_entry["data"] or "superTaskIds" not in task_entry["data"]:
        errors.add("PARENT_METADATA_NOT_RETURNED")
    while stack:
        ref, exiting = stack.pop()
        if exiting:
            active.discard(ref)
            done.add(ref)
            continue
        if ref in active:
            errors.add("PARENT_CYCLE")
            continue
        if ref in done or (ref[0] == "folders" and ref[1] in root_ids):
            continue
        entry = nodes.get(ref)
        if entry is None:
            errors.add("UNRESOLVED_PARENT")
            continue
        active.add(ref)
        stack.append((ref, True))
        record = entry["data"]
        if ref[0] == "folders" and isinstance(record.get("project"), dict):
            found.add(ref[1])
        if "parentIds" not in record or (ref[0] == "tasks" and "superTaskIds" not in record):
            errors.add("PARENT_METADATA_NOT_RETURNED")
        stack.extend((parent, False) for parent in parent_refs(ref[0], entry))
    return sorted(found), ";".join(sorted(errors)) if errors else ("RESOLVED_VISIBLE_PATHS" if found else "NO_PROJECT_ON_VISIBLE_PATHS")


def sample_comparison(previous, selected_ids, current):
    result = []
    for task_id in selected_ids:
        old, new = previous[task_id], current.get(task_id)
        row = dict.fromkeys(DIFFERENCE_FIELDS, "")
        row.update(task_id=task_id, previous_title=old["title"], previous_general_status=old["status"],
                   previous_custom_status_id=old["custom_status_id"],
                   previous_workflow_status=old.get("custom_status_name", ""), previous_observed_at=old["observed_at"])
        if new is None:
            row["result"] = "NOT_RETRIEVED_NOT_PROOF_OF_DELETION"
        else:
            comparable = bool(old["custom_status_id"] and new["custom_status_id"])
            general_comparable = old["status"] in GENERAL_GROUPS and new["general_status"] in GENERAL_GROUPS
            general_changed = general_comparable and old["status"] != new["general_status"]
            custom_changed = comparable and old["custom_status_id"] != new["custom_status_id"]
            coverage_changed = bool(old["custom_status_id"]) != bool(new["custom_status_id"])
            row.update(current_title=new["title"], current_general_status=new["general_status"],
                       current_custom_status_id=new["custom_status_id"], current_workflow_status=new["workflow_status"],
                       current_observed_at=new["observed_at"], general_status_changed=general_changed if general_comparable else "",
                       workflow_status_id_changed=custom_changed if comparable else "",
                       custom_status_comparable=comparable,
                       result="STATUS_FIELDS_INCOMPLETE" if not general_comparable else
                       ("STATUS_DIFFERED" if general_changed or custom_changed else
                       ("CUSTOM_STATUS_COVERAGE_CHANGED" if coverage_changed else
                        ("NO_STATUS_DIFFERENCE" if comparable else "GENERAL_UNCHANGED_WORKFLOW_NOT_COMPARABLE"))))
        result.append(row)
    return result


def run_capture(args, client):
    run_id = utcnow().strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8]
    directory = args.output.expanduser().resolve() / args.mode / run_id
    directory.mkdir(parents=True, exist_ok=False)
    client.log_path = directory / "requests.jsonl"
    report = {"schema_version": 1, "script_version": "1.0", "mode": args.mode, "run_id": run_id,
              "started_at": timestamp(utcnow()), "status": "RUNNING", "errors": [], "warnings": [],
              "api_scope": "Token-visible live projects and tasks/subtasks, no date or status filters; not recycle-bin or blueprint APIs.",
              "timestamp_meaning": "updated_at is entity updatedDate, not status change time; observed_at is our observation.",
              "comparison_limit": "Differences between observations cannot reveal intermediate changes or exact transition times.",
              "csv_encoding": "apostrophe_v1 (formula-leading text is prefixed; use csv_decode to reverse)",
              "project_relationship": "All reachable visible ancestor projects, not a claim of one exclusive owner.",
              "files": {}, "lookups_not_retrieved": []}
    write_json(directory / "summary.json", report)
    tasks, projects, selected_projects = {}, {}, {}
    old_rows, old_manifest, selected_ids = {}, None, []
    task_rows, project_rows = {}, {}
    try:
        if args.mode == "sample":
            old_manifest, old_rows = previous_complete(args.task_snapshot_root.expanduser())
            if not old_manifest or not old_rows:
                raise SnapshotError("No COMPLETE task baseline found under --task-snapshot-root. Point it to the existing output/task_snapshots folder; no API scan has started.")
        account_response = client.get("/account")
        account_id = only_identity(account_response, "account")
        identity = {"account_id": account_id, "user_id": only_identity(client.get("/contacts", {"me": "true"}), "user"),
                    "host": client.host, "scope": SCOPE}
        report["identity"] = identity
        root_ids = {account_response["data"][0].get(key) for key in ("rootFolderId", "recycleBinId")}
        root_ids.discard(None)
        report["known_root_container_ids"] = sorted(root_ids)
        if old_manifest and old_manifest["identity"] != identity:
            raise SnapshotError("Token account/user/host differs from the saved task baseline. Use the same identity for this sample.")
        report["project_inventory"] = {}
        collect(client, "/folders", {"project": "true", "deleted": "false", "pageSize": 1000,
                                     "fields": json.dumps(FOLDER_FIELDS)}, account_id, projects,
                report["project_inventory"], directory / "raw_project_inventory.jsonl")
        if args.mode == "sample":
            rng = random.Random(args.seed) if args.seed is not None else random.SystemRandom()
            selected_ids = rng.sample(sorted(old_rows), min(args.count, len(old_rows)))
            selected_project_ids = rng.sample(sorted(projects), min(args.count, len(projects)))
            report["selection"] = {"method": "random.sample without replacement", "seed": args.seed,
                                   "task_frame": "IDs from saved COMPLETE task snapshot; fresh statuses fetched by ID",
                                   "task_frame_run_id": old_manifest["run_id"], "task_frame_finished_at": old_manifest["finished_at"],
                                   "task_frame_count": len(old_rows), "task_frame_sha256": old_manifest["snapshot_sha256"],
                                   "project_frame": "Current fully paginated live project inventory",
                                   "project_frame_count": len(projects), "task_ids": selected_ids,
                                   "project_ids": selected_project_ids, "requested_per_type": args.count}
            write_json(directory / "selection.json", report["selection"])
            print("Fetching current statuses for %d sampled task IDs and %d sampled project IDs..." % (len(selected_ids), len(selected_project_ids)), flush=True)
            tasks = lookup(client, "tasks", selected_ids, account_id, report["lookups_not_retrieved"])
            selected_projects = lookup(client, "folders", selected_project_ids, account_id, report["lookups_not_retrieved"])
            projects.update(selected_projects)
            report["task_inventory"] = {"status": "SAMPLE_ONLY", "unique": len(tasks)}
            if len(selected_ids) < args.count or len(selected_project_ids) < args.count:
                report["warnings"].append("Fewer than the requested number of IDs exist in at least one sampling frame.")
        else:
            report["task_inventory"] = {}
            collect(client, "/tasks", {"subTasks": "true", "pageSize": 1000, "fields": json.dumps(TASK_FIELDS)},
                    account_id, tasks, report["task_inventory"], directory / "raw_tasks.jsonl")
            selected_projects = projects
        with (directory / "raw_selected_entities.jsonl").open("w", encoding="utf-8") as stream:
            if args.mode == "sample":
                for kind, entries in (("task", tasks), ("project", selected_projects)):
                    for entry in entries.values():
                        stream.write(json.dumps(dict(entry, entity_type=kind), ensure_ascii=False) + "\n")
        print("Resolving account and space workflow names...", flush=True)
        catalog, catalog_report = fetch_workflow_catalog(client)
        report["workflow_lookup"] = catalog_report
        write_csv(directory / "workflow_statuses.csv", CATALOG_FIELDS, catalog)
        index = make_catalog_index(catalog)
        for entity_id, entry in tasks.items():
            task_rows[entity_id] = normalize(entry, "task", index, account_id, run_id)
        for entity_id, entry in selected_projects.items():
            project_rows[entity_id] = normalize(entry, "project", index, account_id, run_id)
        # Save status evidence before optional ancestry lookups, so failures
        # resolving a parent cannot erase successfully observed task statuses.
        write_csv(directory / "tasks.csv", ENTITY_FIELDS, task_rows.values())
        write_csv(directory / "projects.csv", ENTITY_FIELDS, project_rows.values())
        mapping_issues = []
        try:
            nodes = resolve_ancestors(client, tasks, projects, account_id, root_ids, mapping_issues)
        except SnapshotError as exc:
            nodes = {("tasks", key): value for key, value in tasks.items()}
            nodes.update({("folders", key): value for key, value in projects.items()})
            mapping_issues.append({"kind": "ancestry", "id": "", "reason": str(exc)})
        report["ancestry_lookup_issues"] = mapping_issues
        with (directory / "raw_extra_ancestors.jsonl").open("w", encoding="utf-8") as stream:
            for (kind, entity_id), entry in nodes.items():
                if (kind == "tasks" and entity_id not in tasks) or (kind == "folders" and entity_id not in projects):
                    stream.write(json.dumps(dict(entry, entity_type=kind), ensure_ascii=False) + "\n")
        links, mapping_partial = [], 0
        related_project_rows = dict(project_rows)
        for task_id, entry in tasks.items():
            project_ids, mapping = project_membership(entry, nodes, root_ids)
            if mapping not in ("RESOLVED_VISIBLE_PATHS", "NO_PROJECT_ON_VISIBLE_PATHS"):
                mapping_partial += 1
            row = task_rows[task_id]
            row.update(project_ids_json=json.dumps(project_ids), project_mapping=mapping)
            direct = set(ids_field(entry["data"], "parentIds"))
            for project_id in project_ids:
                if project_id not in related_project_rows:
                    related_project_rows[project_id] = normalize(nodes[("folders", project_id)], "project", index, account_id, run_id)
                parent = related_project_rows[project_id]
                links.append({"task_id": task_id, "task_title": row["title"], "task_general_status": row["general_status"],
                              "task_workflow_status": row["workflow_status"], "task_custom_status_id": row["custom_status_id"],
                              "task_observed_at": row["observed_at"], "project_id": project_id, "project_title": parent["title"],
                              "project_general_status": parent["general_status"], "project_workflow_status": parent["workflow_status"],
                              "project_custom_status_id": parent["custom_status_id"], "project_observed_at": parent["observed_at"],
                              "relationship": "DIRECT_PROJECT_PARENT" if project_id in direct else "ANCESTOR_OR_INHERITED_PROJECT",
                              "mapping_status": mapping})
        report["project_mapping"] = {"status": "PARTIAL" if mapping_partial or mapping_issues else "COMPLETE_VISIBLE_PATHS",
                                     "tasks_with_incomplete_mapping": mapping_partial, "task_project_pairs": len(links)}
        write_csv(directory / "tasks.csv", ENTITY_FIELDS, task_rows.values())
        write_csv(directory / "task_project_statuses.csv", LINK_FIELDS, links)
        write_csv(directory / "related_projects.csv", ENTITY_FIELDS, related_project_rows.values())
        all_rows = list(task_rows.values()) + list(project_rows.values())
        unresolved = [row for row in all_rows if row["status_resolution"] != "RESOLVED"]
        write_csv(directory / "status_issues.csv", ENTITY_FIELDS, unresolved)
        report["counts"] = {"tasks": len(task_rows), "projects": len(project_rows), "status_issues": len(unresolved),
                            "related_project_status_issues": sum(row["status_resolution"] != "RESOLVED" for row in related_project_rows.values())}
        if args.mode == "sample":
            comparisons = sample_comparison(old_rows, selected_ids, task_rows)
            write_csv(directory / "sample_previous_comparison.csv", DIFFERENCE_FIELDS, comparisons)
            report["sample_task_status_differences"] = sum(row["result"] == "STATUS_DIFFERED" for row in comparisons)
            enough = len(task_rows) == args.count and len(project_rows) == args.count
        else:
            enough = bool(task_rows) and bool(project_rows)
        report["status_validation"] = "PASS" if enough and not unresolved and not report["lookups_not_retrieved"] else "NEEDS_REVIEW"
        report["status"] = "SAMPLE_COMPLETE" if args.mode == "sample" else "CAPTURE_COMPLETE"
        if report["lookups_not_retrieved"]:
            report["status"] = "SAMPLE_INCOMPLETE"
        if not tasks or not selected_projects:
            report["warnings"].append("An empty task/project result does not prove there are no accessible entities; verify scope and access.")
        if args.mode == "sample":
            report["warnings"].append("A sample checks extraction and mapping, not every status definition or account-wide coverage.")
        if catalog_report["status"] != "COMPLETE":
            report["warnings"].append("Workflow catalog lookup is partial; inspect lookup errors and status_issues.csv.")
    except (SnapshotError, OSError, KeyboardInterrupt) as exc:
        report["status"] = "INCOMPLETE"
        report["errors"].append(str(exc) if isinstance(exc, SnapshotError) else
                                ("Interrupted by user." if isinstance(exc, KeyboardInterrupt) else "Local file operation failed."))
    report["finished_at"] = timestamp(utcnow())
    for name in ("task_inventory", "project_inventory"):
        if report.get(name, {}).get("status") == "RUNNING":
            report[name]["status"] = "INCOMPLETE"
    report["request_counts"] = {"attempts": len(client.events),
                                 "failed_attempts": sum(e.get("outcome") != "success" for e in client.events),
                                 "retries_scheduled": sum("retry_in_seconds" in e for e in client.events)}
    for path in directory.iterdir():
        if not path.is_file() or path.name == "summary.json":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        report["files"][path.name] = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
    write_json(directory / "summary.json", report)
    print("\n%s | status validation: %s" % (report["status"], report.get("status_validation", "NOT_COMPLETED")))
    print("Captured tasks: %d | selected projects: %d" % (len(task_rows), len(project_rows)))
    for row in list(task_rows.values())[:args.count] + list(project_rows.values())[:args.count]:
        # JSON escaping prevents titles with terminal controls from being executed.
        print(json.dumps({key: row[key] for key in ("entity_type", "entity_id", "title", "general_status", "workflow_status", "status_resolution")}, ensure_ascii=True))
    for message in report["errors"] + report["warnings"]:
        print(message)
    print("Output folder: " + str(directory))
    print("Run report: " + str(directory / "summary.json"))
    success = report["status"] in ("SAMPLE_COMPLETE", "CAPTURE_COMPLETE") and report.get("status_validation") == "PASS"
    return (0 if success else 1), report, directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("sample", "all"), help="sample: fresh lookup of 5 tasks + 5 projects; all: paginated full live capture")
    parser.add_argument("--count", type=int, default=5, help="Sample size per entity type (default 5; max 100)")
    parser.add_argument("--seed", type=int, help="Optional reproducible random selection within the same ID sets")
    parser.add_argument("--task-snapshot-root", type=Path, default=Path("output/task_snapshots"), help="Existing task baseline folder for sample mode")
    parser.add_argument("--output", type=Path, default=Path("output/status_capture"), help="New dated local outputs (separate sample/all folders)")
    parser.add_argument("--host", choices=HOSTS, default=HOSTS[0])
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 100:
        parser.error("--count must be between 1 and 100")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Wrike token (hidden; used only for this run): ").strip()
        return run_capture(args, WrikeClient(token, host=args.host))[0]
    except getpass.GetPassWarning:
        print("Run in Terminal so the token can be entered without displaying it.", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
    except SnapshotError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
    except OSError:
        print("Could not write local files; check permissions and free space.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
