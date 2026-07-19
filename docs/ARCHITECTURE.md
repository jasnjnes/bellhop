# Architecture

```text
Claude web / desktop / mobile
              |
    OAuth 2 authorization + PKCE
              |
       Remote MCP on Render
              |
       GitHub REST / Git API
              |
     Arbitrary GitHub repositories
```

## Canonical state

All project state is in GitHub. The gateway has no project filesystem and no synchronization
job. A Render redeploy cannot lose project files because it never stores them.

## Repository structure

The gateway validates only path safety. It never routes documents into predefined folders.
The agent and user decide each repository's structure independently.

## Atomic commits

The gateway uses GitHub's Git database API:

1. Read the branch reference.
2. Verify `expected_head`.
3. Create blobs.
4. Create a tree based on the current tree.
5. Create one commit.
6. Fast-forward the branch reference.

If another writer moves the branch, GitHub rejects the update and the gateway returns a
conflict rather than forcing the branch.

## Authentication boundaries

Two credentials serve different purposes:

- Claude authenticates to the gateway through OAuth and PKCE.
- The gateway authenticates to GitHub with `GITHUB_TOKEN`.

The GitHub credential never leaves Render.

## Scaling

The gateway is stateless and can run multiple instances. GitHub branch references are the
concurrency boundary. No distributed lock is required because non-fast-forward branch
updates are rejected.

## Future production upgrade

For more than one trusted owner:

- Use an external OIDC provider for connector login.
- Install a GitHub App and mint short-lived installation tokens.
- Map authenticated users to approved GitHub installations and repositories.
- Store refresh-token revocation and audit events.
- Add repository allowlists and write policies.
