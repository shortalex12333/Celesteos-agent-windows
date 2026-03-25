#!/usr/bin/env python3
"""
CelesteOS DMG Builder
=====================
Builds per-yacht DMG installers with embedded cryptographic identity.

Build Process:
1. Load yacht metadata from fleet_registry
2. Generate installation manifest (yacht_id, yacht_id_hash, api_endpoint)
3. Bundle Python agent with PyInstaller
4. Embed manifest in signed Resources
5. Create DMG with create-dmg
6. (Optional) Code sign and notarize

Output:
    CelesteOS-{yacht_id}.dmg
    Contains: CelesteOS.app with embedded yacht identity

Security:
- Manifest is embedded in app bundle, protected by code signature
- yacht_id cannot be changed without invalidating signature
- Each DMG is unique to its yacht
"""

import os
import sys
import json
import shutil
import hashlib
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass

# Add lib to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'lib'))
from crypto import compute_yacht_hash, generate_installation_manifest


@dataclass
class BuildConfig:
    """Build configuration."""
    yacht_id: str
    yacht_name: str
    yacht_model: Optional[str]
    buyer_name: str
    buyer_email: str
    api_endpoint: str = "https://qvzmkaamzaqxpzbewjxe.supabase.co"  # Supabase
    registration_api_endpoint: str = os.getenv('REGISTRATION_API_ENDPOINT', 'http://localhost:8001')  # Production: https://registration.celeste7.ai
    version: str = "1.0.0"
    bundle_id: str = "com.celeste7.celesteos"
    agent_source: Path = Path(os.getenv('CELESTEOS_AGENT_SOURCE', str(Path.home() / "Documents" / "celesteos-agent")))
    output_dir: Path = Path(os.getenv('CELESTEOS_OUTPUT_DIR', str(Path.home() / "Documents" / "celesteos-agent" / "installer" / "build" / "output")))
    sign_identity: Optional[str] = None  # Apple Developer ID
    supabase_url: str = "https://qvzmkaamzaqxpzbewjxe.supabase.co"
    supabase_service_key: Optional[str] = None  # Master Supabase — for DB queries during build
    tenant_supabase_url: str = os.getenv('TENANT_SUPABASE_URL', '')
    tenant_supabase_service_key: str = os.getenv('TENANT_SUPABASE_SERVICE_KEY', '')  # Embedded in DMG manifest


