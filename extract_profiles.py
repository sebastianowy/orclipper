#!/usr/bin/env python3
"""Wyciąga spłaszczone profile (machine, process, filament) z pliku .3mf zapisanego w OrcaSlicerze."""

import json
import re
import sys
import zipfile
from pathlib import Path


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "profile"


def extract_from_3mf(threemf_path: Path, out_dir: Path) -> None:
    if not threemf_path.is_file():
        sys.exit(f"Błąd: nie znaleziono pliku {threemf_path}")

    with zipfile.ZipFile(threemf_path, "r") as zf:
        config_name = next(
            (n for n in zf.namelist() if n.endswith("Metadata/project_settings.config")),
            None,
        )
        if not config_name:
            sys.exit("Błąd: w pliku .3mf brakuje Metadata/project_settings.config (zapisz projekt w OrcaSlicerze).")
        settings = json.loads(zf.read(config_name).decode("utf-8"))

    settings.pop("inherits", None)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "process").mkdir(exist_ok=True)
    (out_dir / "filament").mkdir(exist_ok=True)

    machine_name = settings.get("printer_settings_id") or "Machine"
    machine_data = dict(settings)
    machine_data.update({"type": "machine", "name": machine_name, "from": "User"})
    machine_path = out_dir / "machine.json"
    machine_path.write_text(json.dumps(machine_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] Machine:  {machine_path} ({machine_name})")

    process_name = settings.get("print_settings_id") or "Process"
    process_data = dict(settings)
    process_data.update({"type": "process", "name": process_name, "from": "User"})
    process_path = out_dir / "process" / f"{slugify(process_name)}.json"
    process_path.write_text(json.dumps(process_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] Process:  {process_path} ({process_name})")

    fil_names = settings.get("filament_settings_id") or ["Filament"]
    if isinstance(fil_names, str):
        fil_names = [fil_names]

    n_fils = len(fil_names)
    seen = set()
    for idx, fil_name in enumerate(fil_names):
        if not fil_name or fil_name in seen:
            continue
        seen.add(fil_name)

        fil_data = {}
        for k, v in settings.items():
            if isinstance(v, list) and len(v) == n_fils and k != "filament_settings_id":
                fil_data[k] = [v[idx]]
            else:
                fil_data[k] = v

        fil_data.update({"type": "filament", "name": fil_name, "from": "User"})
        fil_data["filament_settings_id"] = [fil_name]
        fil_path = out_dir / "filament" / f"{slugify(fil_name)}.json"
        fil_path.write_text(json.dumps(fil_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[OK] Filament: {fil_path} ({fil_name})")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Użycie: python3 extract_profiles.py <projekt.3mf> [katalog_wyjsciowy]")
    threemf = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path("profiles")
    extract_from_3mf(threemf, out_dir)


if __name__ == "__main__":
    main()
