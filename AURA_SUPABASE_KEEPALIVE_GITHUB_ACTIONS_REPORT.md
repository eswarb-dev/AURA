# A.U.R.A Supabase Keepalive GitHub Actions Report

## Files Added

- `.github/workflows/supabase-keepalive.yml`
- `docs/supabase/aura_keepalive_schema.sql`
- `docs/supabase/AURA_SUPABASE_KEEPALIVE_GITHUB_ACTIONS.md`
- `scripts/test_supabase_keepalive.ps1`

## Files Updated

- `.gitignore`

## Workflow Schedule

The workflow runs every 3 days at 03:20 UTC:

```yaml
cron: "20 3 */3 * *"
```

It also supports manual runs with `workflow_dispatch`.

## Required GitHub Secrets

Add these manually in GitHub repository settings:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Optional:

- `SUPABASE_KEEPALIVE_TABLE`, default `aura_keepalive`

## SQL Schema

`docs/supabase/aura_keepalive_schema.sql` creates and seeds `public.aura_keepalive`.

The table stores only safe keepalive metadata:

- `id`
- `touched_at`
- `source`
- `note`

It must not store conversations, API keys, clipboard content, screenshots, audio, camera frames, or identity descriptors.

## Manual Setup Steps

1. Open the Supabase dashboard.
2. Go to SQL Editor.
3. Run `docs/supabase/aura_keepalive_schema.sql`.
4. Open `https://github.com/eswarb-dev/A.U.R.A`.
5. Go to Settings -> Secrets and variables -> Actions.
6. Add `SUPABASE_URL`.
7. Add `SUPABASE_SERVICE_ROLE_KEY`.
8. Go to Actions -> Supabase Keepalive.
9. Click Run workflow.
10. Confirm the action succeeds.
11. Verify `aura_keepalive.touched_at` updates in Supabase.

## Validation Checklist

- `MASTER_README.md` was read before changes.
- `git status --short` was run before changes and showed an already-dirty worktree.
- `.gitignore` was checked and updated to ignore `.env.*`, `diagnostics/memory/`, and `diagnostics/tools/*.jsonl`.
- Added workflow uses only GitHub Secrets.
- Workflow prints only timestamp, target table, HTTP status, and success/failure.
- Workflow does not print response bodies.
- Workflow fails clearly on HTTP 404 and tells the user to run `docs/supabase/aura_keepalive_schema.sql`.
- No A.U.R.A runtime logic, orb visuals, TTS, camera, tracking, identity, or tool routing code was changed for this patch.

## Security Notes

- Never commit Supabase keys.
- Never print Supabase keys.
- Never expose `SUPABASE_SERVICE_ROLE_KEY` to frontend code.
- Keep the service-role key only in GitHub Actions secrets and backend local `.env` if needed.
- The local validation script reads credentials from process environment variables only.
- This workflow is a lightweight keepalive ping only.

## Remaining Limitation

GitHub Actions keepalive is a practical free-tier workaround, but Supabase Pro is the official guaranteed way to avoid inactivity pausing.
