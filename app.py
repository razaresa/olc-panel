#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import http.cookies
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path("/opt/olcrtc-admin")
DATA_DIR = Path("/var/lib/olcrtc-admin")
SUB_STATE_DIR = DATA_DIR / "subscriptions"
ETC_DIR = Path("/etc/olcrtc-admin")
OLCRTC_ETC_DIR = Path("/etc/olcrtc")
SUB_ETC_DIR = OLCRTC_ETC_DIR / "subscriptions"
JITSI_ETC_DIR = OLCRTC_ETC_DIR / "jitsi"
JITSI_STATE_DIR = DATA_DIR / "jitsi"
JITSI_SYSTEMD_DIR = Path("/etc/systemd/system")
DB_PATH = DATA_DIR / "subscriptions.db"
SERVER_ENV_PATH = OLCRTC_ETC_DIR / "server.env"
TOKEN_PATH = ETC_DIR / "admin.token"
ADMIN_URL_PATH = ETC_DIR / "admin.url"
HOST = "127.0.0.1"
PORT = 8790
LOCAL_TZ = ZoneInfo("Europe/Astrakhan")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
DURATION_OPTIONS = {
    "7": "7 дней",
    "30": "1 месяц",
    "90": "3 месяца",
    "365": "1 год",
}
DELIMITER_RE = re.compile(r"[\x00-\x1f\x7f://?@#%$<>]+")
TELEMOST_ROOM_RE = re.compile(r"(?:https?://telemost\.yandex\.ru/j/)?([0-9]{8,32})")
DEFAULT_CARRIER = "jitsi"
DEFAULT_TRANSPORT = "datachannel"
JITSI_ROOM_BASE_URL = "https://jitsi.etudevs.ru"
JITSI_ROOM_BASE_URLS = [
    item.strip().rstrip("/")
    for item in os.environ.get("OLCRTC_JITSI_ROOM_BASE_URLS", JITSI_ROOM_BASE_URL).split(",")
    if item.strip()
]
AUTO_ROOM_PREFIX = "olcrtc-auto"
JITSI_CONFIG_OWNER_REFERENCE = os.environ.get("OLCRTC_JITSI_CONFIG_OWNER_REFERENCE", "")
DEFAULT_JITSI_CONFIG_UID = 100
DEFAULT_JITSI_CONFIG_GID = 101


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def fmt_dt(value: str) -> str:
    return parse_dt(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def fmt_date(value: str) -> str:
    return parse_dt(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def days_left(value: str) -> str:
    delta = parse_dt(value) - utc_now()
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "истёк"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    if days > 0:
        return f"{days} дн. {hours} ч."
    return f"{max(hours, 1)} ч."


def setup_dirs() -> None:
    for directory in (DATA_DIR, SUB_STATE_DIR, JITSI_STATE_DIR, ETC_DIR, OLCRTC_ETC_DIR, SUB_ETC_DIR, JITSI_ETC_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.chmod(SUB_STATE_DIR, 0o700)
    os.chmod(JITSI_STATE_DIR, 0o700)
    os.chmod(ETC_DIR, 0o700)
    os.chmod(SUB_ETC_DIR, 0o700)
    os.chmod(JITSI_ETC_DIR, 0o700)


def get_token() -> str:
    setup_dirs()
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)
    ADMIN_URL_PATH.write_text(f"http://127.0.0.1:{PORT}/?token={token}\n", encoding="utf-8")
    os.chmod(ADMIN_URL_PATH, 0o600)
    return token


def db() -> sqlite3.Connection:
    setup_dirs()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            client_id TEXT NOT NULL,
            env_path TEXT NOT NULL,
            uri_path TEXT NOT NULL,
            carrier TEXT NOT NULL DEFAULT 'telemost',
            transport TEXT NOT NULL DEFAULT 'vp8channel'
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status_expires ON subscriptions(status, expires_at)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            room_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            assigned_subscription_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status, created_at)")
    return con


def run(cmd: list[str], *, check: bool = True, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, timeout=timeout, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["systemctl", *args], check=check, timeout=45)


def normalize_carrier(carrier: str | None) -> str:
    carrier = (carrier or DEFAULT_CARRIER).strip().lower()
    if carrier == "telemost":
        return "telemost"
    return DEFAULT_CARRIER


def unit_name(sub_id: str, carrier: str | None = None) -> str:
    validate_id(sub_id)
    if normalize_carrier(carrier) == "telemost":
        return f"olcrtc-sub@{sub_id}.service"
    return f"olcrtc-jitsi@{sub_id}.service"


def validate_id(sub_id: str) -> None:
    if not ID_RE.match(sub_id):
        raise ValueError("bad subscription id")


def service_status(sub_id: str, carrier: str | None = None) -> str:
    validate_id(sub_id)
    result = systemctl("is-active", unit_name(sub_id, carrier), check=False)
    return result.stdout.strip() or "unknown"


def legacy_env_path(sub_id: str) -> Path:
    validate_id(sub_id)
    return SUB_ETC_DIR / f"{sub_id}.env"


def legacy_uri_path(sub_id: str) -> Path:
    validate_id(sub_id)
    return SUB_ETC_DIR / f"{sub_id}.uri.txt"


def jitsi_env_path(sub_id: str) -> Path:
    validate_id(sub_id)
    return JITSI_ETC_DIR / f"{sub_id}.env"


def jitsi_yaml_path(sub_id: str) -> Path:
    validate_id(sub_id)
    return JITSI_ETC_DIR / f"{sub_id}.yaml"


def jitsi_uri_path(sub_id: str) -> Path:
    validate_id(sub_id)
    return JITSI_ETC_DIR / f"{sub_id}.uri"


def jitsi_override_path(sub_id: str) -> Path:
    validate_id(sub_id)
    return JITSI_SYSTEMD_DIR / f"olcrtc-jitsi@{sub_id}.service.d" / "override.conf"


def jitsi_endpoint_service_id(sub_id: str, index: int) -> str:
    validate_id(sub_id)
    if index <= 1:
        return sub_id
    suffix = f"-h{index}"
    endpoint_id = f"{sub_id[:63 - len(suffix)]}{suffix}"
    validate_id(endpoint_id)
    return endpoint_id


def jitsi_config_owner_reference() -> Path:
    if JITSI_CONFIG_OWNER_REFERENCE:
        return Path(JITSI_CONFIG_OWNER_REFERENCE)
    return JITSI_ETC_DIR / "reference.yaml"


def jitsi_config_owner_ids() -> tuple[int, int]:
    reference = jitsi_config_owner_reference()
    if reference.exists():
        stat = reference.stat()
        return stat.st_uid, stat.st_gid
    return DEFAULT_JITSI_CONFIG_UID, DEFAULT_JITSI_CONFIG_GID


def apply_jitsi_config_permissions(path: Path) -> None:
    uid, gid = jitsi_config_owner_ids()
    try:
        os.chown(path, uid, gid)
        os.chmod(path, 0o600)
    except (AttributeError, PermissionError, OSError):
        os.chmod(path, 0o644)


def env_path(sub_id: str, carrier: str | None = None) -> Path:
    if normalize_carrier(carrier) == "telemost":
        return legacy_env_path(sub_id)
    return jitsi_env_path(sub_id)


def uri_path(sub_id: str, carrier: str | None = None) -> Path:
    if normalize_carrier(carrier) == "telemost":
        return legacy_uri_path(sub_id)
    return jitsi_uri_path(sub_id)


def state_path(sub_id: str, carrier: str | None = None) -> Path:
    validate_id(sub_id)
    if normalize_carrier(carrier) == "telemost":
        return SUB_STATE_DIR / sub_id
    return JITSI_STATE_DIR / sub_id


def reload_systemd_for_carrier(carrier: str | None) -> None:
    if normalize_carrier(carrier) == DEFAULT_CARRIER:
        systemctl("daemon-reload")


def service_transport(carrier: str | None) -> str:
    if normalize_carrier(carrier) == "telemost":
        return "vp8channel"
    return DEFAULT_TRANSPORT


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def write_secret_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9а-яё]+", "-", value, flags=re.IGNORECASE)
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
        "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    value = "".join(translit.get(ch, ch) for ch in value)
    value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-_")
    if not value:
        value = "client"
    if not re.match(r"^[a-z0-9]", value):
        value = "c-" + value
    return value[:32].strip("-_") or "client"


