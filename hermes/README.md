# Hermes Agent на VPS

Скрипт `deploy-hermes.sh` устанавливает официальный
[Hermes Agent от Nous Research](https://hermes-agent.nousresearch.com/docs/) на
Debian/Ubuntu VPS. Hermes работает от отдельного непривилегированного
пользователя `hermes`, а не от `root`.

## Используемые сервисы

Ниже — внешние сервисы, которые использует эта сборка или которые можно
подключить через её documented integrations. Наличие в списке не означает, что
аккаунт, ключ или платная подписка уже настроены.

| Сервис | Для чего нужен в этой сборке | Когда требуется |
|---|---|---|
| [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) | Основной AI-агент, gateway, terminal tools, skills и dashboard. | Обязателен. |
| [OpenRouter](https://openrouter.ai/docs/quickstart) | Модели LLM через один API key; основной provider в Ansible-примерах. | Нужен при выборе OpenRouter model. |
| [Nous Portal](https://portal.nousresearch.com/) | Альтернатива отдельным ключам: models и Tool Gateway для web, image, TTS и browser. | Опционально, для `deploy-hermes.sh --portal`. |
| [Telegram Bot API](https://core.telegram.org/bots) | Личный chat gateway и alerts Hermes. | Опционально, если нужен Telegram. |
| [Tailscale](https://tailscale.com/) | Приватная сеть и Tailscale SSH; позволяет закрыть публичный SSH. | Рекомендуется для VPS. |
| [GitHub](https://github.com/) | Клонирование репозиториев, commit/push, pull requests и GitHub CLI. | Опционально, для GitHub workflows. |
| [Google Workspace](https://workspace.google.com/) | Gmail, Calendar, Drive, Contacts, Docs и Sheets через OAuth. | Опционально, для Google tools. |
| [Brave Search API](https://brave.com/search/api/) | Web search по явно выданному API key. | Опционально, если не используется Nous Tool Gateway. |
| [Firecrawl](https://docs.firecrawl.dev/) | Извлечение и чтение HTML/PDF/web-страниц. | Опционально, для `web_extract`. |
| [FAL.ai](https://fal.ai/) | Генерация и редактирование изображений. | Опционально: нужен `FAL_KEY`, если не используется image tool Nous Portal. |
| [Grafana](https://grafana.com/docs/) | Локальные dashboards model/token/cost/VPS metrics. | Устанавливается при включённом ops-слое; не требует внешнего аккаунта. |
| [Prometheus](https://prometheus.io/docs/) | Локально собирает и хранит metrics для Grafana и Hermes. | Устанавливается при включённом ops-слое; не требует внешнего аккаунта. |
| [Docker Engine](https://docs.docker.com/engine/) | Сборка, запуск и администрирование контейнеров на VPS. | Устанавливается Ansible при `hermes_host_admin_enabled: true`; доступ Hermes через root-equivalent группу `docker`. |
| [Ansible Vault](https://docs.ansible.com/projects/ansible/latest/vault_guide/index.html) | Шифрует API keys и конфигурацию на Ansible controller. | Рекомендуется для повторяемого Ansible deploy. |

## Быстрый запуск

Путь от только что купленного VPS до работающего Hermes, шаг за шагом:

1. **Заказать VPS.** Для AlphaVPS используйте отдельную инструкцию
   [`VPS-ORDER.md`](VPS-ORDER.md): тариф P12G, Ubuntu 24.04 Minimal, один IPv4
   и London, если Nuremberg недоступен.
2. **Собрать credentials заранее, на своём компьютере — не на VPS.** Пройдите
   [`SECRETS-CHECKLIST.md`](SECRETS-CHECKLIST.md): там перечислены
   варианты model provider credentials, Telegram allowlist, Google OAuth, GitHub, SSH,
   Tailscale и backup credentials, а также правильное место хранения каждого
   типа данных. Не обязательно иметь всё сразу — мастер спросит недостающее
   по ходу установки.
3. **Зайти на свежий VPS по SSH** так, как дал провайдер (root-пароль или
   root SSH-ключ):
   ```bash
   ssh root@ip-адрес-сервера
   ```
4. **Скопировать папку `hermes` на сервер** — `scp -r hermes root@ip:/root/`
   или `git clone` репозитория прямо на VPS.
5. **Запустить установку:**
   ```bash
   cd /root/hermes # замените путь, если клонировали repository в другое место
   chmod +x deploy-hermes.sh
   sudo ./deploy-hermes.sh
   ```
   Если подключение выполняется одной SSH-командой без отдельного захода,
   нужен интерактивный терминал:
   ```bash
   ssh -t user@server 'cd /path/to/hermes && sudo ./deploy-hermes.sh'
   ```
6. **Пройти интерактивный мастер Hermes** — он спросит про недостающие ключи
   из шага 1. Если мастер спросит про тип gateway, выберите **System
   service** либо пропустите этот шаг: скрипт сам установит правильную
   systemd-службу для VPS.
7. **Дождаться конца установки.** Обычный запуск ставит Chromium, полезные
   CLI для разработки и администрирования, Google Workspace CLI, Tailscale,
   каталог проверенных Nous MCP, фоновый gateway, audit log, метрики,
   ежедневный backup, Telegram alerts и loopback-only web dashboard — всё
   сразу, без доп. действий.
8. **Проверить итог.** В конце скрипт сам прогоняет `hermes config check` и
   `hermes doctor` — предупреждения означают недонастроенную интеграцию, не
   ошибку установки. Что доделать руками (GitHub login, Google OAuth,
   подтверждение Tailscale) — раздел
   [«Что ещё стоит настроить»](#что-ещё-стоит-настроить) ниже.

Самый простой способ сразу получить модель, web search, генерацию изображений,
TTS и облачный браузер — Nous Portal, вместо шага 5:

```bash
cd /root/hermes # замените путь, если клонировали repository в другое место
sudo ./deploy-hermes.sh --portal
```

Для Portal необходима соответствующая подписка. Если она не нужна, используйте
обычный запуск и настройте собственные провайдеры в мастере.

Это ручной путь — для повторяемого/восстанавливаемого деплоя без ручных
заходов на VPS есть альтернатива через Ansible, см.
[«Infrastructure as Code и восстановление одной командой»](#infrastructure-as-code-и-восстановление-одной-командой).

## Локальная Docker-проверка

Инструкция, credentials, команды запуска и границы изоляции находятся в
[`docker/README.md`](docker/README.md).

## Паспорт текущей сборки

Этот раздел — короткая карта для владельца VPS и следующего AI-агента. Он
показывает, что уже реализовано, что устанавливается автоматически, где лежит
код и какие части ещё требуют credentials или решения владельца.

### Добавленные возможности

- **Отдельный runtime-user.** Hermes работает от пользователя `hermes`; на
  выделенном VPS ему по умолчанию выдан passwordless `sudo` для установки и
  настройки ПО по запросу владельца.
- **Разработка на VPS.** Агент может клонировать репозитории, создавать ветки,
  читать, создавать, изменять и удалять доступные ему файлы, запускать сборку и
  тесты, делать commit/push и создавать PR после авторизации GitHub.
- **OpenRouter LLM config.** Ansible Vault хранит `OPENROUTER_API_KEY` и
  управляет выбранной OpenRouter model через `config.yaml` overlay.
- **Web search с явными credentials.** `BRAVE_SEARCH_API_KEY` явно включает
  Brave Search; без ключа Hermes автоматически выбирает доступный Nous Tool
  Gateway либо другой явно настроенный web backend. Chromium остаётся локальным.
- **Зашифрованная доставка secrets.** Ansible Vault хранит API keys и bot
  tokens на управляющем компьютере в зашифрованном виде и при создании VPS
  автоматически формирует закрытый Hermes `.env` без вывода значений в лог.
- **Google Workspace.** Установлен `gws`, а bundled skill Hermes может работать
  с Gmail, Calendar, Drive, Contacts, Docs и Sheets после Google OAuth.
- **Telegram и другие messenger-платформы.** System gateway работает 24/7,
  стартует после reboot и перезапускается при сбое.
- **Web dashboard.** Полная установка поднимает `hermes-dashboard.service`;
  GUI слушает только `127.0.0.1:9119` и открывается через SSH/Tailscale tunnel.
- **Metrics прямо в Hermes GUI.** Вкладка **Metrics** читает только локальную
  observability SQLite базу и показывает model/token/cost/tool/health данные;
  она не открывает отдельный порт и не отдаёт secrets.
- **Обновление из чата.** Команда `/update` создаёт backup, обновляет Hermes и
  перезапускает активный gateway. Для локальных изменений используется
  stash/restore.
- **Приватная сеть.** Устанавливается Tailscale, включается Tailscale SSH;
  Ansible умеет после отдельной проверки закрыть публичный TCP/22 через UFW.
- **Health monitoring.** Каждые пять минут проверяются gateway, диск, inode,
  RAM, load, backup и свежесть файла метрик.
- **Telegram alerts без LLM.** Проблемы и сообщения о восстановлении
  отправляются через `hermes send`, поэтому регулярный мониторинг не тратит
  токены модели.
- **Уведомление о старте gateway.** После каждого запуска `hermes-gateway`
  отправляет в настроенный alert target имя VPS, model default и время запуска;
  недоставка уведомления не останавливает gateway.
- **Audit trail.** Записываются метаданные tool/API-вызовов, approvals,
  slash-команд и сессий. Содержимое писем, промптов, ответов и tool results не
  копируется в audit. Запись выполняется в фоновом потоке; audit ограничен
  текущим и одним предыдущим файлом по 5 MiB.
- **Метрики использования.** SQLite хранит вызовы по provider/model/tool,
  токены, latency, ошибки, команды и стоимость, если она известна. Hooks
  Hermes складывают события в ограниченную очередь и не ждут SQLite, disk или
  journald; при переполнении отбрасывается только telemetry, а не работа агента.
- **Отчёты без модели.** `/status`, `/ops` и `hermes-ops-report` анализируют
  локальные данные обычным кодом и не вызывают LLM. `/status` показывает
  gateway, токены последней активной сессии и общий учтённый расход; при
  нескольких параллельных сессиях выбор активной сессии приблизительный.
- **Grafana + Prometheus.** Полная установка поднимает versioned dashboard с
  30-дневной историей model/token/cost/tool и VPS metrics. Grafana,
  Prometheus и node exporter слушают только `127.0.0.1`; текущий textfile
  exporter остаётся источником Hermes-метрик.
- **Резервное копирование.** Первый и еженедельный backup — полный; между ними
  создаются ежедневные quick snapshots. Есть retention и контроль свежести.
- **Checkpoints.** Перед изменениями рабочего проекта Hermes может создавать
  локальные точки отката через `/rollback`.
- **Защита от runaway loops.** В одном turn разрешено не больше 20 web searches
  и 10 subagents; повторяющиеся ошибки останавливаются hard-stop механизмом.
- **Проверка coding-результата.** `verify_on_stop: auto` требует свежую
  проверку после изменений кода там, где это уместно.
- **Infrastructure as Code.** Ansible устанавливает новый VPS или импортирует
  полный Hermes backup вместе с config, auth, memory, sessions, profiles и
  skills.
- **Применение runtime-конфигурации.** После deploy systemd перечитывает units,
  а включённые gateway, dashboard, Prometheus, node-exporter, Grafana и
  monitoring timers перезапускаются. Deploy завершается ошибкой, если
  управляемая служба не вернулась в active state.
- **Расширяемость из чата.** Hermes может устанавливать user-level skills,
  plugins, MCP и project dependencies после approval. На выделенном VPS
  стандартная установка также даёт ему passwordless `sudo`, поэтому по явному
  запросу владельца он может менять `/etc`, firewall и systemd. Ansible также
  устанавливает Docker Engine и добавляет `hermes` в группу `docker`; это тоже
  root-equivalent доступ. Для ограниченного VPS задайте
  `hermes_host_admin_enabled: false` в Ansible или используйте
  `--without-host-admin` при прямой установке.

### Статус функций после запуска deploy

| Функция | Статус | Что ещё требуется |
|---|---|---|
| Файлы, terminal, Git, coding | Готово сразу | Права Unix на нужный workspace/repository |
| OpenRouter LLM | Готово после Vault deploy | Задать `OPENROUTER_API_KEY`, model и разумный `model.max_tokens` в `hermes_llm_config` |
| API keys через Ansible Vault | Готово | Создать и зашифровать локальный `group_vars/all/vault.yml` |
| Public GitHub clone | Готово сразу | Ничего |
| Private clone, push, PR, reviews, issues, Actions | Управляется Ansible | `GITHUB_TOKEN` с минимальными repo permissions в Vault; identity и access probe находятся в `vps_github` |
| Chromium/browser automation | Установлено | Иногда login конкретного сайта |
| `web_search` | Автоматический выбор backend Hermes | Для Brave задать `BRAVE_SEARCH_API_KEY`; либо войти в Nous Portal/настроить другой backend |
| Gmail и Calendar | CLI и skill готовы | Google Cloud project и OAuth consent |
| Telegram gateway | Автоматически стартует, если Vault содержит Bot token и allowlist | Для запуска задать оба Telegram значения в Vault |
| Web dashboard | Включён в полной установке | Открывать только через SSH/Tailscale tunnel на `127.0.0.1:9119` |
| `/update` и auto-restart | Включено | Писать `/update` только из разрешённого аккаунта |
| Tailscale | Установлено | Подтвердить login и проверить tailnet SSH policy |
| Закрытие публичного SSH | Подготовлено в Ansible | Сначала проверить вторую SSH-сессию, затем `hermes_lock_public_ssh: true` |
| Local backups | Включено | Следить за диском и тестировать restore |
| Audit, SQLite и `/ops` | Включено | При compliance отправлять journald во внешнее immutable/SIEM-хранилище |
| Стоимость моделей | Частично автоматически | Если provider не сообщает cost, заполнить `model-prices.json` |
| Grafana + Prometheus | Включено | Открывать через SSH/Tailscale tunnel на `127.0.0.1:3000`; password хранится root-only в `/etc/hermes-grafana.env` |
| LLM-анализ расходов | По запросу | Выбрать модель через `/model` и попросить проанализировать JSON report |
| MCP catalog | Picker открывается при deploy | Установить только реально нужные integrations после review |
| Skills Hub | Инициализируется при deploy | Внешние skills не устанавливаются |

### Установленные CLI и системные инструменты

Полная установка добавляет следующие команды. Они доступны Hermes через
terminal и не создают отдельные MCP schemas в model context.

| Группа | Команды/пакеты | Для чего нужны |
|---|---|---|
| База | `ca-certificates`, `curl`, `wget` | HTTPS, API и загрузка файлов |
| Git и GitHub | `git`, managed `gh`, `git-lfs`, `openssh-client` | clone/fetch/branch/commit/push/PR/reviews/issues/Actions и большие файлы |
| Поиск и данные | `ripgrep`, `jq`, `sqlite3` | Быстрый поиск, JSON и локальная аналитика |
| Сборка | `build-essential`, `pkg-config` | Компиляция C/C++ dependencies и многих language packages |
| Shell quality | `shellcheck` | Проверка Bash-скриптов |
| Проверка качества | `ansible-core`, `python3-pytest`, `python3-yaml` | Локальный запуск полного `hermes/check.sh` (синтаксис Ansible, pytest, YAML-скрипты) |
| Файлы и архивы | `file`, `tree`, `rsync`, `zip`, `unzip`, `xz-utils` | Диагностика, копирование и архивирование |
| Сеть и процессы | `dnsutils`, `lsof`, `netcat-openbsd`, `util-linux` | DNS, порты, процессы, locks и VPS diagnostics |
| Медиа | `ffmpeg` | Audio/video conversion и подготовка voice/media |
| Browser | Chromium/Playwright system libraries, fonts, `xvfb` | Headless browser и динамические сайты |
| Google | `gws` через Hermes-managed Node/npm | Gmail, Calendar, Drive, Docs, Sheets, Contacts |
| Private network | `tailscale`, `tailscaled` | Private WireGuard network и Tailscale SSH |
| Контейнеры (Ansible, host-admin) | `docker.io`, Docker Engine | Сборка, запуск и администрирование контейнеров без `sudo`; группа `docker` root-equivalent |
| Hermes | `hermes`, managed Python venv, managed Node/npm | Agent runtime, skills, plugins, MCP, gateway и update |

Docker Engine устанавливается только Ansible deploy при
`hermes_host_admin_enabled: true`; прямой `deploy-hermes.sh` его не ставит.
При следующем Ansible deploy с `hermes_host_admin_enabled: false` playbook
убирает `hermes` из группы `docker`; сам Docker Engine не удаляется.
Kubernetes, Terraform, cloud SDK, database servers и все существующие MCP не
устанавливаются заранее: они занимают диск, требуют обновлений и расширяют
права агента. Их добавляют только под конкретную задачу.

### Встроенные инструменты Hermes

Если в setup сохранён стандартный toolset, Hermes имеет инструменты для:

- чтения, создания, изменения и удаления файлов;
- terminal-команд, background processes и выполнения небольшого кода;
- web search, извлечения страниц и browser automation;
- памяти, поиска по сессиям и управления skills;
- cron jobs и долгих фоновых задач;
- planning, checkpoints и rollback;
- создания и координации subagents;
- image generation, TTS/STT и media при наличии соответствующего backend;
- MCP servers и plugins;
- Google Workspace через bundled skill;
- gateway для Telegram, Discord, Slack и других поддерживаемых платформ.

Наличие инструмента не означает наличие credentials. Provider API keys, OAuth,
GitHub permissions и messenger tokens подключаются отдельно.

### Папки `hermes` простым языком

- `ansible/` — повторяемая установка и обновление VPS по SSH.
  - `tasks/` — отдельные домены network, runtime и services; основной
    `playbook.yml` только задаёт порядок.
  - `group_vars/all/` — настройки для всех VPS. `vars.yml` содержит обычные
    aliases к общим defaults; только рабочий `vault.yml` содержит credentials
    и model config в зашифрованном виде. `vault.yml.example` — безопасный шаблон.
  - `templates/` — заготовки файлов, которые Ansible заполняет значениями при
    deploy, например закрытый `.env`.
- `config/vps-defaults.yml` — единый видимый файл важных non-secret настроек
  VPS: identity/paths, feature switches, Hermes runtime guardrails и группы
  systemd services для обязательного рестарта.
- `deploy/` — домены прямого deploy: host, runtime, services и reporting.
- `runtime/` — testable helpers, которые применяют общие настройки через
  официальный Hermes CLI.
- `docker/` — отдельная локальная Docker-сборка для разработки и проверки на
  компьютере; это не конфигурация Docker на VPS.
- `ops/` — обслуживание VPS: backups, health checks, метрики, browser verify и
  startup notifications.
  - `install/` — packages, plugin, assets и service lifecycle; верхний
    `install-ops.sh` только валидирует arguments и оркестрирует эти домены.
  - `plugin/` — код plugin Hermes, который считает usage и добавляет `/ops`.
  - `systemd/` — unit-файлы служб и timers для Linux VPS.
  - `templates/` — шаблоны настроек мониторинга и fallback цен моделей.
- `observability/` — Grafana dashboards и Prometheus configuration.
- `deploy-hermes.sh` — прямой установщик VPS без Ansible.
- `README.md`, `SECRETS-CHECKLIST.md`, `VPS-ORDER.md`, `VPS-BACKLOG.md` —
  инструкции, список secrets, заказ VPS и backlog улучшений.

### Общая карта исходных файлов

Эти versioned каталоги используются и Docker-проверкой, и VPS deployment там,
где это указано в таблице. Рабочие credentials, volume state и VPS runtime
файлы в Git не хранятся.

| Файл или каталог | Ответственность |
|---|---|
| [`config/vps-defaults.yml`](config/vps-defaults.yml) | Единый источник важных non-secret deploy/runtime настроек и групп перезапускаемых services |
| [`deploy-hermes.sh`](deploy-hermes.sh) | Тонкий оркестратор прямой установки; реализация доменов находится в [`deploy/`](deploy) |
| [`runtime/apply-config.py`](runtime/apply-config.py) | Идемпотентно применяет общие runtime settings через Hermes CLI и выдаёт service groups |
| [`runtime/verify-update-state.py`](runtime/verify-update-state.py) | Fail-closed проверяет full backup, SQLite integrity и Kanban counts до/после managed update |
| [`SECRETS-CHECKLIST.md`](SECRETS-CHECKLIST.md) | Единый checklist обязательных и optional tokens, OAuth, SSH и backup-данных без настоящих значений |
| [`ops/install-ops.sh`](ops/install-ops.sh) | Тонкий оркестратор ops installation; packages, plugin, assets и services разделены в [`ops/install/`](ops/install) |
| [`ops/plugin/ops-observability`](ops/plugin/ops-observability) | Hermes plugin hooks, audit, SQLite accounting и `/ops` |
| [`ops/health-check.sh`](ops/health-check.sh) | Host/gateway/backup/metrics checks, deduplication и recovery alerts |
| [`ops/backup.sh`](ops/backup.sh) | Daily quick, weekly full и local retention |
| [`ops/export-metrics.py`](ops/export-metrics.py) | Prometheus textfile exporter |
| [`observability/`](observability) | Versioned Prometheus scrape config и Grafana datasource/dashboard provisioning |
| [`ops/ops-report.py`](ops/ops-report.py) | Read-only Markdown/JSON отчёты из SQLite |
| [`ops/status-report.py`](ops/status-report.py) | Компактный `/status` без LLM: gateway, токены активной сессии и общий учтённый расход |
| [`ops/startup-notify.sh`](ops/startup-notify.sh) | Нефатальное сообщение в alert target после запуска gateway: VPS, default model и время |
| [`ops/systemd`](ops/systemd) | Hardened services и timers для backup, health, metrics и startup notification |
| [`ops/templates/hermes-ops.conf`](ops/templates/hermes-ops.conf) | Пороги, paths, retention и alert target по умолчанию |
| [`ops/templates/model-prices.json`](ops/templates/model-prices.json) | Fallback-цены моделей за 1M tokens |
| [`docker/`](docker) | Локальный Docker image c GitHub CLI, Compose, bootstrap и ignored `local.env` для provider, Telegram и GitHub credentials |
| [`docker/AGENTS.md`](docker/AGENTS.md) | Поведение Hermes в local container: `sudo` только внутри container, без доступа к Docker host или macOS |
| [`ansible/playbook.yml`](ansible/playbook.yml) | Порядок provision/restore; network, runtime и services вынесены в [`ansible/tasks/`](ansible/tasks) |
| [`ansible/tasks/github.yml`](ansible/tasks/github.yml) | Git/GitHub packages, identity, credential helper, private-repository probe и managed workflow instructions |
| [`ansible/AGENTS.md`](ansible/AGENTS.md) | Поведение Hermes на выделенном VPS: полный `sudo`, запрет удаления и обращения с secrets |
| [`ansible/inventory.example.ini`](ansible/inventory.example.ini) | Шаблон inventory; рабочий файл — `ansible/inventory.ini`, он игнорируется Git |
| [`ansible/group_vars/all/vars.yml`](ansible/group_vars/all/vars.yml) | Публичные IaC defaults без credentials |
| [`ansible/group_vars/all/vault.yml.example`](ansible/group_vars/all/vault.yml.example) | Шаблон private API keys и tokens; рабочий файл — `ansible/group_vars/all/vault.yml`, он шифруется и игнорируется Git; команды находятся в [`VAULT.md`](ansible/group_vars/all/VAULT.md) |
| [`ansible/templates/hermes.env.j2`](ansible/templates/hermes.env.j2) | Безопасно формирует Hermes `.env` из расшифрованных только на время deploy значений и нормализует GitHub token alias |
| [`runtime/github-cli-wrapper.py`](runtime/github-cli-wrapper.py) | Передаёт `gh` только managed GitHub token из Hermes environment без отдельного plaintext credential store |
| [`check.sh`](check.sh) | Одна локальная и CI-команда для Bash, Python tests, Ansible syntax и whitespace |

### Единый файл критических настроек VPS

Меняйте [`config/vps-defaults.yml`](config/vps-defaults.yml), когда настройка
должна одинаково применяться повторными Ansible deploy:

- `vps_deploy` — пользователь, paths, зафиксированные version/release/commit
  Hermes, SHA-256 installer и критические feature switches;
- `vps_runtime.set` — обязательные Hermes runtime defaults;
- `vps_runtime.set_if_missing` — безопасные fallback-значения, не
  перезаписывающие явный выбор;
- `vps_runtime.unset` — опасные или устаревшие overrides, которые deploy
  удаляет;
- `vps_runtime.capabilities` — backend, включаемый только при наличии
  соответствующего Vault credential;
- `vps_services` — application-owned units, которые верхний deploy
  перезапускает после установки всех файлов и config.

Secrets в этот файл добавлять нельзя: они остаются только в Ansible Vault или
закрытом Hermes `.env`. Параметры командной строки `deploy-hermes.sh` остаются
одноразовыми overrides для ручной установки.

### Runtime paths на VPS

| Путь | Владелец | Содержимое |
|---|---|---|
| `/home/hermes/.hermes` | `hermes` | Config, auth, sessions, memory, skills, plugins и ops database |
| `/home/hermes/.hermes/hermes-agent` | `hermes` | Checkout и runtime-файлы установленного Hermes; обновляется командой Hermes |
| `/home/hermes/.hermes/.env` | `hermes`, mode `0600` | API keys и messenger tokens, доставленные из Ansible Vault либо мастером Hermes |
| `/home/hermes/.local/bin/hermes` | `hermes` | Hermes CLI launcher, вызываемый systemd и из SSH |
| `/home/hermes/workspace` | `hermes` | Репозитории и рабочие файлы агента |
| `/home/hermes/workspace/AGENTS.md` | `hermes` | Скопированная инструкция из `ansible/AGENTS.md` для VPS-администрирования |
| `/home/hermes/hermes-backups` | `hermes` | Local quick/full zip archives |
| `/opt/hermes-bootstrap` | `root` | Временный versioned bundle, который Ansible копирует на VPS для deploy и ops installation |
| `/home/hermes/.hermes/logs/ops-audit.jsonl` | `hermes`, mode `0600` | Privacy-aware local audit |
| `/home/hermes/.hermes/ops/metrics.db` | `hermes`, mode `0600` | SQLite model/tool/activity accounting |
| `/home/hermes/.hermes/ops/metrics/hermes.prom` | `hermes`, mode `0600` | Prometheus textfile, читается loopback-only Hermes node exporter от имени `hermes` |
| `/var/lib/hermes-prometheus` | `prometheus` | 30-day локальная time-series база |
| `/etc/hermes-grafana.env` | `root`, mode `0600` | Grafana admin credentials: Ansible materializes from Vault; direct installer generates them once without printing |
| `/etc/hermes-ops.conf` | `root` | Monitoring thresholds и paths, без secrets |
| `/usr/local/lib/hermes-ops` | `root` | Установленные immutable ops scripts |
| `/etc/systemd/system/hermes-*.service` | `root` | Backup, metrics и health services |

### Контракт для дальнейших улучшений

Следующему агенту следует сохранять эти правила:

1. На VPS `sudo` и Docker socket разрешены только пользователю `hermes` для
   владельческого администрирования. Оба механизма root-equivalent; доступ к
   Docker socket или root других hosts не выдавать.
2. Не открывать dashboard, API, Grafana, Prometheus или SSH публично. Использовать
   `127.0.0.1`, Tailscale и SSH tunnels.
3. Не писать prompts, email bodies, assistant responses, tool results, raw API
   errors и slash-command arguments в audit/metrics.
4. Не помещать tokens, OAuth JSON, SSH private keys, `.env`,
   `inventory.ini`, `group_vars/all/vault.yml`, vault password или backup
   archives в Git.
5. Не ставить дублирующие filesystem/browser/web/Google/GitHub MCP, если
   встроенный tool, bundled skill или CLI уже решает задачу.
6. Новый MCP/plugin сначала inspect/audit, затем минимальные permissions и
   только после этого enable. Удалять неиспользуемые integrations.
7. Новый system service должен работать с минимальным пользователем и правами,
   иметь systemd hardening, health signal, logs, README и понятный rollback.
8. Новую регулярную проверку сначала делать обычным script/SQL. Использовать
   LLM только если действительно нужен смысловой анализ.
9. Новые packages добавлять в соответствующий список deploy с проверкой через
   `apt-cache`; тяжёлые SDK оставлять optional.
10. Изменения SQLite делать обратно совместимо: `CREATE IF NOT EXISTS`,
    additive migrations и отсутствие high-cardinality Prometheus labels вроде
    session ID.
11. Сохранять retry alerts: state обновляется только после успешной доставки.
12. Не закрывать публичный SSH, пока владелец не проверил отдельную Tailscale
    SSH-сессию и tailnet access policy.
13. После изменения plugin запускать `hermes plugins doctor ... --ci`, а после
    изменения units — `systemd-analyze verify` на Linux.
14. Обновлять этот паспорт, таблицу flags и команды проверки вместе с кодом.

Единая локальная проверка перед передачей изменений:

```bash
bash hermes/check.sh
```

CI запускает тот же сценарий в строгом режиме для каждого изменения
`hermes/**`:

```bash
bash hermes/check.sh --require-tools
```

После установки на Debian/Ubuntu VPS дополнительно:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes \
  plugins doctor /home/hermes/.hermes/plugins/ops-observability --ci
sudo systemd-analyze verify /etc/systemd/system/hermes-*.service
sudo systemctl start hermes-backup.service
sudo systemctl start hermes-metrics.service
sudo systemctl start hermes-health.service
sudo systemctl list-timers 'hermes-*'
sudo -u hermes HERMES_HOME=/home/hermes/.hermes hermes-ops-report --period 24h
```

### Приоритеты для следующих улучшений

Актуальный, дополняемый backlog находится в [VPS-BACKLOG.md](VPS-BACKLOG.md).
В нём зафиксированы приоритеты, критерии готовности, риски, rollback и шаблон
для новых задач. Не начинайте задачу со статусом `idea` без отдельного решения
владельца.

## Что включается сразу

### Отдельный пользователь и граница привилегий

Hermes работает как отдельный пользователь `hermes`. На выделенном VPS
стандартная установка выдаёт ему passwordless `sudo`, то есть root-equivalent
доступ для администрирования хоста по явному запросу владельца. Чтобы оставить
его без системных привилегий, используйте `--without-host-admin` либо задайте
`hermes_host_admin_enabled: false` в Ansible; тогда он не сможет напрямую
изменять системные файлы VPS.

### Production-контур: мониторинг, audit и метрики

Полная установка сразу добавляет лёгкий локальный operations-слой:

| Что работает | Простое объяснение | Периодичность |
|---|---|---:|
| `hermes-health.timer` | Проверяет gateway, диск, inode, свободную RAM, load и свежесть backup | 5 минут |
| `hermes-metrics.timer` | Обновляет Prometheus-файл с состоянием VPS и активностью Hermes | 1 минута |
| `hermes-node-exporter.service` | Читает host и Hermes textfile metrics только на `127.0.0.1:9100` | постоянно |
| `hermes-prometheus.service` | Собирает локальные metrics, хранит 30 дней, слушает `127.0.0.1:9090` | постоянно |
| `grafana-server.service` | Versioned Hermes dashboard на `127.0.0.1:3000` | постоянно |
| `hermes-backup.timer` | Делает daily quick и первый/еженедельный full backup | 1 день |
| `hermes-startup-notify.service` | После каждого запуска gateway отправляет VPS, default model и время в alert target; ошибка доставки не влияет на gateway | на каждый старт gateway |
| `ops-observability` | Считает вызовы моделей/tools/команд, токены, ошибки, latency и стоимость | по событиям |
| logrotate | Сжимает и хранит 30 ротаций audit log | ежедневно |

Health check отправляет сообщения через `hermes send`, без запуска LLM. Поэтому
проверка каждые пять минут не расходует токены. Alert отправляется только при
появлении новой проблемы и при восстановлении; одинаковые сообщения не
повторяются. Если доставка не сработала, следующая проверка повторит попытку.

Telegram должен быть настроен в gateway. Для проверки можно специально
запустить службы вручную:

```bash
sudo systemctl start hermes-backup.service
sudo systemctl start hermes-metrics.service
sudo systemctl start hermes-health.service
sudo journalctl -u hermes-health.service -n 50 --no-pager
```

Пороги находятся в `/etc/hermes-ops.conf`. Их можно менять без редактирования
скриптов. После изменения достаточно дождаться следующего запуска timer.

В Telegram доступны отчёты, которые читают SQLite напрямую и тоже не вызывают
модель:

```text
/ops summary 24h
/ops system 24h
/ops models 7d
/ops tools 24h
/ops costs 30d
/ops commands 7d
/ops health
```

Когда в обычном диалоге вы спрашиваете о состоянии Hermes, VPS, расходе
токенов, tool errors или стоимости, агенту доступен read-only tool
`ops_metrics`. Он получает только заранее заданные агрегаты из локальной
SQLite-базы и private Prometheus: gateway, memory, disk, load, calls, tokens,
costs, модели и tools. Произвольный PromQL, URL, записи в Grafana/Prometheus и
изменения VPS этим tool недоступны. На VPS он обращается только к
`http://127.0.0.1:9090`; в Docker — к внутреннему имени `prometheus`, без
новых открытых портов.

В Dashboard есть такая же read-only вкладка **Metrics**: выберите период 24h,
7d или 30d, чтобы посмотреть calls, tokens, cost, модели, tools и состояние
health. Вкладка появляется автоматически после полного deploy; при первом
запуске она заполнится после первых событий Hermes.

Из SSH тот же отчёт:

```bash
sudo -u hermes HERMES_HOME=/home/hermes/.hermes \
  hermes-ops-report --period 7d --format markdown
```

Prometheus textfile создаётся в
`/home/hermes/.hermes/ops/metrics/hermes.prom`. Полная установка автоматически
поднимает для него отдельный node exporter, Prometheus и Grafana, но каждый
слушает только loopback. Поэтому новый публичный порт не появляется.

Запись событий выполняется асинхронно: фоновый worker объединяет до 64 записей
в одну SQLite transaction, обычно не дольше 50 ms. Очередь ограничена 512
событиями; при кратковременном переполнении будут пропущены только отдельные
наблюдения, а Hermes не будет ждать диск. Локальный audit хранит два файла по
5 MiB; при необходимости предел можно изменить через
`HERMES_OBSERVABILITY_AUDIT_MAX_BYTES` (от 64 KiB до 100 MiB).

Стоимость берётся из ответа провайдера, если он её сообщает. Для остальных
моделей можно заполнить
`/home/hermes/.hermes/ops/model-prices.json` актуальными ценами за миллион
токенов. В `api_calls` каждая запись хранит `cost_source`: `provider` — цену
сообщил сам провайдер (в том числе честный `0`, например у кэш-хита или
бесплатного тира), `price-file` — цена посчитана по `model-prices.json`,
`unavailable` — провайдер цену не сообщил и в `model-prices.json` нет записи
для этой модели (тогда `cost_usd` остаётся `0`, но это означает «цена
неизвестна», а не «вызов бесплатный»). У самого Hermes также есть команда
`hermes insights` для штатной аналитики.

Автоматический LLM-анализ каждую минуту намеренно не включён — он постоянно
тратил бы деньги и токены. Когда нужны выводы, выберите нужную модель встроенной
командой `/model` и попросите: «запусти `hermes-ops-report --period 7d --format
json`, сравни модели и команды, найди аномалии и предложи экономию». Так один
осмысленный анализ делается по требованию, а сбор данных остаётся бесплатным.

### Audit log действий агента

Audit хранится в:

```text
/home/hermes/.hermes/logs/ops-audit.jsonl
```

Каждая строка — отдельное JSON-событие: время, session/turn ID, модель,
провайдер, имя tool, длительность, результат approval и slash-команда. Для
terminal сохраняется сокращённая команда с маскированием типичных secrets.
Намеренно не записываются тексты чатов и писем, ответы модели, результаты tools,
сырые provider errors и аргументы slash-команд. Session/turn ID связывает audit
с историей Hermes, если нужно понять контекст «почему», не дублируя личные
данные в отдельном логе.

Best-effort копия метаданных также отправляется в root-managed system journal:

```bash
sudo journalctl -g hermes_audit --since today
```

Локальный audit полезен для диагностики, но пользователь `hermes` владеет своим
state и теоретически может его изменить. Для compliance или защиты от
скомпрометированного агента отправляйте journald в отдельное append-only/SIEM
хранилище; это требует выбранного вами внешнего сервиса и поэтому не включается
автоматически.

```bash
sudo -u hermes tail -f /home/hermes/.hermes/logs/ops-audit.jsonl
sudo -u hermes sqlite3 /home/hermes/.hermes/ops/metrics.db '.tables'
```

### Приватный доступ через Tailscale

Скрипт подключает официальный apt repository Tailscale, запускает `tailscaled`
и выполняет `tailscale up --ssh`. Откройте показанную ссылку и авторизуйте VPS.
После этого используйте Tailscale IP или MagicDNS-имя:

```bash
tailscale ip -4
ssh hermes@100.x.y.z
```

Скрипт не закрывает публичный SSH автоматически: сделать это до проверки
Tailscale означало бы риск потерять доступ к VPS. Сначала откройте вторую SSH
сессию через Tailscale, проверьте reboot, затем в firewall/cloud security group
закройте публичный TCP/22. Dashboard и внутренний API также слушайте только на
`127.0.0.1` и открывайте через Tailscale/SSH tunnel.

В Ansible для уже проверенного сервера можно задать
`hermes_lock_public_ssh: true`: playbook сначала проверит Tailscale IP, затем
включит UFW, разрешит SSH только через `tailscale0` и запретит TCP/22 на
публичных интерфейсах. Для первого запуска оставьте `false`, иначе ошибка в
tailnet policy или auth key может отрезать административный доступ. Отдельно
закройте порт 22 в cloud firewall/security group провайдера VPS.

Для неинтерактивной установки используйте `--skip-tailscale-login`, затем
выполните `sudo tailscale up --ssh`. Полностью отказаться можно флагом
`--without-tailscale`.

### Управляемое обновление и автоподъём

Production VPS обновляется повторным запуском Ansible playbook. Playbook
сравнивает установленный commit с `vps_deploy.source.commit` и запускает
обновление только при расхождении. Перед изменением кода deploy обязательно:

1. останавливает managed gateway;
2. запускает `PRAGMA integrity_check` для `kanban.db` и всех
   `kanban/boards/**/kanban.db`;
3. записывает counts задач по статусам;
4. создаёт полный backup и проверяет ZIP CRC, полноту Kanban DB и counts внутри
   архива;
5. после обновления повторяет integrity/counts и требует точного совпадения.

Любая ошибка до установки прекращает обновление и возвращает прежний gateway в
работу. Ошибка после начала установки прекращает дальнейший deploy и оставляет
gateway остановленным для безопасного ручного разбора. Отчёты и архив находятся
в `/home/hermes/hermes-backups/pre-deploy-*`. После применения config, ops и
timers Ansible перезапускает включённый gateway последней изменяющей операцией,
а затем проверяет, что service находится в состоянии `active`.

Запускайте управляемое обновление с Ansible controller:

```bash
ansible-playbook -i hermes/ansible/inventory.ini \
  hermes/ansible/playbook.yml --ask-vault-pass
```

Встроенная команда Telegram:

```text
/update
```

использует upstream updater и не проходит через обязательную проверку архива и
сравнение всех Kanban DB. Для production VPS её не используйте.

Из SSH доступны read-only проверка версии и отдельный ручной полный backup:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes update --check
sudo -u hermes -H /home/hermes/.local/bin/hermes backup
sudo systemctl status hermes-gateway.service
```

Локальные изменения исходников Hermes при неинтерактивном обновлении
автоматически stash/restore, а не удаляются.

### Infrastructure as Code и восстановление одной командой

В папке `ansible` лежит idempotent playbook. Он может при первом создании VPS
автоматически доставить credentials любого LLM provider, Telegram, web-search и
других integrations. Тем же Vault можно управлять LLM-разделами
`config.yaml`: built-in и named custom providers, основной моделью,
fallback-цепочками и provider-specific options. Секреты расшифровываются на
управляющем компьютере только во время запуска. `.env` и `config.yaml`
записываются с владельцем `hermes` и mode `0600`; чувствительные Ansible tasks
используют `no_log: true` и отключённый diff.

Полные шаги и команды для создания, шифрования, редактирования и применения
`group_vars/all/vault.yml` находятся рядом с файлом в
[`ansible/group_vars/all/VAULT.md`](ansible/group_vars/all/VAULT.md). Шаблон
значений — [`vault.yml.example`](ansible/group_vars/all/vault.yml.example).

Основной deploy запускается из каталога `hermes`:

```bash
ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml \
  --ask-vault-pass \
  --ask-pass
```

Флаг `--ask-pass` нужен при SSH-входе по паролю. При настроенном
`ansible_ssh_private_key_file` запускайте ту же команду без него.

`hermes_llm_config` — shallow authoritative overlay: каждый присутствующий в
нём верхнеуровневый раздел полностью заменяет такой же раздел в существующем
`config.yaml`, а неуказанные разделы сохраняются. API key храните только в
`hermes_secret_env`; не используйте inline `api_key` в `config.yaml`.

По умолчанию `hermes_host_admin_enabled: true`, поэтому playbook создаёт
`/etc/sudoers.d/hermes-host-admin`, устанавливает Docker Engine и добавляет
Hermes в группу `docker`. Поэтому Hermes сможет устанавливать пакеты, запускать
контейнеры и системные services по вашему запросу без отдельного SSH-вмешательства.
И `sudo`, и Docker group дают полный root-equivalent доступ; правило в его
`AGENTS.md` запрещает удаление данных, но не может технически ограничить root.
Задайте `false`, если нужен ограниченный VPS.

При `hermes_manage_secret_env: true` Vault становится источником истины для
всего Hermes `.env`: ручные изменения на VPS будут заменены следующим запуском
playbook. Добавление и ротация ключей описаны в
[`ansible/group_vars/all/VAULT.md`](ansible/group_vars/all/VAULT.md).

Если Vault содержит оба значения `TELEGRAM_BOT_TOKEN` и
`TELEGRAM_ALLOWED_USERS`, playbook автоматически поднимет gateway уже с
обновлёнными ключами. Если хотите продолжать вводить credentials вручную через
`hermes model`, оставьте `hermes_manage_secret_env: false` и не создавайте
`vault.yml`.

SSH private key работает иначе: он всегда остаётся на управляющем компьютере.
В cloud-init/VPS добавляется только соответствующий public key, а в
`inventory.ini` при необходимости указывается локальный путь через
`ansible_ssh_private_key_file`. Никогда не копируйте SSH private key в Hermes
`.env` или на сам VPS.

`tailscale_auth_key` в том же Vault — отдельный, необязательный секрет.
Playbook подключает VPS к tailnet командой `tailscale up --ssh`, а Ansible
неинтерактивен: без auth key эта команда выведет ссылку авторизации в браузер
и зависнет, ждать клик по ссылке в headless-прогоне некому. Auth key даёт
playbook залогиниться самому — задача сработает только если ключ не пустой И
сервер ещё не в tailnet. Возьмите его в [Tailscale admin console → Settings →
Keys](https://login.tailscale.com/admin/settings/keys); рекомендуется
generate ephemeral и/или tag-scoped ключ, а не reusable-навсегда, так как он
хранится как обычный секрет в `vault.yml`. Если оставить `tailscale_auth_key`
пустым, playbook пропустит этот шаг — тогда `tailscale up --ssh` нужно будет
один раз выполнить на VPS руками и авторизовать по ссылке, как делает
`deploy-hermes.sh` при обычном (не-Ansible) запуске.

Playbook устанавливает Hermes, Tailscale, operations-слой и systemd units. Для
полного восстановления с конфигурацией, OAuth/API credentials, memory,
сессиями, profiles и skills передайте полный архив `hermes backup`:

```bash
ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml \
  --ask-vault-pass \
  --ask-pass \
  -e hermes_backup_archive=/secure/hermes-backup.zip
```

Это одна команда восстановления, но сам backup должен уже находиться в
защищённом хранилище на Ansible controller. Без backup playbook восстанавливает
инфраструктуру и environment-secrets из Vault, но не может восстановить OAuth
sessions, memory и другие данные, которых нет ни в Vault, ни в backup.

Если нужно перенести только проверенные настройки (STT, model, web и toolsets),
а не OAuth/sessions/memory, используйте безопасный экспортёр из
[`docker/README.md`](docker/README.md#перенос-проверенных-настроек-на-vps-без-секретов).

[Официальная документация Ansible Vault](https://docs.ansible.com/projects/ansible/latest/vault_guide/index.html)

### Полный набор встроенных инструментов

Стандартный toolset `hermes-cli` содержит работу с файлами, terminal, web,
браузером, памятью, skills, изображениями, планами, фоновыми процессами,
подагентами, выполнением кода и cron. Реальная доступность web, изображений и
других внешних инструментов зависит от выбранных провайдеров и ключей. Чтобы
сохранить этот полный набор, не выбирайте в мастере режим **Blank Slate**.

Проверить и изменить инструменты:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes tools
```

[Описание toolsets](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference)

### Готовые CLI для кода, GitHub, Google и VPS

Полная установка добавляет инструменты, которыми Hermes может пользоваться из
terminal без отдельного MCP:

| Инструмент | Простое объяснение |
|---|---|
| `git`, managed `gh`, Git LFS, SSH | Клонировать GitHub-репозитории, создавать ветки, коммиты, PR/reviews/issues, читать Actions и делать push |
| `gws` | Работать с Gmail, Calendar, Drive, Contacts, Sheets и Docs через Google OAuth |
| `build-essential`, `pkg-config`, `shellcheck` | Собирать проекты и проверять shell-скрипты |
| `jq`, SQLite | Читать JSON и работать с локальными базами данных |
| `rsync`, `zip`, `unzip`, `wget` | Копировать, скачивать и архивировать файлы |
| `dig`, `lsof`, `nc`, `file`, `tree` | Диагностировать сеть, процессы, порты и структуру файлов |
| `ffmpeg`, `ripgrep` | Обрабатывать аудио/видео и быстро искать по исходному коду |

Для Git автоматически включаются безопасные настройки: ветка `main` по
умолчанию, prune веток/tags, fast-forward-only pull и автоматическая привязка
новой ветки при первом push. Ansible берёт commit identity, GitHub owner,
workspace и write boundaries из `vps_github` в едином файле
[`config/vps-defaults.yml`](config/vps-defaults.yml).

### Web search и работа с интернетом

Hermes может искать информацию, извлекать текст со страниц и работать с
интерактивными сайтами. Chromium и его системные библиотеки устанавливаются
автоматически. Terminal и Chromium имеют обычный исходящий доступ в интернет
через сеть VPS; входящие публичные порты скрипт не открывает.

Для Brave `web_search` добавьте `BRAVE_SEARCH_API_KEY` в
`hermes_secret_env` Vault. Имя backend в Hermes — `brave-free`, но это не
означает отсутствие API key. Deploy больше не закрепляет этот backend, если
ключа нет: Hermes автоматически выберет Nous Tool Gateway после OAuth либо
другой явно настроенный web backend. При наличии ключа deploy выбирает Brave
для поиска, не затрагивая общий backend извлечения. Brave — search-only; для чтения страниц
используйте установленный Chromium или отдельно настройте extract-capable
backend (Nous Tool Gateway, Firecrawl, Tavily, Exa или Parallel).

Важно: результат `web_search` — это только список ссылок и snippets, а не
содержимое страниц. Если Hermes должен найти CV на сайте и проанализировать
его, ему требуется extract-capable backend либо рабочий Chromium; для
приватного Google Drive также нужен Google OAuth. Инструкции, устанавливаемые
в workspace, запрещают выдавать сведения из сниппета за прочитанный документ
и требуют честно сообщать, какая именно стадия чтения недоступна.

Для интерактивных сайтов deploy дополнительно ставит локальный
[`agent-browser`](https://github.com/vercel-labs/agent-browser) и его
изолированный Chrome for Testing. Это бесплатный browser runtime на VPS:
Hermes использует его для навигации, кликов, форм и JavaScript-страниц. Он не
обходит CAPTCHA, paywall или авторизацию. При каждом deploy запускается
реальный цикл `open → snapshot → close` с фактическими launch-параметрами;
ошибка проверки останавливает playbook вместо неявно работающего браузера.

Для этого VPS playbook задаёт `browser.backend: off`: это отключает Browser Use
CLI и оставляет встроенные `browser_*` инструменты Hermes поверх проверенного
`agent-browser`. Причина — Browser Use CLI на headless VPS мог быть установлен,
но не запускать собственный Chrome harness. Не меняйте это значение, пока не
появится успешная live-проверка именно `browser_exec`.

На Ubuntu 23.10+ некоторые VPS-образы блокируют sandbox Chromium через
AppArmor. Поэтому deploy задаёт для отдельного непривилегированного пользователя
`hermes` параметры `--no-sandbox,--disable-dev-shm-usage`. Они позволяют
запустить Chrome for Testing, но уменьшают изоляцию процессов браузера: не
используйте этот браузер для ввода личных паролей и не открывайте неизвестные
файлы как доверенные.

Настройка и проверка:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes tools
sudo -u hermes -H /home/hermes/.local/bin/hermes status
```

[Web Search & Extract](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search)

### Браузерная автоматизация

Hermes получает Chromium и может открывать сайты, нажимать кнопки, заполнять
формы и читать динамические страницы. Для обычного поиска лучше использовать
`web_search`, а браузер оставлять для сайтов, где необходимо взаимодействие.

### Gmail, Google Calendar и другие Google-сервисы

Скрипт устанавливает `gws`, а официальный bundled skill Hermes уже умеет
использовать Gmail, Calendar, Drive, Contacts, Sheets и Docs. OAuth-токены
обновляются автоматически. Отдельный Google MCP не ставится: он дублировал бы
этот skill, добавлял лишние tool-схемы в контекст и тратил больше токенов.
По той же причине не нужно отдельно импортировать весь каталог skills из
репозитория `gws`.

После установки откройте Hermes и напишите:

```text
Настрой Google Workspace для Gmail и Calendar
```

Hermes проведёт по шагам:

1. Создать проект в Google Cloud.
2. Включить нужные API: Gmail, Calendar и только те дополнительные сервисы,
   которыми вы действительно будете пользоваться.
3. Создать OAuth 2.0 credentials типа **Desktop app** и загрузить JSON.
4. Открыть выданную ссылку, разрешить доступ и вернуть redirect URL в Hermes.

Не кладите OAuth JSON и токены в Git-репозиторий. Проверяйте список разрешений
на экране Google. Skill требует явного подтверждения перед отправкой письма,
созданием или удалением события, удалением/публикацией Drive-файла и изменением
Docs/Sheets.

[Официальная инструкция Google Workspace skill](https://hermes-agent.nousresearch.com/docs/user-guide/skills/google-workspace)

`gws` выпускается организацией Google Workspace, но не считается официально
поддерживаемым продуктом Google. Он пока развивается до версии 1.0 и может иметь
несовместимые обновления. Если после обновления команда перестала работать,
сначала обновите Hermes и попросите его использовать bundled Python fallback.

### GitHub: clone, файлы, commit, push и PR

Hermes использует встроенный terminal и bundled skills `github-auth`,
`github-code-review`, `github-issue-to-pr`, `github-issues`,
`github-pr-workflow` и `github-repo-management`. Отдельный GitHub MCP не нужен.
`git` выполняет локальные операции, а `gh` — PR, unresolved review threads,
issues, Actions и API-запросы.

Для Ansible deployment задайте fine-grained PAT в encrypted Vault:

```yaml
hermes_secret_env:
  GITHUB_TOKEN: "replace-inside-ansible-vault"
```

В `vps_github` задаются ожидаемый login, default owner, commit identity,
repository workspace, write owners и приватный access probe. Каждый deploy
устанавливает `git`/`gh`/LFS/SSH/`jq`/`ripgrep`/`rsync`, проверяет GitHub login,
доступ к probe repository и HTTPS clone/fetch. Managed `gh` wrapper получает
токен из Hermes `.env` во время запуска и не создаёт второй plaintext token
store. После изменения Vault gateway перезапускается и получает новый token.

Рекомендуемые fine-grained permissions: Metadata read, Contents read/write,
Pull requests read/write, Issues read/write и Actions read. Workflows
read/write добавляйте только если Hermes должен изменять `.github/workflows`.
Токен ограничьте владельцем `YauheniPo` и только нужными repositories.

Проверка после deploy не раскрывает token:

```bash
sudo -u hermes -H /home/hermes/.local/bin/gh auth status
sudo -u hermes -H /home/hermes/.local/bin/gh api user --jq .login
sudo -u hermes -H /home/hermes/.local/bin/gh repo view YauheniPo/popot-bot-2.0
```

Workspace `AGENTS.md` предписывает сохранять dirty worktrees, работать через
отдельную branch + PR, учитывать unresolved review threads и не выполнять
опасные repository/admin операции без явного разрешения владельца.

### Работа 24/7

Скрипт устанавливает `hermes-gateway.service`. Gateway:

- запускается вместе с VPS;
- автоматически перезапускается после сбоя;
- обслуживает Telegram, Discord, Slack и другие платформы;
- проверяет cron-задачи каждую минуту;
- использует systemd watchdog и перезапускается при зависании event loop.

Проверка и логи:

```bash
sudo systemctl status hermes-gateway
sudo journalctl -u hermes-gateway -f
```

### Постоянная рабочая папка

Задачи gateway и cron по умолчанию выполняются в:

```text
/home/hermes/workspace
```

Это отделяет рабочие файлы агента от его конфигурации и исходного кода.

### Память, сессии и skills

Встроенная память, поиск по предыдущим сессиям, bundled skills, создание новых
skills и делегирование задач подагентам входят в стандартную установку.

### Checkpoints и откат изменений

Перед изменением файлов или потенциально разрушительной terminal-командой
Hermes сохраняет снимок рабочей директории. Посмотреть и откатить изменения
можно внутри сессии:

```text
/rollback
/rollback diff 1
/rollback 1
```

Снимки хранятся отдельно от Git проекта, автоматически очищаются и по умолчанию
ограничены штатным лимитом Hermes в 500 MB.

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes checkpoints status
sudo -u hermes -H /home/hermes/.local/bin/hermes checkpoints prune
```

[Checkpoints и rollback](https://hermes-agent.nousresearch.com/docs/user-guide/checkpoints-and-rollback)

### Резервное копирование

Перед управляемым обновлением полный backup и Kanban-проверка обязательны, а
systemd дополнительно создаёт scheduled backup:

```text
/home/hermes/hermes-backups
```

Первый scheduled backup полный. Затем создаётся quick snapshot каждый день и
full snapshot раз в неделю. Quick snapshots хранятся 14 дней, full — 35 дней.
Health check отдельно сообщает, если любой backup старше 26 часов или полный
старше 8 дней. Пороги, день недели и retention меняются в
`/etc/hermes-ops.conf`.

Также включается `updates.pre_update_backup: full`: перед будущими обновлениями
Hermes создаёт полный архив `HERMES_HOME` с настройками, авторизацией, сессиями,
памятью и skills. На маленьком диске режим можно заменить на `quick`:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes \
  config set updates.pre_update_backup quick
```

Важно: local backup на том же VPS не спасает при удалении VPS или отказе диска.
Сейчас система хранит только локальные архивы; следите за доступным местом и
периодически проверяйте, что архив восстанавливается.

[Обновления и backups](https://hermes-agent.nousresearch.com/docs/getting-started/updating)

### Автоматическая диагностика

В конце установки запускаются `hermes config check` и `hermes doctor`. Ошибки
диагностики выводятся как предупреждения, потому что некоторые функции могут
ожидать ключ или интеграцию, которую пользователь ещё не настроил.

## Режимы установки

Ниже — команды для VPS после перехода в каталог со скопированной папкой Hermes:

```bash
cd /root/hermes # замените путь, если repository находится в другом каталоге
```

| Команда | Результат |
|---|---|
| `sudo ./deploy-hermes.sh` | Полная установка: Chromium, CLI, Google, Tailscale, мастер, MCP, gateway, audit, metrics, alerts и passwordless sudo для Hermes |
| `sudo ./deploy-hermes.sh --portal` | То же самое плюс быстрая настройка Nous Portal |
| `sudo ./deploy-hermes.sh --minimal` | Только базовый Hermes без Chromium, дополнительных CLI, мастеров и gateway |
| `sudo ./deploy-hermes.sh --without-browser` | Полная установка без локального Chromium |
| `sudo ./deploy-hermes.sh --without-dev-cli` | Не ставить дополнительный набор CLI для кода/VPS |
| `sudo ./deploy-hermes.sh --without-google-cli` | Не ставить `gws`; skill сможет попробовать Python fallback |
| `sudo ./deploy-hermes.sh --skip-setup` | Не запускать интерактивный мастер |
| `sudo ./deploy-hermes.sh --skip-mcp` | Не открывать интерактивный каталог MCP |
| `sudo ./deploy-hermes.sh --no-gateway` | Не создавать фоновую systemd-службу |
| `sudo ./deploy-hermes.sh --without-ops` | Не ставить audit, metrics, backup timer и health alerts |
| `sudo ./deploy-hermes.sh --without-tailscale` | Не устанавливать Tailscale |
| `sudo ./deploy-hermes.sh --skip-tailscale-login` | Установить Tailscale, но подключить tailnet позже |
| `sudo ./deploy-hermes.sh --without-host-admin` | Оставить Hermes без системного `sudo` |
| `sudo ./deploy-hermes.sh --user NAME` | Использовать другое имя сервисного пользователя |
| `sudo ./deploy-hermes.sh --branch NAME` | Установить указанную ветку Hermes |
| `sudo ./deploy-hermes.sh --expected-version VER --release TAG --commit SHA --installer-sha256 HASH` | Осознанно заменить зафиксированный релиз, commit и checksum installer |

Явные флаги `--with-browser`, `--with-dev-cli`, `--with-google-cli`, `--setup`,
`--setup-mcp`, `--enable-gateway`, `--with-ops`, `--with-tailscale` и
`--tailscale-login` также поддерживаются, но это уже поведение полной установки
по умолчанию.

## Что ещё стоит настроить

Следующие функции нельзя безопасно включить без выбора пользователя, ключей или
доступа к внешнему сервису.

### 1. Модели и экономия токенов

Deploy не фиксирует inference provider или model. Hermes поддерживает built-in
providers с API key/OAuth, named custom providers и локальные
OpenAI-compatible endpoints. Для Ansible укажите любые нужные ENV keys в
`hermes_secret_env`, а non-secret provider/model/fallback config — в
`hermes_llm_config`. Добавление нового custom endpoint не требует изменения
playbook.

Без Ansible либо для OAuth provider запустите официальный мастер один раз для
каждого нужного provider:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes model
```

При Vault workflow API keys уже находятся в закрытом `.env`, а named custom
providers и выбранная модель могут быть полностью описаны в
`hermes_llm_config`. Мастер автоматически увидит built-in credentials; повторно
вставлять их не нужно. OAuth flows по-прежнему выполняются через `hermes model`,
поскольку их нельзя безопасно заменить статическим API key в Vault.

Для основной работы выбирайте tool-capable модель с достаточным context window.
Model IDs не закреплены в репозитории: каталоги и доступность меняются. Не
присылайте ключи в Telegram или в чат агенту.

Переключение внутри Hermes или Telegram не требует перезапуска и не теряет
историю диалога:

```text
/model
/model <built-in-provider>:<model-id>
/model custom:<provider-name>:<model-id>
```

После первоначальной настройки проверьте конфигурацию:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes config check
sudo -u hermes -H /home/hermes/.local/bin/hermes doctor
sudo -u hermes -H /home/hermes/.local/bin/hermes status
```

`provider_routing` имеет смысл только для aggregators, которые его поддерживают
(сейчас OpenRouter и Nous Portal), поэтому deploy больше не включает его
глобально. Если выбран такой provider, нужные `sort`, allow/deny lists,
`require_parameters` и `data_collection` задайте в `hermes_llm_config`.
Provider-specific cache также включается только явно.

Задайте API keys отдельные spending limits и следите за расходом через
`/ops costs 7d` либо `hermes-ops-report`. Если provider не сообщает стоимость,
заполните `model-prices.json` актуальными ценами.

Официальные справочники: [providers в Hermes](https://hermes-agent.nousresearch.com/docs/integrations/providers),
[routing aggregators](https://hermes-agent.nousresearch.com/docs/user-guide/features/provider-routing),
[fallback providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers).

Если основная модель недоступна или достигла лимита, Hermes сможет продолжить
задачу через другого провайдера:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes fallback
```

Лучше использовать другого провайдера, а не только другую модель в том же
сервисе. Затем откройте настройку моделей:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes model
```

Практичная экономная схема:

- основная сильная модель — код, планирование и сложные решения;
- дешёвая быстрая модель — названия сессий, web-страницы, skill search и MCP
  routing;
- модель для сжатия контекста — недорогая, но с контекстным окном не меньше,
  чем у основной модели;
- `web_search` — для поиска, Chromium — только когда надо нажимать и заполнять;
- встроенный `execute_code` — для цепочки небольших преобразований за один
  вызов модели.

Prompt caching в Hermes работает автоматически. Skills загружаются постепенно,
поэтому лучше поставить несколько точных skills, чем огромный дублирующий
набор. Объединяйте похожие cron-проверки в один отчёт вместо многих отдельных
запусков.

Скрипт удаляет пользовательские overrides `agent.max_turns` и
`goals.max_turns`, поэтому обычные запросы и `/goal` используют полноценные
встроенные бюджеты текущей версии Hermes. Hard-stop по-прежнему останавливает
только повторяющиеся ошибки после трёх неудач; один turn ограничен 20 web
searches и 10 subagents. Для компактного Telegram отключён tool-progress, о
background process приходит только итог, а сессия автоматически сбрасывается
после 48 часов простоя. Метрики, health checks, backups и `/ops` работают без
LLM; автоматический анализ запускается только по вашему запросу.

[Fallback Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)

### 2. Telegram или другой messenger

Настройка:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes gateway setup
```

Обязательно разрешите доступ только своим user ID, например:

```dotenv
TELEGRAM_ALLOWED_USERS=123456789
```

Не устанавливайте `GATEWAY_ALLOW_ALL_USERS=true` для агента с terminal-доступом.
Неизвестных пользователей Hermes по умолчанию блокирует или предлагает
подключить через одноразовый pairing-код.

После этого в Telegram можно писать, например: «клонируй репозиторий в
workspace, реализуй задачу, запусти тесты и создай PR». По умолчанию Ansible
также выдаёт пользователю `hermes` passwordless `sudo` и доступ к Docker, то
есть root-equivalent права на VPS. Поэтому запускайте опасные действия только
по явному запросу владельца и оставляйте подтверждения для удаления данных,
изменений сети и production-сервисов. Для ограниченного VPS задайте
`hermes_host_admin_enabled: false`.

[Безопасность gateway](https://hermes-agent.nousresearch.com/docs/user-guide/security/)

### 3. Изоляция команд через rootless Podman

По умолчанию terminal работает локально от пользователя `hermes`. Это позволяет
агенту обслуживать сам VPS, но ошибки могут повредить файлы этого пользователя.

Для разработки и обработки недоверенных данных лучше поставить rootless Podman
и выбрать Docker backend:

```bash
sudo apt-get install podman uidmap slirp4netns fuse-overlayfs
sudo -u hermes -H /home/hermes/.local/bin/hermes setup terminal
```

В ограниченном VPS не добавляйте `hermes` к обычному rootful Docker socket:
членство в группе `docker` фактически даёт root-доступ. На выделенном VPS с
`hermes_host_admin_enabled: true` Ansible делает это намеренно, потому что
владелец разрешил Hermes администрировать хост. Для обработки недоверенного
кода всё равно предпочтителен rootless Podman.

[Terminal backends](https://hermes-agent.nousresearch.com/docs/user-guide/configuration/)

### 4. Полезные skills

Ищите только те skills, которые нужны для ваших задач:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes skills browse
sudo -u hermes -H /home/hermes/.local/bin/hermes skills search docker
sudo -u hermes -H /home/hermes/.local/bin/hermes skills search github
sudo -u hermes -H /home/hermes/.local/bin/hermes skills audit
```

Для VPS обычно полезны Git/GitHub, Docker/Podman, мониторинг, backups, базы
данных, обработка документов и deployment workflows. Перед установкой
стороннего skill используйте `hermes skills inspect`.

[Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)

### 5. Cron-задачи

После проверки обычного чата можно добавить:

- контроль свободного места;
- проверку сайтов и SSL-сертификатов;
- отчёты о логах и ошибках;
- резервное копирование проектов;
- ежедневные отчёты в Telegram.

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes cron create
sudo -u hermes -H /home/hermes/.local/bin/hermes cron list
sudo -u hermes -H /home/hermes/.local/bin/hermes cron status
```

[Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)

### 6. MCP-интеграции без лишнего расхода

В конце обычной установки скрипт открывает интерактивный каталог MCP,
проверенных командой Nous. Позже его можно открыть снова:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes mcp
sudo -u hermes -H /home/hermes/.local/bin/hermes mcp catalog
sudo -u hermes -H /home/hermes/.local/bin/hermes mcp list
```

Что выбирать:

- GitHub MCP не нужен: в текущем Hermes bundled GitHub skills вместе с `gh`
  дают более полную интеграцию, поэтому Nous намеренно не кладёт GitHub в MCP
  catalog;
- task/project MCP — только если вы реально используете соответствующий сервис;
- database MCP — отдельно для конкретной базы, желательно с read-only учёткой;
- n8n или внутренний API — если хотите строить свои автоматизации.

Не нужен отдельный filesystem MCP, browser MCP, web-search MCP или Google MCP:
эти возможности уже есть во встроенных tools, Chromium и Google skill. Каждый
MCP добавляет схемы инструментов в контекст, расходует память и получает новые
права. При установке оставляйте включёнными только реально нужные tools; список
можно сузить через `hermes mcp configure NAME`.

Даже каталог Nous запускает сторонний код. Перед установкой посмотрите показанный
`source` и bootstrap-команды. Используйте отдельные токены с минимальными
правами. Для недоверенного MCP отключайте sampling и задавайте лимиты на число
запросов и токены.

[MCP в Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/)

Hermes может по команде из чата найти и установить для себя skill, plugin или
MCP в собственный `/home/hermes/.hermes`, а также поставить project-local
dependency через уже доступные `npm`, `pip`, `go` и другие package managers.
Такая установка проходит обычные approvals и появляется в audit log. Просите
его сначала показать source, permissions, команды установки и ожидаемый расход
контекста, а затем подтверждайте.

На выделенном VPS по умолчанию у `hermes` есть passwordless `sudo`, поэтому по
явному запросу владельца он может устанавливать системные пакеты через `apt`,
менять firewall/systemd и писать в `/etc`. В ограниченном режиме
(`--without-host-admin` или `hermes_host_admin_enabled: false`) этого `sudo`
нет: такие изменения добавляйте в deploy/Ansible bundle и применяйте
администратором после review. Не выдавайте агенту общий root token, если
ограниченный режим достаточен для задачи.

### 7. Расширенная память

Встроенной памяти обычно достаточно. Для длительной персонализации можно
подключить Honcho, OpenViking, Mem0 или другой memory provider:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes memory setup
```

Внешняя память может иметь отдельную стоимость и хранить данные вне VPS, поэтому
её не следует включать автоматически.

### 8. CLI только под конкретные проекты

Не стоит заранее ставить все языки, cloud SDK и базы: они занимают диск,
обновляются и увеличивают поверхность атаки. Когда появится реальная задача,
добавьте только нужную группу:

- PostgreSQL/Redis clients — для диагностики конкретных баз;
- Terraform/OpenTofu и Ansible — для инфраструктуры;
- `kubectl` и Helm — только для Kubernetes;
- AWS, Google Cloud или Azure CLI — только для используемого облака;
- Tesseract и Poppler — для OCR и PDF;
- SDK языка проекта: Go, Java, Rust и другие.

Ставьте их по официальной инструкции поставщика и авторизуйте от пользователя
`hermes`. Для production используйте отдельные service accounts с минимальными
правами. Это полезнее и дешевле по ресурсам, чем универсальный образ «со всем».

### 9. Dashboard или API

Полная установка автоматически запускает `hermes-dashboard.service` после
reboot. Dashboard слушает только loopback; откройте его через SSH tunnel:

```bash
# На VPS: проверить службу
sudo systemctl status hermes-dashboard.service

# На локальном компьютере
ssh -L 9119:127.0.0.1:9119 user@server
```

Затем откройте `http://127.0.0.1:9119`. Не публикуйте dashboard или API напрямую
в интернет. Для API обязательно задайте сильный `API_SERVER_KEY`.

### 10. Grafana и исторические метрики

Полная VPS-установка автоматически устанавливает Grafana OSS, Prometheus и
node exporter. Все три HTTP-службы ограничены loopback. Откройте Grafana через
второй SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 user@server
```

Затем откройте `http://127.0.0.1:3000`, войдите как `hermes` и откройте
**Hermes / Hermes Overview**. При Ansible deploy password задаётся как
`hermes_grafana_admin_password` в encrypted Vault и материализуется в
root-only `/etc/hermes-grafana.env` до первого Grafana start. При прямом
`deploy-hermes.sh` password генерируется один раз в тот же файл и не печатается
в log. Не меняйте файл после первого Grafana start для ротации пароля:
используйте штатную Grafana admin-команду.

## Полезные команды

```bash
# Открыть Hermes
sudo -u hermes -H /home/hermes/.local/bin/hermes

# Проверить состояние Hermes и его зависимости
sudo -u hermes -H /home/hermes/.local/bin/hermes status

# В Telegram: компактный статус без LLM — gateway и учтённые токены
/status

# Проверить зависимости и настройки
sudo -u hermes -H /home/hermes/.local/bin/hermes doctor
sudo -u hermes -H /home/hermes/.local/bin/hermes config check

# Проверить обновление без установки
sudo -u hermes -H /home/hermes/.local/bin/hermes update --check

# Обновить с полным backup и автоматическим restart gateway
sudo -u hermes -H /home/hermes/.local/bin/hermes update --backup

# Создать ручной полный backup
sudo -u hermes -H /home/hermes/.local/bin/hermes backup

# Логи gateway
sudo journalctl -u hermes-gateway -f

# Состояние production timers
sudo systemctl list-timers 'hermes-*'

# Метрики моделей, tools, команд и стоимости
sudo -u hermes HERMES_HOME=/home/hermes/.hermes \
  hermes-ops-report --period 7d

# Приватный IP VPS
tailscale ip -4
```

Не храните API-ключи и bot tokens в этом репозитории или shell history. Вводите
их через мастер Hermes. Кроме локального Chromium, gateway выполняет исходящие
подключения к выбранным платформам; скрипт не открывает публичных firewall-портов.

## Удаление

Удаление намеренно не автоматизировано, чтобы случайно не стереть память,
сессии и конфигурацию:

```bash
sudo /home/hermes/.local/bin/hermes gateway uninstall --system
sudo -u hermes -H /home/hermes/.local/bin/hermes uninstall
```

Пользователь `hermes`, его рабочая папка и backups сохраняются до явного
удаления администратором.
