# Локальный Docker запуск Hermes

Этот каталог содержит самодостаточный local environment для проверки Hermes
перед VPS-релизом. Он запускает Hermes, repository plugins, Dashboard и GitHub
CLI в изолированном container. Telegram gateway запускается автоматически,
только если заданы обе Telegram-переменные.

В обычном local запуске Hermes может через `sudo` устанавливать пакеты и
настраивать **свой container** по вашему запросу. Это не даёт ему Docker socket,
host mounts, devices, privileged mode или доступ к macOS. Изменения вне
`/opt/data` намеренно исчезают при `--build --force-recreate`.

Каждый shell-блок ниже запускается одним вызовом **из корня repository** и сам
переходит в `hermes/docker`; после выполнения ваш каталог в терминале не
изменится.

## Важные каталоги и данные

Общие versioned исходники — `hermes/ops` (observability plugin, systemd и
scripts) и `hermes/observability` (Prometheus/Grafana provisioning) — описаны
один раз в [общей карте Hermes](../README.md#общая-карта-исходных-файлов).

| Путь | Где находится | Назначение |
| --- | --- | --- |
| `hermes/docker/` | Repository на Mac | Compose, Dockerfile, bootstrap, эта инструкция и `AGENTS.md` для local container |
| `hermes/docker/local.env` | Repository на Mac, ignored Git | Runtime credentials Hermes: LLM, Telegram, GitHub, Brave и Dashboard auth |
| `hermes/docker/grafana.env` | Repository на Mac, ignored Git | Отдельные Grafana administrator credentials |
| `/opt/hermes` | Внутри container, ephemeral | Upstream Hermes installation и Python virtualenv; меняется при пересборке image |
| `/opt/hermes-local` | Внутри container, ephemeral | Наши bootstrap и observability assets, скопированные Dockerfile |
| `/opt/data` | Volume `hermes_local_test_data` | Постоянное Hermes state: config, home, sessions, plugins, logs, ops и workspace |
| `/opt/data/workspace` | Внутри persistent `/opt/data` | Репозитории и рабочие файлы агента; bootstrap кладёт сюда `AGENTS.md` |
| `hermes_local_test_metrics` | Docker named volume, не container | Файл `hermes.prom`, которым обмениваются metrics collector и provider |
| `hermes_local_test_prometheus` | Docker named volume, не container | Локальная Prometheus time-series база |
| `hermes_local_test_grafana` | Docker named volume, не container | Grafana database, пользователи и dashboards state |

## Быстрый запуск

1. `local.env.example` — только шаблон. Docker требует заполненную рабочую
   копию `hermes/docker/local.env`. Grafana использует второй, изолированный
   рабочий файл `hermes/docker/grafana.env`; из корня repository создайте оба:

   ```bash
   (
     cd hermes/docker
     cp local.env.example local.env
     cp grafana.env.example grafana.env
   )
   ```

2. Задайте в `hermes/docker/local.env` credentials, нужные для проверки:

   ```dotenv
   TELEGRAM_BOT_TOKEN="token-из-BotFather"
   TELEGRAM_ALLOWED_USERS="ваш-числовой-Telegram-user-ID"
   GITHUB_TOKEN="fine-grained-token-только-для-тестовых-репозиториев"
   BRAVE_SEARCH_API_KEY="ключ-Brave-Search-API"
   HERMES_LOCAL_GIT_NAME="Your Name"
   HERMES_LOCAL_GIT_EMAIL="you@example.com"
   HERMES_DASHBOARD_BASIC_AUTH_USERNAME="hermes"
   HERMES_DASHBOARD_BASIC_AUTH_PASSWORD="длинный-уникальный-local-пароль"
   ```

   `TELEGRAM_ALLOWED_USERS` обязателен вместе с token: неизвестные отправители
   не смогут вызывать agent. Для GitHub используется HTTPS и `gh`: токен не
   записывается в image или volume, а Git credential helper читает его из
   environment при каждом вызове. Дайте fine-grained token доступ только к
   нужным test repositories и правам Contents/Pull requests.
   Если заданы оба Telegram значения, Dashboard стартует вместе с gateway и
   использует эти basic-auth credentials. Если не задано ни одного, container
   запускает только Dashboard: можно проверить GUI, LLM, GitHub и plugins без
   доступа к Telegram. Если указано лишь одно значение, bootstrap остановит
   запуск с понятной ошибкой.

   В `hermes/docker/grafana.env` задайте отдельные Grafana credentials:

   ```dotenv
   GF_SECURITY_ADMIN_USER=hermes
   GF_SECURITY_ADMIN_PASSWORD="отдельный-длинный-уникальный-local-пароль"
   ```

   Задайте их до первого запуска: Grafana сохраняет administrator password в
   своём volume. Файл намеренно отдельный: Grafana не получает credentials
   Hermes, Telegram, GitHub или LLM provider.

3. Поднимите environment одной командой:

   ```bash
   (
     cd hermes/docker
     docker compose -f docker-compose.local.yml up --build --detach
   )
   ```

   Это полный локальный запуск: Hermes Dashboard, repository plugins, GitHub
   CLI, Brave Search (если задан ключ), Prometheus и Grafana. Если в
   `local.env` заполнены обе Telegram-переменные, этой же командой запускается
   Telegram gateway; без них остаётся безопасный Dashboard-only режим.

   По умолчанию запускается OpenRouter с `openrouter/free`: router выберет
   совместимую бесплатную модель. Лимит одного ответа — 2048 tokens. Другую
   доступную в OpenRouter модель задайте через `HERMES_LOCAL_OPENROUTER_MODEL`,
   а лимит — через `HERMES_LOCAL_OPENROUTER_MAX_TOKENS` в `local.env`.
   Платную модель задавайте только при намеренно включённом billing: это
   предотвращает ошибку OpenRouter `HTTP 402` на free account.

   Озвучивание использует бесплатный Edge TTS с русским голосом
   `ru-RU-SvetlanaNeural`. При временном ответе Edge без аудио Hermes повторит
   запрос до двух раз, не повторяя другие ошибки. При необходимости голос
   меняется через `HERMES_LOCAL_EDGE_TTS_VOICE` в `local.env`.

   Для `web_search` через Brave укажите `BRAVE_SEARCH_API_KEY`. Несмотря на
   внутреннее имя Hermes `brave-free`, Brave требует API key. При наличии
   ключа bootstrap явно выбирает Brave для поиска, даже если Nous Tool Gateway
   ранее сохранил Firecrawl как общий backend. Без ключа Hermes не фиксируется
   на неработающем Brave backend: он использует доступный Nous Tool Gateway
   после OAuth либо другой отдельно настроенный backend. Brave предназначен
   только для поиска; для извлечения полной страницы Hermes использует browser
   или один из extract-capable backends (например, Nous Tool Gateway/Firecrawl).

   Без `OPENROUTER_API_KEY` container тоже стартует — это позволяет проверить
   bootstrap и plugins, но не вызовы модели.

   После запуска откройте GUI на `http://127.0.0.1:9119` и войдите с
   `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` и
   `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`. Вкладка **Metrics** показывает
   локально собранные calls моделей, tokens, стоимость, tools и health после
   появления первых сообщений в Hermes.

   Исторические графики доступны в Grafana на `http://127.0.0.1:3000`.
   Войдите с `GF_SECURITY_ADMIN_USER` и `GF_SECURITY_ADMIN_PASSWORD`, затем откройте
   автоматически подготовленный dashboard **Hermes / Hermes Overview**.
   Prometheus и metrics provider не публикуют порты на host: их видит только
   Grafana во внутренней monitoring-сети. Metrics provider использует образ
   node-exporter только для выдачи Hermes textfile metrics и не читает macOS.
   Сам Hermes получает read-only доступ к private Prometheus через tool
   `ops_metrics`: можно спросить агента обычным сообщением о CPU/load, памяти,
   диске, gateway, токенах, tool errors или стоимости. Порт Prometheus наружу
   не публикуется.

## Проверка

```bash
# Логи в реальном времени; остановите просмотр сочетанием Ctrl+C.
(cd hermes/docker && docker compose -f docker-compose.local.yml logs --follow hermes)

# Остальные проверки можно запускать по одной.
(cd hermes/docker && docker compose -f docker-compose.local.yml exec hermes hermes config check)
(cd hermes/docker && docker compose -f docker-compose.local.yml exec hermes hermes config get model.provider)
(cd hermes/docker && docker compose -f docker-compose.local.yml exec hermes gh auth status --hostname github.com)
(cd hermes/docker && docker compose -f docker-compose.local.yml ps)
(cd hermes/docker && docker compose -f docker-compose.local.yml logs --tail=80 prometheus grafana metrics-collector)
```

Если заданы Telegram credentials, отправьте боту сообщение именно с
разрешённого account. В log не должно быть `No messaging platforms enabled`.
Без них это ожидаемо: container работает в Dashboard-only mode. Для GitHub
используйте test repository: попросите Hermes клонировать его, создать branch
и открыть draft PR. Protected branches сохраняют свои rules.

Для интерактивной сессии вместо gateway:

```bash
(
  cd hermes/docker
  docker compose -f docker-compose.local.yml run --rm hermes chat
)
```

## Перенос проверенных настроек на VPS без секретов

После настройки STT, model, web search и других параметров в локальном GUI
можно экспортировать только безопасный конфигурационный overlay. Рабочий файл
называется `hermes/docker/vps-config.yml`; он ignored Git и не содержит
`.env`, API keys, OAuth, sessions, memory, Telegram/GitHub credentials или
содержимое Docker volume целиком.

```bash
(
  cd hermes/docker
  docker compose -f docker-compose.local.yml exec --user hermes -T hermes \
    /opt/hermes/.venv/bin/python /opt/hermes-local/export-vps-config.py \
    > vps-config.yml
)
```

Проверьте файл, затем безопасно откройте рабочий зашифрованный
`hermes/ansible/group_vars/all/vault.yml` по
[Vault-инструкции](../ansible/group_vars/all/VAULT.md) и замените только его
раздел `hermes_llm_config` экспортированным разделом. Не заменяйте весь Vault:
`hermes_secret_env` должен оставаться там, а реальные VPS keys задаются
отдельно. После этого обычный `ansible-playbook` применит настройки до старта
VPS gateway.

`/voice on` остаётся настройкой конкретной TUI-сессии и в export не входит.
STT configuration и списки включённых toolsets входят. OAuth Nous Portal,
GitHub и другие авторизации намеренно не переносятся; войдите в них на VPS
отдельно, если это требуется.

## Границы изоляции

В обычном запуске container не получает каталоги хоста, Docker socket или устройства; terminal-
команды агента выполняются только в container filesystem. Hermes может иметь
`sudo` и writable root filesystem только в пределах этого container, чтобы
устанавливать запрошенные tools; это не даёт host privileges. Опубликованы только
локальные порты dashboard `127.0.0.1:9119` и Grafana `127.0.0.1:3000`; они
недоступны из сети. Prometheus и metrics provider остаются во внутренней
monitoring-сети. Одноразовый `metrics-storage-init` готовит права только
отдельного metrics volume. Metrics collector не имеет сети, читает Hermes
volume только read-only и записывает только агрегированный `.prom` файл в
отдельный volume.
Состояние хранится в отдельном Docker volume `hermes_local_test_data`.

Для Hermes root filesystem намеренно writable, а `sudo` действует только в его
container: это нужно для установки tools по запросу и изменения пропадают при
пересоздании. Остальные sidecars остаются read-only, capabilities сброшены и
для них включён `no-new-privileges`; всем сервисам заданы PID/CPU/RAM limits.
Сетевой egress оставлен для LLM, Telegram и GitHub API; он не даёт доступа к
файлам, процессам или Docker daemon хоста. Не передавайте в `local.env`
production credentials.

## Остановка и очистка

Остановить environment, сохранив данные:

```bash
(
  cd hermes/docker
  docker compose -f docker-compose.local.yml down
)
```

Полностью удалить локальное состояние:

```bash
(
  cd hermes/docker
  docker compose -f docker-compose.local.yml down --volumes
)
```
