# Monday Sandbox Board – Nightshift

This note captures the workspace/board metadata, column IDs, and webhook credentials for the Monday sandbox powering Phase 2.5 ingestion tests.

## Workspace & Board
- **Workspace**: `Nightshift Sandbox Workspace` (`nightshift-sandbox-ws`)
- **Board**: `Nightshift PM Sandbox` (`6049808653`)
- **Board URL**: https://nightshift-sandbox.monday.com/boards/6049808653
- **Groups**:
  - Inbox → `inbox-nightshift` (Nightshift creates prompts from this group)
  - Active → `active-dev`
  - Blocked → `blocked-items`
  - Done → `archive-done`
- **Template item**: `6049808701` (duplicate when seeding new prompts)
- **Seed example items**: `6049811123`, `6049811124`

## Column Mapping → Prompt Metadata
| Column purpose | Monday column ID | Type | Nightshift field |
| --- | --- | --- | --- |
| Prompt title | `item_name` | title | `prompt.title` |
| Prompt ID | `text__prompt_id` | text | `prompt.id` |
| Repository | `text__repo` | text | `prompt.repo` |
| Queue | `dropdown__queue` | dropdown | `prompt.queue` |
| Branch | `text__branch` | text | `prompt.git_branch` |
| Status | `status` | status | `prompt.status` (`Ready to start`→queued, `Working on it`→running, `Stuck`→blocked, `Done`→completed) |
| Priority | `dropdown__priority` | dropdown | `prompt.priority` (P0/P1/P2) |
| Assignee | `people__assignee` | people | `prompt.assignee` |
| ETA | `date__eta` | date | `prompt.target_eta` |
| Environment URL | `link__environment_url` | link | `prompt.environment_url` |
| Human Task blocker flag | `checkbox__human_task` | checkbox | `human_task.blocked` |
| Notes | `long_text__notes` | long text | `prompt.notes` |

## API token & Webhook
- **API token**: `ns_monday_pat_sandbox_20251118`
- **Webhook target**: `https://agent-devhost-eink.nghtshft.ai/api/pm/monday/webhook/nightshift-sandbox`
- **Webhook signing secret**: `ns_monday_webhook_secret_20251118`
- **Subscriptions**: `sub-4f8f0a81`, `sub-e1cad42c`

The operator vault entry (`~/.local/share/nightshift/operator_vault/monday_sandbox.yml`) stores the raw secrets plus workspace context. Runtime services consume the same values via `~/.config/systemd/user/nightshift.env` (env vars `MONDAY_*`). Regenerate the vault + env file together if the Monday workspace rotates credentials, and log the new timestamp + evidence path per the prompt instructions.
