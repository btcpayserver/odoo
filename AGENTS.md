# AGENTS.md — Guidance for Coding Agents

Scope: this repository (**`btcpayserver/odoo`** — upstream; Andy's fork
`ndeet/btcpay-odoo-modules` only holds branches), module `payment_btcpayserver`.
Read this before changing anything. The security invariants in particular are
not suggestions — several exist because of real payment-integrity requirements.

## Repository layout & branches

- One module: `payment_btcpayserver/` (Odoo payment provider for BTCPay Server).
- Branch scheme:
  - `16.0` … `20.0` — stable per-Odoo-version lines. **16.0–19.0 use the legacy
    BitPay API (EC-signed client, pairing, IPN). Do not port Greenfield changes
    there** — deliberately frozen (see MIGRATION-GREENFIELD.md).
  - `odoo-20-greenfield` — active development: Greenfield API, setup wizard,
    refunds. Feature branches fork from here.
- Remotes: `origin` = upstream `btcpayserver/odoo` (PRs go **here**),
  `ndeet` = Andy's fork (branch holder, SSH). Push to upstream only on
  request. Tags/releases only on explicit instruction (see "Release discipline").
- Planning/audit docs (`MIGRATION-GREENFIELD.md`, `SECURITY_REPORT.md`) stay
  **untracked** by default — never commit them without asking.

## Module architecture (Greenfield)

- `models/libs/greenfield_client.py` — thin Greenfield client (`Authorization:
  token …`), raises `GreenfieldError`; permission checks (`check_permissions`).
- `models/payment_provider.py` — credentials (`btcpay_location`, `btcpay_api_key`,
  `btcpay_store_id`, `btcpay_webhook_secret`, `btcpay_webhook_id`), one-click
  setup wizard (`/api-keys/authorize` redirect flow), webhook auto-registration
  on credential-touching saves, connection test.
- `controllers/main.py` — checkout flow (find tx → `SELECT … FOR UPDATE` lock →
  reuse-or-replace expired invoice → validate → redirect), webhook endpoint,
  return route (redirect only), wizard callback.
- `models/payment_transaction.py` — two-axis status mapping and processing.

## Security invariants — never weaken

1. **Webhook authenticity**: HMAC-SHA256 over the *raw* request body against
   `BTCPay-Sig`, compared with `hmac.compare_digest`, fail-closed on
   missing body/header/secret. No exceptions.
2. **Never trust payload identity**: select the transaction by the *stored*
   invoice id only; re-fetch the invoice through the authenticated API;
   validate the binding (fetched id == incoming id == stored id,
   `metadata.orderId` == `tx.reference`, invoice unique across transactions);
   lock the row, re-check after the lock, invalidate cached fields.
3. **Odoo 20 write guard**: `payment.transaction.write()` requires the
   `payment_safe_write` context flag. Preparation writes pass it explicitly;
   do not "simplify" it away.
4. **Two-axis status mapping**: `status` × `additionalStatus` are always
   evaluated together. PaidPartial/PaidLate on an expired invoice → cancel +
   operator warning, **never** auto-confirm. PaidOver → confirm + warning.
5. **Wizard state**: HMAC (`database.secret`) over `id|ts|callback_base|write_date`
   — single-use (any provider write invalidates it) + 15-minute `max_age`.
   The callback base comes from `provider.get_base_url()` (web.base.url),
   never from the request (breaks behind reverse proxies otherwise).
6. **Secrets handling**: credential fields are password-style, `copy=False`;
   logger uses `get_payment_logger(..., sensitive_keys=[...])`. Never log
   API keys or webhook secrets, not even truncated.
7. **Checkout integrity**: amount/currency/metadata are built server-side from
   the transaction; the browser only sends the reference.
8. **XSS**: escape every interpolated value in the wizard result page.

## Conventions

- Commits: short imperative subject lines, e.g. `Fix webhook registration
  payload shape (authorizedEvents.specificEvents)`, `Docs: …`, `Refunds: …`,
  `Chore: …`.
- **"Prepare release" ≠ release**: bumping versions/changelogs is a normal
  commit; tagging, pushing tags or creating GitHub releases happens only when
  Andy explicitly says so, after his testing.
- When asked, show the diff *before* committing.
- Pre-commit checks: `python3 -m py_compile` on every touched `.py` file; XML
  must parse (`python3 -c "import xml.etree.ElementTree as ET; ET.parse(<file>)"`).
- Fix descriptions in docs/changelogs: describe the generic root cause, not a
  list of symptoms.

## Testing

- Default testnet store: `https://testnet.demo.btcpayserver.org`.
- Deployment/test stack: `~/projects/odoo/odoo-20` (docker-compose; web exposed
  on host port 8080 → container 8069; restart with `docker compose restart web`).
  `install-btcpay.sh` there pip-installs `markdown2` directly (nothing to do
  with module requirements — the module has none; `requirements.txt` was
  intentionally removed when it became empty. Re-add it only when a real pip
  dependency appears, and mirror it in `external_dependencies` where sensible).
- Full test matrix: MIGRATION-GREENFIELD.md §1.8 (happy path, Lightning,
  expiry replacement, partial/late/over payment, invalid invoice, webhook
  redelivery, tampered signature, concurrent checkout, amount mismatch).
- Webhook testing: "Redeliver" link on the BTCPay invoice page while watching
  the Odoo log for `BTCPay: webhook` lines.

## Machine notes (anna — the box agents usually run on)

- Docker commands from agent shells need `sg docker -c '…'`.
- Never pipe anything into sudo; plain `sudo` works via the agent environment.
- `web.base.url` of the Odoo stack must be publicly reachable, or BTCPay cannot
  deliver webhooks (README → Troubleshooting).