def ascii_label(value: str) -> str:
    value = slugify(value)
    value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-_")
    return value[:48] or "olcrtc-subscription"


def clean_comment(value: str) -> str:
    value = DELIMITER_RE.sub("-", value)
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-_.")
    return value[:80] or "olcrtc-subscription"


def create_id(name: str) -> str:
    base = slugify(name)
    suffix = secrets.token_hex(3)
    sub_id = f"{base}-{suffix}"[:63].strip("-_")
    if len(sub_id) < 3:
        sub_id = f"sub-{suffix}"
    validate_id(sub_id)
    return sub_id


def parse_telemost_room_id(value: str) -> str:
    value = value.strip()
    match = TELEMOST_ROOM_RE.search(value)
    if not match:
        raise ValueError(f"bad Telemost room/link: {value[:80]}")
    return match.group(1)


def telemost_url(room_id: str) -> str:
    return f"https://telemost.yandex.ru/j/{room_id}"


def parse_jitsi_room_url(value: str) -> str:
    value = value.strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path in ("", "/") or parsed.fragment:
        raise ValueError(f"bad Jitsi room/link: {value[:120]}")
    if parsed.netloc.lower() == "telemost.yandex.ru":
        raise ValueError("нужна Jitsi room/link, не Telemost")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def is_telemost_room(value: str) -> bool:
    value = value.strip()
    return "telemost.yandex.ru" in value.lower() or bool(re.fullmatch(r"[0-9]{8,32}", value))


def room_matches_carrier(room_value: str, carrier: str | None) -> bool:
    carrier = normalize_carrier(carrier)
    if carrier == "telemost":
        return is_telemost_room(room_value)
    return not is_telemost_room(room_value)


def room_value_for_carrier(room: sqlite3.Row, carrier: str | None) -> str:
    if normalize_carrier(carrier) == "telemost":
        return room["room_id"]
    return room["url"]


def list_rooms(carrier: str | None = DEFAULT_CARRIER) -> list[sqlite3.Row]:
    with db() as con:
        rows = list(con.execute("SELECT * FROM rooms ORDER BY status = 'free' DESC, created_at DESC, room_id"))
    if carrier is None:
        return rows
    return [row for row in rows if room_matches_carrier(row["url"] or row["room_id"], carrier)]


def room_for_subscription(sub_id: str, carrier: str | None = None) -> str | None:
    validate_id(sub_id)
    with db() as con:
        row = con.execute("SELECT room_id, url FROM rooms WHERE assigned_subscription_id = ?", (sub_id,)).fetchone()
        return room_value_for_carrier(row, carrier) if row else None


def add_rooms(raw: str) -> tuple[int, int]:
    added = 0
    skipped = 0
    seen: set[str] = set()
    created = iso(utc_now())
    values = re.split(r"[\s,]+", raw.strip())
    with db() as con:
        for item in values:
            if not item:
                continue
            room_url = parse_jitsi_room_url(item)
            if room_url in seen:
                skipped += 1
                continue
            seen.add(room_url)
            cur = con.execute(
                "INSERT OR IGNORE INTO rooms(room_id, url, status, created_at) VALUES (?, ?, 'free', ?)",
                (room_url, room_url, created),
            )
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
    return added, skipped


def primary_jitsi_room_base_url() -> str:
    return JITSI_ROOM_BASE_URLS[0] if JITSI_ROOM_BASE_URLS else JITSI_ROOM_BASE_URL


def generated_room_url() -> str:
    return f"{primary_jitsi_room_base_url()}/{AUTO_ROOM_PREFIX}-{secrets.token_hex(16)}"


