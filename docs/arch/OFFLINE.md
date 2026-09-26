# Запуск в изолированной локальной сети

Режим карточек 112 и ДДС не требует облачного API или голосовых моделей. В `docker-compose.yml` сервер работает с `OFFLINE=true`, `VOICE_ENABLED=false`; PostgreSQL, backend и frontend находятся в одной локальной сети Docker. `make offline` запускает **только уже имеющиеся образы** (`--pull never --no-build`). Это офлайн-работа после подготовки образов, а не обещание собрать зависимости с нуля без сети.

## Подготовить образы на машине с доступом к пакетам

```sh
docker compose pull postgres
docker compose build backend frontend
docker compose -f docker-compose.yml -f docker-compose.sip.yml build sip
docker save -o lct-hack-images.tar postgres:16-alpine lct-hack-backend:local lct-hack-frontend:local lct-hack-sip:local
```

Перенесите репозиторий (включая `scenarios/`) и `lct-hack-images.tar` на стенд. Архив содержит только образы; данные PostgreSQL и учётные записи в него не входят.

## Запустить без интернета

```sh
docker load -i lct-hack-images.tar
make offline-production
make offline-sip
```

До первого запуска скопируйте `.env.example` в `.env` и задайте случайный
уникальный `POSTGRES_PASSWORD`, например:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Пароль должен состоять из URL-safe символов латиницы, цифр, точки, подчёркивания,
тильды и дефиса, поскольку он передаётся в PostgreSQL DSN backend/backup.
Не используйте `make offline` для рабочего стенда: эта команда оставлена для
разработки и использует пароль БД по умолчанию. Для production/offline-развёртывания
с TLS выполните `make offline-production`. Для сборки из исходников используется
`make production`. В обоих случаях Compose откажется запускаться, если
`POSTGRES_PASSWORD` is empty, too short, or contains unsupported characters. The
production overlay also forces `DEMO_NO_DB=false`,
`DEV_AUTH_BYPASS=false` and `SECURE_COOKIES=true`; backend startup rejects
known/default or short session-signing secrets, rejects production unless
`OFFLINE=true` and `LLM_PROVIDER=local`, and browsers send the auth cookie only
over HTTPS/WSS. Compose generates and persists a random signing secret in
`securitydata` if `SESSION_SECRET` is left blank.

В другом терминале один раз выполните `make users-docker` и сохраните выведенные пароли. При старте контейнера backend автоматически накатывает миграции и загружает библиотеку сценариев. Данные остаются в Docker volume `pgdata`; повторный запуск не должен пересоздавать пользователей.

PostgreSQL применяет `POSTGRES_PASSWORD` только при инициализации нового
`pgdata`. Если volume уже создан, смена `.env` сама не меняет пароль роли: до
подключения production overlay выполните ротацию роли `lct` в PostgreSQL и
обновите приложение вместе с ней. Не удаляйте volume ради смены пароля.

Сервис `backup` сразу создаёт первую SQL-копию, затем повторяет операцию не
реже раза в сутки и хранит 14 последних файлов в отдельном volume `backups`.
Параметры `BACKUP_INTERVAL_SECONDS`, `BACKUP_RETRY_SECONDS` и `BACKUP_KEEP`
задаются в `.env`. АРМ администратора показывает время и свежесть последней
успешной копии. Живая проверка полного восстановления описана в
[LOAD-TEST.md](LOAD-TEST.md#резервное-копирование-и-восстановление).

Аудиозаписи голосового упражнения хранятся отдельно в volume `recordings`, а
SQL-копии — в `backups`.
Они не входят в архив Docker-образов и должны копироваться/резервироваться
отдельно вместе с политикой доступа к персональным данным. Удаление контейнера
не удаляет записи; `docker compose down -v` удаляет volumes и потому для
рабочего стенда использоваться не должно.

Для рабочего контура вместо HTTP запустите TLS-профиль:

```sh
make offline-production
# пульт: https://localhost:5443/instructor
```

При первом запуске контейнер создаёт bootstrap self-signed сертификат в
`.local/tls`. Он шифрует трафик, но не подтверждает подлинность сервера до
добавления сертификата в доверенные. Для класса администратор должен заменить
`tls.crt` и `tls.key` сертификатом внутреннего УЦ, содержащим DNS-имя/IP стенда,
и установить корневой сертификат УЦ на рабочие места. Закрытый ключ не входит
в репозиторий. TLS-профиль разрешает TLS 1.2/1.3, перенаправляет HTTP на HTTPS,
использует WSS и Secure-cookie. Compose также создаёт стойкий случайный ключ
подписи сессий в volume `securitydata`; удаление этого volume завершит все
действующие входы, но не удалит пользователей или результаты.

Проверка: `docker compose -f docker-compose.yml -f docker-compose.tls.yml config --quiet`,
затем `curl --cacert .local/tls/tls.crt https://localhost:5443/api/health`.
В ответе ожидается `"offline":true`; `"models_ready":false` для режима без
голоса нормально. По умолчанию порты доступны только на самой машине. Для
класса задайте `BIND_HOST=0.0.0.0`, настройте межсетевой экран и открывайте с
другой машины DNS-имя стенда, указанное в сертификате, а не `localhost`.

Режим голосового звонка заявителя требует отдельного предварительного переноса моделей и зависимостей; `make demo` проверяет наличие файлов, но не доказывает готовность офлайн-упаковки. Локальный Asterisk от моделей не зависит: после `make offline-sip` получите пароли командой `make sip-credentials` и проверьте его по [инструкции SIP](SIP.md). Веб-телефон АРМ ДДС использует те же локальные Asterisk и Nginx по WSS/WebRTC; оцениваемый событийный протокол остаётся отдельным от свободной речи. Холодный запуск на машине заказчика, 20 параллельных занятий и безопасность стенда требуют отдельного приёмочного испытания.
