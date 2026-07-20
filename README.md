# Bellhop

Bellhop carries your work to GitHub and back. It is a stateless Python gateway that lets
Claude and other MCP clients create repositories,
read and write arbitrary project files, make atomic Git commits, create branches and pull
requests, and publish releases directly in GitHub.

GitHub is the only project storage and versioning layer. Render stores no repositories
and needs no persistent disk.

## What this intentionally does not do

- It does not impose a standard repository or folder structure.
- It does not create `v1`, `v2`, or `final-final` copies for versioning.
- It does not clone or synchronize repositories on Render.
- It does not delete repositories.
- It does not expose the GitHub token to Claude.

Each project can be structured however you and the agent decide. Versioning is handled by
Git commits, branches, pull requests, tags, and releases.

## MCP tools

- `github_connection_status`
- `list_repositories`
- `create_repository`
- `initialize_repository_files`
- `get_repository`
- `get_branch_head`
- `list_repository_files`
- `read_repository_file`
- `commit_repository_files`
- `move_repository_file`
- `delete_repository_files`
- `create_repository_branch`
- `repository_history`
- `compare_repository_versions`
- `create_repository_release`
- `create_repository_pull_request`
- `search_repository_code`

## How safe writes work

Before changing an existing branch, the agent calls `get_branch_head`. The returned commit
SHA is passed to `commit_repository_files` as `expected_head`.

If the branch changed in between, the gateway refuses the write instead of silently
overwriting newer work. Related changes are written through GitHub's Git database API as
one commit.

## Local development

This project requires Python 3.12 or newer. macOS ships 3.9, so use `uv` to get a
matching interpreter rather than the system Python:

```bash
cp .env.example .env
# Fill in the secrets in .env

brew install uv          # once
uv venv --python 3.13
uv pip install -r requirements-dev.txt

.venv/bin/uvicorn app.main:app --reload
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

The local MCP URL is:

```text
http://localhost:8000/mcp
```

## GitHub credential

For the simplest personal setup, create a fine-grained GitHub personal access token and
store it only in Render as `GITHUB_TOKEN`. Select **All repositories** and grant
**Administration**, **Contents**, and **Pull requests** read/write. Add **Workflows**
read/write only if agents should modify files under `.github/workflows`.

The gateway has no repository deletion tool.

For a longer-lived multi-user product, replace the PAT with a GitHub App installation
token flow.

## Deploy to Render

For the exact click-by-click setup, read [`DEPLOY_RENDER.md`](DEPLOY_RENDER.md).

1. Push this gateway code to a private GitHub repository.
2. In Render, create a Blueprint or Python Web Service from that repository.
3. Add the required secret environment variables from `.env.example`.
4. Render automatically provides `RENDER_EXTERNAL_URL`; the gateway uses it for OAuth
   discovery and its MCP resource URL.
5. Keep the service on a continuously available plan. Sleeping free instances can make
   OAuth and MCP connection startup unreliable.
6. Set the health check path to `/health`.

Render runs:

```text
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers
```

No disk or database is required.

## Connect Claude

In Claude:

1. Open **Customize → Connectors**.
2. Add a custom connector.
3. Enter:
   - Name: `Bellhop`
   - URL: `https://YOUR-SERVICE.onrender.com/mcp`
4. In advanced settings, enter:
   - OAuth Client ID: the value of `MCP_OAUTH_CLIENT_ID`
   - OAuth Client Secret: the value of `MCP_OAUTH_CLIENT_SECRET`
5. Connect the connector.
6. On the gateway authorization page, enter `MCP_LOGIN_PASSWORD`.

The same remote connector is then available to Claude's hosted surfaces, including mobile.

## Example instructions to Claude

Create a project without forcing a structure:

> Create a private repository called Picaform. Based on the architecture we agreed on,
> decide on an appropriate repository structure, create all initial code and documents in
> one commit, and tell me the commit SHA.

Update an existing project safely:

> Read the current repository first. Update the architecture and implementation together
> in one commit. Do not change the existing repository organization unless the change is
> necessary, and explain any structural change in the commit message.

Use a review branch:

> Create a branch for the new document pipeline, make the code and documentation changes
> there, and open a pull request instead of writing directly to main.

## OAuth design

The gateway implements OAuth authorization-code flow with S256 PKCE and a fixed client ID
and secret. This matches Claude custom connectors where you provide pre-registered client
credentials in advanced settings.

The consent screen is protected by `MCP_LOGIN_PASSWORD`. Authorization codes, access
tokens, and refresh tokens are short-lived signed JWTs, so the service remains stateless.

This is appropriate for one owner. For a shared service, use a real identity provider,
per-user GitHub authorization, persistent token revocation, and per-repository policy.

## Current limitations

- GitHub code search may lag immediately after a commit because GitHub indexes it
  asynchronously.
- MCP tool results are intentionally capped. Large files remain in GitHub.
- Small binaries can be written with Base64. Larger artifacts should use an upload ticket
  (see below), Git LFS, or a release asset workflow.
- Stateless authorization codes cannot be centrally revoked before expiry. They expire in
  five minutes and require PKCE.
- Branch protection and repository rules can reject direct updates. Use a branch and pull
  request when required.


## Upload tickets

Inline Base64 commits push file bytes through the model's context, which caps them at
`MAX_BINARY_INPUT_BYTES` (2.5 MB) and wastes tokens. An upload ticket avoids that: the agent
asks for a ticket over the authenticated MCP connection, then POSTs the raw bytes from its
own execution environment straight to the gateway.

1. The agent calls `create_upload_ticket(owner, repository, path, branch, message)`.
2. The gateway returns an `upload_url` valid for `UPLOAD_TICKET_TTL_SECONDS` (default 300).
3. The agent POSTs the file bytes to that URL.
4. The gateway commits them with the GitHub credential it already holds.

The ticket is a capability, not a credential. It is a signed JWT authorizing exactly one
path, on one branch, in one repository, and it grants no GitHub access if it leaks. It is
burned on first redemption; retries need a fresh ticket. Uploads are capped at
`MAX_UPLOAD_BYTES` (default 25 MiB, under GitHub's 100 MB blob limit).

`POST /upload/{ticket}` is intentionally not behind the OAuth bearer requirement, because
the sandbox posting the bytes does not hold a gateway access token. The ticket is the
credential.

**Single-use is enforced in memory.** Redeemed ticket IDs live in a process-local set, so a
restart inside a ticket's TTL window would allow one replay of that ticket against its own
pre-committed path. This is accepted deliberately: the gateway is single-owner and runs as
one instance with no datastore. A shared deployment needs Redis or Postgres there.

**The caller's environment must be allowed to reach the gateway's host.** In claude.ai this
means adding the gateway domain under settings → capabilities. Without it the POST fails at
the network layer, after a ticket has already been issued, which looks like a gateway error
but is not one.

## Empty repository initialization

`create_repository` defaults to `auto_init=false`. That prevents the gateway from forcing
even a README into a new project. The agent then calls `initialize_repository_files` with
the exact paths you agreed on. GitHub receives those paths as the first atomic commit.
