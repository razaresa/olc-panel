from __future__ import annotations

import importlib.util
import zoneinfo
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


APP_PATH = Path(__file__).with_name("app.py")
SPEC = importlib.util.spec_from_file_location("olcrtc_admin_app", APP_PATH)
assert SPEC and SPEC.loader
app = importlib.util.module_from_spec(SPEC)
ORIGINAL_ZONEINFO = zoneinfo.ZoneInfo
zoneinfo.ZoneInfo = lambda key: timezone(timedelta(hours=4)) if key == "Europe/Astrakhan" else ORIGINAL_ZONEINFO(key)
try:
    SPEC.loader.exec_module(app)
finally:
    zoneinfo.ZoneInfo = ORIGINAL_ZONEINFO


@pytest.fixture()
def isolated_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    etc_dir = tmp_path / "etc"
    olcrtc_dir = etc_dir / "olcrtc"

    monkeypatch.setattr(app, "DATA_DIR", data_dir)
    monkeypatch.setattr(app, "SUB_STATE_DIR", data_dir / "subscriptions")
    monkeypatch.setattr(app, "DB_PATH", data_dir / "subscriptions.db")
    monkeypatch.setattr(app, "ETC_DIR", etc_dir / "olcrtc-admin")
    monkeypatch.setattr(app, "OLCRTC_ETC_DIR", olcrtc_dir)
    monkeypatch.setattr(app, "SUB_ETC_DIR", olcrtc_dir / "subscriptions")
    monkeypatch.setattr(app, "JITSI_ETC_DIR", olcrtc_dir / "jitsi", raising=False)
    monkeypatch.setattr(app, "JITSI_STATE_DIR", data_dir / "jitsi", raising=False)
    monkeypatch.setattr(app, "JITSI_SYSTEMD_DIR", tmp_path / "systemd", raising=False)
    monkeypatch.setattr(app, "SERVER_ENV_PATH", olcrtc_dir / "server.env")
    monkeypatch.setattr(app, "TOKEN_PATH", etc_dir / "olcrtc-admin" / "admin.token")
    monkeypatch.setattr(app, "ADMIN_URL_PATH", etc_dir / "olcrtc-admin" / "admin.url")
    monkeypatch.setattr(app, "JITSI_HOSTS_PATH", etc_dir / "olcrtc-admin" / "jitsi-hosts.txt", raising=False)

    return tmp_path


