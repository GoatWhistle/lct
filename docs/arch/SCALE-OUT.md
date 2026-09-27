# Масштабирование нескольких backend-узлов

Статус: реализованы lease, транзакционное fencing и восстановление checkpoint при takeover в opt-in профиле двух backend-узлов. Локально пройдены PostgreSQL fencing/claim тесты, kill-owner и DB network-partition с живым старым процессом. Для обоих отказов полный маршрут карточки ДДС продолжен на новом владельце. Дополнительно live kill-owner smoke подтвердил границу повторной доставки: `card.status:arrived` и его command ID попали в PostgreSQL checkpoint, старый владелец был аварийно остановлен до доставки ACK в приложение браузера, новый владелец принял повтор с тем же ID и подтвердил дубликат; итоговый отчёт содержит статус ровно один раз. В harness ACK был увиден, но намеренно не dispatch-ился в симулированный браузер — это не имитация потери сетевого пакета. Команда, отправленная старому владельцу уже после разрыва DB route, прогонами 25.09 и 27.09 не проверялась: `arrived` в partition smoke — та же lost-ack команда, зафиксированная до отказа. Проверка реальной команды после разрыва добавлена в smoke и на стенде ещё не прогонялась (см. п. 4 ниже). Все основные бизнес-метрики полного DDS-сценария пройдены. [Повторяемое evidence](../evidence/dds-failover-2026-09-25.json). Это ещё не полная HA-приёмка: универсальная гарантия exactly-once для всех WS-команд, отказоустойчивая нагрузка, изменение состава пула и целевое оборудование не проверены. Временной коэффициент включает аварийный простой; методику score должен утвердить заказчик.

После добавления UUID маршрута в URL все четыре WebSocket-канала (`control`, `call`, `observe`, `station`) и session REST используют один Nginx consistent-hash ключ. PostgreSQL сохраняет `backend_node_id`, короткую lease и монотонный fencing epoch. Новый узел может атомарно переназначить истёкшую lease; записи старого владельца блокируются сравнением `(node_id, epoch)` в той же транзакции. При повторном подключении новый узел выдаёт восстановленный авторитетный checkpoint.

25.09.2026 профиль `docker-compose.cluster.yml` прошёл живой TLS/Nginx smoke на временной PostgreSQL: **20 занятий и 100 разных аккаунтов**, распределение по владельцам `backend-a: 7`, `backend-b: 13`; 100/100 страниц готовы, p95 готовности **1532,30 мс**, браузерных и routing ошибок нет. Проверялись управляющий `/ws/control`, курсантские `/ws/call` и `/ws/station`, преподавательский `/ws/observe` и `GET /api/sessions/{id}`. Отчёт: [`browser-load-cluster-2026-09-25.json`](../evidence/browser-load-cluster-2026-09-25.json). Этот локальный результат не является тестом на целевом сервере 6 ядер/32 ГБ и не включает отказ узла.

Повтор после добавления lease/fencing: **20 занятий / 100 уникальных пользователей**, владельцы `backend-a: 14`, `backend-b: 6`; 100/100 страниц готовы, 20/20 REST и call WS probes успешны, ошибок нет; общее p95 — **1871,45 мс**. p95 только DDS-страниц — **2002,58 мс**, на 2,58 мс выше ориентира 2 с. [Отчёт](../evidence/browser-load-cluster-leases-2026-09-25.json). Оба замера локальные и не характеризуют целевой сервер.

