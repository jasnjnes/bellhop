# Deploy Bellhop on Render

This is a one-time setup. After it is connected, use a normal Claude chat on desktop or
mobile and explicitly say:

> Use Bellhop to create a private repository named `my-project`.

GitHub stores all project files and versions. Render only runs the stateless MCP gateway.

## 1. Put this code in GitHub

Render deploys Bellhop from a Git repository, so the gateway's own source needs to live in
one. This directory is already a Git repository with committed history, so you only need to
create the remote and push.

1. In GitHub, create a new **private** repository named `bellhop`.
2. Do not add a README, `.gitignore`, or license — this repository already has them, and
   adding them creates a conflicting initial commit.
3. Push from this directory:

```bash
git remote add origin https://github.com/YOUR_GITHUB_NAME/bellhop.git
git push -u origin main
```

Confirm `.gitignore` is present before pushing. It keeps `.env` out of the repository, and
that file holds your GitHub token.

## 2. Create the GitHub token used by the gateway

**Recommended: issue this token from a dedicated GitHub account** used only for these
document repositories, not your primary account. The token is the gateway's entire blast
radius, so isolating it means a leak cannot touch your main account or other projects.
Add your main account as a collaborator on individual repos when you want to open one in
Claude Code.

In GitHub (on whichever account will own the workspace repos):

1. Open **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Select **Generate new token**.
3. Set the resource owner to that account.
4. Set **Repository access** to **All repositories**. This lets the gateway work with
   repositories created after the token is issued.
5. Grant these repository permissions:
   - **Administration: Read and write** (required only for creating repositories)
   - **Contents: Read and write**
   - **Pull requests: Read and write**
6. Add **Workflows: Read and write** only when you want Claude to edit files under
   `.github/workflows`.
7. Copy the token. GitHub only shows it once.

If you would rather create repositories yourself and let the gateway only read and write
files, you can drop **Administration** entirely; the only tool that needs it is
`create_repository`.

The gateway intentionally has no tool for deleting an entire repository.

## 3. Generate two gateway secrets

Run this command twice and save both different values:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use the first value for:

```text
MCP_OAUTH_CLIENT_SECRET
```

Use the second value for:

```text
MCP_LOGIN_PASSWORD
```

`MCP_LOGIN_PASSWORD` is the password you will type into the authorization page when
connecting Claude. Use the full generated value — the authorization form limits repeated
wrong attempts, but that is a backstop, not a replacement for a long random password.

## 4. Deploy with Render Blueprint

The repository already contains `render.yaml`.

1. Sign in to Render.
2. Select **New → Blueprint**.
3. Connect your GitHub account when prompted.
4. Select the private `bellhop` repository.
5. Render detects `render.yaml`.
6. Enter these secret values when prompted:

| Render variable | Value |
|---|---|
| `GITHUB_TOKEN` | The fine-grained GitHub token |
| `GITHUB_DEFAULT_OWNER` | The workspace account's GitHub username |
| `MCP_OAUTH_CLIENT_SECRET` | First generated random secret |
| `MCP_LOGIN_PASSWORD` | Second generated random secret |

Render generates `JWT_SECRET` automatically.

7. Apply the Blueprint and let the deployment finish.

No database and no persistent disk are required.

The Blueprint uses Render's Starter plan. A sleeping free service can make OAuth or MCP
startup unreliable. You can change `plan: starter` to `plan: free` for testing, but
Starter is the safer choice for normal phone use.

## 5. Verify Render

Render gives the service a URL similar to:

```text
https://bellhop.onrender.com
```

Open:

```text
https://YOUR-SERVICE.onrender.com/health
```

Expected response:

```json
{"status":"ok"}
```

Also open the root URL. It should report that GitHub is the canonical storage and that no
persistent disk is required.

## 6. Add it to Claude

For an individual Pro or Max account:

1. In Claude, open **Customize → Connectors**.
2. Select **+ → Add custom connector**.
3. Name it:

```text
Bellhop
```

4. Use this remote MCP URL:

```text
https://YOUR-SERVICE.onrender.com/mcp
```

5. Open **Advanced settings**.
6. Enter:

```text
OAuth Client ID: bellhop
OAuth Client Secret: the exact MCP_OAUTH_CLIENT_SECRET stored in Render
```

7. Add and connect the connector.
8. When the gateway authorization screen appears, enter `MCP_LOGIN_PASSWORD`.

## 7. Test it in Claude

Start with a read-only test:

> Use Bellhop and run `github_connection_status`.

Then:

> Use Bellhop and list my five most recently updated repositories.

Then create a disposable test repository:

> Use Bellhop to create a private repository named
> `mcp-gateway-test`. Initialize it with a README that says the gateway works.

The repository should appear immediately in GitHub.

## 8. Use it in future chats

Be explicit at the beginning of a new chat:

> Use Bellhop for this project. Create a new private repository called
> `picaform`, choose the repository structure based on the architecture we agree on, and
> commit related code and documents together.

For an existing repository:

> Use Bellhop and work in `YOUR_NAME/REPOSITORY`. Read the current
> branch before changing it, preserve its existing organization unless we deliberately
> decide to restructure it, and commit the code and documentation together.

The gateway does not impose a folder layout. Claude and you decide the paths for each
project.

### Working from more than one chat

Every write checks that the branch has not moved since Claude last read it. If you edit a
repository from two chats at once and the edits touch **different** files, the gateway
re-applies the second change onto the latest commit automatically (the result reports
`rebased_onto_current_head: true`). If both chats edit the **same** file, the second
commit is refused with a `conflicting_paths` list instead of silently overwriting the
first. When that happens, ask Claude to re-read those files and commit again. This is the
safety guard working, not an error.

## 9. Updating the gateway itself

Edit the gateway repository and push to `main`. Render automatically builds and deploys
the new commit. Project repositories require no synchronization because every MCP
operation writes directly to GitHub.

Note that the login attempt limiter keeps its count in memory, so a redeploy or restart
also clears any temporary lockout.

## Troubleshooting

### Claude says the connector cannot authenticate

Confirm that:

- Claude's OAuth Client Secret exactly matches `MCP_OAUTH_CLIENT_SECRET` on Render.
- The MCP URL ends in `/mcp`.
- The Render service is live.
- `/health` responds successfully.

### The authorization page says "Too many attempts"

The form locks after several wrong passwords (default 5 within 15 minutes) and returns a
429 for a few minutes. Wait for the window to clear, then enter the correct
`MCP_LOGIN_PASSWORD`. A successful login resets the counter.

### `github_connection_status` returns 401 or 403

The `GITHUB_TOKEN` is invalid, expired, or blocked by an organization policy.

### Claude can read but cannot create a repository

Confirm the fine-grained token has **Administration: Read and write**.

### Claude can create a repository but cannot commit files

Confirm the token has **Contents: Read and write**.

### A commit was refused with `conflicting_paths`

The branch changed since Claude last read it and your new edit touches one of the same
files. The gateway refuses rather than overwrite the newer version. Ask Claude to re-read
the listed files at the current head and commit again. Edits to different files are
rebased automatically, so you only see this on genuine overlaps.

### Direct commits fail on a protected branch

Tell Claude to create a new branch, commit there, and open a pull request.

## Security summary

- The GitHub token is the whole blast radius — prefer a dedicated account (see step 2).
- Access tokens expire after one hour and refresh tokens after seven days, so a captured
  token ages out within a week on its own.
- Use the full random `MCP_LOGIN_PASSWORD`; the attempt limiter is a backstop, not a
  substitute for entropy.
- The GitHub token never leaves Render. Claude only ever holds a short-lived gateway
  token, never your GitHub credential.
