# CelesteOS Agent

macOS application that runs on each yacht's computer. Syncs documents from the yacht's local NAS storage to the cloud, making them searchable in CelesteOS.

## What This Does

### First Launch (Installer)
When the app is first opened, it runs a setup wizard:

1. **Welcome** — Shows yacht name, begins registration
2. **Verify Identity** — Buyer enters 6-digit code from email
3. **Select Folder** — Auto-detects NAS devices, buyer confirms document root
4. **Done** — Installs auto-start, begins syncing

The GUI is a native macOS window (pywebview) with an embedded HTML interface. Falls back to CLI prompts if pywebview is not available.

### Normal Operation (Sync Daemon)
After setup, the agent runs as a background daemon:

- Scans the NAS folder every 5 minutes for new/changed/deleted files
- Uploads new files to Supabase Storage
- Creates metadata records in the tenant database
- Marks files for text extraction (handled by a separate Docker worker)
- Sends health heartbeats to the cloud
- Recovers gracefully from crashes (idempotent uploads, SQLite manifest)

### Auto-Start
Installs a macOS launchd plist so it:
- Starts automatically on user login
- Restarts if it crashes
- Logs to `~/.celesteos/logs/`

## Architecture

```
NAS Folder (/Volumes/YachtNAS)
  ↓ scan (every 5 min + real-time watcher)
Agent (this repo)
  ↓ upload to Supabase Storage
  ↓ upsert doc_metadata + search_index
Tenant Database (Supabase)
  ↓ embedding_status = 'pending_extraction'
Extraction Worker (Docker, separate service)
  ↓ extracts text, generates embeddings
Search Index → searchable in CelesteOS app
```

## Running in Development

```bash
# Set environment variables
export YACHT_ID=TEST_YACHT_001
export NAS_ROOT=/path/to/test/nas
export SUPABASE_URL=https://your-tenant.supabase.co
export SUPABASE_SERVICE_KEY=your-service-key

# Run one sync cycle
python -m agent --once

# Run continuously
python -m agent

# Launch the installer GUI directly
python -m agent.installer_ui
```

## Configuration

The agent loads config in priority order:
1. **Embedded manifest** (install_manifest.json in app bundle)
2. **Environment variables** (YACHT_ID, NAS_ROOT, etc.)
3. **~/.celesteos/.env.local** (persisted settings)
4. **macOS Keychain** (shared_secret, service keys)

## Building a DMG

```bash
cd installer/build

# Set required env var
export SUPABASE_SERVICE_KEY=your-key

# Build for a specific yacht (fetches info from database)
python build_dmg.py TEST_YACHT_001

# Build without upload
python build_dmg.py TEST_YACHT_001 --no-upload
```

This produces `CelesteOS-{yacht_id}.dmg` containing a signed macOS app bundle with the yacht's identity embedded.

## Security Model

- **yacht_id** — Public identifier embedded in DMG (immutable)
- **yacht_id_hash** — SHA-256 proof of possession
- **shared_secret** — 256-bit random, received once during activation via 2FA
- **Keychain storage** — Secret stored in macOS Keychain, never on disk
- **HMAC-SHA256** — All subsequent API calls signed with shared_secret

## File Classification

The agent classifies files by path and extension:

| Tier | Extensions | What happens |
|------|-----------|-------------|
| Indexable | .pdf, .docx, .xlsx, .csv, .txt, .md, .json, .jpg, .png | Upload + index + extraction worker processes |
| Storage Only | .mp4, .mov, .zip, .psd, .exe | Upload only, no text extraction |
| Skip | .tmp, .ds_store, .swp, .lock, .bak | Ignored completely |

Files are also tagged by system type based on path keywords: electrical, propulsion, navigation, safety, etc.

## Related Repos

- [`celesteos-registration`](https://github.com/shortalex12333/celesteos-registration) — Registration API (2FA, download portal)
- `Cloud_PMS` — Main yacht management system