Общее состояние полномочий между узлами сверяется с PostgreSQL раз в секунду. HTTP/WS handshake сравнивает `auth_version` подписанной cookie с локальным кэшем последней синхронизации; если кэш старше 2 с или его ещё не удалось загрузить, HTTP-запрос закрывается с 503, WebSocket — с 1013, а открытые сокеты закрываются после истечения того же порога. Изменения на текущем узле применяются сразу после commit; удалённая блокировка, смена роли/пароля или logout обычно сходятся за один интервал опроса. Это убирает отдельный SQL lookup при каждом handshake, сохраняя fail-closed при потере синхронизации. После изменения измерен двухузловый 20×100 TLS/Nginx прогон: ramp-up 2 с дал p95 1187,08 мс; zero-ramp — 3335,75 мс (выше ориентира 2 с и хуже предыдущего 2489,69 мс). Разница не изолирует причинный эффект auth-cache: это локальные прогоны с разной вариативностью, не целевой сервер 6 ядер/32 ГБ. [Ramp-up](../evidence/browser-load-cluster-authcache-ramp2-2026-09-25.json), [zero-ramp](../evidence/browser-load-cluster-authcache-zero-ramp-2026-09-25.json). Lease действует 15 секунд, heartbeat и цикл поиска истёкших lease выполняются раз в 5 секунд. Поэтому при потере БД старым владельцем его открытые сокеты обычно закрывает auth — кэш поколений устаревает через 2 с, код 1013, — раньше, чем supervisor lease заметит потерю владения и закроет их с 1012. Оба кода — fail-closed, фронт переподключается одинаково; partition smoke засчитывает 1012 или 1013 с reason от auth и сохраняет фактический код в evidence. 1013 о fencing ничего не говорит — auth закрывает все сокеты узла, — поэтому fencing smoke проверяет отдельно: после возврата БД ждёт свежий auth на старом узле (`/api/auth/me` → 200), и только тогда отказ рукопожатия control (403) засчитывается за fencing. Прогон 27.09 сделан предыдущей версией smoke без этих проверок: все четыре канала закрылись с 1013, takeover за 11,19 с, 27 из 27 проверок — [`cluster-partition-2026-09-27.json`](../evidence/cluster-partition-2026-09-27.json); fencing при разделении в нём не наблюдался. В live-smoke takeover занял 10,68 с после разрыва DB-path и 15,28 с в более раннем прогоне; время зависит от возраста lease к началу отказа и фазы supervisor.

25.09.2026 crash-run `scripts/test_cluster_failover.py` аварийно остановил backend-владельца в изолированном TLS/Nginx/PostgreSQL Compose: owner сменился `backend-b → backend-a`, fencing epoch `1 → 2`, сохранённый checkpoint восстановлен; REST и все четыре авторизованных WS-канала в итоге подключились. Наблюдалась сходимость Nginx: session REST ответил после пяти клиентских попыток (первые попытки получили 502), WS — с первой попытки. Эта запись описывает отказ процесса; отдельный live DB network-partition с сохранённым живым старым процессом пройден позднее и зафиксирован в [`cluster-partition-2026-09-25.json`](../evidence/cluster-partition-2026-09-25.json). Ни один локальный smoke не доказывает мгновенное переподключение на каждом Nginx worker или HA на целевом оборудовании.

## Повторить локальный smoke маршрутизации

Команда поднимает отдельную БД/сеть/тома; `load_browser.py --distinct-users` создаёт и удаляет 100 временных учёток и 20 занятий. Задайте свободные loopback-порты и тестовые секреты:

```sh
export POSTGRES_PORT=15432 BACKEND_PORT=18000 FRONTEND_PORT=15173 TLS_PORT=15443
export POSTGRES_PASSWORD=lct-cluster-test-only
export SESSION_SECRET=lct-cluster-test-session-secret-only
export TLS_CERT_DIR=./.local/cluster-test-tls

docker compose -p lct-cluster-test \
  -f docker-compose.yml -f docker-compose.tls.yml \
  -f docker-compose.load-test.yml -f docker-compose.cluster.yml \
  up --pull never --no-build --detach --wait

python3 scripts/load_browser.py \
  --frontend-url https://127.0.0.1:15443 \
  --backend-url http://127.0.0.1:18000 \
  --control-url https://127.0.0.1:15443 \
  --database-url postgresql+asyncpg://lct:lct-cluster-test-only@127.0.0.1:15432/lct \
  --sessions 20 --clients 100 --ramp-seconds 2 --distinct-users \
  --ignore-https-errors --expect-backend-nodes 2 \
  --output docs/evidence/browser-load-cluster.json

docker compose -p lct-cluster-test \
  -f docker-compose.yml -f docker-compose.tls.yml \
  -f docker-compose.load-test.yml -f docker-compose.cluster.yml \
  down --volumes --remove-orphans
```

Bootstrap TLS и секреты выше годятся только для loopback smoke. Не запускайте этот профиль с рабочей БД или пользовательскими данными. Для отказа владельца повторите запуск с теми же временными тестовыми параметрами и выполните:

```sh
python3 scripts/test_cluster_failover.py \
  --project lct-cluster-test --postgres-port 15432 --backend-port 18000 \
  --frontend-port 15173 --tls-port 15443 \
  --postgres-password lct-cluster-test-only \
  --session-secret lct-cluster-test-session-secret-only \
  --tls-cert-dir ./.local/cluster-test-tls \
  --frontend-url https://127.0.0.1:15443
```