def jitsi_room_candidates(room_url: str) -> list[str]:
    room_url = parse_jitsi_room_url(room_url)
    parsed_room = urllib.parse.urlsplit(room_url)
    candidates = [room_url]
    for base_url in JITSI_ROOM_BASE_URLS:
        parsed_base = urllib.parse.urlsplit(base_url)
        if parsed_base.scheme not in ("http", "https") or not parsed_base.netloc:
            continue
        candidate = urllib.parse.urlunsplit(
            (
                parsed_base.scheme.lower(),
                parsed_base.netloc.lower(),
                parsed_room.path,
                parsed_room.query,
                "",
            )
        )
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def generate_rooms(count: int) -> tuple[int, int]:
    if count < 1 or count > 100:
        raise ValueError("room count must be 1..100")
    added = 0
    skipped = 0
    attempts = 0
    max_attempts = count * 20
    created = iso(utc_now())
    seen: set[str] = set()
    with db() as con:
        while added < count and attempts < max_attempts:
            attempts += 1
            room_url = generated_room_url()
            if room_url in seen:
                skipped += 1
                continue
            seen.add(room_url)
            cur = con.execute(
                "INSERT OR IGNORE INTO rooms(room_id, url, status, created_at) VALUES (?, ?, 'free', ?)",
                (room_url, room_url, created),
            )
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
    if added < count:
        raise RuntimeError(f"generated only {added} of {count} rooms")
    return added, skipped


def allocate_room(sub_id: str, carrier: str | None = DEFAULT_CARRIER) -> str:
    validate_id(sub_id)
    carrier = normalize_carrier(carrier)
    with db() as con:
        existing = con.execute("SELECT room_id, url FROM rooms WHERE assigned_subscription_id = ?", (sub_id,)).fetchone()
        if existing:
            return room_value_for_carrier(existing, carrier)
        rows = list(con.execute("SELECT room_id, url FROM rooms WHERE status = 'free' ORDER BY created_at, room_id"))
        row = next((item for item in rows if room_matches_carrier(item["url"] or item["room_id"], carrier)), None)
        if not row:
            if carrier == "telemost":
                raise RuntimeError("нет свободных Telemost-комнат; добавь комнаты в pool")
            raise RuntimeError("нет свободных Jitsi-комнат; добавь ссылки в pool")
        con.execute(
            "UPDATE rooms SET status = 'assigned', assigned_subscription_id = ? WHERE room_id = ?",
            (sub_id, row["room_id"]),
        )
        return room_value_for_carrier(row, carrier)


def release_room(sub_id: str) -> None:
    validate_id(sub_id)
    with db() as con:
        con.execute(
            "UPDATE rooms SET status = 'free', assigned_subscription_id = '' WHERE assigned_subscription_id = ?",
            (sub_id,),
        )


def sync_existing_rooms() -> None:
    created = iso(utc_now())
    with db() as con:
        rows = list(con.execute("SELECT id, env_path, carrier FROM subscriptions WHERE status = 'active'"))
        for row in rows:
            values = read_env_file(Path(row["env_path"]))
            room_id = values.get("OLCRTC_ROOM_ID", "").strip()
            if not room_id:
                continue
            carrier = normalize_carrier(row["carrier"])
            room_key = room_id if carrier == "telemost" else parse_jitsi_room_url(room_id)
            room_url = telemost_url(room_id) if carrier == "telemost" else room_key
            con.execute(
                "INSERT OR IGNORE INTO rooms(room_id, url, status, assigned_subscription_id, created_at) VALUES (?, ?, 'assigned', ?, ?)",
                (room_key, room_url, row["id"], created),
            )
            con.execute(
                "UPDATE rooms SET status = 'assigned', assigned_subscription_id = ? WHERE room_id = ? AND assigned_subscription_id = ''",
                (row["id"], room_key),
            )

def default_room_id() -> str:
    values = read_env_file(SERVER_ENV_PATH)
    room_id = values.get("OLCRTC_ROOM_ID", "").strip()
    if not room_id:
        raise RuntimeError("server room is not configured in /etc/olcrtc/server.env")
    return room_id


def vp8_env_lines(env_values: dict[str, str]) -> list[str]:
    fps = env_values.get("OLCRTC_VP8_FPS", "60")
    batch = env_values.get("OLCRTC_VP8_BATCH", "64")
    return [f"OLCRTC_VP8_FPS={fps}", f"OLCRTC_VP8_BATCH={batch}"]


def transport_uri_options(transport: str, env_values: dict[str, str]) -> str:
    if transport != "vp8channel":
        return ""
    fps = env_values.get("OLCRTC_VP8_FPS", "60")
    batch = env_values.get("OLCRTC_VP8_BATCH", "64")
    return f"<vp8-fps={fps}&vp8-batch={batch}>"


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def yaml_bool(value: str) -> str:
    return "true" if value.strip().lower() in {"1", "true", "yes", "on"} else "false"


def jitsi_server_yaml(room_url: str, key: str, dns: str, debug: str) -> str:
    return f"""mode: srv
link: direct
auth:
  provider: jitsi
room:
  id: {yaml_scalar(room_url)}
crypto:
  key: {key}
net:
  transport: datachannel
  dns: {yaml_scalar(dns)}
liveness:
  interval: 30s
  timeout: 15s
  failures: 6
data: /usr/share/olcrtc
debug: {yaml_bool(debug)}
"""


def jitsi_env_lines(room_url: str, room_urls: list[str], client_id: str, key: str, dns: str, debug: str) -> list[str]:
    return [
        "OLCRTC_MODE=srv",
        "OLCRTC_AUTH=jitsi",
        "OLCRTC_CARRIER=jitsi",
        "OLCRTC_TRANSPORT=datachannel",
        f"OLCRTC_ROOM_ID={room_url}",
        f"OLCRTC_ROOM_IDS={','.join(room_urls)}",
        f"OLCRTC_CLIENT_ID={client_id}",
        f"OLCRTC_KEY={key}",
        f"OLCRTC_DNS={dns}",
        f"OLCRTC_DEBUG={debug}",
        "",
    ]


def write_jitsi_override(sub_id: str) -> None:
    content = """[Service]
ExecStart=
ExecStart=/usr/bin/podman run --name olcrtc-jitsi-%i --rm --network host -v /var/lib/olcrtc-admin/jitsi/%i:/var/lib/olcrtc:Z -v /etc/olcrtc/jitsi/%i.yaml:/etc/olcrtc/config.yaml:ro,Z --env-file /etc/olcrtc/jitsi/%i.env olcrtc/server:universal-carrier /etc/olcrtc/config.yaml
"""
    write_secret_file(jitsi_override_path(sub_id), content)


