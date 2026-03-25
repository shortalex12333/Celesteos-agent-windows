#!/usr/bin/env python3
"""
CelesteOS Windows EXE Builder
===============================
Builds per-yacht Windows installers with embedded cryptographic identity.

Build Process:
1. Load yacht metadata from fleet_registry (same as build_dmg.py)
2. Generate installation manifest (yacht_id, yacht_id_hash, api_endpoint)
3. Bundle Python agent with PyInstaller → CelesteOS.exe
4. Embed manifest in Resources/ directory alongside exe
5. Generate Inno Setup script → compile to CelesteOS-Setup-{yacht_id}.exe

Output:
    CelesteOS-Setup-{yacht_id}.exe
    Contains: CelesteOS.exe with embedded yacht identity + installer wrapper

Security:
- Manifest is embedded alongside exe, protected by installer signature
- yacht_id cannot be changed without rebuilding
- Each EXE is unique to its yacht
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
from crypto import compute_yacht_hash


@dataclass
class BuildConfig:
    """Build configuration."""
    yacht_id: str
    yacht_name: str
    yacht_model: Optional[str]
    buyer_name: str
    buyer_email: str
    api_endpoint: str = "https://qvzmkaamzaqxpzbewjxe.supabase.co"
    registration_api_endpoint: str = os.getenv('REGISTRATION_API_ENDPOINT', 'http://localhost:8001')
    version: str = "1.0.0"
    bundle_id: str = "com.celeste7.celesteos"
    agent_source: Path = Path(os.getenv('CELESTEOS_AGENT_SOURCE', str(Path.home() / "Documents" / "celesteos-agent")))
    output_dir: Path = Path(os.getenv('CELESTEOS_OUTPUT_DIR', str(Path.home() / "Documents" / "celesteos-agent" / "installer" / "build" / "output")))
    supabase_url: str = "https://qvzmkaamzaqxpzbewjxe.supabase.co"
    supabase_service_key: Optional[str] = None
    tenant_supabase_url: str = os.getenv('TENANT_SUPABASE_URL', '')
    tenant_supabase_service_key: str = os.getenv('TENANT_SUPABASE_SERVICE_KEY', '')


class EXEBuilder:
    """Builds Windows EXE installers."""

    def __init__(self, config: BuildConfig):
        self.config = config
        self.build_dir = Path(tempfile.mkdtemp(prefix='celesteos_build_'))
        self.exe_path: Optional[Path] = None
        self.installer_path: Optional[Path] = None

    def build(self) -> Path:
        """Execute full build pipeline. Returns path to installer EXE."""
        print(f"Building CelesteOS (Windows) for yacht: {self.config.yacht_id}")
        print(f"Build directory: {self.build_dir}")
        print()

        try:
            self._generate_manifest()
            self._bundle_exe()
            self._embed_manifest()
            self._create_installer()

            # Copy to output directory
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            final_path = self.config.output_dir / self.installer_path.name
            shutil.copy2(self.installer_path, final_path)
            self.installer_path = final_path

            print(f"\n[OK] Build complete: {final_path}")
            return final_path

        finally:
            shutil.rmtree(self.build_dir, ignore_errors=True)

    def _generate_manifest(self):
        """Generate installation manifest."""
        print("1. Generating installation manifest...")

        if not self.config.tenant_supabase_service_key:
            raise BuildError(
                "TENANT_SUPABASE_SERVICE_KEY environment variable required. "
                "This is embedded in the EXE — the agent needs it to talk to the tenant database."
            )

        manifest = {
            'yacht_id': self.config.yacht_id,
            'yacht_id_hash': compute_yacht_hash(self.config.yacht_id),
            'yacht_name': self.config.yacht_name,
            'api_endpoint': self.config.api_endpoint,
            'registration_api_endpoint': self.config.registration_api_endpoint,
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

    def _bundle_exe(self):
        """Bundle Python agent with PyInstaller into a single EXE."""
        print("2. Bundling application with PyInstaller...")

        sep = os.pathsep  # ; on Windows, : on macOS/Linux
        ico_path = Path(__file__).parent / "celesteos.ico"
        icon_arg = ['--icon', str(ico_path)] if ico_path.exists() else []

        cmd = [
            sys.executable, '-m', 'PyInstaller',
            '--name', 'CelesteOS',
            '--onedir',
            '--windowed',  # No console window
            '--distpath', str(self.build_dir / 'dist'),
            '--workpath', str(self.build_dir / 'work'),
            '--noconfirm',
            '--clean',
            '--add-data', f'{self.manifest_path}{sep}Resources',
            '--paths', str(self.config.agent_source),
            '--hidden-import', 'agent',
            '--hidden-import', 'agent.daemon',
            '--hidden-import', 'agent.config',
            '--hidden-import', 'agent.scanner',
            '--hidden-import', 'agent.hasher',
            '--hidden-import', 'agent.uploader',
            '--hidden-import', 'agent.indexer',
            '--hidden-import', 'agent.classifier',
            '--hidden-import', 'agent.manifest_db',
            '--hidden-import', 'agent.heartbeat',
            '--hidden-import', 'agent.watcher',
            '--hidden-import', 'agent.log_config',
            '--hidden-import', 'agent.constants',
            '--hidden-import', 'agent.folder_selector',
            '--hidden-import', 'agent.platform',
            '--hidden-import', 'agent.platform_win',
            '--hidden-import', 'agent.platform_mac',
            '--hidden-import', 'agent.status_tray',
            '--hidden-import', 'agent.status_window',
            '--hidden-import', 'lib',
            '--hidden-import', 'lib.crypto',
            '--hidden-import', 'lib.installer',
            '--hidden-import', 'keyring',
            '--hidden-import', 'keyring.backends',
            '--hidden-import', 'keyring.backends.Windows',
            '--hidden-import', 'pystray',
            '--hidden-import', 'pystray._win32',
            '--hidden-import', 'plyer',
            '--hidden-import', 'plyer.platforms.win',
            '--hidden-import', 'plyer.platforms.win.notification',
            '--exclude-module', 'test',
            '--exclude-module', 'unittest',
            '--exclude-module', 'rumps',  # macOS only
            *icon_arg,
            str(self.config.agent_source / 'agent' / '__main__.py'),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"   PyInstaller stdout: {result.stdout[-1000:]}")
            print(f"   PyInstaller stderr: {result.stderr[-1000:]}")
            raise BuildError(f"PyInstaller failed (rc={result.returncode})")

        self.exe_path = self.build_dir / 'dist' / 'CelesteOS'

        if not self.exe_path.exists():
            raise BuildError("EXE bundle directory not created")

        print(f"   Created: {self.exe_path}")

    def _embed_manifest(self):
        """Embed manifest in Resources/ directory alongside the exe."""
        print("3. Embedding installation manifest...")

        resources_dir = self.exe_path / 'Resources'
        resources_dir.mkdir(parents=True, exist_ok=True)

        manifest_dest = resources_dir / 'install_manifest.json'
        shutil.copy2(self.manifest_path, manifest_dest)

        print(f"   Embedded at: {manifest_dest}")

    def _create_installer(self):
        """Generate Inno Setup script and compile installer."""
        print("4. Creating installer...")

        iss_content = self._generate_iss()
        iss_path = self.build_dir / 'celesteos.iss'
        iss_path.write_text(iss_content, encoding='utf-8')

        # Try to compile with Inno Setup
        iscc_paths = [
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            r"C:\Program Files\Inno Setup 6\ISCC.exe",
            "ISCC.exe",  # On PATH (e.g. GitHub Actions)
        ]

        iscc = None
        for path in iscc_paths:
            if os.path.isfile(path) or shutil.which(path):
                iscc = path
                break

        if iscc:
            result = subprocess.run(
                [iscc, str(iss_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"   Inno Setup error: {result.stderr[-500:]}")
                raise BuildError("Inno Setup compilation failed")

            installer_name = f"CelesteOS-Setup-{self.config.yacht_id}.exe"
            self.installer_path = self.build_dir / 'installer_output' / installer_name
            print(f"   Created installer: {self.installer_path}")
        else:
            # Fallback: just zip the dist directory
            print("   Inno Setup not found — creating ZIP archive instead")
            zip_name = f"CelesteOS-{self.config.yacht_id}"
            zip_path = shutil.make_archive(
                str(self.build_dir / zip_name), 'zip', self.exe_path,
            )
            self.installer_path = Path(zip_path)
            print(f"   Created archive: {self.installer_path}")

    def _generate_iss(self) -> str:
        """Generate Inno Setup .iss script."""
        return f"""\