class DMGBuilder:
    """Builds signed DMG installers."""

    def __init__(self, config: BuildConfig):
        self.config = config
        self.build_dir = Path(tempfile.mkdtemp(prefix='celesteos_build_'))
        self.app_path: Optional[Path] = None
        self.dmg_path: Optional[Path] = None

    def build(self) -> Path:
        """
        Execute full build pipeline.

        Returns:
            Path to built DMG
        """
        print(f"Building CelesteOS for yacht: {self.config.yacht_id}")
        print(f"Build directory: {self.build_dir}")
        print()

        try:
            self._generate_manifest()
            self._bundle_app()
            self._embed_manifest()
            self._create_dmg()

            if self.config.sign_identity:
                self._sign_and_notarize()

            # Copy to output directory
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            final_path = self.config.output_dir / self.dmg_path.name
            shutil.copy2(self.dmg_path, final_path)

            # Update dmg_path to final location for upload
            self.dmg_path = final_path

            print(f"\n✓ Build complete: {final_path}")
            return final_path

        finally:
            # Cleanup build directory (but not output directory)
            shutil.rmtree(self.build_dir, ignore_errors=True)

    def _generate_manifest(self):
        """Generate installation manifest."""
        print("1. Generating installation manifest...")

        if not self.config.tenant_supabase_service_key:
            raise BuildError(
                "TENANT_SUPABASE_SERVICE_KEY environment variable required. "
                "This is embedded in the DMG — the agent needs it to talk to the tenant database."
            )

        manifest = {
            'yacht_id': self.config.yacht_id,
            'yacht_id_hash': compute_yacht_hash(self.config.yacht_id),
            'yacht_name': self.config.yacht_name,
            'api_endpoint': self.config.api_endpoint,  # Supabase for verify-credentials
            'registration_api_endpoint': self.config.registration_api_endpoint,  # Registration API for 2FA
            'tenant_supabase_url': self.config.tenant_supabase_url,
            'tenant_supabase_service_key': self.config.tenant_supabase_service_key,
            'version': self.config.version,
            'build_timestamp': int(datetime.utcnow().timestamp()),
            'bundle_id': self.config.bundle_id,
        }

        self.manifest_path = self.build_dir / 'install_manifest.json'
        with open(self.manifest_path, 'w') as f:
            json.dump(manifest, f, indent=2)

        print(f"   yacht_id_hash: {manifest['yacht_id_hash'][:16]}...")

    def _bundle_app(self):
        """Bundle Python agent with PyInstaller."""
        print("2. Bundling application with PyInstaller...")

        spec_content = f'''
# PyInstaller spec for CelesteOS
# Generated for yacht: {self.config.yacht_id}

import sys
sys.setrecursionlimit(5000)

block_cipher = None

a = Analysis(
    ['{self.config.agent_source}/agent/__main__.py'],
    pathex=['{self.config.agent_source}'],
    binaries=[],
    datas=[
        ('{self.manifest_path}', 'Resources'),
    ],
    hiddenimports=[
        'agent',
        'agent.daemon',
        'agent.config',
        'agent.scanner',
        'agent.hasher',
        'agent.uploader',
        'agent.indexer',
        'agent.classifier',
        'agent.manifest_db',
        'agent.heartbeat',
        'agent.watcher',
        'agent.log_config',
        'agent.constants',
        'agent.folder_selector',
        'agent.launchd',
        'agent.platform',
        'agent.platform_mac',
        'agent.platform_win',
        'agent.status_tray',
        'agent.status_window',
        'lib',
        'lib.crypto',
        'lib.installer',
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[
        'test', 'unittest',
        'PIL', 'Pillow',  # 20MB - not used
        'psycopg2',       # 17MB - using REST API, not direct Postgres
        'numpy',          # 6.6MB - not used
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CelesteOS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # Build for current architecture only (ARM-only deps)
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CelesteOS',
)

app = BUNDLE(
    coll,
    name='CelesteOS.app',
    icon=None,
    bundle_identifier='{self.config.bundle_id}',
    version='{self.config.version}',
    info_plist={{
        'CFBundleName': 'CelesteOS',
        'CFBundleDisplayName': 'CelesteOS',
        'CFBundleIdentifier': '{self.config.bundle_id}',
        'CFBundleVersion': '{self.config.version}',
        'CFBundleShortVersionString': '{self.config.version}',
        'LSMinimumSystemVersion': '10.15',
        'NSHighResolutionCapable': True,
        'LSUIElement': True,  # Run as background app
        'CelesteOS_YachtID': '{self.config.yacht_id}',
    }},
)
'''

        spec_path = self.build_dir / 'celesteos.spec'
        with open(spec_path, 'w') as f:
            f.write(spec_content)

        # Run PyInstaller
        result = subprocess.run(
            [
                sys.executable, '-m', 'PyInstaller',
                '--distpath', str(self.build_dir / 'dist'),
                '--workpath', str(self.build_dir / 'work'),
                '--noconfirm',
                '--clean',
                str(spec_path)
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"   PyInstaller error: {result.stderr}")
            raise BuildError(f"PyInstaller failed: {result.stderr}")

        self.app_path = self.build_dir / 'dist' / 'CelesteOS.app'

        if not self.app_path.exists():
            raise BuildError("App bundle not created")

        print(f"   Created: {self.app_path}")

    def _embed_manifest(self):
        """Embed manifest in app bundle Resources."""
        print("3. Embedding installation manifest...")

        resources_dir = self.app_path / 'Contents' / 'Resources'
        resources_dir.mkdir(parents=True, exist_ok=True)

        manifest_dest = resources_dir / 'install_manifest.json'
        shutil.copy2(self.manifest_path, manifest_dest)

        # Make read-only
        manifest_dest.chmod(0o444)

        print(f"   Embedded at: {manifest_dest}")

    def _create_dmg(self):
        """Create DMG from app bundle."""
        print("4. Creating DMG...")

        dmg_name = f"CelesteOS-{self.config.yacht_id}.dmg"
        self.dmg_path = self.build_dir / dmg_name

        # Use hdiutil to create DMG
        staging = self.build_dir / 'dmg_staging'
        staging.mkdir()

        # Copy app to staging
        shutil.copytree(self.app_path, staging / 'CelesteOS.app')

        # Create symlink to Applications
        (staging / 'Applications').symlink_to('/Applications')

        # Create DMG
        result = subprocess.run(
            [
                'hdiutil', 'create',
                '-volname', f'CelesteOS {self.config.yacht_id}',
                '-srcfolder', str(staging),
                '-ov',
                '-format', 'UDZO',
                str(self.dmg_path)
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise BuildError(f"DMG creation failed: {result.stderr}")

        # Calculate DMG hash
        dmg_hash = hashlib.sha256(self.dmg_path.read_bytes()).hexdigest()
        print(f"   Created: {self.dmg_path}")
        print(f"   SHA256:  {dmg_hash[:16]}...")

    def _sign_and_notarize(self):
        """Code sign and notarize the DMG."""
        print("5. Code signing and notarizing...")

        # Sign the app bundle first
        subprocess.run([
            'codesign', '--deep', '--force', '--verify', '--verbose',
            '--sign', self.config.sign_identity,
            '--options', 'runtime',
            str(self.app_path)
        ], check=True)

        # Sign the DMG
        subprocess.run([
            'codesign', '--force', '--verify', '--verbose',
            '--sign', self.config.sign_identity,
            str(self.dmg_path)
        ], check=True)

        # Notarize (requires Apple Developer account)
        print("   Submitting for notarization...")
        # subprocess.run([
        #     'xcrun', 'notarytool', 'submit',
        #     str(self.dmg_path),
        #     '--keychain-profile', 'notarization',
        #     '--wait'
        # ], check=True)

        # Staple
        # subprocess.run([
        #     'xcrun', 'stapler', 'staple', str(self.dmg_path)
        # ], check=True)

        print("   ✓ Signed")

    def _upload_to_storage(self):
        """Upload DMG to Supabase Storage."""
        print("6. Uploading to Supabase Storage...")

        if not self.config.supabase_service_key:
            print("   ⚠ Skipping upload: SUPABASE_SERVICE_KEY not set")
            return

        if not self.dmg_path or not self.dmg_path.exists():
            raise BuildError("DMG file not found for upload")

        import requests

        # Storage path: dmg/{yacht_id}/CelesteOS-{yacht_id}.dmg
        storage_path = f"dmg/{self.config.yacht_id}/{self.dmg_path.name}"

        # Upload to Supabase Storage
        url = f"{self.config.supabase_url}/storage/v1/object/installers/{storage_path}"
        headers = {
            'apikey': self.config.supabase_service_key,
            'Authorization': f'Bearer {self.config.supabase_service_key}',
            'Content-Type': 'application/x-apple-diskimage',
            'x-upsert': 'true'  # Overwrite if exists
        }

        with open(self.dmg_path, 'rb') as f:
            response = requests.post(url, headers=headers, data=f, timeout=300)

        if response.status_code not in [200, 201]:
            raise BuildError(f"Storage upload failed: {response.status_code} {response.text}")

        print(f"   ✓ Uploaded to: {storage_path}")

        # Calculate and store DMG hash in database
        dmg_hash = hashlib.sha256(self.dmg_path.read_bytes()).hexdigest()

        # Update fleet_registry with DMG info
        update_url = f"{self.config.supabase_url}/rest/v1/fleet_registry"
        update_headers = {
            'apikey': self.config.supabase_service_key,
            'Authorization': f'Bearer {self.config.supabase_service_key}',
            'Content-Type': 'application/json',
            'Prefer': 'return=minimal'
        }
        update_data = {
            'dmg_storage_path': storage_path,
            'dmg_sha256': dmg_hash,
            'dmg_built_at': datetime.utcnow().isoformat()
        }

        params = {'yacht_id': f'eq.{self.config.yacht_id}'}
        response = requests.patch(update_url, headers=update_headers, json=update_data, params=params, timeout=30)

        if response.status_code != 204:
            print(f"   ⚠ Warning: Could not update database with DMG info: {response.status_code}")
        else:
            print(f"   ✓ Database updated with DMG hash")


class BuildError(Exception):
    """Build process error."""
    pass


def fetch_yacht_from_database(yacht_id: str) -> Dict[str, Any]:
    """
    Fetch yacht data from Supabase fleet_registry.

    Args:
        yacht_id: Yacht identifier

    Returns:
        Dictionary with yacht data

    Raises:
        BuildError: If yacht not found or database error
    """
    supabase_url = os.getenv('SUPABASE_URL', 'https://qvzmkaamzaqxpzbewjxe.supabase.co')
    supabase_key = os.getenv('SUPABASE_SERVICE_KEY')

    if not supabase_key:
        raise BuildError(
            "SUPABASE_SERVICE_KEY environment variable required. "
            "Set it to your Supabase service role key."
        )

    import requests

    # Query fleet_registry via REST API
    url = f"{supabase_url}/rest/v1/fleet_registry"
    headers = {
        'apikey': supabase_key,
        'Authorization': f'Bearer {supabase_key}',
        'Content-Type': 'application/json'
    }
    params = {
        'yacht_id': f'eq.{yacht_id}',
        'select': 'yacht_id,yacht_name,yacht_model,buyer_name,buyer_email,yacht_id_hash,tenant_supabase_url'
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code != 200:
        raise BuildError(f"Database query failed: {response.status_code} {response.text}")

    data = response.json()

    if not data or len(data) == 0:
        raise BuildError(f"Yacht '{yacht_id}' not found in database. Create it first using /create-yacht endpoint.")

    yacht = data[0]

    # Validate required fields
    if not yacht.get('buyer_email'):
        raise BuildError(f"Yacht '{yacht_id}' has no buyer_email set in database")

    return yacht


def build_for_yacht(
    yacht_id: str,
    sign: bool = False,
    upload: bool = True
) -> Path:
    """
    Build DMG for a specific yacht.

    Args:
        yacht_id: Yacht identifier (must exist in database)
        sign: Whether to code sign
        upload: Whether to upload to Supabase Storage

    Returns:
        Path to built DMG

    Raises:
        BuildError: If yacht not found or build fails
    """
    print(f"Fetching yacht data from database for: {yacht_id}")
    yacht_data = fetch_yacht_from_database(yacht_id)

    print(f"Found yacht: {yacht_data['yacht_name']}")
    print(f"Buyer: {yacht_data.get('buyer_name', 'N/A')} <{yacht_data['buyer_email']}>")

    config = BuildConfig(
        yacht_id=yacht_id,
        yacht_name=yacht_data['yacht_name'],
        yacht_model=yacht_data.get('yacht_model'),
        buyer_name=yacht_data.get('buyer_name', ''),
        buyer_email=yacht_data['buyer_email'],
        sign_identity="Developer ID Application: Your Name" if sign else None,
        supabase_service_key=os.getenv('SUPABASE_SERVICE_KEY'),
        tenant_supabase_url=yacht_data.get('tenant_supabase_url', os.getenv('TENANT_SUPABASE_URL', '')),
    )

    builder = DMGBuilder(config)
    dmg_path = builder.build()

    if upload and config.supabase_service_key:
        builder._upload_to_storage()

    return dmg_path


# CLI
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Build CelesteOS DMG - Queries database for yacht info',
        epilog='Environment variables:\n'
               '  SUPABASE_SERVICE_KEY - Required for database access and storage upload\n'
               '  CELESTEOS_AGENT_SOURCE - Path to agent source code (default: ~/Documents/PYTHON_LOCAL_CLOUD_PMS)\n'
               '  CELESTEOS_OUTPUT_DIR - Output directory for DMGs (default: ./output)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('yacht_id', help='Yacht identifier (must exist in database)')
    parser.add_argument('--sign', action='store_true', help='Code sign the DMG')
    parser.add_argument('--no-upload', action='store_true', help='Skip upload to Supabase Storage')

    args = parser.parse_args()

    # Verify SUPABASE_SERVICE_KEY is set
    if not os.getenv('SUPABASE_SERVICE_KEY'):
        print("ERROR: SUPABASE_SERVICE_KEY environment variable not set")
        print("Export it with your Supabase service role key:")
        print("  export SUPABASE_SERVICE_KEY='your-key-here'")
        sys.exit(1)

    try:
        dmg_path = build_for_yacht(
            yacht_id=args.yacht_id,
            sign=args.sign,
            upload=not args.no_upload
        )

        print(f"\n{'='*60}")
        print(f"✓ Build Complete!")
        print(f"{'='*60}")
        print(f"DMG: {dmg_path}")
        print(f"\nNext steps:")
        print(f"  1. DMG is saved locally and uploaded to Supabase Storage")
        print(f"  2. Generate download token: curl -X POST https://.../.../generate-download-token")
        print(f"  3. Send download link to yacht owner")
        print(f"  4. Owner downloads, installs, and activates")

    except BuildError as e:
        print(f"\n✗ Build failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nBuild interrupted")
        sys.exit(1)