def test_jitsi_client_uri_keeps_canonical_url_scheme(
    isolated_panel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []
    chowns: list[tuple[Path, int, int]] = []

    def fake_systemctl(*args: str, check: bool = True) -> SimpleNamespace:
        commands.append(args)
        return SimpleNamespace(stdout="active\n", stderr="", returncode=0)

    def fake_chown(path: Path | str, uid: int, gid: int) -> None:
        chowns.append((Path(path), uid, gid))

    monkeypatch.setattr(app, "systemctl", fake_systemctl)
    monkeypatch.setattr(app.os, "chown", fake_chown, raising=False)
    monkeypatch.setattr(app.secrets, "token_hex", lambda n: "f" * (n * 2))
    app.add_rooms("https://jitsi.etudevs.ru/olcrtc-panel-client")

    sub_id = app.create_subscription("Ivan", "android", 30)
    row = app.get_subscription(sub_id)
    assert row is not None

    env_text = app.jitsi_env_path(sub_id).read_text(encoding="utf-8")
    assert "OLCRTC_ROOM_ID=https://jitsi.etudevs.ru/olcrtc-panel-client" in env_text

    yaml_text = app.jitsi_yaml_path(sub_id).read_text(encoding="utf-8")
    assert 'id: "https://jitsi.etudevs.ru/olcrtc-panel-client"' in yaml_text

    uri = app.jitsi_uri_path(sub_id).read_text(encoding="utf-8").strip()
    assert uri == (
        "olcrtc://jitsi?datachannel@https://jitsi.etudevs.ru/olcrtc-panel-client"
        "#ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "$ivan-until-" + app.fmt_date(row["expires_at"])
    )
    assert "datachannel@https://" in uri
    assert f"%{sub_id}" not in uri

    assert chowns == [(app.jitsi_yaml_path(sub_id), 100, 101)]
    assert commands == [
        ("daemon-reload",),
        ("enable", "--now", f"olcrtc-jitsi@{sub_id}.service"),
    ]


def test_jitsi_subscription_writes_failover_profiles_and_uri(
    isolated_panel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = "https://jitsi.etudevs.ru/olcrtc-panel-client"
    backup = "http://meet.small-dm.ru/olcrtc-panel-client"
    commands: list[tuple[str, ...]] = []

    def fake_systemctl(*args: str, check: bool = True) -> SimpleNamespace:
        commands.append(args)
        return SimpleNamespace(stdout="active\n", stderr="", returncode=0)

    monkeypatch.setattr(app, "systemctl", fake_systemctl)
    monkeypatch.setattr(app.os, "chown", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(app.secrets, "token_hex", lambda n: "e" * (n * 2))
    monkeypatch.setattr(
        app,
        "JITSI_ROOM_BASE_URLS",
        ["https://jitsi.etudevs.ru", "http://meet.small-dm.ru"],
        raising=False,
    )
    app.add_rooms(primary)

    sub_id = app.create_subscription("Petr", "android", 7)
    row = app.get_subscription(sub_id)
    assert row is not None

    env_text = app.jitsi_env_path(sub_id).read_text(encoding="utf-8")
    assert f"OLCRTC_ROOM_ID={primary}" in env_text
    assert f"OLCRTC_ROOM_IDS={primary},{backup}" in env_text

    yaml_text = app.jitsi_yaml_path(sub_id).read_text(encoding="utf-8")
    assert f'id: "{primary}"' in yaml_text
    assert "profiles:" not in yaml_text

    backup_id = app.jitsi_endpoint_service_id(sub_id, 2)
    backup_env_text = app.jitsi_env_path(backup_id).read_text(encoding="utf-8")
    assert f"OLCRTC_ROOM_ID={backup}" in backup_env_text
    backup_yaml_text = app.jitsi_yaml_path(backup_id).read_text(encoding="utf-8")
    assert f'id: "{backup}"' in backup_yaml_text

    uri = app.jitsi_uri_path(sub_id).read_text(encoding="utf-8").strip()
    assert uri == (
        f"olcrtc://jitsi?datachannel@{primary},{backup}"
        "#eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        "$petr-until-" + app.fmt_date(row["expires_at"])
    )
    assert commands == [
        ("daemon-reload",),
        ("enable", "--now", f"olcrtc-jitsi@{sub_id}.service"),
        ("enable", "--now", f"olcrtc-jitsi@{backup_id}.service"),
    ]


def test_jitsi_hosts_can_be_saved_and_used_for_new_rooms(
    isolated_panel: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.secrets, "token_hex", lambda n: "a" * (n * 2))

    saved = app.write_jitsi_room_base_urls("jitsi.etudevs.ru\nhttp://meet.small-dm.ru/")

    assert saved == ["https://jitsi.etudevs.ru", "http://meet.small-dm.ru"]
    assert app.configured_jitsi_room_base_urls() == saved
    assert app.generated_room_url() == "https://jitsi.etudevs.ru/olcrtc-auto-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert app.jitsi_room_candidates("https://jitsi.etudevs.ru/olcrtc-client") == [
        "https://jitsi.etudevs.ru/olcrtc-client",
        "http://meet.small-dm.ru/olcrtc-client",
    ]


def test_dashboard_renders_editable_jitsi_hosts(
    isolated_panel: Path,
) -> None:
    app.write_jitsi_room_base_urls("https://jitsi.etudevs.ru\nhttp://meet.small-dm.ru")

    html = app.dashboard("token").decode("utf-8")

    assert 'action="/jitsi-hosts/save"' in html
    assert "https://jitsi.etudevs.ru" in html
    assert "http://meet.small-dm.ru" in html
