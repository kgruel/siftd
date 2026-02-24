# Sync

siftd stores conversations in a local SQLite database. Sync moves conversations between databases — your laptop, a server, a NAS. You ingest on each machine where you work, then sync to keep a complete picture.

## Why sync

You use Claude Code on your laptop during the day and on a home server in the evening. Each machine has its own siftd database. Without sync, searching on either machine gives you a partial view.

Sync fills the gap: push your laptop's conversations to the server, pull the server's conversations to your laptop. After a round trip, both databases have everything.

## The mental model

Sync works like git remotes. You register a named remote — a database on another machine (or a local path). Then push and pull deltas:

```
laptop                          alcove (server)
┌──────────┐                   ┌──────────┐
│ siftd.db │ ──── push ────▶  │ team.db  │
│          │ ◀──── pull ────  │          │
└──────────┘                   └──────────┘
```

Push sends your local conversations to the remote. Pull brings the remote's conversations to you. Both directions merge cleanly — duplicates are deduplicated by ID, and newer versions replace older ones.

## Setting up remotes

Register a remote with a name and a target:

```bash
# SSH remote — host:path
siftd db remote add alcove deploy@192.168.1.44:/data/siftd/team.db

# Local path — NAS mount, external drive, shared directory
siftd db remote add nas /mnt/nas/siftd/team.db
```

List and manage remotes:

```bash
siftd db remote list       # show all remotes with last push/pull times
siftd db remote remove nas
```

The remote name is what you use in push/pull commands. The target tells siftd how to reach the database — over SSH or by direct file access.

## Push

Push sends conversations from your local database to the remote:

```bash
siftd db push alcove
```

```
Pushing to alcove (deploy@192.168.1.44)...
Pushed 47 conversations (182.3 KB)
```

The first push to a new remote creates the database. Subsequent pushes merge only the delta — conversations added since the last push.

### Filtering what you push

```bash
siftd db push alcove --since 7d       # only last 7 days
siftd db push alcove -w myproject     # only one workspace
siftd db push alcove --all            # everything, ignoring last_push
siftd db push alcove --dry-run        # preview without transferring
```

## Pull

Pull brings conversations from the remote to your local database:

```bash
siftd db pull alcove
```

```
Pulling from alcove (deploy@192.168.1.44)...
Pulled 12 conversations (45.2 KB)
```

Same filtering options as push:

```bash
siftd db pull alcove --since 7d       # only last 7 days
siftd db pull alcove -w project       # only one workspace
siftd db pull alcove --all            # everything, ignoring last_pull
siftd db pull alcove --dry-run        # preview without merging
```

Pull creates the local database if it doesn't exist yet — useful for bootstrapping a new machine from a server.

## Delta tracking

siftd tracks when you last pushed and pulled to each remote:

```bash
siftd db remote list
```

```
alcove               deploy@192.168.1.44:/data/siftd/team.db
                     last push: 2026-02-24T08:30:00+00:00
                     last pull: 2026-02-24T08:35:00+00:00
```

When you run `siftd db push alcove` without `--since`, it automatically pushes only conversations newer than `last_push`. Same for pull with `last_pull`. This makes repeated syncs fast — only the delta transfers.

The `--since` flag overrides delta tracking for that invocation without updating the timestamp. Use this for one-off transfers:

```bash
siftd db push alcove --since 30d    # push last 30 days, don't advance last_push
```

The `--all` flag ignores the timestamp entirely — useful for a full resync.

## Transport

### SSH

For remote hosts, siftd uses a single SSH connection. Push pipes a slice database over stdin to `siftd db receive` on the remote. Pull runs `siftd db send` on the remote and streams the result back.

```
Push:  local slice.db ─── stdin ───▶ ssh host "siftd db receive"
Pull:  ssh host "siftd db send" ───▶ local temp.db ─── merge
```

Requirements:
- SSH access to the remote host
- siftd installed on the remote (`uv tool install siftd`)
- The remote database path must be accessible to the remote siftd process

### Local paths

For local-path remotes (NAS mounts, shared directories), siftd reads and writes the file directly. No SSH, no network — just filesystem access.

### SSH configuration

Global SSH options apply to all remotes:

```toml
# ~/.config/siftd/config.toml
[sync.ssh]
options = ["-o", "StrictHostKeyChecking=no"]
connect_timeout_s = 30
```

Per-remote options override the global ones:

```toml
[sync.remotes.alcove.ssh]
options = ["-i", "~/.ssh/alcove_key"]
```

## The pipe primitives

Push and pull are high-level workflows built on two lower-level commands:

| Command | Direction | Purpose |
|---------|-----------|---------|
| `db send` | DB → stdout | Slice the database, write binary SQLite to stdout |
| `db receive` | stdin → DB | Read binary SQLite from stdin, create-or-merge |

These are the building blocks that push and pull orchestrate over SSH. You can use them directly for custom workflows:

```bash
# Manual transfer via file
siftd db send --since 7d > /tmp/slice.db
scp /tmp/slice.db server:/tmp/
ssh server "siftd db receive < /tmp/slice.db"

# Pipe between local databases
siftd --db source.db db send | siftd --db target.db db receive
```

Both commands use the same pattern: binary data on stdout/stdin, JSON metadata on stderr. This lets the caller parse transfer stats without interfering with the data stream.

## Merge behavior

Both push and pull use the same merge logic:

- **New conversations** are inserted
- **Existing conversations** with the same ID are replaced if the incoming version is newer
- **Tags, workspaces, models** are merged (created if missing, matched by content)
- **Content blobs** are deduplicated by SHA256 hash
- **Foreign key integrity** is maintained throughout

The merge is idempotent — pushing or pulling the same data twice produces the same result.

## Typical workflows

### Daily sync between laptop and server

```bash
# End of day: push laptop → server
siftd db push alcove

# Start of day: pull server → laptop (picks up overnight work)
siftd db pull alcove
```

### Bootstrap a new machine

```bash
# On the new machine
siftd db remote add alcove deploy@server:/data/siftd/team.db
siftd db pull alcove --all
```

### Periodic backup to NAS

```bash
siftd db remote add nas /mnt/nas/siftd/backup.db
siftd db push nas    # run this on a cron, or whenever you think of it
```
