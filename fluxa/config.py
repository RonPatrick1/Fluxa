from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LibraryConfig:
    name: str
    kind: str
    path: Path


@dataclass(frozen=True)
class FluxaConfig:
    config_path: Path
    host: str
    port: int
    data_dir: Path
    libraries: tuple[LibraryConfig, ...]
    scan_on_start: bool = False
    probe_on_start: bool = True

    @property
    def database_path(self) -> Path:
        return self.data_dir / "fluxa.db"


def load_config(path: str | Path = "fluxa.json") -> FluxaConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"Fluxa configuration not found: {config_path}. "
            "Copy config.example.json to fluxa.json first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {config_path}: {exc}") from exc

    host = str(raw.get("host", "127.0.0.1")).strip()
    port = int(raw.get("port", 8097))
    if not host:
        raise ValueError("host cannot be empty")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")

    data_value = Path(str(raw.get("data_dir", ".fluxa"))).expanduser()
    data_dir = (
        data_value.resolve()
        if data_value.is_absolute()
        else (config_path.parent / data_value).resolve()
    )

    libraries: list[LibraryConfig] = []
    seen_paths: set[Path] = set()
    for index, item in enumerate(raw.get("libraries", []), start=1):
        if not isinstance(item, dict):
            raise ValueError(f"library {index} must be an object")
        name = str(item.get("name", "")).strip()
        kind = str(item.get("kind", "")).strip().lower()
        root_value = str(item.get("path", "")).strip()
        if not name or not root_value:
            raise ValueError(f"library {index} needs a name and path")
        if kind not in {"video", "music"}:
            raise ValueError(f"library {name!r} kind must be video or music")
        root = Path(root_value).expanduser().resolve()
        if root in seen_paths:
            raise ValueError(f"library path is duplicated: {root}")
        seen_paths.add(root)
        libraries.append(LibraryConfig(name=name, kind=kind, path=root))

    if not libraries:
        raise ValueError("at least one library must be configured")

    return FluxaConfig(
        config_path=config_path,
        host=host,
        port=port,
        data_dir=data_dir,
        libraries=tuple(libraries),
        scan_on_start=bool(raw.get("scan_on_start", False)),
        probe_on_start=bool(raw.get("probe_on_start", True)),
    )
