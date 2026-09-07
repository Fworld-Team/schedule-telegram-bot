# Подключение существующего сервера

Репозиторий: https://github.com/Fwor1d/schedule-telegram-bot

Настройки ниже рассчитаны на Linux VPS с Docker Engine, Compose и `flock`.
На сервере работает один экземпляр. Рабочий каталог: `/opt/schedule-bot`.
GitHub хранит код; токен и пользовательские данные остаются на сервере.

## 1. Сначала определить состояние старого бота

Отправьте `/start` в Telegram: новый чат или чат после `/stop` молчит
до этой команды. В каталоге текущего серверного проекта выполните:

```bash
docker compose ps --all
docker compose logs --tail=100 bot
docker inspect "$(docker compose ps -q bot)" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

Последняя команда показывает имя существующего named volume. Если контейнер
остановлен, возьмите его ID из `docker compose ps -a -q bot`. Если вывод
пустой, возможно используется bind mount или бот работает без Docker —
не переключайте его на пустой volume, сначала перенесите данные.

Перед переносом сделайте согласованную копию всего `/data`: остановите
старый бот, скопируйте данные в новый каталог резервной копии, затем
запустите старую копию. Не перезаписывайте прошлые резервные копии.
Например, после выбора ещё не существующего пути:

```bash
docker compose stop bot
docker compose cp bot:/data /opt/schedule-bot-backup-YYYYMMDD-HHMM
docker compose start bot
```

Сохраните копию вне сервера. Никогда не используйте `docker compose down -v`:
эта команда удаляет обычные тома вместе с расписанием и SQLite.
Данные в локальном `media/` — начальные примеры. Новые админские загрузки,
активный семестр и состояние чатов хранятся именно в `/data`.

## 2. Доступ сервера к GitHub

Создайте отдельный read-only deploy key на сервере и добавьте его публичную
часть в `Settings → Deploy keys` этого репозитория. `Allow write access`
не требуется. Приватная часть остаётся на сервере.
Настройте SSH для GitHub с этим ключом и проверьте fingerprint хоста по
[официальному списку GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).

Выделите серверного пользователя, владеющего `/opt/schedule-bot`. Доступ
к Docker daemon фактически даёт права уровня root — используйте отдельный
ключ для этого проекта. Клонируйте в ещё не существующий каталог:

```bash
git clone git@github.com:Fwor1d/schedule-telegram-bot.git /opt/schedule-bot
cd /opt/schedule-bot
cp .env.example .env
nano .env
```

Укажите в `.env`:

```dotenv
ADMIN_USER_IDS=123456789
BOT_DATA_VOLUME=ИМЯ_СУЩЕСТВУЮЩЕГО_VOLUME
```

Замените ID своим числовым Telegram ID, а имя тома — полученным на шаге 1.
`deploy/compose.production.yaml` требует существующий внешний том и
останавливает запуск при неверном имени, чтобы не создать пустую базу
при переименовании каталога. Только для совершенно нового бота без данных
можно один раз создать новый том через `docker volume create schedule_bot_data`.

Поместите токен в `secrets/telegram_bot_token.txt` редактором на сервере.
У этого проекта Linux UID/GID контейнера — 10001. Дайте файлу группу 10001
и права 0640; каталог `secrets` ограничьте владельцем (0700):

```bash
chmod 700 secrets
sudo chgrp 10001 secrets/telegram_bot_token.txt
chmod 640 secrets/telegram_bot_token.txt
chmod 600 .env
```

Compose монтирует файл секрета с его правами Linux; `chmod 600` для файла,
принадлежащего другому UID, не позволит контейнеру его прочитать.

Перед первым запуском проверьте конфигурацию и соберите образ:

```bash
docker compose -p schedule-bot -f compose.yaml -f deploy/compose.production.yaml config --quiet
docker compose -p schedule-bot -f compose.yaml -f deploy/compose.production.yaml build bot
```

После успешной сборки остановите старую копию бота её прежней командой
и отключите её старый автозапуск. Не удаляйте её данные.
Запустите новую копию:

```bash
docker compose -p schedule-bot -f compose.yaml -f deploy/compose.production.yaml up -d --wait --wait-timeout 120 bot
bash scripts/status.sh
```

Отправьте `/start`, затем `/today` и `/status` в Telegram. Убедитесь,
что сохранены расписание и админские настройки. При сбое проверьте
`docker compose -p schedule-bot -f compose.yaml -f deploy/compose.production.yaml logs --tail=100 bot`.

## 3. Доступ GitHub Actions к серверу

Это другая пара SSH-ключей. Приватный ключ добавляется в GitHub Secrets,
публичный — в `authorized_keys` выделенного серверного пользователя.
Для периодической проверки используйте отдельную пару ключей; её запись
в `authorized_keys` ограничьте единственной командой:

```text
restrict,command="bash /opt/schedule-bot/scripts/status.sh" ssh-ed25519 PUBLIC_KEY monitor
```

Для ключа деплоя запретите forwarding и PTY через опцию `restrict`.
Он должен иметь доступ к checkout и Docker. Проверка по отдельному ключу
не сможет запускать произвольные команды через SSH.

В `Settings → Secrets and variables → Actions → Secrets` добавьте:

| Secret | Значение |
| --- | --- |
| `SERVER_HOST` | DNS-имя или IPv4 сервера |
| `SERVER_PORT` | SSH-порт, обычно `22` |
| `SERVER_USER` | Выделенный серверный пользователь |
| `SERVER_SSH_KEY` | Приватный ключ деплоя |
| `SERVER_MONITOR_SSH_KEY` | Приватный ключ ограниченной проверки |
| `SERVER_KNOWN_HOSTS` | Проверенная строка host key сервера в формате known_hosts |

Host key получите через доверенную консоль хостинга и сравните fingerprint.
Не отключайте `StrictHostKeyChecking`. Для нестандартного порта запись
known_hosts должна использовать `[hostname]:port`.
Токен бота не нужен в Actions: сервер читает собственный secret-файл.

## 4. Включить автоматизацию

После успешного ручного запуска и проверки данных добавьте в соседней
вкладке `Variables`:

```text
DEPLOY_ENABLED=true
MONITORING_ENABLED=true
```

Откройте `Actions → Deploy → Run workflow` для `main`. Деплой повторно
запускает CI и получает ровно проверенный commit. Ветка, локальные правки
отслеживаемых файлов и отсутствующий том не перезаписываются принудительно.
Если `main` уже изменился, старый деплой завершится ошибкой: используйте
новейший запуск. На сервере стоит блокировка от одновременных обновлений.

Сбой сборки сохраняет старый работающий контейнер. Если приложение уже
пересоздано и не запустилось, автоматического отката нет: сначала изучите
логи, затем отмените ошибочный commit через `git revert` и отправьте
исправление. При изменении формата SQLite может потребоваться резервная копия.

`Server status` проверяет сервер два раза в час, также доступен ручной запуск.
Отсутствие настройки при ручной проверке отображается ошибкой, а не ложным
зелёным статусом. Сейчас в README показан серый индикатор «Сервер: ещё не
подключён» — это заглушка, а не результат проверки. После включения мониторинга
и первой успешной проверки замените строку с `docs/server-status.svg` в README на:

```markdown
[![Server status](https://github.com/Fwor1d/schedule-telegram-bot/actions/workflows/bot-status.yml/badge.svg?branch=main)](https://github.com/Fwor1d/schedule-telegram-bot/actions/workflows/bot-status.yml)
```

Badge отражает последнюю проверку, а не непрерывный текущий статус.
Проверяется SSH, работа контейнера и SQLite. Зависший polling или ошибка
обработчика Telegram могут остаться незамеченными: полноценный контроль
ответов Telegram ещё не реализован. Не проверяйте бот вторым `getUpdates`:
он будет конкурировать с рабочим polling.

GitHub может задерживать расписание; исчерпание Actions-минут остановит
проверки. Проверяйте дату последнего запуска. Оповещения о провалах Actions
настраиваются в личных Notification settings GitHub. Непрерывный мониторинг
с быстрыми оповещениями потребует отдельного сервиса или heartbeat.

## Документация

- [Deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
- [Workflow badges](https://docs.github.com/en/actions/how-tos/monitor-workflows/add-a-status-badge)
- [GitHub Actions schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)