[Setup]
AppName=CelesteOS
AppVersion={self.config.version}
AppPublisher=Celeste7 Ltd
DefaultDirName={{autopf}}\\CelesteOS
DefaultGroupName=CelesteOS
OutputDir={self.build_dir / 'installer_output'}
OutputBaseFilename=CelesteOS-Setup-{self.config.yacht_id}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
DisableProgramGroupPage=yes

[Files]
Source: "{self.exe_path}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{{group}}\\CelesteOS"; Filename: "{{app}}\\CelesteOS.exe"
Name: "{{userstartup}}\\CelesteOS"; Filename: "{{app}}\\CelesteOS.exe"; Comment: "Start CelesteOS on login"

[Run]
Filename: "{{app}}\\CelesteOS.exe"; Description: "Launch CelesteOS"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{{app}}\\CelesteOS.exe"; Parameters: "--uninstall"; Flags: skipifdoesntexist
"""


class BuildError(Exception):
    """Build process error."""
    pass


def fetch_yacht_from_database(yacht_id: str) -> Dict[str, Any]:
    """Fetch yacht data from Supabase fleet_registry."""
    supabase_url = os.getenv('SUPABASE_URL', 'https://qvzmkaamzaqxpzbewjxe.supabase.co')
    supabase_key = os.getenv('SUPABASE_SERVICE_KEY')

    if not supabase_key:
        raise BuildError(
            "SUPABASE_SERVICE_KEY environment variable required. "
            "Set it to your Supabase service role key."
        )

    import requests

    url = f"{supabase_url}/rest/v1/fleet_registry"
    headers = {
        'apikey': supabase_key,
        'Authorization': f'Bearer {supabase_key}',
        'Content-Type': 'application/json',
    }
    params = {
        'yacht_id': f'eq.{yacht_id}',
        'select': 'yacht_id,yacht_name,yacht_model,buyer_name,buyer_email,yacht_id_hash,tenant_supabase_url',
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code != 200:
        raise BuildError(f"Database query failed: {response.status_code} {response.text}")

    data = response.json()

    if not data or len(data) == 0:
        raise BuildError(f"Yacht '{yacht_id}' not found in database.")

    yacht = data[0]
    if not yacht.get('buyer_email'):
        raise BuildError(f"Yacht '{yacht_id}' has no buyer_email set in database")

    return yacht


def build_for_yacht(yacht_id: str, upload: bool = True) -> Path:
    """Build EXE installer for a specific yacht."""
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
        supabase_service_key=os.getenv('SUPABASE_SERVICE_KEY'),
        tenant_supabase_url=yacht_data.get('tenant_supabase_url', os.getenv('TENANT_SUPABASE_URL', '')),
    )

    builder = EXEBuilder(config)
    return builder.build()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Build CelesteOS Windows EXE — Queries database for yacht info',
        epilog='Environment variables:\n'
               '  SUPABASE_SERVICE_KEY - Required for database access\n'
               '  TENANT_SUPABASE_SERVICE_KEY - Required, embedded in EXE\n'
               '  CELESTEOS_AGENT_SOURCE - Path to agent source code\n'
               '  CELESTEOS_OUTPUT_DIR - Output directory for installers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('yacht_id', help='Yacht identifier (must exist in database)')
    parser.add_argument('--no-upload', action='store_true', help='Skip upload to Supabase Storage')

    args = parser.parse_args()

    if not os.getenv('SUPABASE_SERVICE_KEY'):
        print("ERROR: SUPABASE_SERVICE_KEY environment variable not set")
        sys.exit(1)

    try:
        exe_path = build_for_yacht(
            yacht_id=args.yacht_id,
            upload=not args.no_upload,
        )

        print(f"\n{'='*60}")
        print(f"[OK] Build Complete!")
        print(f"{'='*60}")
        print(f"Installer: {exe_path}")
        print(f"\nNext steps:")
        print(f"  1. Transfer installer to the yacht's Windows PC")
        print(f"  2. Run CelesteOS-Setup-{args.yacht_id}.exe")
        print(f"  3. Owner enters 2FA code from email")
        print(f"  4. Select NAS folder, sync starts")

    except BuildError as e:
        print(f"\n[FAIL] Build failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nBuild interrupted")
        sys.exit(1)