Smoke script создаёт временные учётки и занятие в этой БД, останавливает и затем запускает обратно только backend-владелец, проверяет REST/WS и удаляет записи.

Для проверки публикации курсантской КИО на A и её немедленного запуска на холодном B достаточно поднять PostgreSQL и оба backend (в кластере порт B опубликован только на loopback):

```sh
export POSTGRES_PORT=15432 BACKEND_PORT=18000 BACKEND_B_PORT=18001
export FRONTEND_PORT=15180 TLS_PORT=15443 TLS_CERT_DIR=./.local/lct-student-registry-tls
export POSTGRES_PASSWORD=lct-cluster-test-only
export SESSION_SECRET=lct-cluster-test-session-secret-only
docker compose -p lct-student-registry \
  -f docker-compose.yml -f docker-compose.tls.yml \
  -f docker-compose.load-test.yml -f docker-compose.cluster.yml \
  up --pull never --no-build --detach --wait backend backend-b frontend

backend/.venv/bin/python scripts/test_cluster_scenario_registry.py \
  --postgres-port 15432 --backend-a-port 18000 --backend-b-port 18001 \
  --postgres-password lct-cluster-test-only \
  --frontend-url https://127.0.0.1:15443

# Повторить с SIGKILL выбранного backend B после старта DDS и восстановлением на A:
backend/.venv/bin/python scripts/test_cluster_scenario_registry.py \
  --postgres-port 15432 --backend-a-port 18000 --backend-b-port 18001 \
  --postgres-password lct-cluster-test-only \
  --frontend-url https://127.0.0.1:15443 \
  --failure-mode kill-backend-b --project lct-student-registry \
  --session-secret lct-cluster-test-session-secret-only \
  --tls-cert-dir "$TLS_CERT_DIR" --frontend-port 15180 --tls-port 15443

docker compose -p lct-student-registry \
  -f docker-compose.yml -f docker-compose.tls.yml \
  -f docker-compose.load-test.yml -f docker-compose.cluster.yml \
  down --volumes --remove-orphans
rm "$TLS_CERT_DIR/tls.key" "$TLS_CERT_DIR/tls.crt"
rmdir "$TLS_CERT_DIR"
```

Smoke удаляет создаваемые записи и проверяет их отсутствие; команда `down` затем уничтожает только тома этого отдельного проекта.

Чтобы оставить backend-владелец живым, но отключить только его PostgreSQL-путь, используйте fault-injection overlay при `up`:

```sh
export BACKEND_B_PORT=18001
docker compose -p lct-cluster-partition \
  -f docker-compose.yml -f docker-compose.tls.yml \
  -f docker-compose.load-test.yml -f docker-compose.cluster.yml \
  -f docker-compose.partition-test.yml \
  up --pull never --no-build --detach --wait

python3 scripts/test_cluster_failover.py \
  --project lct-cluster-partition --failure-mode partition \
  --postgres-port 15432 --backend-port 18000 --backend-b-port 18001 \
  --frontend-port 15173 --tls-port 15443 \
  --postgres-password lct-cluster-test-only \
  --session-secret lct-cluster-test-session-secret-only \
  --tls-cert-dir ./.local/cluster-test-tls \
  --frontend-url https://127.0.0.1:15443

docker compose -p lct-cluster-partition \
  -f docker-compose.yml -f docker-compose.tls.yml \
  -f docker-compose.load-test.yml -f docker-compose.cluster.yml \
  -f docker-compose.partition-test.yml \
  down --volumes --remove-orphans
```

Этот профиль запускает TCP-прокси только как fault-injection; после прогона удалите весь изолированный Compose project этой командой.

Обычный Compose по-прежнему запускает один backend. Двухузловой профиль — отдельная opt-in конфигурация; не добавляйте несколько workers и не убирайте affinity. До прохождения оставшихся приёмок ниже нельзя заявлять кластер как полностью соответствующий требованиям высокой доступности.

## Реализованная модель и пределы восстановления

