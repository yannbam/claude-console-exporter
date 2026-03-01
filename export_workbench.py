#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "untitled"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def parse_cookie_header(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


@dataclass(frozen=True)
class ApiConfig:
    base_url: str = "https://platform.claude.com"
    timeout_seconds: float = 60.0
    max_retries: int = 5
    retry_backoff_seconds: float = 0.8


class ClaudeApi:
    def __init__(self, config: ApiConfig, cookie_header: str) -> None:
        self.config = config
        self.cookie_header = cookie_header

    def request_json(self, path: str, method: str = "GET", body: Any | None = None) -> Any:
        if not path.startswith("/"):
            raise ValueError(f"Path must start with '/': {path}")
        url = f"{self.config.base_url}{path}"
        data = None
        headers = {
            "accept": "application/json",
            "x-requested-with": "XMLHttpRequest",
            "cookie": self.cookie_header,
            "origin": "https://platform.claude.com",
            "referer": "https://platform.claude.com/workbench",
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
        }
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        def should_retry(attempt: int) -> bool:
            return attempt < self.config.max_retries

        def backoff_sleep(attempt: int) -> None:
            time.sleep(self.config.retry_backoff_seconds * (2**attempt))

        for attempt in range(self.config.max_retries + 1):
            req = request.Request(url=url, method=method, data=data, headers=headers)
            try:
                with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except error.HTTPError as exc:
                status = exc.code
                body_preview = exc.read().decode("utf-8", errors="replace")[:300]
                if status in RETRYABLE_STATUS and should_retry(attempt):
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(
                    f"{method} {path} failed with HTTP {status}: {body_preview}"
                ) from exc
            except error.URLError as exc:
                if should_retry(attempt):
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"{method} {path} failed: {exc}") from exc
            except (TimeoutError, socket.timeout) as exc:
                if should_retry(attempt):
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"{method} {path} timed out: {exc}") from exc
            except OSError as exc:
                if "timed out" in str(exc).lower() and should_retry(attempt):
                    backoff_sleep(attempt)
                    continue
                raise RuntimeError(f"{method} {path} failed: {exc}") from exc


@dataclass
class ExportResult:
    prompt_id: str
    prompt_name: str
    downloaded: bool
    revisions: int
    evaluations: int


