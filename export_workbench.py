#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


COOKIE_LINE_RE = re.compile(
    r"^(?P<name>[^=]+)=(?P<value>.*) \(domain: (?P<domain>[^,]+), path: (?P<path>[^)]+)\)$"
)
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


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str


def parse_cookie_dump(raw: str) -> list[BrowserCookie]:
    cookies: list[BrowserCookie] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("###"):
            continue
        m = COOKIE_LINE_RE.match(line)
        if not m:
            continue
        cookies.append(
            BrowserCookie(
                name=m.group("name"),
                value=m.group("value"),
                domain=m.group("domain"),
                path=m.group("path"),
            )
        )
    return cookies


def read_playwright_cookies(pwcli_path: Path) -> tuple[dict[str, str], str]:
    if not pwcli_path.exists():
        raise RuntimeError(f"Playwright wrapper not found: {pwcli_path}")
    env = os.environ.copy()
    env.setdefault("NPM_CONFIG_CACHE", "/tmp/.npm-cache")
    proc = subprocess.run(
        ["bash", str(pwcli_path), "cookie-list"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to read browser cookies: {proc.stderr.strip()}")
    parsed = parse_cookie_dump(proc.stdout)
    cookies_by_name: dict[str, str] = {}
    claude_cookie_parts: list[str] = []
    seen_cookie_names: set[str] = set()
    for cookie in parsed:
        cookies_by_name[cookie.name] = cookie.value
    for cookie in parsed:
        if "claude.com" not in cookie.domain:
            continue
        if cookie.name in seen_cookie_names:
            continue
        seen_cookie_names.add(cookie.name)
        claude_cookie_parts.append(f"{cookie.name}={cookie.value}")
    cookie_header = "; ".join(claude_cookie_parts)

    for required in ("sessionKey", "routingHint"):
        if required not in cookies_by_name:
            raise RuntimeError(
                f"Missing required cookie '{required}'. Ensure the visible browser is logged in."
            )
    return cookies_by_name, cookie_header


@dataclass(frozen=True)
class ApiConfig:
    base_url: str = "https://platform.claude.com"
    timeout_seconds: float = 30.0
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

        for attempt in range(self.config.max_retries + 1):
            req = request.Request(url=url, method=method, data=data, headers=headers)
            try:
                with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else None
            except error.HTTPError as exc:
                status = exc.code
                body_preview = exc.read().decode("utf-8", errors="replace")[:300]
                if status in RETRYABLE_STATUS and attempt < self.config.max_retries:
                    time.sleep(self.config.retry_backoff_seconds * (2**attempt))
                    continue
                raise RuntimeError(
                    f"{method} {path} failed with HTTP {status}: {body_preview}"
                ) from exc
            except error.URLError as exc:
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_backoff_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"{method} {path} failed: {exc}") from exc


@dataclass
class ExportResult:
    prompt_id: str
    prompt_name: str
    revisions: int
    evaluations: int


class WorkbenchExporter:
    def __init__(
        self,
        api: ClaudeApi,
        org_id: str,
        output_root: Path,
        overwrite: bool = False,
    ) -> None:
        self.api = api
        self.org_id = org_id
        self.output_root = output_root
        self.overwrite = overwrite

    def list_prompt_ids(self) -> list[str]:
        prompts = self.api.request_json(
            f"/api/organizations/{self.org_id}/workbench/prompts"
        )
        if not isinstance(prompts, list):
            raise RuntimeError("Prompt list endpoint did not return an array.")
        return [p["id"] for p in prompts if isinstance(p, dict) and p.get("id")]

    def export_prompt(self, prompt_id: str) -> ExportResult:
        prompt = self.api.request_json(
            f"/api/organizations/{self.org_id}/workbench/prompts/{prompt_id}"
        )
        if not isinstance(prompt, dict):
            raise RuntimeError(f"Prompt {prompt_id}: prompt endpoint did not return an object.")

        prompt_name = str(prompt.get("name") or "untitled")
        prompt_dir = self.output_root / f"{slugify(prompt_name)}-{prompt_id}"
        revisions_dir = prompt_dir / "revisions"
        evaluations_dir = prompt_dir / "evaluations"
        revisions_dir.mkdir(parents=True, exist_ok=True)
        evaluations_dir.mkdir(parents=True, exist_ok=True)

        prompt_path = prompt_dir / "prompt.json"
        if self.overwrite or not prompt_path.exists():
            write_json(prompt_path, prompt)

        compact = self.api.request_json(
            f"/api/organizations/{self.org_id}/workbench/prompts/{prompt_id}/revisions?compact=true"
        )
        if not isinstance(compact, list):
            raise RuntimeError(
                f"Prompt {prompt_id}: revisions endpoint did not return an array."
            )

        revision_count = 0
        evaluation_count = 0
        for revision in compact:
            if not isinstance(revision, dict) or not revision.get("id"):
                continue
            rev_id = revision["id"]
            revision_count += 1

            revision_path = revisions_dir / f"{rev_id}.json"
            if self.overwrite or not revision_path.exists():
                revision_full = self.api.request_json(
                    f"/api/organizations/{self.org_id}/workbench/prompts/{prompt_id}/revisions/{rev_id}"
                )
                write_json(revision_path, revision_full)

            evaluations_path = evaluations_dir / f"{rev_id}.json"
            if self.overwrite or not evaluations_path.exists():
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
            else:
                try:
                    existing = json.loads(evaluations_path.read_text(encoding="utf-8"))
                    if isinstance(existing, list):
                        evaluation_count += len(existing)
                except Exception:
                    pass

        return ExportResult(
            prompt_id=prompt_id,
            prompt_name=prompt_name,
            revisions=revision_count,
            evaluations=evaluation_count,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Claude Workbench prompts, revisions, and per-revision evaluations."
    )
    default_pwcli = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills" / "playwright" / "scripts" / "playwright_cli.sh"
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
    parser.add_argument("--workers", type=int, default=4, help="Parallel prompt workers.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing prompt/revision/evaluation files.",
    )
    parser.add_argument(
        "--pwcli-path",
        default=str(default_pwcli),
        help="Path to playwright_cli.sh.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    cookies, cookie_header = read_playwright_cookies(Path(args.pwcli_path))
    org_id = args.org_id or cookies.get("lastActiveOrg")
    if not org_id:
        raise RuntimeError("Missing --org-id and lastActiveOrg cookie is not available.")

    api = ClaudeApi(
        config=ApiConfig(),
        cookie_header=cookie_header,
    )
    exporter = WorkbenchExporter(
        api=api,
        org_id=org_id,
        output_root=output_root,
        overwrite=args.overwrite,
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

    workers = max(1, args.workers)
    success_count = 0
    failure_count = 0
    total_revisions = 0
    total_evaluations = 0

    if workers == 1 or len(prompt_ids) == 1:
        for pid in prompt_ids:
            try:
                result = exporter.export_prompt(pid)
                success_count += 1
                total_revisions += result.revisions
                total_evaluations += result.evaluations
                print(
                    f"[ok] {result.prompt_id} ({result.prompt_name}) "
                    f"revisions={result.revisions} evaluations={result.evaluations}"
                )
            except Exception as exc:
                failure_count += 1
                print(f"[error] {pid}: {exc}", file=sys.stderr)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(exporter.export_prompt, pid): pid for pid in prompt_ids}
            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    result = fut.result()
                    success_count += 1
                    total_revisions += result.revisions
                    total_evaluations += result.evaluations
                    print(
                        f"[ok] {result.prompt_id} ({result.prompt_name}) "
                        f"revisions={result.revisions} evaluations={result.evaluations}"
                    )
                except Exception as exc:
                    failure_count += 1
                    print(f"[error] {pid}: {exc}", file=sys.stderr)

    print(
        f"done prompts_ok={success_count} prompts_failed={failure_count} "
        f"revisions={total_revisions} evaluations={total_evaluations}"
    )
    return 0 if failure_count == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        raise SystemExit(2)