Сессия получает UUID в браузере до открытия управляющего WebSocket; он присутствует в URL `control`, `call`, `observe` и `station`. Nginx профиля кластера извлекает этот UUID и передаёт один consistent-hash ключ в upstream для WS и `/api/sessions/{id}`. Внутри узла остаётся один Uvicorn worker и существующий `SessionHub`; PostgreSQL хранит журнал, владельца узла и контрольные точки. `BACKEND_NODE_ID` задаётся отдельно каждому backend-узлу и должен оставаться стабильным при его перезапуске. Профиль находится в `docker-compose.cluster.yml`, использует TLS-конфигурацию `nginx.cluster.tls.conf.template`, выполняет миграции/seed только на backend A и ждёт его healthcheck перед запуском B.

При `scenario.start` каждый узел сверяет запрошенные ID с общей таблицей опубликованных сценариев и подгружает тело из PostgreSQL, если локальный кэш холодный или устарел. `status` и `owner_login` из БД авторитетны: чужие, ожидающие модерации и архивные сценарии не становятся стартуемыми из-за оставшейся в памяти копии. DB cache-miss путь после модерации покрывает PostgreSQL integration test вместе с выдачей шаблона в DDS station; реальный обмен двумя отдельными процессами после публикации ещё не измерен.

Для безопасного добавления и удаления узлов одной consistent-hash конфигурации недостаточно: изменение состава backend меняет размещение ключа. PostgreSQL сохраняет назначение `session_id → backend_node` при первом `scenario.start`; повторное начало на другом узле отклоняется. Проверены фиксированный состав двух узлов и takeover одной сессии. Безопасная ребалансировка активных занятий при изменении состава пула остаётся отдельной задачей.

### Safety-протокол и остаточный риск

Переназначать владельца только по факту истечения аренды недостаточно: старый процесс может быть жив при сетевом разделении. Текущая реализация объединяет lease и fencing в общем PostgreSQL:

1. Каждый узел с коротким интервалом продлевает lease активных занятий; heartbeat обновляет только строки, всё ещё принадлежащие этому `backend_node_id` и текущему монотонному `fencing_epoch`.
2. Takeover выполняется транзакционно под `SELECT ... FOR UPDATE SKIP LOCKED`/условным `UPDATE`: только истёкшая lease может сменить владельца, а epoch атомарно увеличивается. Lease одной сессии нельзя получить двум узлам.
3. **Каждая** долговечная мутация хода занятия — реплика, ответ карточки, статус/бригада/зона, самооценка, оценка, checkpoint и завершение — проверяет пару `(backend_node_id, fencing_epoch)` в той же транзакции до записи. Проверки только на старте WebSocket или перед checkpoint недостаточно: старый узел иначе сохранит устаревшие события после takeover.
4. Если старый владелец не может продлить lease или получает несовпадение epoch, он прекращает приём команд, останавливает таймеры/голос и закрывает все каналы сессии. Ошибка БД не трактуется как разрешение продолжать запись «на авось».
5. Новый владелец атомарно захватывает новую epoch и восстанавливает последний сохранённый checkpoint. Полное повторное проигрывание журнала событий после checkpoint пока не реализовано; поэтому интервал между checkpoint может потерять подтверждённые только в памяти изменения. Фронтенд повторяет только ограниченные по времени (35 с) replace-style `kio.patch` и `card.reply` до checkpoint echo/snapshot; команды с возможным накоплением эффекта (статус, бригада, звонок и другие переходы) автоматически не повторяются. Это не exactly-once delivery.

Текущие значения lease/heartbeat зафиксированы в коде и ещё требуют замера на целевой сети. Строки хода занятия и checkpoint одной операции коммитятся одной транзакцией (`SessionStore.commit`), события уходят после неё — частичного коммита между журналом и снимком нет. Продление, подхват просроченных lease и восстановление активных занятий ведёт `SessionHub` поверх того же port-а (`restore`, `maintain_lease`, `save_all`); код запуска приложения только включает супервизор и в реестр хаба не заглядывает. Успешный proxy retry сам по себе не доказывает восстановление состояния.

## Затрагиваемые общие ресурсы

- Все узлы используют один PostgreSQL; Alembic и seed запускаются отдельным единственным шагом до backend, а не параллельно при старте каждой реплики.
- Аудиозаписи и другие создаваемые файлы должны быть доступны всем узлам на общем локальном томе или объектном хранилище внутри сети.
- `GET /api/sessions/active` объединяет локальный hub с активными PostgreSQL checkpoint, ограниченными преподавателем-владельцем; некорректные удалённые снимки журналируются и пропускаются. Путь БД покрыт unit-тестом с подменённым результатом репозитория, но не live PostgreSQL или многопроцессной приёмкой.
- Утверждённые курсантские сценарии хранятся в общей БД; узел, получивший `scenario.start`, должен загружать сценарий из БД при cache miss.
- SIP-сервер остаётся отдельным локальным сервисом; его регистрация не является владельцем состояния учебной WebSocket-сессии.