def write_jitsi_subscription_files(
    sub_id: str, name: str, expires_at: str, *, room_id: str | None = None, key: str | None = None
) -> str:
    validate_id(sub_id)
    env_values = read_env_file(jitsi_env_path(sub_id))
    server_values = read_env_file(SERVER_ENV_PATH)
    room_url = room_id or env_values.get("OLCRTC_ROOM_ID") or server_values.get("OLCRTC_ROOM_ID")
    if not room_url:
        raise RuntimeError("нет Jitsi room URL")
    room_url = parse_jitsi_room_url(room_url)
    room_urls = jitsi_room_candidates(room_url)
    client_room_value = ",".join(room_urls)
    key = key or env_values.get("OLCRTC_KEY") or secrets.token_hex(32)
    dns = env_values.get("OLCRTC_DNS") or server_values.get("OLCRTC_DNS") or "1.1.1.1:53"
    debug = env_values.get("OLCRTC_DEBUG") or server_values.get("OLCRTC_DEBUG") or "true"
    client_id = sub_id
    for index, candidate in enumerate(room_urls, start=1):
        service_id = jitsi_endpoint_service_id(sub_id, index)
        lines = jitsi_env_lines(candidate, room_urls, client_id, key, dns, debug)
        yaml = jitsi_server_yaml(candidate, key, dns, debug)
        write_secret_file(jitsi_env_path(service_id), "\n".join(lines))
        write_secret_file(jitsi_yaml_path(service_id), yaml)
        apply_jitsi_config_permissions(jitsi_yaml_path(service_id))
        write_jitsi_override(service_id)
        state_path(service_id, DEFAULT_CARRIER).mkdir(parents=True, exist_ok=True)
        os.chmod(state_path(service_id, DEFAULT_CARRIER), 0o700)
    comment = clean_comment(f"{ascii_label(name)}-until-{fmt_date(expires_at)}")
    uri = f"olcrtc://jitsi?datachannel@{client_room_value}#{key}${comment}\n"
    write_secret_file(jitsi_uri_path(sub_id), uri)
    return uri.strip()


def write_telemost_subscription_files(
    sub_id: str, name: str, expires_at: str, *, room_id: str | None = None, key: str | None = None
) -> str:
    validate_id(sub_id)
    env_values = read_env_file(legacy_env_path(sub_id))
    server_values = read_env_file(SERVER_ENV_PATH)
    carrier = "telemost"
    transport = env_values.get("OLCRTC_TRANSPORT") or server_values.get("OLCRTC_TRANSPORT") or "vp8channel"
    room_id = room_id or env_values.get("OLCRTC_ROOM_ID") or server_values.get("OLCRTC_ROOM_ID") or default_room_id()
    key = key or env_values.get("OLCRTC_KEY") or secrets.token_hex(32)
    client_id = sub_id
    lines = [
        "OLCRTC_MODE=srv",
        f"OLCRTC_AUTH={carrier}",
        f"OLCRTC_CARRIER={carrier}",
        f"OLCRTC_TRANSPORT={transport}",
        f"OLCRTC_ROOM_ID={room_id}",
        f"OLCRTC_CLIENT_ID={client_id}",
        f"OLCRTC_KEY={key}",
        f"OLCRTC_DNS={env_values.get('OLCRTC_DNS') or server_values.get('OLCRTC_DNS') or '1.1.1.1:53'}",
    ]
    if transport == "vp8channel":
        merged = {**server_values, **env_values}
        lines.extend(vp8_env_lines(merged))
    lines.extend([f"OLCRTC_DEBUG={env_values.get('OLCRTC_DEBUG') or server_values.get('OLCRTC_DEBUG') or 'false'}", ""])
    write_secret_file(legacy_env_path(sub_id), "\n".join(lines))
    comment = clean_comment(f"{ascii_label(name)}-until-{fmt_date(expires_at)}")
    uri_options = transport_uri_options(transport, {**server_values, **env_values})
    uri = f"olcrtc://{carrier}?{transport}{uri_options}@{room_id}#{key}%{client_id}${comment}\n"
    write_secret_file(legacy_uri_path(sub_id), uri)
    state_path(sub_id, carrier).mkdir(parents=True, exist_ok=True)
    os.chmod(state_path(sub_id, carrier), 0o700)
    return uri.strip()


def write_subscription_files(
    sub_id: str,
    name: str,
    expires_at: str,
    *,
    room_id: str | None = None,
    key: str | None = None,
    carrier: str | None = DEFAULT_CARRIER,
) -> str:
    if normalize_carrier(carrier) == "telemost":
        return write_telemost_subscription_files(sub_id, name, expires_at, room_id=room_id, key=key)
    return write_jitsi_subscription_files(sub_id, name, expires_at, room_id=room_id, key=key)


def jitsi_endpoint_service_ids(sub_id: str) -> list[str]:
    values = read_env_file(jitsi_env_path(sub_id))
    rooms = [item.strip() for item in values.get("OLCRTC_ROOM_IDS", values.get("OLCRTC_ROOM_ID", "")).split(",") if item.strip()]
    if not rooms:
        return [sub_id]
    return [jitsi_endpoint_service_id(sub_id, index) for index in range(1, len(rooms) + 1)]


def enable_jitsi_endpoint_services(sub_id: str) -> None:
    for service_id in jitsi_endpoint_service_ids(sub_id):
        systemctl("enable", "--now", unit_name(service_id, DEFAULT_CARRIER))


def disable_jitsi_endpoint_services(sub_id: str, *, check: bool = False) -> None:
    for service_id in reversed(jitsi_endpoint_service_ids(sub_id)):
        systemctl("disable", "--now", unit_name(service_id, DEFAULT_CARRIER), check=check)


def restart_jitsi_endpoint_services(sub_id: str) -> None:
    for service_id in jitsi_endpoint_service_ids(sub_id):
        systemctl("restart", unit_name(service_id, DEFAULT_CARRIER))


