"""Local OCR and calibrated root evidence; no hardware access.

The entry workflow separately checks the authorized profile before using a
strict root match. Generic BIOS classification alone never enables apply.
"""
import json
import os
from pathlib import Path
import re
import subprocess


def tweaker_root_match(ocr):
    """Match the calibrated root's fields, not just the persistent BIOS tabs."""
    text = re.sub(r'\s+', ' ', str(ocr.get('text', '')).casefold()).strip()
    features = {
        'advanced_mode': 'advanced mode' in text,
        'tweaker': 'tweaker' in text,
        'cpu_clock_control': 'cpu clock control' in text,
        'system_memory_multiplier': 'system memory multiplier' in text,
        'uclk': bool(re.search(r'\buclk\b', text)),
        'advanced_memory_settings': 'advanced memory settings' in text,
    }
    obstructed = any(x in text for x in ('are you sure', 'enter profile',
                                         'boot failure', 'profile saved', 'save profiles'))
    return {'matched': ocr.get('ok') is True and all(features.values()) and not obstructed,
            'features': features, 'obstructed': obstructed,
            'scope': 'Calibrated Advanced/Tweaker root; any selected row, macro reanchors using End'}


def classify_text(text):
    """Return failure, BIOS, windows, or unknown; black/empty is never failure."""
    if not isinstance(text, str) or not text.strip():
        return 'unknown'
    normal = re.sub(r'\s+', ' ', text.casefold()).strip()
    # Require explicit motherboard diagnostic wording, not a generic 'failed'
    # seen in a Windows test log or an instruction describing boot recovery.
    if (re.search(r'\bboot failure detected\b', normal)
            or re.search(r'\bthe system has experienced (?:a )?boot failure\b', normal)):
        return 'failure'
    if ('tweaker' in normal and any(x in normal for x in ('advanced mode', 'save & exit'))
            or 'aorus' in normal and any(x in normal for x in ('tweaker', 'advanced mode', 'save & exit'))
            or 'enter profile' in normal and 'name' in normal):
        return 'BIOS'
    if any(x in normal for x in ('windows sign-in', 'windows sign in', 'sign-in options',
                                 'type here to search', 'recycle bin')):
        return 'windows'
    return 'unknown'


def read_text(path, *, timeout_s=30, executable=None):
    """Return {ok,text,error,classification}; failure/unavailable -> unknown.

    Windows PowerShell's system WinRT bridge is used intentionally: PowerShell
    7/.NET does not consistently expose the desktop WinRT projection.
    """
    path = Path(path)
    if not path.is_file():
        return {'ok': False, 'text': '', 'error': 'Image file not found', 'classification': 'unknown'}
    if os.name != 'nt' and executable is None:
        return {'ok': False, 'text': '', 'error': 'Windows OCR unavailable on this OS', 'classification': 'unknown'}
    helper = Path(__file__).resolve().parents[1] / 'scripts' / 'read-image-text.ps1'
    command = [executable or 'powershell.exe', '-NoProfile', '-NonInteractive',
               '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-ImagePath', str(path.resolve())]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout_s, check=False)
        payload = json.loads(result.stdout.lstrip('\ufeff').strip())
        if result.returncode or payload.get('ok') is not True:
            return {'ok': False, 'text': '', 'error': payload.get('error') or 'OCR command failed',
                    'classification': 'unknown'}
        text = payload.get('text', '')
        if not isinstance(text, str):
            raise ValueError('Invalid OCR text')
        return {'ok': True, 'text': text, 'error': None, 'classification': classify_text(text)}
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return {'ok': False, 'text': '', 'error': type(error).__name__, 'classification': 'unknown'}