## Приёмочные проверки перед заявлением поддержки кластера

1. **Пройдены локально:** два backend и TLS proxy; control, call, observe, station, session REST, 20 занятий и 100 разных аккаунтов. См. browser-load JSON выше.
2. **Пройдено локально:** одна карточка ДДС начата до partition/crash, продолжена после takeover и завершена; отчёт сохранил решение, бригаду, все пять статусов и прошёл основные бизнес-метрики. В partition проверена reconciliation lost-ack статуса по recovered snapshot. В отдельном kill-owner smoke станционная команда и её ID подтверждены в PostgreSQL checkpoint до сбоя; ACK не dispatch-ился в приложение браузера; новый владелец подтвердил дубликат с тем же ID и статус остался в отчёте ровно один раз. Harness видел ACK на протокольном сокете, поэтому это проверка потери при доставке в приложение, не сетевого пакета и не гонка на точной транзакционной границе. См. [DDS failover evidence](../evidence/dds-failover-2026-09-25.json). Failure-overlap нагрузка ещё не генерировалась.
3. **Пройден межпроцессный publish/start/kill-owner recovery через TLS Nginx**: курсант создал КИО и преподаватель утвердил её через backend A; новый UUID маршрутизировался на B, где сессия стартовала из общей PostgreSQL. После SIGKILL B lease/fencing передали сессию A за 18,65 с; station WS восстановил карточку, статус-команда получила ACK и попала в checkpoint нового владельца. Перед отказом также подтверждены instructor catalog/detail на B; после smoke SQL показал ноль временных users/groups/submissions/sessions/scenarios, backend B поднялся обратно healthy. [Failover evidence](../evidence/cross-node-student-scenario-failover-2026-09-25.json), [Nginx-only evidence](../evidence/cross-node-student-scenario-nginx-2026-09-25.json), [direct evidence](../evidence/cross-node-student-scenario-2026-09-25.json). Сетевой отказ во время самого publish transaction, конкурентные 20/100 занятия под отказом и общий live-реестр наблюдаемых занятий остаются незавершёнными.
4. **Пройдена safety-часть локально:** и kill-owner, и DB network partition с живым старым backend; epoch `1 → 2`, checkpoint восстановлен, все четыре уже открытых канала закрылись fail-closed (в прогоне 25.09 — 1012 от fencing; после кэша поколений учёток auth закрывает их раньше кодом 1013, см. ниже), процесс старого владельца оставался healthy и после возврата DB напрямую отклонил control (403; в прогонах 25.09 и 27.09 без проверки свежести auth, поэтому 403 не отличал fencing от 1013). `card.status:arrived` в partition — lost-ack команда, зафиксированная до обрыва (`committed before takeover`): после takeover её повтор подтверждён по `_command_id`, статус сохранился в отчёте ровно один раз. Запрет записи старым владельцем после потери БД этими прогонами не доказан: реальная команда после обрыва не отправлялась. Теперь smoke сразу после обрыва отправляет в открытый station-сокет `card.status:working` с `_command_id` и требует: ACK нет, id нет в checkpoint и после возврата БД, статус после takeover — `arrived`. На стенде эта версия ещё не прогонялась. Через TLS proxy восстановились REST и все WS. PostgreSQL-тесты отдельно подтвердили отказ старого epoch писать transcript. Для station WebSocket есть ограниченный `command_id` ledger (512 последних ID в checkpoint), frontend outbox до 100 команд/35 секунд и live kill-owner replay уже committed команды после takeover; отдельный browser/PostgreSQL test подтверждает восстановление KIO после потерянного echo. Replace-style `kio.patch`/`card.reply` используют snapshot-based подтверждение. Полный реестр активных занятий/утверждённых курсантских сценариев между узлами и конкурентный отказоустойчивый нагрузочный тест ещё не завершены.
5. Измерить 20 занятий и 100 пользователей при штатной работе и во время отказа/возврата узла на целевом сервере от 6 ядер и 32 ГБ. **Не проверено.**

До прохождения пунктов 2–5 двухузловой Compose можно использовать только как локальную экспериментальную failover-конфигурацию; штатный production multi-node режим и полное соответствие отказоустойчивости пока не подтверждены.
