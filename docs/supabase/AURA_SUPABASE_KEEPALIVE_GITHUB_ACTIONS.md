# A.U.R.A Supabase Keepalive GitHub Actions

## Purpose

This workflow periodically updates one lightweight row in Supabase so the free-tier A.U.R.A Supabase project has regular usage activity.

## Why This Exists

Free Supabase projects can become inactive after long periods without usage. The keepalive workflow is a practical lightweight ping. It is not a replacement for paid Supabase uptime guarantees.

## Required GitHub Secrets

Add these in GitHub repository settings:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Optional:

- `SUPABASE_KEEPALIVE_TABLE`, defaults to `aura_keepalive`

## SQL Setup

1. Open Supabase dashboard.
2. Go to SQL Editor.
3. Run `docs/supabase/aura_keepalive_schema.sql`.
4. Confirm the `public.aura_keepalive` table exists.

The table stores only:

- `id`
- `touched_at`
- `source`
- `note`

It must not store conversations, API keys, clipboard content, screenshots, audio, camera frames, or identity descriptors.

## GitHub Setup

1. Open GitHub repo: `https://github.com/eswarb-dev/A.U.R.A`
2. Go to Settings -> Secrets and variables -> Actions.
3. Add `SUPABASE_URL`.
4. Add `SUPABASE_SERVICE_ROLE_KEY`.
5. Go to Actions -> Supabase Keepalive.
6. Click Run workflow.
7. Confirm the action succeeds.
8. Verify `aura_keepalive.touched_at` updates in Supabase.

## Workflow

Workflow file:

`/.github/workflows/supabase-keepalive.yml`

Schedule:

`20 3 */3 * *`

This runs every 3 days at 03:20 UTC. The workflow can also be run manually with `workflow_dispatch`.

## Troubleshooting

- Missing `SUPABASE_URL`: add the GitHub repository secret.
- Missing `SUPABASE_SERVICE_ROLE_KEY`: add the GitHub repository secret.
- HTTP 404: run `docs/supabase/aura_keepalive_schema.sql` in Supabase SQL Editor.
- Other non-2xx status: check Supabase REST API status, table name, project URL, and repository secrets.
- Timeout: rerun the workflow and check Supabase project availability.

The workflow does not print response bodies by default.

## Security Rules

- Keep `SUPABASE_SERVICE_ROLE_KEY` only in GitHub Actions secrets and backend local `.env` if needed.
- Never expose the service-role key to frontend code.
- Never commit Supabase keys.
- Never print Supabase keys.
- Use repository secrets only.
- Keep this workflow as a lightweight keepalive ping.
- Do not use this workflow to create schemas unless explicitly requested.