def create_subscription(name: str, note: str, days: int) -> str:
    name = name.strip()
    note = note.strip()
    if not name:
        raise ValueError("name is required")
    if days < 1 or days > 3660:
        raise ValueError("duration must be 1..3660 days")
    sub_id = create_id(name)
    carrier = DEFAULT_CARRIER
    transport = DEFAULT_TRANSPORT
    room_id = allocate_room(sub_id, carrier)
    created_at = iso(utc_now())
    expires_at = iso(utc_now() + timedelta(days=days))
    try:
        write_subscription_files(sub_id, name, expires_at, room_id=room_id, carrier=carrier)
        with db() as con:
            con.execute(
                """
                INSERT INTO subscriptions(id, name, note, created_at, expires_at, status, client_id, env_path, uri_path, carrier, transport)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    sub_id,
                    name[:120],
                    note[:500],
                    created_at,
                    expires_at,
                    sub_id,
                    str(env_path(sub_id, carrier)),
                    str(uri_path(sub_id, carrier)),
                    carrier,
                    transport,
                ),
            )
        reload_systemd_for_carrier(carrier)
        if carrier == DEFAULT_CARRIER:
            enable_jitsi_endpoint_services(sub_id)
        else:
            systemctl("enable", "--now", unit_name(sub_id, carrier))
        return sub_id
    except Exception:
        release_room(sub_id)
        raise


def get_subscription(sub_id: str) -> sqlite3.Row | None:
    validate_id(sub_id)
    with db() as con:
        return con.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()


def list_subscriptions() -> list[sqlite3.Row]:
    with db() as con:
        return list(con.execute("SELECT * FROM subscriptions ORDER BY status = 'active' DESC, expires_at DESC, created_at DESC"))


def revoke_subscription(sub_id: str, status: str = "revoked") -> None:
    validate_id(sub_id)
    row = get_subscription(sub_id)
    carrier = row["carrier"] if row else DEFAULT_CARRIER
    if normalize_carrier(carrier) == DEFAULT_CARRIER:
        disable_jitsi_endpoint_services(sub_id, check=False)
    else:
        systemctl("disable", "--now", unit_name(sub_id, carrier), check=False)
    release_room(sub_id)
    with db() as con:
        con.execute("UPDATE subscriptions SET status = ? WHERE id = ?", (status, sub_id))


def restore_subscription(sub_id: str) -> None:
    row = get_subscription(sub_id)
    if not row:
        raise ValueError("subscription not found")
    if parse_dt(row["expires_at"]) <= utc_now():
        raise ValueError("subscription is expired; extend it first")
    carrier = normalize_carrier(row["carrier"])
    transport = service_transport(carrier)
    room_id = allocate_room(sub_id, carrier)
    write_subscription_files(sub_id, row["name"], row["expires_at"], room_id=room_id, carrier=carrier)
    reload_systemd_for_carrier(carrier)
    if carrier == DEFAULT_CARRIER:
        enable_jitsi_endpoint_services(sub_id)
    else:
        systemctl("enable", "--now", unit_name(sub_id, carrier))
    with db() as con:
        con.execute(
            "UPDATE subscriptions SET status = 'active', carrier = ?, transport = ?, env_path = ?, uri_path = ? WHERE id = ?",
            (carrier, transport, str(env_path(sub_id, carrier)), str(uri_path(sub_id, carrier)), sub_id),
        )


def restart_subscription(sub_id: str) -> None:
    row = get_subscription(sub_id)
    if not row:
        raise ValueError("subscription not found")
    if row["status"] != "active":
        raise ValueError("only active subscriptions can be restarted")
    if normalize_carrier(row["carrier"]) == DEFAULT_CARRIER:
        restart_jitsi_endpoint_services(sub_id)
    else:
        systemctl("restart", unit_name(sub_id, row["carrier"]))


def extend_subscription(sub_id: str, days: int) -> None:
    row = get_subscription(sub_id)
    if not row:
        raise ValueError("subscription not found")
    if days < 1 or days > 3660:
        raise ValueError("duration must be 1..3660 days")
    base = max(parse_dt(row["expires_at"]), utc_now())
    expires_at = iso(base + timedelta(days=days))
    carrier = normalize_carrier(row["carrier"])
    transport = service_transport(carrier)
    room_id = room_for_subscription(sub_id, carrier)
    if row["status"] != "active" or not room_id:
        room_id = allocate_room(sub_id, carrier)
    write_subscription_files(sub_id, row["name"], expires_at, room_id=room_id, carrier=carrier)
    with db() as con:
        con.execute(
            "UPDATE subscriptions SET expires_at = ?, status = 'active', carrier = ?, transport = ?, env_path = ?, uri_path = ? WHERE id = ?",
            (expires_at, carrier, transport, str(env_path(sub_id, carrier)), str(uri_path(sub_id, carrier)), sub_id),
        )
    reload_systemd_for_carrier(carrier)
    if carrier == DEFAULT_CARRIER:
        enable_jitsi_endpoint_services(sub_id)
    else:
        systemctl("enable", "--now", unit_name(sub_id, carrier))


def expire_subscriptions() -> int:
    expired = 0
    with db() as con:
        rows = list(con.execute("SELECT id FROM subscriptions WHERE status = 'active' AND expires_at <= ?", (iso(utc_now()),)))
    for row in rows:
        revoke_subscription(row["id"], status="expired")
        expired += 1
    return expired


def shell_escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_page(title: str, body: str, *, token: str, message: str = "") -> bytes:
    msg_html = f'<div class="toast">{html.escape(message)}</div>' if message else ""
    content = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · OlcRTC</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0c0f0d;
  --panel: rgba(246, 238, 214, .08);
  --panel-strong: rgba(246, 238, 214, .14);
  --text: #f6eed6;
  --muted: #a9a18d;
  --line: rgba(246, 238, 214, .16);
  --ok: #8cffb1;
  --warn: #ffd166;
  --bad: #ff6b6b;
  --accent: #d6ff47;
  --ink: #11140f;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  background:
    radial-gradient(circle at 18% 8%, rgba(214,255,71,.14), transparent 28rem),
    radial-gradient(circle at 82% 18%, rgba(140,255,177,.08), transparent 26rem),
    linear-gradient(135deg, #070807 0%, #101611 48%, #090b0a 100%);
  color: var(--text);
  font: 14px/1.42 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
a {{ color: var(--accent); text-decoration: none; }}
code {{ color: var(--muted); }}
.wrap {{ width: min(1380px, calc(100vw - 28px)); margin: 0 auto; padding: 16px 0 34px; }}
.card, .brand {{ border: 1px solid var(--line); background: var(--panel); border-radius: 20px; box-shadow: 0 18px 60px rgba(0,0,0,.28); backdrop-filter: blur(16px); }}
.card {{ padding: 14px; }}
.brand {{ padding: 16px 18px; position: relative; overflow: hidden; }}
.brand:after {{ content: ""; position: absolute; inset: auto -10% -70% 46%; height: 120px; background: var(--accent); filter: blur(56px); opacity: .14; transform: rotate(-8deg); }}
h1 {{ font-size: clamp(24px, 3.2vw, 42px); line-height: 1; margin: 0 0 8px; letter-spacing: -0.06em; }}
h2 {{ margin: 0; font-size: 16px; letter-spacing: -0.03em; }}
.subtitle {{ color: var(--muted); max-width: 720px; font-size: 13px; }}
.topbar {{ display: grid; grid-template-columns: minmax(320px, 1fr) minmax(420px, .95fr); gap: 12px; align-items: stretch; margin-bottom: 12px; }}
.grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0 0 12px; }}
.stat {{ border: 1px solid var(--line); border-radius: 15px; padding: 10px 12px; background: rgba(0,0,0,.16); }}
.stat b {{ display:block; font-size: 21px; line-height: 1; }}
.stat span {{ color: var(--muted); font-size: 11px; }}
.form-row {{ display: grid; grid-template-columns: 1.1fr .55fr .45fr; gap: 8px; }}
.room-form {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: start; }}
input, select, textarea {{
  width: 100%; border: 1px solid var(--line); border-radius: 12px; background: rgba(0,0,0,.28); color: var(--text);
  padding: 10px 11px; font: inherit; outline: none;
}}
textarea {{ min-height: 68px; resize: vertical; }}
input:focus, select:focus, textarea:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(214,255,71,.12); }}
button, .button {{
  appearance: none; border: 0; border-radius: 999px; padding: 9px 13px; background: var(--accent); color: var(--ink);
  font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 7px; white-space: nowrap;
}}
button.secondary, .button.secondary {{ background: var(--panel-strong); color: var(--text); border: 1px solid var(--line); }}
button.danger {{ background: rgba(255,107,107,.15); color: #ffd6d6; border: 1px solid rgba(255,107,107,.35); }}
.stack {{ display: grid; gap: 10px; }}
.title-row {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
.sub-list {{ display: grid; gap: 7px; }}
.sub {{ display: grid; grid-template-columns: minmax(240px, 1fr) auto auto; gap: 9px; align-items: center; border: 1px solid var(--line); border-radius: 16px; padding: 9px 10px; background: rgba(0,0,0,.18); }}
.sub-name {{ display: inline-block; color: var(--text); font-size: 15px; font-weight: 800; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }}
.sub-name:hover {{ color: var(--accent); }}
.meta {{ color: var(--muted); font-size: 11px; display: flex; flex-wrap: wrap; gap: 6px; }}
.pill {{ display: inline-flex; align-items:center; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; background: rgba(0,0,0,.18); max-width: 100%; }}
.pill.ok {{ color: var(--ok); }} .pill.warn {{ color: var(--warn); }} .pill.bad {{ color: var(--bad); }}
.actions {{ display:flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; align-items: center; }}
.quick-extend {{ display: grid; grid-template-columns: 112px auto; gap: 6px; align-items: center; }}
.detail-card {{ padding: 12px 14px; margin-bottom: 10px; }}
.detail-head {{ display: grid; grid-template-columns: minmax(260px, 1fr) auto; gap: 12px; align-items: center; }}
.detail-title {{ min-width: 0; }}
.detail-title h1 {{ font-size: clamp(22px, 2.6vw, 34px); margin: 4px 0 7px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.uri-box {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; }}
.uri {{ font-size: 12px; word-break: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.uri-input {{ height: 42px; font-size: 12px; }}
details {{ color: var(--muted); font-size: 11px; }}
summary {{ cursor: pointer; margin-top: 8px; }}
.toast {{ margin-bottom: 10px; border: 1px solid rgba(214,255,71,.35); color: var(--accent); background: rgba(214,255,71,.08); padding: 10px 12px; border-radius: 14px; }}
.footer {{ margin-top: 12px; color: var(--muted); font-size: 11px; }}
@media (max-width: 980px) {{
  .topbar, .detail-head, .sub {{ grid-template-columns: 1fr; }}
  .form-row, .room-form, .uri-box {{ grid-template-columns: 1fr; }}
  .grid {{ grid-template-columns: repeat(2, 1fr); }}
  .actions {{ justify-content:flex-start; }}
  .quick-extend {{ grid-template-columns: 1fr auto; }}
}}

</style>
</head>
<body>
<div class="wrap">
{msg_html}
{body}
<div class="footer">Панель слушает только <code>127.0.0.1:{PORT}</code>. Новые Jitsi URI хранятся на сервере в <code>/etc/olcrtc/jitsi</code>.</div>
</div>
<script>
async function copyText(id) {{
  const el = document.getElementById(id);
  if (!el) return;
  await navigator.clipboard.writeText(el.value || el.textContent);
  const old = document.title;
  document.title = 'Скопировано';
  setTimeout(() => document.title = old, 900);
}}
</script>
</body>
</html>"""
    return content.encode("utf-8")


def status_class(status: str, service: str) -> str:
    if status == "active" and service == "active":
        return "ok"
    if status == "expired":
        return "warn"
    return "bad"


def dashboard(token: str, message: str = "") -> bytes:
    rows = list_subscriptions()
    rooms = list_rooms(DEFAULT_CARRIER)
    free_rooms = sum(1 for room in rooms if room["status"] == "free")
    assigned_rooms = sum(1 for room in rooms if room["status"] == "assigned")
    active = sum(1 for row in rows if row["status"] == "active")
    expired = sum(1 for row in rows if row["status"] == "expired")
    revoked = sum(1 for row in rows if row["status"] == "revoked")
    expiring = sum(1 for row in rows if row["status"] == "active" and parse_dt(row["expires_at"]) <= utc_now() + timedelta(days=7))
    cards = []
    room_cards = []
    token_html = html.escape(token)
    for room in rooms:
        cls = "ok" if room["status"] == "free" else "warn"
        assigned = room["assigned_subscription_id"] or "свободна"
        room_cards.append(f"""
        <article class="sub">
          <div class="sub-main">
            <a class="sub-name" href="{html.escape(room['url'])}" target="_blank">{html.escape(room['room_id'])}</a>
            <div class="meta"><span class="pill {cls}">{html.escape(room['status'])}</span><span class="pill">{html.escape(assigned)}</span></div>
          </div>
        </article>
        """)
    for row in rows:
        svc = service_status(row["id"], row["carrier"])
        cls = status_class(row["status"], svc)
        row_id_raw = row["id"]
        row_id = html.escape(row_id_raw)
        name_html = html.escape(row["name"])
        note = f'<span class="pill">{html.escape(row["note"])}</span>' if row["note"] else ""
        assigned_room = room_for_subscription(row["id"], row["carrier"]) or read_env_file(Path(row["env_path"])).get("OLCRTC_ROOM_ID", "")
        room_note = f'<span class="pill">room: {html.escape(assigned_room)}</span>' if assigned_room else ""
        carrier_note = f'<span class="pill">{html.escape(row["carrier"])} · {html.escape(row["transport"])}</span>'
        detail_url = f"/subscription/{urllib.parse.quote(row_id_raw)}"
        if row["status"] == "active":
            state_actions = (
                f'<form method="post" action="/restart"><input type="hidden" name="token" value="{token_html}"><input type="hidden" name="id" value="{row_id}"><button class="secondary">рестарт</button></form>'
                f'<form method="post" action="/revoke" onsubmit="return confirm(\'Отключить подписку?\')"><input type="hidden" name="token" value="{token_html}"><input type="hidden" name="id" value="{row_id}"><button class="danger">откл.</button></form>'
            )
        else:
            state_actions = f'<form method="post" action="/restore"><input type="hidden" name="token" value="{token_html}"><input type="hidden" name="id" value="{row_id}"><button class="secondary">вкл.</button></form>'
        cards.append(f"""
        <article class="sub">
          <div class="sub-main">
            <a class="sub-name" href="{detail_url}" title="{name_html}">{name_html}</a>
            <div class="meta">
              <span class="pill {cls}">{html.escape(row['status'])} · {html.escape(svc)}</span>
              <span class="pill">1 устройство</span>
              <span class="pill">до {html.escape(fmt_dt(row['expires_at']))}</span>
              <span class="pill">{html.escape(days_left(row['expires_at']))}</span>
              {carrier_note}
              {room_note}
              {note}
            </div>
          </div>
          <form method="post" action="/extend" class="quick-extend">
            <input type="hidden" name="token" value="{token_html}">
            <input type="hidden" name="id" value="{row_id}">
            <select name="days" aria-label="Продлить">
              <option value="30">+1 мес.</option><option value="365">+1 год</option><option value="7">+7 дн.</option>
            </select>
            <button class="secondary">+</button>
          </form>
          <div class="actions">
            <a class="button secondary" href="{detail_url}">URI</a>
            {state_actions}
          </div>
        </article>
        """)
    if not cards:
        cards.append('<div class="sub"><div><a class="sub-name" href="#">Подписок пока нет</a><div class="meta"><span class="pill">Создай первую — панель сама сгенерирует room/key и запустит контейнер.</span></div></div></div>')
    if not room_cards:
        room_cards.append('<div class="sub"><div><a class="sub-name" href="#">Комнат пока нет</a><div class="meta"><span class="pill">Вставь ссылки Jitsi/meet в поле выше.</span></div></div></div>')
    body = f"""
    <div class="topbar">
      <section class="brand">
        <h1>OlcRTC</h1>
        <div class="subtitle">Компактная выдача Jitsi/datachannel подписок: один человек — один URI, свой срок и отдельный контейнер.</div>
      </section>
      <section class="card">
        <div class="title-row"><h2>Новая подписка</h2></div>
        <form method="post" action="/create" class="stack">
          <input type="hidden" name="token" value="{token_html}">
          <div class="form-row">
            <input name="name" placeholder="Профиль: Иван / телефон / клиент" maxlength="120" required>
            <select name="days">
              {''.join(f'<option value="{k}">{v}</option>' for k, v in DURATION_OPTIONS.items())}
            </select>
            <button>создать</button>
          </div>
          <input name="note" placeholder="Заметка: Telegram, оплата, устройство" maxlength="500">
        </form>
      </section>
    </div>
    <div class="grid">
      <div class="stat"><b>{active}</b><span>активных</span></div>
      <div class="stat"><b>{expiring}</b><span>истекают за 7 дней</span></div>
      <div class="stat"><b>{expired}</b><span>истекли</span></div>
      <div class="stat"><b>{revoked}</b><span>отключены</span></div>
    </div>
    <section class="card"><div class="title-row"><h2>Подписки</h2><span class="pill">всего: {len(rows)}</span></div><div class="sub-list">{''.join(cards)}</div></section>
    <section class="card stack" style="margin-top:12px">
      <div class="title-row"><h2>Jitsi комнаты</h2><div class="meta"><span class="pill ok">free: {free_rooms}</span><span class="pill warn">assigned: {assigned_rooms}</span></div></div>
      <form method="post" action="/rooms/add" class="room-form">
        <input type="hidden" name="token" value="{token_html}">
        <textarea name="rooms" placeholder="https://jitsi.etudevs.ru/olcrtc-client-one&#10;https://jitsi.etudevs.ru/olcrtc-client-two"></textarea>
        <button>добавить</button>
      </form>
      <form method="post" action="/rooms/generate" class="room-form">
        <input type="hidden" name="token" value="{token_html}">
        <select name="count" aria-label="Количество комнат">
          <option value="10">10 комнат</option>
          <option value="5">5 комнат</option>
          <option value="20">20 комнат</option>
          <option value="50">50 комнат</option>
        </select>
        <button>сгенерировать</button>
      </form>
      <div class="sub-list">{''.join(room_cards)}</div>
    </section>
    """
    return render_page("Подписки", body, token=token, message=message)

def detail_page(sub_id: str, token: str, message: str = "") -> bytes:
    row = get_subscription(sub_id)
    if not row:
        raise KeyError("subscription not found")
    uri = Path(row["uri_path"]).read_text(encoding="utf-8").strip() if Path(row["uri_path"]).exists() else "URI file missing"
    svc = service_status(row["id"], row["carrier"])
    token_html = html.escape(token)
    row_id = html.escape(row["id"])
    name_html = html.escape(row["name"])
    uri_attr = html.escape(uri, quote=True)
    body = f"""
    <section class="card detail-card">
      <div class="detail-head">
        <div class="detail-title">
          <a class="button secondary" href="/">← список</a>
          <h1 title="{name_html}">{name_html}</h1>
          <div class="meta">
            <span class="pill {status_class(row['status'], svc)}">{html.escape(row['status'])} · {html.escape(svc)}</span>
            <span class="pill">1 устройство</span>
            <span class="pill">до {html.escape(fmt_dt(row['expires_at']))}</span>
            <span class="pill">осталось: {html.escape(days_left(row['expires_at']))}</span>
            <span class="pill">client-id: {html.escape(row['client_id'])}</span>
            <span class="pill">{html.escape(row['carrier'])} · {html.escape(row['transport'])}</span>
          </div>
        </div>
        <div class="actions">
          <form method="post" action="/extend" class="quick-extend">
            <input type="hidden" name="token" value="{token_html}">
            <input type="hidden" name="id" value="{row_id}">
            <select name="days"><option value="30">+1 мес.</option><option value="365">+1 год</option><option value="7">+7 дн.</option></select>
            <button>продлить</button>
          </form>
          <form method="post" action="/revoke" onsubmit="return confirm('Отключить подписку?')"><input type="hidden" name="token" value="{token_html}"><input type="hidden" name="id" value="{row_id}"><button class="danger">отключить</button></form>
        </div>
      </div>
    </section>
    <section class="card stack">
      <div class="title-row"><h2>URI для клиента</h2><button onclick="copyText('uri')">скопировать</button></div>
      <div class="uri-box"><input id="uri" class="uri uri-input" readonly value="{uri_attr}"><button class="secondary" onclick="copyText('uri')">copy</button></div>
      <details><summary>технические детали</summary><div class="meta"><span class="pill">env: {html.escape(row['env_path'])}</span><span class="pill">uri: {html.escape(row['uri_path'])}</span></div></details>
    </section>
    """
    return render_page(row["name"], body, token=token, message=message)

class Handler(BaseHTTPRequestHandler):
    server_version = "olcrtc-admin/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def token(self) -> str:
        return get_token()

    def cookie_token(self) -> str:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("olcrtc_admin")
        return morsel.value if morsel else ""

    def query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def authorized(self, fields: dict[str, list[str]] | None = None) -> bool:
        token = self.token()
        query_token = self.query().get("token", [""])[0]
        form_token = fields.get("token", [""])[0] if fields else ""
        return token in (self.cookie_token(), query_token, form_token)

    def send_bytes(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8", set_cookie: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        if set_cookie:
            self.send_header("Set-Cookie", f"olcrtc_admin={self.token()}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        body = b"redirect"
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def forbidden(self) -> None:
        body = b"Forbidden. Open /etc/olcrtc-admin/admin.url through SSH tunnel."
        self.send_bytes(body, status=403, content_type="text/plain; charset=utf-8")

    def not_found(self) -> None:
        self.send_bytes(b"Not found", status=404, content_type="text/plain; charset=utf-8")

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self.send_bytes(b"ok\n", content_type="text/plain; charset=utf-8")
            return
        set_cookie = bool(self.query().get("token", [""])[0] == self.token())
        if not self.authorized():
            self.forbidden()
            return
        message = self.query().get("msg", [""])[0]
        try:
            if path == "/":
                self.send_bytes(dashboard(self.token(), message=message), set_cookie=set_cookie)
                return
            match = re.fullmatch(r"/subscription/([a-z0-9][a-z0-9_-]{2,63})", path)
            if match:
                self.send_bytes(detail_page(match.group(1), self.token(), message=message), set_cookie=set_cookie)
                return
        except Exception as exc:
            self.send_bytes(render_page("Ошибка", f"<section class='card'><h1>Ошибка</h1><p>{html.escape(str(exc))}</p><p><a href='/'>назад</a></p></section>", token=self.token()), status=500, set_cookie=set_cookie)
            return
        self.not_found()

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            raise ValueError("request too large")
        raw = self.rfile.read(length).decode("utf-8")
        return urllib.parse.parse_qs(raw)

    def form_value(self, fields: dict[str, list[str]], name: str, default: str = "") -> str:
        return fields.get(name, [default])[0]

    def do_POST(self) -> None:
        try:
            fields = self.read_form()
            if not self.authorized(fields):
                self.forbidden()
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/rooms/add":
                added, skipped = add_rooms(self.form_value(fields, "rooms"))
                self.redirect("/?msg=" + urllib.parse.quote(f"Комнат добавлено: {added}, пропущено: {skipped}"))
                return
            if path == "/rooms/generate":
                added, skipped = generate_rooms(int(self.form_value(fields, "count", "10")))
                self.redirect("/?msg=" + urllib.parse.quote(f"Комнат сгенерировано: {added}, совпадений: {skipped}"))
                return
            if path == "/create":
                sub_id = create_subscription(
                    self.form_value(fields, "name"),
                    self.form_value(fields, "note"),
                    int(self.form_value(fields, "days", "30")),
                )
                self.redirect(f"/subscription/{urllib.parse.quote(sub_id)}?msg=" + urllib.parse.quote("Подписка создана"))
                return
            sub_id = self.form_value(fields, "id")
            if path == "/extend":
                extend_subscription(sub_id, int(self.form_value(fields, "days", "30")))
                self.redirect(f"/subscription/{urllib.parse.quote(sub_id)}?msg=" + urllib.parse.quote("Подписка продлена"))
                return
            if path == "/revoke":
                revoke_subscription(sub_id)
                self.redirect("/?msg=" + urllib.parse.quote("Подписка отключена"))
                return
            if path == "/restore":
                restore_subscription(sub_id)
                self.redirect(f"/subscription/{urllib.parse.quote(sub_id)}?msg=" + urllib.parse.quote("Подписка включена"))
                return
            if path == "/restart":
                restart_subscription(sub_id)
                self.redirect("/?msg=" + urllib.parse.quote("Сервис перезапущен"))
                return
        except Exception as exc:
            self.send_bytes(render_page("Ошибка", f"<section class='card'><h1>Ошибка</h1><p>{html.escape(str(exc))}</p><p><a href='/'>назад</a></p></section>", token=self.token()), status=500)
            return
        self.not_found()


def serve() -> None:
    setup_dirs()
    get_token()
    init_db = db()
    init_db.close()
    sync_existing_rooms()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"olcrtc-admin listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "expire", "init"], nargs="?", default="serve")
    args = parser.parse_args()
    if args.command == "init":
        setup_dirs()
        get_token()
        db().close()
        print(f"initialized {DB_PATH}")
        return 0
    if args.command == "expire":
        count = expire_subscriptions()
        print(f"expired={count}")
        return 0
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
