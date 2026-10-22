"""Audit journal for gate-n-guard.

An optional, append-only JSONL decision journal. Setting GATE_N_GUARD_JOURNAL
gives `record` a file to append to and `audit` a file to read back. Without a
path both raise JournalError; `check` never touches the journal.

Records are written by `record_decision` with a server-assigned id (uuid4)
and UTC timestamp, then never mutated or deleted — there is no rewrite or
delete path here. `read_journal` returns records newest-first with filters
and never fails on a malformed line: unparseable lines are skipped and
counted.

Single-line appends are atomic on POSIX for journal-sized lines (< PIPE_BUF);
concurrent writers interleave whole lines, never partial ones.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .rules import VERDICTS

MAX_AUDIT_LIMIT = 1000

_OPTIONAL_STRINGS = ("rulebook", "outcome", "actor", "note")


class JournalError(ValueError):
    """The journal is not configured or a record/audit parameter is invalid."""


def record_decision(
    journal_path: Optional[str],
    *,
    action: dict,
    verdict: str,
    matched: Optional[list] = None,
    rulebook: Optional[str] = None,
    outcome: Optional[str] = None,
    actor: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Append one decision record to the journal and return it.

    Raises JournalError when the journal is unconfigured or a field is
    malformed. Records carry only the fields the caller provided (plus id and
    ts), so the JSONL schema is a convention, not a fixed shape.
    """
    path = _require_path(journal_path, "record")
    if not isinstance(action, dict) or not isinstance(action.get("tool"), str) \
            or not action["tool"]:
        raise JournalError("record: action must be an object with a non-empty string tool")
    if verdict not in VERDICTS:
        raise JournalError(f"record: verdict must be one of {VERDICTS!r}, got {verdict!r}")
    if matched is not None and (
        not isinstance(matched, list)
        or not all(isinstance(item, str) and item for item in matched)
    ):
        raise JournalError("record: matched must be a list of non-empty rule id strings")
    for name in _OPTIONAL_STRINGS:
        value = locals()[name]
        if value is not None and not isinstance(value, str):
            raise JournalError(f"record: {name} must be a string")

    record = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "verdict": verdict,
    }
    if matched is not None:
        record["matched"] = matched
    for name in _OPTIONAL_STRINGS:
        value = locals()[name]
        if value is not None:
            record[name] = value

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_journal(
    journal_path: Optional[str],
    *,
    limit: int = 50,
    since: Optional[str] = None,
    before: Optional[str] = None,
    verdict: Optional[str] = None,
    outcome: Optional[str] = None,
    tool: Optional[str] = None,
) -> dict:
    """Read the journal newest-first, applying filters.

    Returns {"count": N, "skipped": M, "records": [...]}. A line that is not
    valid JSON, is not an object, or lacks a parseable `ts` is skipped and
    counted in `skipped` — one bad line never fails an audit. A configured
    but never-written file yields count 0, not an error.
    """
    path = _require_path(journal_path, "audit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise JournalError("audit: limit must be a positive integer")
    limit = min(limit, MAX_AUDIT_LIMIT)
    if verdict is not None and verdict not in VERDICTS:
        raise JournalError(f"audit: verdict must be one of {VERDICTS!r}, got {verdict!r}")
    since_dt = _parse_filter_time("since", since)
    before_dt = _parse_filter_time("before", before)
    tool = _require_filter_string("tool", tool)
    outcome_filter = _require_filter_string("outcome", outcome)

    if not Path(path).is_file():
        return {"count": 0, "skipped": 0, "records": []}

    parsed = []
    skipped = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            record = _parse_line(line)
            if record is None:
                skipped += 1
                continue
            parsed.append(record)

    matched_records = []
    for record in parsed:
        ts = _parse_ts(record)
        if ts is None:
            skipped += 1
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if before_dt is not None and ts >= before_dt:
            continue
        if verdict is not None and record.get("verdict") != verdict:
            continue
        if outcome_filter is not None and record.get("outcome") != outcome_filter:
            continue
        if tool is not None and not isinstance(record.get("action"), dict):
            continue
        if tool is not None and record["action"].get("tool") != tool:
            continue
        record["__ts"] = ts
        matched_records.append(record)

    matched_records.sort(key=lambda r: (r["__ts"], r["ts"]), reverse=True)
    records = [{k: v for k, v in r.items() if k != "__ts"} for r in matched_records[:limit]]
    return {"count": len(records), "skipped": skipped, "records": records}


def _require_path(journal_path: Optional[str], tool: str) -> str:
    if not journal_path:
        raise JournalError(
            f"{tool}: journal not configured — set GATE_N_GUARD_JOURNAL to enable"
        )
    return journal_path


def _require_filter_string(name: str, value: Optional[str]) -> Optional[str]:
    if value is not None and (not isinstance(value, str) or not value):
        raise JournalError(f"audit: {name} must be a non-empty string")
    return value


def _parse_filter_time(name: str, value: Optional[str]):
    if value is None:
        return None
    if not isinstance(value, str):
        raise JournalError(f"audit: {name} must be an ISO-8601 string")
    parsed = _parse_ts({"ts": value})
    if parsed is None:
        raise JournalError(
            f"audit: {name} must be a parseable ISO-8601 timestamp, got {value!r}"
        )
    return parsed


def _parse_line(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    if "id" not in record or "ts" not in record or "verdict" not in record:
        return None
    return record


def _parse_ts(record: dict):
    ts = record["ts"]
    if not isinstance(ts, str):
        return None
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
