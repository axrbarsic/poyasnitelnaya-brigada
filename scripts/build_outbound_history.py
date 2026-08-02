#!/usr/bin/env python3
"""Build one exact outbound conversation-history snapshot from evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Sequence


FORBIDDEN = ("\u2013", "\u2014", "\u00a0", "\u200b", "\u200c", "\u200d", "\ufeff")
HANDLE_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}")


def required_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required text field: {name}")
    return value.strip()


def optional_text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid optional text field: {name}")
    return value.strip()


def validate_status_id(value: str, name: str) -> str:
    if not value.isdigit() or len(value) > 19:
        raise ValueError(f"{name} must be a numeric X status ID")
    return value


def status_id(payload: dict[str, Any], name: str) -> str:
    return validate_status_id(required_text(payload, name), name)


def author_handle(payload: dict[str, Any]) -> str:
    value = required_text(payload, "author_handle").lstrip("@")
    if HANDLE_PATTERN.fullmatch(value) is None:
        raise ValueError("author_handle must be a valid X handle")
    return f"@{value}"


def canonical_status_url(
    payload: dict[str, Any],
    *,
    expected_status_id: str,
    field_name: str,
) -> str:
    handle = author_handle(payload).lstrip("@")
    expected = f"https://x.com/{handle}/status/{expected_status_id}"
    value = required_text(payload, "url")
    if value != expected:
        raise ValueError(
            f"{field_name} URL does not match author and status ID"
        )
    return value


def read_exact_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text


def resolve_evidence_file(
    evidence_path: Path,
    value: str,
) -> Path:
    directory = evidence_path.resolve().parent
    candidate = (directory / value).resolve()
    if candidate.parent != directory:
        raise ValueError("evidence text files must stay in one directory")
    if not candidate.is_file():
        raise ValueError(f"evidence text file does not exist: {candidate}")
    return candidate


def target_exact_text(
    evidence_path: Path,
    target: dict[str, Any],
) -> str:
    value = target.get("exact_text")
    if isinstance(value, str):
        return value
    filename = required_text(target, "exact_text_file")
    return read_exact_file(resolve_evidence_file(evidence_path, filename))


def reply_exact_text(
    evidence_path: Path,
    reply: dict[str, Any],
    *,
    maximum_length: int,
    enforce_publication_constraints: bool = True,
) -> str:
    filename = required_text(reply, "file")
    text = read_exact_file(resolve_evidence_file(evidence_path, filename))
    if not enforce_publication_constraints:
        return text
    forbidden = [character for character in FORBIDDEN if character in text]
    if forbidden:
        raise ValueError("reply contains forbidden Unicode characters")
    if not text:
        raise ValueError("reply must not be empty")
    if len(text) > maximum_length:
        raise ValueError(
            f"reply must contain at most {maximum_length} code points, "
            f"got {len(text)}"
        )
    return text


def build_record(
    evidence_path: Path,
    *,
    maximum_length: int = 4000,
) -> dict[str, Any]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ValueError("evidence root must be an object")
    return build_record_from_payload(
        evidence_path,
        evidence,
        maximum_length=maximum_length,
        enforce_reply_constraints=True,
    )


def build_record_from_payload(
    evidence_path: Path,
    evidence: dict[str, Any],
    *,
    maximum_length: int = 4000,
    enforce_reply_constraints: bool = True,
) -> dict[str, Any]:
    """Build history from an already parsed evidence object."""

    target = evidence.get("target")
    reply = evidence.get("reply")
    if not isinstance(target, dict):
        raise ValueError("evidence must contain a target object")
    if reply is not None and not isinstance(reply, dict):
        raise ValueError("evidence reply must be an object")

    target_status_id = status_id(target, "status_id")
    target_parent_status_id = optional_text(target, "parent_status_id")
    if target_parent_status_id is not None:
        target_parent_status_id = validate_status_id(
            target_parent_status_id,
            "target.parent_status_id",
        )
    chain_id = optional_text(target, "chain_id") or target_status_id
    root_status_id = optional_text(target, "root_status_id") or chain_id
    chain_provenance = (
        optional_text(target, "chain_provenance")
        or optional_text(evidence, "chain_provenance")
        or "pro"
    )
    chain_id = validate_status_id(chain_id, "target.chain_id")
    root_status_id = validate_status_id(
        root_status_id,
        "target.root_status_id",
    )

    target_media = target.get("media", [])
    sources = evidence.get("sources", [])
    if not isinstance(target_media, list):
        raise ValueError("target media must be an array")
    if not isinstance(sources, list) or not all(
        isinstance(value, str) and value.strip() for value in sources
    ):
        raise ValueError("sources must be an array of non-empty strings")

    target_turn = {
        "status_id": target_status_id,
        "parent_status_id": target_parent_status_id,
        "actor": "target",
        "author": author_handle(target),
        "url": canonical_status_url(
            target,
            expected_status_id=target_status_id,
            field_name="target",
        ),
        "exact_text": target_exact_text(evidence_path, target),
        "posted_at": optional_text(
            target,
            "posted_at",
        )
        or optional_text(target, "published_at"),
        "provenance": optional_text(target, "provenance") or "pro",
        "media": target_media,
        "source_urls": [],
    }
    record = {
        "chain_id": chain_id,
        "root_status_id": root_status_id,
        "provenance": chain_provenance,
        "turns": [target_turn],
    }
    if chain_id == target_status_id:
        record["ledger_reference"] = str(evidence_path)
    if reply is None:
        return record

    reply_status_id = status_id(reply, "status_id")
    reply_parent_status_id = status_id(reply, "parent_status_id")
    if reply_parent_status_id != target_status_id:
        raise ValueError("reply parent_status_id does not match target")
    reply_url = canonical_status_url(
        reply,
        expected_status_id=reply_status_id,
        field_name="reply",
    )

    reply_turn = {
        "status_id": reply_status_id,
        "parent_status_id": reply_parent_status_id,
        "actor": "alex",
        "author": author_handle(reply),
        "url": reply_url,
        "exact_text": reply_exact_text(
            evidence_path,
            reply,
            maximum_length=maximum_length,
            enforce_publication_constraints=enforce_reply_constraints,
        ),
        "posted_at": optional_text(reply, "posted_at"),
        "provenance": optional_text(reply, "provenance") or "pro",
        "media": [],
        "source_urls": [value.strip() for value in sources],
    }
    record["turns"].append(reply_turn)
    return record


def write_record(output_path: Path, record: dict[str, Any]) -> str:
    serialized = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if output_path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing history output conflicts with evidence")
        return "unchanged"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(serialized)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, output_path)
    return "created"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max", dest="maximum", type=int, default=4000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.maximum <= 0:
        raise ValueError("--max must be positive")
    record = build_record(
        arguments.evidence,
        maximum_length=arguments.maximum,
    )
    state = write_record(arguments.output, record)
    print(
        json.dumps(
            {
                "state": state,
                "output": str(arguments.output),
                "chain_id": record["chain_id"],
                "status_ids": [
                    turn["status_id"] for turn in record["turns"]
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {"state": "error", "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from error
