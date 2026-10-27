from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    """Read settings without importing or executing another project's code."""
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if value.startswith(('"', "'")):
                quote = value[0]
                closing = value.find(quote, 1)
                if closing >= 0:
                    value = value[1:closing]
            else:
                value = value.split(" #", 1)[0].strip()
            values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    website_dir: Path
    launcher_dir: Path
    cookie_file: Path
    service_account_file: Path
    supabase_url: str
    supabase_key: str = field(repr=False)
    sheet_id: str = "1kXKg5EtlLCg0wgOELc1QEA89-Fzlki14kBiPziFOHjo"
    sheet_name: str = "Coverage SA"
    station_id: str = "4232"
    areas: tuple[str, ...] = ("KV1", "KV2", "KV3", "KV4", "KV5")
    workers: int = 8
    inventory_workers: int = 16
    batch_size: int = 200
    coverage_alias_file: Path | None = None
    delivery_workers: int = 48

    @classmethod
    def load(cls, root: Path = ROOT):
        values = read_env(root / ".env")

        def get(key, default=""):
            return os.environ.get(key, "").strip() or values.get(key, "").strip() or default

        def resolve(value):
            path = Path(value).expanduser()
            return (path if path.is_absolute() else root / path).resolve()

        website = resolve(get("WEBSITE_DIR", "../sdd-performance-project"))
        launcher = resolve(get("LAUNCHER_DIR", "../spx_py_laucher"))
        website_env = read_env(website / ".env")
        website_env.update(read_env(website / ".env.local"))
        areas = tuple(x.strip().upper() for x in get("OPERATING_AREAS", "KV1,KV2,KV3,KV4,KV5").split(","))
        if not areas or any(x not in {f"KV{i}" for i in range(1, 6)} for x in areas):
            raise ValueError("OPERATING_AREAS chỉ được chứa KV1 đến KV5.")
        url = get("SUPABASE_URL", website_env.get("NEXT_PUBLIC_SUPABASE_URL", "")).rstrip("/")
        if url and (urlparse(url).scheme != "https" or not urlparse(url).hostname):
            raise ValueError("SUPABASE_URL phải là địa chỉ HTTPS hợp lệ.")
        website_url = website_env.get("NEXT_PUBLIC_SUPABASE_URL", "").rstrip("/")
        if website_url and url != website_url:
            raise ValueError("Supabase đích khác cấu hình website. Kiểm tra WEBSITE_DIR/SUPABASE_URL để không ghi nhầm database.")
        return cls(
            website, launcher,
            resolve(get("SPX_COOKIE_FILE", "config/cookies/spx.json")),
            resolve(get("GOOGLE_SERVICE_ACCOUNT_FILE", str(launcher / "config/credentials/service_account_assign_pick.json"))),
            url, get("SUPABASE_SERVICE_ROLE_KEY", website_env.get("SUPABASE_SERVICE_ROLE_KEY", "")),
            get("ZONE_SHEET_ID", cls.sheet_id), get("ZONE_SHEET_NAME", "Coverage SA"),
            get("STATION_ID", "4232"), areas,
            max(1, min(32, int(get("FETCH_WORKERS", "16")))),
            max(1, min(24, int(get("INVENTORY_WORKERS", "16")))),
            max(1, min(1000, int(get("WRITE_BATCH_SIZE", "500")))),
            resolve(get("COVERAGE_ALIAS_FILE", "config/mappings/coverage-old-new.csv")),
            max(1, min(48, int(get("DELIVERY_WORKERS", "48")))),
        )