class WorkbenchExporter:
    def __init__(
        self,
        api: ClaudeApi,
        org_id: str,
        output_root: Path,
        force_refresh: bool = False,
    ) -> None:
        self.api = api
        self.org_id = org_id
        self.output_root = output_root
        self.force_refresh = force_refresh

    def list_prompt_ids(self) -> list[str]:
        prompts = self.api.request_json(
            f"/api/organizations/{self.org_id}/workbench/prompts"
        )
        if not isinstance(prompts, list):
            raise RuntimeError("Prompt list endpoint did not return an array.")
        return [p["id"] for p in prompts if isinstance(p, dict) and p.get("id")]

    def _resolve_prompt_dir(self, prompt_id: str, prompt_name: str) -> Path:
        desired = self.output_root / f"{slugify(prompt_name)}-{prompt_id}"
        if desired.exists():
            return desired
        matches = sorted(self.output_root.glob(f"*-{prompt_id}"))
        if matches:
            return matches[0]
        return desired

    @staticmethod
    def _json_file_stems(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {p.stem for p in path.glob("*.json") if p.is_file()}

    def _is_prompt_synced(self, prompt_dir: Path, remote_revision_ids: list[str]) -> bool:
        prompt_path = prompt_dir / "prompt.json"
        if not prompt_path.exists():
            return False
        remote_set = set(remote_revision_ids)
        local_revision_ids = self._json_file_stems(prompt_dir / "revisions")
        local_evaluation_ids = self._json_file_stems(prompt_dir / "evaluations")
        return local_revision_ids == remote_set and local_evaluation_ids == remote_set

    def export_prompt(self, prompt_id: str) -> ExportResult:
        prompt = self.api.request_json(
            f"/api/organizations/{self.org_id}/workbench/prompts/{prompt_id}"
        )
        if not isinstance(prompt, dict):
            raise RuntimeError(f"Prompt {prompt_id}: prompt endpoint did not return an object.")

        prompt_name = str(prompt.get("name") or "untitled")
        compact = self.api.request_json(
            f"/api/organizations/{self.org_id}/workbench/prompts/{prompt_id}/revisions?compact=true"
        )
        if not isinstance(compact, list):
            raise RuntimeError(
                f"Prompt {prompt_id}: revisions endpoint did not return an array."
            )
        remote_revision_ids = [
            revision["id"]
            for revision in compact
            if isinstance(revision, dict) and revision.get("id")
        ]

        prompt_dir = self._resolve_prompt_dir(prompt_id, prompt_name)
        revisions_dir = prompt_dir / "revisions"
        evaluations_dir = prompt_dir / "evaluations"
        if (not self.force_refresh) and self._is_prompt_synced(prompt_dir, remote_revision_ids):
            return ExportResult(
                prompt_id=prompt_id,
                prompt_name=prompt_name,
                downloaded=False,
                revisions=len(remote_revision_ids),
                evaluations=0,
            )

        prompt_dir.mkdir(parents=True, exist_ok=True)
        if revisions_dir.exists():
            shutil.rmtree(revisions_dir)
        if evaluations_dir.exists():
            shutil.rmtree(evaluations_dir)
        revisions_dir.mkdir(parents=True, exist_ok=True)
        evaluations_dir.mkdir(parents=True, exist_ok=True)
        write_json(prompt_dir / "prompt.json", prompt)

        revision_count = 0
        evaluation_count = 0
        for rev_id in remote_revision_ids:
            revision_count += 1

            revision_path = revisions_dir / f"{rev_id}.json"
            revision_full = self.api.request_json(
                f"/api/organizations/{self.org_id}/workbench/prompts/{prompt_id}/revisions/{rev_id}"
            )
            write_json(revision_path, revision_full)

            evaluations_path = evaluations_dir / f"{rev_id}.json"
            evaluations = self.api.request_json(
                f"/api/organizations/{self.org_id}/workbench/revisions/{rev_id}/evaluations/list"
            )
            if not isinstance(evaluations, list):
                raise RuntimeError(
                    f"Prompt {prompt_id} revision {rev_id}: evaluations endpoint "
                    "did not return an array."
                )
            write_json(evaluations_path, evaluations)
            evaluation_count += len(evaluations)

        return ExportResult(
            prompt_id=prompt_id,
            prompt_name=prompt_name,
            downloaded=True,
            revisions=revision_count,
            evaluations=evaluation_count,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Claude Workbench prompts, revisions, and per-revision evaluations."
    )
    parser.add_argument("--org-id", help="Organization UUID. Defaults to cookie lastActiveOrg.")
    parser.add_argument(
        "--prompt-id",
        action="append",
        default=[],
        help="Prompt UUID to export. Repeat to export multiple specific prompts.",
    )
    parser.add_argument(
        "--output-root",
        default="output/workbench-export",
        help="Root output directory.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Always re-download prompt data even when local files appear up to date.",
    )
    parser.add_argument(
        "--cookie-header",
        default=os.environ.get("CLAUDE_COOKIE_HEADER", ""),
        help=(
            "Raw Cookie header copied from a platform.claude.com API request. "
            "Can also be provided via CLAUDE_COOKIE_HEADER."
        ),
    )
    parser.add_argument(
        "--cookie-header-file",
        default="",
        help=(
            "Path to a text file containing the Cookie header value. "
            "Useful to avoid shell quoting issues."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="HTTP request timeout in seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Max retries for retryable HTTP/network failures.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=0.8,
        help="Base retry backoff in seconds (exponential).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    cookie_header = ""
    if args.cookie_header_file:
        cookie_header = Path(args.cookie_header_file).read_text(encoding="utf-8").strip()
    elif args.cookie_header:
        cookie_header = args.cookie_header.strip()

    if not cookie_header:
        raise RuntimeError(
            "Missing cookie header.\n"
            "Pass --cookie-header '<cookie string>' (IMPORTANT: wrap in single quotes),\n"
            "or pass --cookie-header-file /path/to/cookie.txt,\n"
            "or set CLAUDE_COOKIE_HEADER.\n"
            "How to get it quickly:\n"
            "1) Open platform.claude.com in your browser.\n"
            "2) Open DevTools -> Network, then refresh the page.\n"
            "3) Open any request to https://platform.claude.com/api/...\n"
            "4) Copy the 'Cookie' request header VALUE only (not full JSON)."
        )
    cookies = parse_cookie_header(cookie_header)
    for required in ("sessionKey", "routingHint"):
        if required not in cookies:
            raise RuntimeError(
                f"Cookie header is missing '{required}'. Copy the full Cookie header "
                "from a platform.claude.com/api network request."
            )

    org_id = args.org_id or cookies.get("lastActiveOrg")
    if not org_id:
        raise RuntimeError(
            "Missing organization id. Provide --org-id or include lastActiveOrg in cookie header."
        )

    api = ClaudeApi(
        config=ApiConfig(
            timeout_seconds=args.timeout_seconds,
            max_retries=max(0, args.max_retries),
            retry_backoff_seconds=max(0.0, args.retry_backoff_seconds),
        ),
        cookie_header=cookie_header,
    )
    exporter = WorkbenchExporter(
        api=api,
        org_id=org_id,
        output_root=output_root,
        force_refresh=args.force_refresh,
    )

    prompt_ids: list[str]
    if args.prompt_id:
        seen: set[str] = set()
        prompt_ids = []
        for pid in args.prompt_id:
            if pid not in seen:
                seen.add(pid)
                prompt_ids.append(pid)
    else:
        prompt_ids = exporter.list_prompt_ids()

    if not prompt_ids:
        print("No prompts to export.")
        return 0

    success_count = 0
    skipped_count = 0
    failure_count = 0
    total_revisions = 0
    total_evaluations = 0

    for pid in prompt_ids:
        try:
            result = exporter.export_prompt(pid)
            total_revisions += result.revisions
            total_evaluations += result.evaluations
            if result.downloaded:
                success_count += 1
                print(
                    f"[ok] {result.prompt_id} ({result.prompt_name}) "
                    f"revisions={result.revisions} evaluations={result.evaluations}"
                )
            else:
                skipped_count += 1
                print(
                    f"[skip] {result.prompt_id} ({result.prompt_name}) "
                    f"revisions={result.revisions}"
                )
        except Exception as exc:
            failure_count += 1
            print(f"[error] {pid}: {exc}", file=sys.stderr)

    print(
        f"done prompts_downloaded={success_count} prompts_skipped={skipped_count} "
        f"prompts_failed={failure_count} "
        f"revisions={total_revisions} evaluations={total_evaluations}"
    )
    return 0 if failure_count == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        raise SystemExit(2)
