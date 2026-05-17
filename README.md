# OLC Panel

Простая веб-панель для выдачи OlcRTC VPN-подписок.

Панель создает новые подписки через **Jitsi + datachannel** и запускает для каждой подписки отдельный systemd-сервис `olcrtc-jitsi@<id>.service`.

## Что умеет

- создавать новую подписку;
- генерировать длинные Jitsi-комнаты автоматически;
- хранить пул комнат;
- показывать URI для клиента;
- продлевать, отключать, включать и перезапускать подписки;
- хранить runtime-файлы на сервере, а не в git.

Новые URI имеют вид:

```text
olcrtc://jitsi?datachannel@meet.cryptopro.ru/<room>#<key>$<name>
```

`<key>` генерируется на сервере и не должен публиковаться.

## Требования

- Linux-сервер с systemd;
- Python 3.10+;
- Podman;
- образ `olcrtc/server:universal-carrier`;
- рабочая сеть до выбранного Jitsi-сервера.

По умолчанию панель слушает только `127.0.0.1:8790`. Открывать ее наружу без reverse proxy и авторизации не нужно.

## Установка

```bash
sudo mkdir -p /opt/olcrtc-admin
sudo cp app.py /opt/olcrtc-admin/app.py
sudo cp systemd/olcrtc-admin.service /etc/systemd/system/olcrtc-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now olcrtc-admin.service
```

Админ-токен создается автоматически в `/etc/olcrtc-admin/admin.token`.

Ссылка для локального доступа сохраняется в:

```text
/etc/olcrtc-admin/admin.url
```

Обычно панель открывают через SSH-туннель:

```bash
ssh -L 8790:127.0.0.1:8790 root@SERVER_IP
```

Потом открыть:

```text
http://127.0.0.1:8790
```

## Где хранятся данные

Панель создает runtime-файлы на сервере:

```text
/var/lib/olcrtc-admin/
/etc/olcrtc-admin/
/etc/olcrtc/jitsi/
```

В git нельзя добавлять:

- `*.env`;
- `*.uri`;
- `*.yaml` с реальными ключами;
- `admin.token`;
- `admin.url`;
- базу `subscriptions.db`.

## Важный момент про права

YAML-файл подписки должен читаться пользователем внутри контейнера `olcrtc/server:universal-carrier`.

По умолчанию панель выставляет владельца `100:101` и права `600`.

Если на сервере нужен другой владелец, создай эталонный YAML и передай путь через переменную окружения:

```ini
Environment=OLCRTC_JITSI_CONFIG_OWNER_REFERENCE=/etc/olcrtc/jitsi/reference.yaml
```

Если клиент пишет `Connected`, но трафика нет или есть `read welcome timeout`, проверь:

```bash
systemctl status 'olcrtc-jitsi@*.service'
journalctl -u 'olcrtc-jitsi@SUB_ID.service' -n 100 --no-pager
```

Частая причина: контейнер не может прочитать `/etc/olcrtc/config.yaml`.

## Как пользоваться

1. Открой панель через SSH-туннель.
2. Нажми генерацию Jitsi-комнат или добавь свои комнаты.
3. Создай подписку.
4. Скопируй URI подписки.
5. Вставь URI в клиент Olcbox.

Одна подписка рассчитана на одно устройство.

## Безопасность

Этот репозиторий содержит только код панели и пример systemd-unit. Реальные подписки, ключи, URI и токены должны оставаться только на сервере.
