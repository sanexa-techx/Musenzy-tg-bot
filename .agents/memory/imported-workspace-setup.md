---
name: Imported workspace setup
description: Environment-specific setup needed when importing a mixed Python and pnpm workspace from GitHub.
---

For a GitHub import that includes both Python services and a pnpm workspace, generated dependency directories may be stale or incomplete even when the project files are present. Recreate the project-local Python environment before installing Python dependencies, and install JavaScript dependencies from the committed pnpm lockfile before restarting artifact workflows.

**Why:** An empty or invalid `.pythonlibs` directory caused the managed Python installer to refuse environment creation, while the imported artifact workflows initially had no `node_modules`.

**How to apply:** Treat these generated directories as environment state, not repository source. Repair them after import without changing pinned dependency versions, then restart every imported workflow and inspect logs.