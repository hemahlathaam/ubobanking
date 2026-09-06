# MiniBank Demo

Demo only. No real money or bank connections. The interface uses banking-style
terminology and transaction references for realism, but all balances and
payments are local simulation data.

All timestamps are stored as UTC-aware values where the database supports
timezones. The shared `sgt` template filter converts every displayed timestamp
to `SGT / Asia/Singapore (UTC+8)`, including transactions, messages, audit
events, notifications, scheduled transfers, and statements. Datetime fields entered in the UI (such as scheduled transfers)
are interpreted as Singapore time and normalized to UTC for storage.

## Run on Windows

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```powershell
$env:ADMIN_USERNAME="admin"
$env:ADMIN_PASSWORD="ChooseASecretPassword123"
$env:SECRET_KEY="LongRandomSecretKeyReplaceThis"
flask --app app run --host=0.0.0.0 --port=5000
```

`SECRET_KEY` and `ADMIN_PASSWORD` are required for a fresh database. Startup
fails clearly when `SECRET_KEY` is missing instead of running with an insecure
default. Use a password manager to generate production values.

## Account types

- **Standard**: normal banking, transfers, messages, and profile access.
- **Developer**: the same management permissions as Admin.
- **Admin**: management permissions, including user creation, balances, resets,
  enable/disable, and deletion.

The legacy `is_admin` flag remains supported for existing databases and is
treated as Admin access.

Management users can impersonate enabled accounts from the Admin panel. The
banner identifies an impersonated session and the return action restores the
management session. The actor is kept separately from the impersonated
identity, stale/disabled sessions are ended safely, and starts and ends are
written to `AuditLog`. Management users also have a separate privileged-transfer
form requiring enabled source and recipient accounts, a positive sufficient
amount, and a reason. The balance updates, transaction, and audit record commit
together.

Messages supports both direct messages and creator-owned group chats. Any
enabled user can create a group; owners can add or remove enabled members and
post messages. Each member has an independent read state so visiting one chat
does not clear another chat's unread count.

## Demo feature set

The app intentionally uses local, dependency-free implementations for the
approved roadmap:

- fine-grained permission records, reasoned/audited impersonation, and pending
  privileged-transfer approval requests (`/admin/privileged-transfer`), while
  the legacy `/admin/transfer` form remains an immediate demo operation;
- composable Admin and Developer roles (including users holding both roles),
  a management-only checking balance view, and audited demo-check
  deposits between enabled accounts;
- unlimited transfers subject to valid amounts and available balances,
  duplicate protection, scheduled and recurring transfers, beneficiaries, a
  five-minute cancellation window, and double-entry ledger entries;
- searchable audit logs, in-app Socket.IO notifications, and CSV statements
  (the PDF URL explicitly falls back to
  CSV when no renderer is installed);
- group owners/moderators, reactions, URL attachments, @mentions, read
  receipts, and unread counters;
- developer-only tools for system health, sandbox user generation, feature
  flags, scoped API keys, and recent audit activity;
- the developer console uses a compact black-and-white terminal-style
  interface with a complete legacy-portal directory for every active banking
  module;

No feature sends money, email, SMS, or card transactions to an external
provider. Scheduled transfers are processed when the owner visits the
scheduled-transfers page; a production deployment should replace that demo
trigger with a trusted worker and database migrations.

The shared navigation includes an account/security status indicator and every
page includes the global copyright footer:
`Copyright Pranav Hemahlathaa Harish and Hari Suhanth Karthikeyan, 2026.`
Role identity badges are rendered without external assets: Admin uses a
winged Patron-style crown emblem and Developer uses a `</>` coding emblem.
Both badges appear together for dual-role accounts, with accessible labels and
titles.

Saved beneficiaries can be selected directly from the transfer form, approval
requests are checked again before execution, and failed scheduled payments
remain active for a later retry. Admins can grant and audit individual
permissions, while chat pages show reaction and read-receipt status.

Admin/Developer role selection is stored in `UserRole` records. The legacy
`account_type` and `is_admin` columns remain readable for older databases; both
management roles receive the same management permissions. A demo check deposit
debits the enabled check writer and credits the enabled recipient in one
transaction, records check/reference details, creates balanced ledger entries,
and writes an audit event. The management balance view is read-only and also
writes an audit event.

MiniBank now uses one checking account per user. On startup, legacy databases
move each user's former secondary-account balance into checking in a single
migration transaction, then retire the old account and internal-transfer
records. No new secondary accounts or internal account transfers are created.

## Future hardening

Multi-factor authentication, password reset by verified email, fraud/risk
review, accessibility improvements, CSRF protection, a trusted scheduled-job
worker, and a proper database migration tool are natural next steps for a more
realistic bank simulation.

## Smoke tests

Run the built-in tests with:

```powershell
python -m unittest discover -s tests -v
```
