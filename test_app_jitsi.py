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
    app.add_rooms("https://meet.cryptopro.ru/olcrtc-panel-client")

    sub_id = app.create_subscription("Ivan", "android", 30)
    row = app.get_subscription(sub_id)
    assert row is not None

    env_text = app.jitsi_env_path(sub_id).read_text(encoding="utf-8")
    assert "OLCRTC_ROOM_ID=https://meet.cryptopro.ru/olcrtc-panel-client" in env_text

    yaml_text = app.jitsi_yaml_path(sub_id).read_text(encoding="utf-8")
    assert 'id: "https://meet.cryptopro.ru/olcrtc-panel-client"' in yaml_text

    uri = app.jitsi_uri_path(sub_id).read_text(encoding="utf-8").strip()
    assert uri == (
        "olcrtc://jitsi?datachannel@https://meet.cryptopro.ru/olcrtc-panel-client"
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
