---
name: GitHub API upload pacing
description: Rate-limit behavior when pushing large workspace snapshots through the authenticated GitHub connector
---

When uploading many Git blobs through the authenticated GitHub connector, keep requests below roughly 10 per second and honor `Retry-After` on HTTP 429 responses.

**Why:** The connector proxy enforces a per-Repl request-rate limit, so parallel blob creation can fail before the tree or commit is created even when authentication is healthy.

**How to apply:** Use a paced sequential or tightly throttled uploader for Git data API snapshots, with bounded 429 retries. This applies to blob batches; tree, commit, and ref updates can follow after the batch completes.