# B1 public mailbox deployment and verification

B1 is a public, anonymous temporary-mail edge and is deployed independently from the A private aggregation hub. Keep its database, volumes, credentials, origin, API path and Caddy instance separate from A.

## Deployment topology

Use the standalone file on the public host:

```sh
cp .env.public.example .env.public
chmod 600 .env.public
# Replace every placeholder in .env.public. Do not commit it.
docker compose --env-file .env.public -f infra/public-compose.yaml config --quiet
docker compose --env-file .env.public -f infra/public-compose.yaml up -d --build
docker compose --env-file .env.public -f infra/public-compose.yaml ps
```

`public-db` is a separate PostgreSQL 16 instance and named volume. `public-api` is the only service that reads the public upstream admin key and encryption key. `public-web` is the only published service; it proxies `/public-api/*` to the API and serves the static page. The compose file uses port 443 by default. Set `PUBLIC_MAIL_HTTPS_PORT=8443` only when the public and private stacks share a development host.

The public host DNS must point `PUBLIC_MAIL_DOMAIN` to the host. Caddy obtains a trusted certificate when DNS and inbound TCP 443 are ready. For an internal test host, replace the site block with `tls internal` and install the Caddy root CA on the test client; do not use `curl -k` as evidence of HTTPS acceptance.

## Secrets and isolation checks

Generate two different 32-byte keys and two different high-entropy database passwords. `PUBLIC_MAIL_*` values must not reuse A's `MAILS_*` values. Never place `PUBLIC_MAIL_ADMIN_KEY` in the Web bundle or URL.

```sh
# Run in a private terminal; do not paste generated secrets into chat.
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Before startup, verify the public URL points at `public-db` and that the service does not use A's database:

```sh
docker compose --env-file .env.public -f infra/public-compose.yaml config --quiet
docker compose --env-file .env.public -f infra/public-compose.yaml exec public-api sh -c 'printf "%s\\n" "$PUBLIC_MAIL_DATABASE_URL" | sed "s#:[^:@]*@#:<redacted>@#"'
```

Do not print the admin key or encryption key. On the host, restrict `.env.public` to the deployment account.

## API smoke checks

Use a cookie jar so the capability cookie is retained. Replace `PUBLIC_MAIL_DOMAIN` with the configured hostname:

```sh
curl --fail --cookie-jar public.cookies \
  -H 'Origin: https://PUBLIC_MAIL_DOMAIN' \
  -X POST https://PUBLIC_MAIL_DOMAIN/public-api/v1/mailboxes
# The response contains only id, address, status, expires_at and remaining_seconds.
# It must not contain jwt, token, address_jwt_ciphertext or admin credentials.
curl --fail --cookie public.cookies \
  https://PUBLIC_MAIL_DOMAIN/public-api/v1/mailboxes/MAILBOX_ID
curl --fail --cookie public.cookies \
  https://PUBLIC_MAIL_DOMAIN/public-api/v1/mailboxes/MAILBOX_ID/messages
curl --fail --cookie public.cookies \
  https://PUBLIC_MAIL_DOMAIN/public-api/v1/mailboxes/MAILBOX_ID/messages/MESSAGE_ID
```

Verify that the `Set-Cookie` capability is `HttpOnly`, `SameSite=Lax`, `Secure` in production, and has `Path=/`. The list response must contain summaries only; the detail response may contain `body_text` and a determined verification code, never upstream HTML execution or raw provider errors.

## Required rejection checks

- A request with no capability cookie returns `401 PUBLIC_MAIL_CAPABILITY_REQUIRED`.
- A capability from another mailbox cannot read a mailbox it did not create.
- An expired mailbox returns `410 PUBLIC_MAILBOX_EXPIRED` and never calls the upstream.
- More than three creates from one IP in one hour returns `429 PUBLIC_MAIL_CREATE_RATE_LIMITED`.
- More than 30 list calls per mailbox per minute returns `429 PUBLIC_MAIL_LIST_RATE_LIMITED`.
- More than 60 detail calls per mailbox per minute returns `429 PUBLIC_MAIL_DETAIL_RATE_LIMITED`.
- Bad or unavailable upstream responses return fixed public codes and never echo response bodies.

## Rollback and data retention

Before changing the image, record `docker compose ... ps` and the current deployment commit. Roll back by checking out the previous source revision and running `up -d --build`; do not delete `public_mail_pgdata` during rollback. Database backups and retention are handled separately from the image lifecycle. Removing public mailbox data is an explicit operator action only.

## Local verification limitations

On the Windows development host, Docker/PostgreSQL/real Cloudflare credentials may be unavailable. Record these checks as `SKIPPED`, not passed. The source-level checks remain:

```powershell
$env:PYTHONPATH = 'apps/public_api/src'
& 'apps/api/.venv/Scripts/python.exe' -m pytest apps/public_api/tests -q
& 'apps/api/.venv/Scripts/python.exe' -m ruff check apps/public_api/src apps/public_api/tests
& 'apps/api/.venv/Scripts/python.exe' -m ruff check apps/api/src apps/api/tests tests/integration
& 'apps/api/.venv/Scripts/python.exe' -m ruff check apps/public_api/src apps/public_api/tests
Push-Location apps/public_web
npm run typecheck
npm test -- --run
npm run build
Pop-Location
git diff --check
```

A real Docker, PostgreSQL, HTTPS and Cloudflare run is required before claiming production acceptance.