# claude-console-exporter

Exports your `platform.claude.com` Claude Console data to local JSON files:

- prompt metadata (`prompt.json`)
- all revisions (`revisions/<revision_id>.json`)
- evaluations per revision (`evaluations/<revision_id>.json`)

## Quick Start

1. Log in to `platform.claude.com` in your normal browser.
2. Open DevTools -> Network and refresh the page.
3. Open any request to `https://platform.claude.com/api/...`.
4. In Request Headers, right-click `Cookie` -> `Copy Value`.
5. Run:

```bash
python3 claude_console_exporter.py --cookie-header 'PASTE_COOKIE_VALUE'
```

Notes:

- Keep the cookie value wrapped in single quotes `'...'`.
- If `lastActiveOrg` is not in the cookie, pass `--org-id <org_uuid>`.

## Common Commands

Default incremental sync (download only changed/new prompts):

```bash
python3 claude_console_exporter.py --cookie-header 'PASTE_COOKIE_VALUE'
```

Force full re-download for prompts being processed:

```bash
python3 claude_console_exporter.py --cookie-header 'PASTE_COOKIE_VALUE' --force-refresh
```

Export only selected prompts:

```bash
python3 claude_console_exporter.py \
  --cookie-header 'PASTE_COOKIE_VALUE' \
  --prompt-id <prompt_id_1> \
  --prompt-id <prompt_id_2>
```

## Incremental Logic

For each prompt, the exporter compares remote revision IDs with local files:

- local `revisions/*.json` stems vs remote revision IDs
- local `evaluations/*.json` stems vs remote revision IDs

If both sets match and `prompt.json` exists, the prompt is skipped.
Otherwise, the full prompt dataset is re-downloaded.

## Failure Retry

On failures, the script prints:

- `failed_prompt_ids: ...`
- `rerun_tip: ...` reminding you to run the same command again.

## Output Layout

```text
output/claude-console-export/
  <prompt-slug>--<prompt-id>/
    prompt.json
    revisions/
      <revision-id>.json
    evaluations/
      <revision-id>.json
```

## Exit Codes

- `0`: success (no failed prompts)
- `2`: partial failure or fatal configuration/auth error

## Disclaimer

This project is provided as-is and you use it at your own risk. Always review
exported data and keep your own backups. The authors are not responsible for
data loss, account issues, or other consequences of use.

```text
   _____ _                 _        _____          _
  / ____| |               | |      / ____|        | |
 | |    | | __ _ _   _  __| | ___ | |     ___   __| | _____  __
 | |    | |/ _` | | | |/ _` |/ _ \| |    / _ \ / _` |/ _ \ \/ /
 | |____| | (_| | |_| | (_| |  __/| |___| (_) | (_| |  __/>  <
  \_____|_|\__,_|\__,_|\__,_|\___| \_____\___/ \__,_|\___/_/\_\
```
