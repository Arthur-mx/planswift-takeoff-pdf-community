"""Install the community Python helpers for the current Windows user.
SPDX-License-Identifier: AGPL-3.0-only
"""
from pathlib import Path
import argparse
import os
import shutil
import struct
import subprocess
import sys

RUNTIME_FILES = ('pdf_takeoff_builder.py', 'pdf_export_picker.py', 'pdf_export_preview.py',
                 'pdf_preview_thumbnails.py', 'pdf_source_paths.py', 'pdf_source_labels.py',
                 'pdf_section_records.py')


def installation_root():
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        raise RuntimeError('LOCALAPPDATA is unavailable. Install from a normal Windows user account.')
    return Path(local) / 'PlanSwift' / 'Takeoff PDF Community'


def install(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if os.name != 'nt':
        raise RuntimeError('This installer is for Windows.')
    if not (3, 10) <= sys.version_info[:2] <= (3, 13) or struct.calcsize('P') != 8:
        raise RuntimeError('Use 64-bit Python 3.10 through 3.13; Python 3.13 is the tested version.')
    for name in RUNTIME_FILES + ('requirements.txt',):
        if not (source / name).is_file():
            raise RuntimeError('Extract the entire ZIP before installing. Missing: ' + name)
    import tkinter  # Check that the Python installation includes Tcl/Tk.
    target.mkdir(parents=True, exist_ok=True)
    runtime = target / 'runtime'
    python = runtime / 'Scripts' / 'python.exe'
    print('Installing to:', target, flush=True)
    subprocess.run([sys.executable, '-m', 'venv', str(runtime)], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--only-binary=:all:',
                    '-r', str(source / 'requirements.txt')], check=True)
    subprocess.run([str(python), '-c', 'import tkinter, fitz; print("PDF engine:", fitz.VersionBind)'], check=True)
    # Only overwrite the shipped application files, never saved preferences or jobs.
    if source != target:
        for name in RUNTIME_FILES + ('requirements.txt', 'VERSION.txt', 'LICENSE.txt', 'THIRD_PARTY_NOTICES.txt'):
            shutil.copy2(source / name, target / name)
        shutil.copytree(source / 'assets', target / 'assets', dirs_exist_ok=True)
    print('\nInstallation complete. Import the matching SwiftPluginPackage in PlanSwift.', flush=True)
    print('If building the plugin yourself, use the separate full PlanSwift Script TXT.', flush=True)
    return python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, help='Developer test directory; the PlanSwift script uses the default folder.')
    args = parser.parse_args()
    try:
        install(Path(__file__).parent, args.target or installation_root())
    except Exception as error:
        print('\nInstallation did not finish:', error, file=sys.stderr)
        print('Keep this message when requesting help. See INSTALL.txt.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
