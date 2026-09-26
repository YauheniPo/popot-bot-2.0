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
| [Ollama Cloud](https://docs.ollama.com/cloud) | Один из доступных LLM providers; модель выбирается в конфигурации или меню Hermes. | `OLLAMA_API_KEY` в Vault, если выбран `ollama-cloud`. |
| [Nous Portal](https://portal.nousresearch.com/) | Альтернатива отдельным ключам: models и Tool Gateway для web, image, TTS и browser. | Опционально, для `deploy-hermes.sh --portal`. |
| [Telegram Bot API](https://core.telegram.org/bots) | Личный chat gateway и alerts Hermes. | Опционально, если нужен Telegram. |
| [Tailscale](https://tailscale.com/) | Приватная сеть и Tailscale SSH; позволяет закрыть публичный SSH. | Рекомендуется для VPS. |
| [GitHub](https://github.com/) | Клонирование репозиториев, commit/push, pull requests и GitHub CLI. | Опционально, для GitHub workflows. |
| [Azure DevOps](https://dev.azure.com/) | Builds, logs, repositories и другие операции через REST API. | Опционально: `AZURE_DEVOPS_EXT_PAT` в Vault. |
| [SonarQube Cloud](https://sonarcloud.io/) | Issues, metrics и Quality Gate через REST API. | Опционально: `SONAR_TOKEN` в Vault. |
| [Google Workspace](https://workspace.google.com/) | Gmail, Calendar, Drive, Contacts, Docs и Sheets через OAuth. | Опционально, для Google tools. |
| [Brave Search API](https://brave.com/search/api/) | Web search по явно выданному API key. | Опционально, если не используется Nous Tool Gateway. |
| [Firecrawl](https://docs.firecrawl.dev/) | Извлечение и чтение HTML/PDF/web-страниц. | Опционально, для `web_extract`. |
| [FAL.ai](https://fal.ai/) | Генерация и редактирование изображений. | Опционально: нужен `FAL_KEY`, если не используется image tool Nous Portal. |
| [Grafana](https://grafana.com/docs/) | Локальные dashboards model/token/cost/VPS metrics. | Устанавливается при включённом ops-слое; не требует внешнего аккаунта. |
| [Prometheus](https://prometheus.io/docs/) | Локально собирает и хранит metrics для Grafana и Hermes. | Устанавливается при включённом ops-слое; не требует внешнего аккаунта. |
| [Docker Engine](https://docs.docker.com/engine/) | Сборка, запуск и администрирование контейнеров на VPS. | Устанавливается Ansible при `vps_deploy.features.host_admin: true`; доступ Hermes через root-equivalent группу `docker`. |
| [Ansible Vault](https://docs.ansible.com/projects/ansible/latest/vault_guide/index.html) | Шифрует API keys и конфигурацию на Ansible controller. | Рекомендуется для повторяемого Ansible deploy. |

Для новых inventory используйте `vps_deploy.features.host_admin`. Старый
(deprecated) ключ `hermes_host_admin_enabled` остаётся совместимым с уже
развёрнутыми inventory, но его следует перенести на новый ключ; не задавайте
оба значения одновременно.

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

### Настройка Nous Portal на VPS

После установки Hermes на headless VPS подключите Nous Portal от имени
сервисного пользователя `hermes`:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes auth add nous --type oauth
```

Hermes покажет device-code и ссылку. Откройте ссылку на своём компьютере,
введите код и подтвердите вход; браузер на VPS не нужен. После успешной
авторизации refresh token сохраняется в
`/home/hermes/.hermes/auth.json`.

Проверьте авторизацию и выберите модель Nous:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes portal info
sudo -u hermes -H /home/hermes/.local/bin/hermes model
```

В меню `model` выберите **Nous Portal**. Для включения отдельных инструментов
запустите `hermes tools` и выберите **Nous Subscription** для web search,
image generation, TTS или browser. Повторно проверьте результат:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes portal info
```

В выводе должно быть `Model: ✓ using Nous as inference provider`. Настройки
`config.yaml` и OAuth-файл `auth.json` входят в полный Hermes backup. Portal
может тарифицировать модели и hosted tools согласно плану; бесплатный план
ограничен free-моделями и стандартными лимитами.

Если gateway уже запущен, примените новый provider перезапуском службы:

```bash
sudo systemctl restart hermes-gateway.service
```

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
- **Настраиваемые LLM providers.** Ansible Vault хранит ключи выбранных
  providers, а `vps_hermes.config.managed_overlay` задаёт модели для чата,
  вспомогательных задач и cron, а также список fallback.
- **OpenRouter access.** При необходимости добавьте `OPENROUTER_API_KEY` в
  Vault для выбора через `/model` или использования в настроенном fallback.
- **NVIDIA NIM.** Для альтернативного inference provider добавьте
  `NVIDIA_API_KEY` в Vault и выберите модель командой `/model
  nvidia:<model-id>`. NVIDIA имеет собственные квоты и rate limits; бесплатный
  ключ не означает безлимитное использование.
- **Nous Portal.** Portal подключается OAuth-командой `hermes auth add nous` и
  хранит refresh token в `~/.hermes/auth.json`. Модели и Tool Gateway могут
  тарифицироваться по плану Portal; бесплатный план ограничен free-моделями и
  стандартными лимитами. Проверяйте фактическое потребление в Portal Usage.
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
- **Отчёты без модели.** `/status` и `hermes-ops-report` анализируют
  локальные данные обычным кодом и не вызывают LLM. `/status` показывает
  gateway, токены последней активной сессии и общий учтённый расход, а также
  учитывает активных subagents текущего чата: их результат автоматически
  вернётся в этот же диалог. При нескольких параллельных сессиях выбор
  активной сессии приблизительный.
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
  `vps_deploy.features.host_admin: false` в Ansible или используйте
  `--without-host-admin` при прямой установке.

### Статус функций после запуска deploy

| Функция | Статус | Что ещё требуется |
|---|---|---|
| Файлы, terminal, Git, coding | Готово сразу | Права Unix на нужный workspace/repository |
| Ollama Cloud LLM | Готово после Vault deploy | Задать `OLLAMA_API_KEY` в Vault; provider/model policy — в `vps_hermes.config.managed_overlay`, `model.max_tokens` — в `vps_runtime.set` |
| API keys через Ansible Vault | Готово | Создать и зашифровать локальный `group_vars/all/vault.yml` |
| Public GitHub clone | Готово сразу | Ничего |
| Private clone, push, PR, reviews, issues, Actions | Управляется Ansible | `GITHUB_TOKEN` с минимальными repo permissions в Vault; identity и access probe находятся в `vps_github` |
| Chromium/browser automation | Установлено | Иногда login конкретного сайта |
| `web_search` | Автоматический выбор backend Hermes | Для Brave задать `BRAVE_SEARCH_API_KEY`; либо войти в Nous Portal/настроить другой backend |
| Gmail и Calendar | CLI и skill готовы | Google Cloud project и OAuth consent |
| Telegram gateway | Автоматически стартует, если Vault содержит Bot token и allowlist | Для запуска задать оба Telegram значения в Vault |
| Web dashboard | Включён в полной установке | Для OAuth зарегистрировать Tailscale `.ts.net` URL и сохранить `HERMES_DASHBOARD_OAUTH_CLIENT_ID` + `HERMES_DASHBOARD_PUBLIC_URL` в Vault |
| `/update` и auto-restart | Включено | Писать `/update` только из разрешённого аккаунта |
| Tailscale | Установлено | Подтвердить login и проверить tailnet SSH policy |
| Закрытие публичного SSH | Включено по умолчанию после проверки Tailscale IP | Перед deploy убедиться, что VPS уже подключён к tailnet и Tailscale SSH проверен |
| Local backups | Включено | Следить за диском и тестировать restore |
| Audit, SQLite и CLI `hermes-ops-report` | Включено | При compliance отправлять journald во внешнее immutable/SIEM-хранилище |
| Стоимость моделей | Частично автоматически | Если provider не сообщает cost, заполнить `model-prices.json` |
| Grafana + Prometheus | Включено | Открывать через SSH/Tailscale tunnel на `127.0.0.1:3000`; password хранится root-only в `/etc/hermes-grafana.env` |
| LLM-анализ расходов | По запросу | Выбрать модель через `/model` и попросить проанализировать JSON report |
| MCP catalog | Picker открывается при deploy | Установить только реально нужные integrations после review |
| Skills Hub | Инициализируется при deploy | Engineering-навыки из [mattpocock/skills](https://github.com/mattpocock/skills/tree/main/skills/engineering) подключаются автоматически из закреплённой ревизии; остальные внешние skills можно добавлять в `skills.external_dirs` |

Engineering-навыки находятся на VPS в
`/opt/hermes-external-skills/mattpocock-skills/skills/engineering`. Их исходный
репозиторий и SHA закреплены в `vps_external_skills.matt_pocock_engineering` в
`config/vps-defaults.yml`; чтобы обновить набор, сначала меняйте SHA в этом
файле, затем запускайте обычный deploy. Каталог принадлежит `root`: Hermes
может читать навыки, но не может незаметно изменить закреплённый источник.

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
| Проверка качества | `ansible-core`, `python3-pytest`, `python3-yaml` | Локальный запуск полного `hermes/check.sh` (синтаксис Ansible, pytest, YAML-скрипты); коллекция `ansible.posix` ставится из `hermes/ansible/requirements.yml` |
| Файлы и архивы | `file`, `tree`, `rsync`, `zip`, `unzip`, `xz-utils` | Диагностика, копирование и архивирование |
| Сеть и процессы | `dnsutils`, `lsof`, `netcat-openbsd`, `util-linux` | DNS, порты, процессы, locks и VPS diagnostics |
| Медиа | `ffmpeg` | Audio/video conversion и подготовка voice/media |
| Browser | Chromium/Playwright system libraries, fonts, `xvfb` | Headless browser и динамические сайты |
| Google | `gws` через Hermes-managed Node/npm | Gmail, Calendar, Drive, Docs, Sheets, Contacts |
| Private network | `tailscale`, `tailscaled` | Private WireGuard network и Tailscale SSH |
| Контейнеры (Ansible, host-admin) | `docker.io`, Docker Engine | Сборка, запуск и администрирование контейнеров без `sudo`; группа `docker` root-equivalent |
| Hermes | `hermes`, managed Python venv, managed Node/npm | Agent runtime, skills, plugins, MCP, gateway и update |

Docker Engine устанавливается только Ansible deploy при
`vps_deploy.features.host_admin: true`; прямой `deploy-hermes.sh` его не ставит.
При следующем Ansible deploy с `vps_deploy.features.host_admin: false` playbook
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
    в зашифрованном виде. `vault.yml.example` — безопасный шаблон.
  - `templates/` — заготовки файлов, которые Ansible заполняет значениями при
    deploy, например закрытый `.env`.
- `config/vps-defaults.yml` — единый видимый файл важных non-secret настроек
  VPS: identity/paths, feature switches, Hermes runtime guardrails, backup и
  health policy, observability topology, pinned browser tooling, VS Code и
  группы systemd services для обязательного рестарта.
- `deploy/` — домены прямого deploy: host, runtime, services и reporting.
- `runtime/` — testable helpers, которые применяют общие настройки через
  официальный Hermes CLI.
- `docker/` — отдельная локальная Docker-сборка для разработки и проверки на
  компьютере; это не конфигурация Docker на VPS.
- `ops/` — обслуживание VPS: backups, health checks, метрики, browser verify и
  startup notifications.
  - `install/` — packages, plugin, assets и service lifecycle; верхний
    `install-ops.sh` только валидирует arguments и оркестрирует эти домены.
  - `plugin/` — hooks Hermes для сбора usage и audit в SQLite и локальный log;
    метрики экспортируются в Prometheus и отображаются в Grafana.
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
| [`runtime/apply-config.py`](runtime/apply-config.py) | Применяет runtime settings через Hermes CLI, валидирует/рендерит ops assets и выдаёт service groups/отдельные scalar settings |
| [`runtime/verify-update-state.py`](runtime/verify-update-state.py) | Fail-closed проверяет полноту full backup, сохранность личных файлов, SQLite integrity и Kanban counts до/после managed update |
| [`SECRETS-CHECKLIST.md`](SECRETS-CHECKLIST.md) | Единый checklist обязательных и optional tokens, OAuth, SSH и backup-данных без настоящих значений |
| [`ops/install-ops.sh`](ops/install-ops.sh) | Тонкий оркестратор ops installation; packages, plugin, assets и services разделены в [`ops/install/`](ops/install) |
| [`ops/plugin/ops-observability`](ops/plugin/ops-observability) | Hermes plugin hooks, audit и SQLite accounting для Prometheus/Grafana |
| [`ops/health-check.sh`](ops/health-check.sh) | Host/gateway/backup/metrics checks, deduplication и recovery alerts |
| [`ops/backup.sh`](ops/backup.sh) | Daily quick, weekly full и local retention |
| [`ops/export-metrics.py`](ops/export-metrics.py) | Prometheus textfile exporter |
| [`observability/`](observability) | Versioned Prometheus scrape config и Grafana datasource/dashboard provisioning |
| [`ops/ops-report.py`](ops/ops-report.py) | Read-only Markdown/JSON отчёты из SQLite |
| [`ops/status-report.py`](ops/status-report.py) | Компактный `/status` без LLM: gateway, токены активной сессии и общий учтённый расход |
| [`ops/startup-notify.sh`](ops/startup-notify.sh) | Нефатальное сообщение в alert target после запуска gateway вне окна deploy: VPS, default model и время; во время deploy оно подавляется, чтобы итоговое сообщение было единственным |
| [`ops/systemd`](ops/systemd) | Hardened services и timers для backup, health, metrics и startup notification |
| [`ops/templates/hermes-ops.conf`](ops/templates/hermes-ops.conf) | Шаблон root-owned ops config, который каждый deploy рендерит из `vps-defaults.yml` |
| [`ops/templates/model-prices.json`](ops/templates/model-prices.json) | Fallback-цены моделей за 1M tokens |
| [`docker/`](docker) | Локальный Docker image c GitHub CLI, Compose, bootstrap и ignored `local.env` для provider, Telegram и GitHub credentials |
| [`instructions/common.md`](instructions/common.md) | Единый источник общих правил Hermes для VPS и Docker: безопасность, workflow, память, проверки и общение |
| [`docker/AGENTS.md`](docker/AGENTS.md) | Поведение Hermes в local container: `sudo` только внутри container, без доступа к Docker host или macOS |
| [`ansible/playbook.yml`](ansible/playbook.yml) | Порядок provision/restore; network, runtime и services вынесены в [`ansible/tasks/`](ansible/tasks) |
| [`ansible/tasks/github.yml`](ansible/tasks/github.yml) | Git/GitHub packages, identity, credential helper, private-repository probe и managed workflow instructions |
| [`ansible/AGENTS.md`](ansible/AGENTS.md) | Только особенности VPS: возможности host administration, Ansible, systemd и источники интеграций |
| [`ansible/inventory.ini`](ansible/inventory.ini) | Нейтральный inventory-алиас; адрес VPS и SSH-пользователь загружаются из зашифрованного Vault |
| [`ansible/group_vars/all/vars.yml`](ansible/group_vars/all/vars.yml) | Публичные IaC defaults без credentials |
| [`ansible/group_vars/all/vault.yml.example`](ansible/group_vars/all/vault.yml.example) | Шаблон private API keys и tokens; рабочий файл — `ansible/group_vars/all/vault.yml`, он шифруется и игнорируется Git; команды находятся в [`VAULT.md`](ansible/group_vars/all/VAULT.md) |
| [`ansible/templates/hermes.env.j2`](ansible/templates/hermes.env.j2) | Безопасно формирует Hermes `.env` из расшифрованных только на время deploy значений и нормализует GitHub token alias |
| [`runtime/github-cli-wrapper.py`](runtime/github-cli-wrapper.py) | Передаёт `gh` только managed GitHub token из Hermes environment без отдельного plaintext credential store |
| [`runtime/apply-hermes-patches.py`](runtime/apply-hermes-patches.py) | Идемпотентно применяет локальные патчи gateway (`.gw-restart`, `model_global`, reasoning в `status`) после каждого install/update Hermes |
| [`check.sh`](check.sh) | Одна локальная и CI-команда для Bash, Python tests, Ansible syntax и whitespace |

### Единый файл критических настроек VPS

Меняйте [`config/vps-defaults.yml`](config/vps-defaults.yml), когда настройка
должна одинаково применяться повторными Ansible deploy:

- `vps_deploy.identity` — пользователь и постоянные Hermes paths;
- `vps_deploy.hermes_source` — зафиксированные branch/version/release/commit
  именно upstream Hermes Agent и SHA-256 его installer;
- `vps_deploy.bundle.dir` — путь к локальной копии deployment-файлов этого
  репозитория, не к исходникам Hermes Agent;
- `vps_deploy.features` — критические feature switches;
- `vps_runtime.set` — обязательные Hermes runtime defaults;
  `platforms.telegram.extra.command_menu.priority` закрепляет команды в
  указанном порядке, а `usage_ranking` сортирует остальные команды Telegram
  по накопленному числу вызовов и обновляет меню с заданной периодичностью;
- `vps_runtime.unset` — опасные или устаревшие overrides, которые deploy
  удаляет;
- `vps_runtime.capabilities` — backend, включаемый только при наличии
  соответствующего Vault credential;
- `vps_ops` — backup retention, health thresholds и интервалы timers;
- `vps_observability` — loopback addresses/ports, scrape intervals, SQLite и audit retention;
- `vps_network`/`vps_packages`/`vps_tools` — SSH/Tailscale/UFW policy,
  package channels/retries и pinned auxiliary CLI versions;
- `vps_hermes.config.managed_overlay` — authoritative non-secret config.yaml
  policy без `model.default`, если `/model_global` должен сохраняться;
- `vps_vscode`/`vps_browser` — образ code-server `latest` (проверяется при каждом
  deploy с code-server), закреплённая версия browser package и безопасная локальная
  browser/IDE topology;
- `vps_agent_policy` — только repository-owned блоки поведения, без замены
  личного `SOUL.md`; языковое правило задаёт язык ответов и объяснений по
  последнему сообщению пользователя (или его явному выбору), не внутреннего
  reasoning. Личная часть хранится в `$HERMES_HOME/SOUL.md` вне маркеров
  `ANSIBLE MANAGED RESPONSE LANGUAGE` и сохраняется при deploy;
- `vps_github` — identity, write boundaries и Git defaults;
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
| `/home/hermes/workspace/AGENTS.md` | `hermes` | Общие правила + возможности окружения + managed-интеграции + личные дополнения |
| `/home/hermes/.hermes/operator-state/workspace-AGENTS.md` | `hermes`, mode `0600` | Restorable mirror workspace-инструкций, включаемый в full backup |
| `/home/hermes/hermes-backups` | `hermes` | Local quick/full zip archives |
| `/opt/hermes-bootstrap` | `root` | Временный versioned bundle, который Ansible копирует на VPS для deploy и ops installation |
| `/home/hermes/.hermes/logs/ops-audit.jsonl` | `hermes`, mode `0600` | Privacy-aware local audit |
| `/home/hermes/.hermes/ops/metrics.db` | `hermes`, mode `0600` | SQLite model/tool/activity accounting |
| `/home/hermes/.hermes/ops/metrics/hermes.prom` | `hermes`, mode `0600` | Prometheus textfile, читается loopback-only Hermes node exporter от имени `hermes` |
| `/var/lib/hermes-prometheus` | `prometheus` | 30-day локальная time-series база |
| `/etc/hermes-grafana.env` | `root`, mode `0600` | Grafana admin credentials: Ansible materializes from Vault; direct installer generates them once without printing |
| `/etc/hermes-ops.conf` | `root` | Сгенерированные из `vps-defaults.yml` monitoring thresholds, paths и retention, без secrets; ручные изменения заменяются deploy |
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
`vps_deploy.features.host_admin: false` в Ansible; тогда он не сможет напрямую
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
| `hermes-observability-prune.timer` | Сворачивает строки локальной SQLite старше 90 дней в rollup-счётчики и удаляет их | 1 день |
| `hermes-startup-notify.service` | После запуска gateway вне окна deploy отправляет VPS, default model и время в alert target; во время deploy уведомление подавляется, ошибка доставки не влияет на gateway | на каждый старт вне окна deploy |
| `ops-observability` | Считает вызовы моделей/tools/команд, токены, ошибки, latency и стоимость | по событиям |
| audit rotation | Ограничивает основной log и 2 ротации размером 5 MiB каждая | при записи и ежедневно |

Health check отправляет сообщения через `hermes send`, без запуска LLM. Поэтому
проверка каждые пять минут не расходует токены. Alert отправляется только при
появлении новой проблемы и при восстановлении; одинаковые сообщения не
повторяются. Если доставка не сработала, следующая проверка повторит попытку.

Проверка load различает превышение порога и ошибку самой проверки: отсутствие
данных CPU/load или сбой `awk` дают проблему `load-check`, а не ложный healthy.
Ansible создаёт maintenance-маркер до остановки существующего gateway для backup,
обновляет его перед настройкой служб и удаляет в общем `always` после этапов
deploy, включая обработанные ошибки. Пока маркер свежий, health/startup-уведомления
подавляются; сохранённое health-state не переписывается. Если контроллер оборван
или VPS недоступен, cleanup не гарантирован: защита истекает через 900 секунд
после последнего обновления маркера. Долгий deploy может выйти за это окно.

Telegram должен быть настроен в gateway. Для проверки можно специально
запустить службы вручную:

```bash
sudo systemctl start hermes-backup.service
sudo systemctl start hermes-metrics.service
sudo systemctl start hermes-health.service
sudo journalctl -u hermes-health.service -n 50 --no-pager
```

Пороги задаются в `vps_ops` файле
[`config/vps-defaults.yml`](config/vps-defaults.yml). Повторный deploy
перегенерирует `/etc/hermes-ops.conf` и systemd timers; ручные изменения этого
root-owned файла намеренно не сохраняются.

Метрики доступны только в Grafana: откройте dashboard **Hermes Overview**.
Он показывает gateway, host resources, backup freshness, API/tool error rate,
latency, usage и стоимость по provider/model.

#### Метрики моделей и провайдеров

Два источника данных в Grafana, у каждого своя роль:

- **Prometheus** (`hermes-prometheus`) — счётчики и таймстемпы из textfile
  exporter: `hermes_api_calls_total{status}`, `hermes_api_success_total`,
  `hermes_api_errors_total`, `hermes_api_rate_limits_total`,
  `hermes_api_retries_total`, tokens/cost, `hermes_api_last_*_timestamp_seconds`,
  плюс host/gateway/backup. Это основа графиков `rate()` за 30 дней и алертов.
- **SQLite** (`hermes-sqlite`, плагин `frser-sqlite-datasource`) — per-call
  аналитика маршрутов прямо из событий `api_calls`, `tool_calls`, `sessions`,
  `route_fallbacks`: точный p95, first-attempt success, finish reasons, пустые
  ответы, requested vs served model, fallbacks, ошибки tool calls по модели.
  Окно — time picker, история — retention SQLite (90 дней).

Grafana не читает приватную WAL-базу в home Hermes. Экспортёр каждую минуту
делает `VACUUM INTO` snapshot в rollback-режиме в
`/var/lib/hermes-observability/metrics.db` (каталог `hermes:grafana`, файл
`0640`; путь задаёт `HERMES_ANALYTICS_FILE` в `hermes-metrics.service`) и
публикует `hermes_analytics_snapshot_timestamp_seconds`; `0` означает, что
snapshot не обновился. Плагин открывает файл на каждый запрос в режиме
`query_only`, поэтому атомарная замена snapshot безопасна. Версия плагина
задаётся `vps_observability.grafana.sqlite_plugin_version` и устанавливается
`grafana-cli` при deploy; в Docker-стеке — `GF_INSTALL_PLUGINS` в compose.

Плагин записывает в `api_calls` `requested_model` и `call_index`
(`api_call_count` Hermes). First-attempt success связывает успех с error-строками
того же логического вызова по `session_id` + `call_index`; Hermes не передаёт
`retry_count` в `post_api_request`, без корреляции успех считается первой
попыткой. Модель для tool calls берётся из последнего `session start` этой сессии.

Счётчики Prometheus монотонны: `hermes-observability-prune` перед удалением
строк сворачивает их в таблицы `*_rollup` по измерениям экспортёра, а экспортёр
суммирует live-строки и rollup. Retention поэтому не создаёт ложных counter
reset для `rate()`/`increase()`. SQLite-панели rollup не используют и видят
только сохранённые строки.

Prometheus загружает versioned правила `observability/rules/hermes.rules.yml`:
recording rules `hermes_route:calls:*` и `hermes_route:availability:*` (1h/24h)
и алерты `HermesRouteAvailabilityLow`, `HermesRouteNoRecentSuccess`,
`HermesRouteRateLimitBurst`, `HermesAnalyticsSnapshotStale`. Alertmanager не
развёрнут: алерты видны в Prometheus и Grafana, доставка в Telegram остаётся за
health-check timer. `check.sh` прогоняет `promtool check rules`, если promtool
установлен; тест `observability/test_dashboard_sql.py` исполняет каждый SQL
дашборда на реальной схеме плагина.

Grafana dashboard **Hermes Overview** разбит на разделы **At a glance**,
**Usage and tools**, **User profiles**, **Model reliability and routing**,
**VPS resources** и **AI review in CI**. В верхних карточках видны состояние
gateway, доля неудачных API-запросов, средняя задержка, возраст backup, место
на диске, оценка стоимости и число токенов. Стоимость и токены относятся к
выбранному диапазону времени; скорости на графиках измеряются в секунду.
При отсутствии вызовов доля ошибок и задержка показывают отсутствие данных,
а не ложный ноль. Нагрузка хоста, занятые CPU-ядра и проценты CPU/inode
отображаются отдельно, поскольку у них разные единицы.

В Grafana панель **Route scorecard** сводит availability, first-attempt
success, p95, output tok/s, доли обрезок и пустых ответов и стоимость успешного
вызова; ниже — ошибки по классам, p95, finish reasons, fallbacks, ошибки tool
calls по модели и расхождение requested/served. Метрики измеряют надёжность и
форму ответа, не правильность содержания.

Для сравнения CI-ревьюеров используется таблица `review_runs`. После
опубликованного GitHub review импортируйте только технические поля командой:

```bash
GITHUB_TOKEN="$(gh auth token)" \
  /usr/local/lib/hermes-ops/extract-review-metrics.py --pr 43
```

Импорт идемпотентен и не сохраняет diff, prompts, findings или тексты
комментариев. Панель **AI review runs — selected range** показывает reviewer,
provider/model, результат, coverage чанков, retries и время провайдера; панель
**AI review provider time** позволяет сравнивать скорость маршрутов во времени.
Токен передаётся только через окружение и не записывается в конфигурацию Hermes.
Явный `Result` определяет результат review. Если его нет, `Coverage: partial`
сохраняется как `partial`, `Coverage: complete` — как `success`, а отсутствие
распознаваемого coverage — как `unknown` (в том числе для старых комментариев
без обоих полей). Успешная модель сама по себе не доказывает завершение review.
После deploy исправленного импортёра повторный импорт PR обновляет ранее ошибочный результат
в той же записи без создания дубликата.

Панели **Requests by profile** показывают обращения к основному `default` и
именованным профилям. Одно обращение — одна сохранённая запись `role=user`
в `state.db` соответствующего профиля, а не запуск tmux, tool call или запрос
к LLM. Служебные compression summaries исключены; записанные smoke-check
prompts считаются обращениями. Не дошедшие до записи задания не учитываются.
Экспортёр читает только агрегаты и timestamps, не текст сообщений.
В dashboard оставлена одна карточка времени — **Profile last activity**;
внутренний timestamp последнего user-запроса остаётся в экспортёре для
совместимости, но не дублируется на экране. Диагностическая метрика успешности
чтения истории также не занимает отдельную панель.

- `hermes_profile_user_requests{profile,window}` — количество за последние
  `1h`, `24h`, `7d` и всю **сохранённую** историю (`retained`). Это gauge:
  удаление/импорт/восстановление истории меняет значения; `rate`/`increase`
  к нему неприменимы, это не пожизненный счётчик.
- `hermes_profile_last_request_timestamp_seconds{profile}` — последнее
  сохранённое обращение, Unix seconds; `0` означает пустую историю.
- `hermes_profile_last_activity_timestamp_seconds{profile}` — последнее
  наблюдаемое событие профиля, включая ответ assistant, Unix seconds.
- `hermes_profile_response_duration_seconds{profile,window}` — суммарное
  приблизительное время от user-сообщения до следующего assistant-ответа за
  `1h`, `24h`, `7d` и `retained`. Это время работы диалога, а не CPU-время
  процесса; если ответ не был записан, интервал не учитывается.
- `hermes_profile_last_request_duration_seconds{profile}` — длительность
  последнего завершённого вызова профиля от timestamp user до timestamp
  assistant.
- `hermes_profile_last_request_start_timestamp_seconds{profile}` и
  `hermes_profile_last_request_end_timestamp_seconds{profile}` — границы
  последнего завершённого вызова в Unix seconds.
- `hermes_profile_history_readable{profile}` — успешность чтения. При отсутствии,
  повреждении БД или timeout возвращается `0`, а usage-серии не публикуются:
  неизвестная активность не подменяется нулём.

Все обычные каталоги `profiles/*` с `config.yaml` обнаруживаются автоматически;
symlink-профили не обходятся. График показывает скользящее часовое окно,
снимки — день/неделю/retained, время последнего запроса, последнюю активность и
длительность ответов по профилям. Для time-series Grafana показывает значения
`Last`, `Max` и `Mean` в таблице легенды и пересчитывает видимый диапазон при
изменении time picker. История графика
появляется с момента начала сбора Prometheus; ранние поминутные события не
восстанавливаются. Новых публичных endpoint и labels с prompt/session ID нет.

Команда `/ops` (включая `summary`, `models`, `health`, `costs`) и инструмент
`ops_metrics` удалены намеренно; панель метрик внутри Hermes также удалена.
Для просмотра метрик используйте Grafana, а для локальной диагностики и
выгрузки данных — CLI `hermes-ops-report --period 7d --format json` на VPS
от имени пользователя `hermes`. Это не slash-команда Telegram. Плагин
`ops-observability` остаётся включённым: его hooks и фоновый worker записывают
события в SQLite, откуда их читает экспортёр Prometheus.

Prometheus textfile создаётся в
`/home/hermes/.hermes/ops/metrics/hermes.prom`. Полная установка автоматически
поднимает для него отдельный node exporter, Prometheus и Grafana, но каждый
слушает только loopback. Поэтому новый публичный порт не появляется.

Экспортёр группирует события по итоговым нормализованным меткам Prometheus.
Для `hermes_turns_total` сохранён приоритет `completed` → `interrupted` → `failed`:
разные комбинации флагов с одинаковым outcome суммируются в одну серию.
`NULL`, пустые и `unknown` значения также не создают дубли; средняя latency
рассчитывается по исходным событиям, а не как среднее уже усреднённых групп.

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
После этого используйте Tailscale IP:

```bash
tailscale ip -4
ssh hermes@100.x.y.z
```

Скрипт не закрывает публичный SSH автоматически: сделать это до проверки
Tailscale означало бы риск потерять доступ к VPS. Сначала откройте вторую SSH
сессию через Tailscale, проверьте reboot, затем в firewall/cloud security group
закройте публичный TCP/22. Dashboard и внутренний API также слушайте только на
`127.0.0.1` и открывайте через Tailscale/SSH tunnel.

В managed-профиле Ansible автоматически настраивает полные HTTPS MagicDNS
адреса через Tailscale Serve для локальных web-сервисов VPS:

- `https://your-hermes-host.tailnet-example.ts.net/` — Hermes Dashboard;
- `https://your-hermes-host.tailnet-example.ts.net:3000` — Grafana;
- `https://your-hermes-host.tailnet-example.ts.net:9090` — Prometheus;
- `https://your-hermes-host.tailnet-example.ts.net:3001` — code-server;
- `https://your-hermes-host.tailnet-example.ts.net:8888` — приватный SearXNG.

Эти адреса доступны только внутри tailnet и не открывают порты на публичном
интерфейсе VPS.

В managed-конфигурации `hermes_lock_public_ssh: true` включён по умолчанию.
Перед запуском Ansible VPS должен быть подключён к tailnet, а вход по Tailscale
SSH — проверен из второй сессии. Тогда playbook проверит Tailscale IP, включит
UFW, разрешит SSH только через `tailscale0` и запретит TCP/22 на публичных
интерфейсах. Если это первый deploy и Tailscale ещё не авторизован, сначала
выполните bootstrap ниже либо временно переопределите
`vps_deploy.features.lock_public_ssh: false`; не включайте firewall до проверки
доступа. Отдельно закройте порт 22 в cloud firewall/security group провайдера
VPS.

Для неинтерактивной установки используйте `--skip-tailscale-login`, затем
выполните `sudo tailscale up --ssh`. Полностью отказаться можно флагом
`--without-tailscale`.

Если Hermes уже установлен и нужно одним SSH-сеансом подключить VPS к tailnet
и открыть локальные web-сервисы только внутри Tailscale, запустите:

```bash
sudo ./ops/setup-tailscale-access.sh
```

Скрипт устанавливает Tailscale при необходимости, запускает интерактивную
авторизацию с Tailscale SSH, настраивает Serve для Dashboard, Grafana,
Prometheus, code-server и SearXNG, а также добавляет полный MagicDNS hostname в
`~/.hermes/config.yaml`, чтобы Hermes принимал Host header от Serve. Публичные
порты VPS скрипт не открывает.

Для нового VPS полный порядок такой:

1. Установите Hermes обычным способом и войдите на VPS по временному
   публичному SSH.
2. Передайте bootstrap-скрипт и запустите его одной командой:

   ```bash
   scp hermes/ops/setup-tailscale-access.sh root@VPS_PUBLIC_IP:/root/
   ssh root@VPS_PUBLIC_IP 'chmod 0755 /root/setup-tailscale-access.sh && /root/setup-tailscale-access.sh'
   ```

   Если скрипт уже находится на VPS, достаточно выполнить:

   ```bash
   sudo hermes-setup-tailscale-access
   ```

3. Откройте ссылку авторизации Tailscale, которую напечатает скрипт, и
   подтвердите VPS в том же tailnet, где находится iPhone.
4. Проверьте SSH по адресу `100.x.y.z` и откройте выведенные полные HTTPS
   web-адреса Dashboard, Grafana, Prometheus, code-server и SearXNG.

Скрипт не закрывает временный публичный SSH. После проверки Tailscale переведите
Ansible на Tailscale-адрес; стандартное значение `lock_public_ssh: true` при
следующем deploy закроет публичный SSH. На сервере без подтверждённого
Tailscale-доступа временно задайте `lock_public_ssh: false`.

### Управляемое обновление и автоподъём

Текущий pin — стабильный [Hermes 0.21.4 / v2026.9.21](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.21).
Commit и SHA-256 installer берутся только из `vps_deploy.hermes_source`.
Локальные gateway/Telegram-патчи адаптированы к разделённым upstream-модулям;
Edge TTS retry оборачивает новый provider entry point, сохраняя остальные providers.
Изменение pin в Git само по себе не обновляет работающий VPS — нужен deploy ниже.

После проверки SHA-256 скачанного installer managed deploy адаптирует его
Git-update: существующий checkout переключается сразу на pinned commit, без
промежуточного обновления `main`, и лишь затем восстанавливаются локальные правки.
Python-подготовщик получает только текст через stdin и возвращает его через stdout;
он не принимает файловые пути. Shell заменяет свой временный файл только после
успешной подготовки, сохраняя скачанный installer при ошибке преобразования.
Неизвестная структура installer останавливает deploy ещё до остановки gateway.
Конфликт восстановления останавливает установку без сброса рабочего дерева;
stash сохраняется. Managed deploy создаёт собственные
`hermes-managed-install-autostash-*`; их наличие или неразрешённые Git-конфликты
блокируют повторный запуск до ручного разбора. Исторические upstream-stash
`hermes-install-autostash-*` и пользовательские WIP не блокируют обновление,
не применяются и не удаляются: deploy сохраняет текущие локальные правки отдельно.
Обычный Hermes backup исключает `hermes-agent/` и не заменяет сохранение Git-правок.

Azure CI задаёт `ANSIBLE_LOCAL_TEMP` только для своего агента; временный каталог
на VPS выбирает Ansible. Путь `$(Agent.TempDirectory)` не передаётся как remote temp.

Перед следующим обновлением проверяйте патчи на чистом checkout выбранного
upstream commit (не на установленном Hermes с личными данными):

```bash
HERMES_UPSTREAM_DIR=/path/to/hermes-agent python3.11 -m unittest \
  hermes.runtime.test_hermes_upstream -v
```

Проверка сверяет commit/version/installer checksum, применяет gateway-патчи
к временным копиям, компилирует результат и проверяет идемпотентность,
маршрутизацию команд, приоритеты Telegram menu и совместимость проверки backup
со штатным обходчиком файлов закреплённой версии. Она не устанавливает Hermes,
не обращается к VPS и не заменяет backup и post-deploy проверки.

#### Режимы deploy

По умолчанию playbook запускается в консервативном режиме `full`: он может
обновить pinned Hermes, применяет всю инфраструктурную конфигурацию и создаёт
полный backup перед config-only изменениями. Для уже проверенного VPS доступны
явные быстрые режимы:

```bash
# Изменить managed-конфигурацию и инфраструктурные настройки без upstream update
ansible-playbook -i inventory.yml playbook.yml -e hermes_deploy_mode=config-only

# Применить только Hermes runtime и service configuration; сеть, SearXNG,
# GitHub tooling и code-server не изменяются
ansible-playbook -i inventory.yml playbook.yml -e hermes_deploy_mode=runtime-only
```

Оба быстрых режима откажутся запускаться, если установленный Hermes не совпадает
с pinned commit, отсутствует его venv или маркер успешного завершения установщика
`<hermes_user_home>/.hermes-install-complete` с этим commit. В этом случае сначала
запустите `full`. На ранее установленных VPS без маркера потребуется один полный
проход установщика с обязательным backup. Маркер удаляется только после успешной
проверки backup, непосредственно перед установкой, и записывается в конце
`deploy-hermes.sh`; ошибка после обновления HEAD не считается завершённой установкой.
Маркер подтверждает завершение shell-установщика, а не последующих задач Ansible
или live-проверок: они выполняются playbook отдельно.

Production VPS обновляется повторным запуском Ansible playbook. Playbook
сравнивает установленный commit и маркер завершения с `vps_deploy.hermes_source.commit`,
проверяет наличие venv и повторяет установку при расхождении или незавершённом
предыдущем запуске. Перед изменением кода deploy обязательно:

1. синхронизирует restorable mirror личного workspace `AGENTS.md` и
   останавливает managed gateway;
2. инвентаризирует сохраняемые файлы и запускает `PRAGMA integrity_check` для `kanban.db` и всех
   `kanban/boards/**/kanban.db`;
3. записывает counts задач по статусам;
4. создаёт полный backup и проверяет ZIP CRC, наличие каждого файла, полноту
   Kanban DB и counts внутри архива;
5. после обновления повторяет inventory/integrity/counts и требует, чтобы
   личные файлы не исчезли, а Kanban точно совпал.

Если commit уже совпадает, source installation пропускается, но перед
изменением config/ops всё равно создаётся такой же обязательный full backup.
При ошибке backup ранее активный gateway возвращается в работу, а managed
configuration не меняется.

Любая ошибка до установки прекращает обновление и возвращает прежний gateway в
работу. Ошибка после начала установки прекращает дальнейший deploy и оставляет
gateway остановленным для безопасного ручного разбора. Отчёты и архив находятся
в `/home/hermes/hermes-backups/pre-deploy-*`. После применения config, ops и
timers Ansible перезапускает включённый gateway последней изменяющей операцией,
а затем проверяет, что service находится в состоянии `active`.

В конфигурации также включён [Hermes Workspace](workspace-ui/README.md):
отдельная приватная панель/Office поверх существующего gateway и общего
Hermes home. Перед первым deploy добавьте `API_SERVER_KEY` и
`HERMES_WORKSPACE_PASSWORD` в Vault; оба значения — строки минимум по 4 символа
(длинные случайные значения безопаснее; минимум 4 символа — только технический
нижний порог, а не рекомендация. Для production используйте отдельные
высокоэнтропийные значения, например `openssl rand -hex 32`: хеширование
API-значения через SHA-256 не увеличивает стойкость короткого исходного
секрета). API-значение
преобразуется в SHA-256
для внутренней авторизации; пароль Workspace используется как введён.
Для Sessions/Skills/Jobs сначала войдите в официальный Dashboard на
`https://<VPS-Tailscale-hostname>/login` (443), затем в Workspace на том же
hostname с портом **3002**. Managed bridge передаёт текущую Dashboard-сессию
и её обновлённые cookies; Gateway API token не заменяет этот вход.
Настройки модели, изменённые через UI, теперь сохраняются при повторном deploy
с включённым Workspace. Подробности владения настройками — в инструкции выше.

Для сложных задач из Telegram настроена [политика нативного делегирования](workspace-ui/README.md#задачи-из-telegram-и-субагенты):
исследователь, разработчик, проверяющий и диагност; до двух исполнителей в одном
вызове, без вложенного делегирования, с бюджетами времени и шагов. Главный Hermes
должен проверить результаты и вернуть единый итог в исходный чат. Это не создаёт
постоянных агентов в `/swarm` и не делает Office монитором всех Telegram-сессий.

Запускайте управляемое обновление с Ansible controller:

```bash
ANSIBLE_CONFIG=hermes/ansible/ansible.cfg ansible-playbook -i hermes/ansible/inventory.ini \
  hermes/ansible/playbook.yml --ask-vault-pass
```

`inventory.ini` содержит только нейтральный алиас `hermes_target`.
Значения `ansible_host` и `ansible_user` задаются в зашифрованном
`ansible/group_vars/all/vault.yml`, поэтому адрес VPS не хранится в репозитории.

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
других integrations. Non-secret LLM-разделами `config.yaml` управляет
`config/vps-defaults.yml`; Vault содержит только credentials. Секреты расшифровываются на
управляющем компьютере только во время запуска. `.env` и `config.yaml`
записываются с владельцем `hermes` и mode `0600`; чувствительные Ansible tasks
используют `no_log: true` и отключённый diff.

Полные шаги и команды для создания, шифрования, редактирования и применения
`group_vars/all/vault.yml` находятся рядом с файлом в
[`ansible/group_vars/all/VAULT.md`](ansible/group_vars/all/VAULT.md). Шаблон
значений — [`vault.yml.example`](ansible/group_vars/all/vault.yml.example).

Основной deploy запускается из каталога `hermes`:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml \
  --ask-vault-pass \
  --ask-pass
```

Флаг `--ask-pass` нужен при SSH-входе по паролю. При настроенном
`ansible_ssh_private_key_file` запускайте ту же команду без него.

При подключении через **Tailscale SSH** `--ask-pass` тоже не нужен:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml --ask-vault-pass
```

Перед сбором фактов playbook проверяет SSH отдельным подключением без
повторного использования старого SSH control socket. Для адресов tailnet
(`100.64.0.0/10`, Tailscale IPv6 или `*.ts.net`) выполняются максимум две
попытки по 60 секунд, с сообщением о состоянии каждые 10 секунд.
Если Tailscale требует check-mode авторизацию, ссылка выводится прямо в
интерактивный терминал controller и автоматически открывается браузер
(на macOS — `open`, на Linux — `xdg-open`, если доступен). Ссылка не попадает
в результат задачи или CI-лог; пароль и Vault-секреты не передаются в команду SSH.
При локальном запуске без `/dev/tty` (например, из IDE) браузер всё равно
открывается, если доступен opener, и проверка ждёт SSH в тех же пределах.
Если нет ни терминала, ни opener, ссылка печатается в вывод playbook на
controller без записи в Ansible log (не в результат задачи) и повторяется с
каждой новой попыткой, чтобы подтвердить запрос вручную. Ошибка открытия
браузера не прерывает ожидание: авторизация могла быть начата другим локальным
процессом. Успех означает успешное SSH-подключение, а не запуск браузера или
только вход в аккаунт.

Если вкладка закрыта без подтверждения, по истечении таймаута процесс SSH
завершается и создаётся новое подключение с новым запросом авторизации.
После двух неудачных попыток deploy прекращается **до изменения VPS** с
понятной ошибкой. Закрытие вкладки само по себе не определяется — повтор
происходит по таймауту. После подтверждения playbook продолжает работу сам.
Сбор фактов выполняется отдельной задачей с общим лимитом 60 секунд; нумерация
прогресса начинается с проверки подключения, а не с последнего шага.

В CI (`CI`, `TF_BUILD` или `GITHUB_ACTIONS` равен `true`/`1`) запрос browser
approval сразу завершает проверку ошибкой без открытия браузера или вывода
ссылки. Для автоматического deploy нужна отдельно разрешённая
SSH-идентичность; playbook не отключает check mode, ACL или проверку host key.
Для обычного SSH по публичному адресу эта browser-проверка пропускается.
При обычном password SSH через tailnet пароль обрабатывает сам Ansible,
а не проверочный subprocess; используйте `--ask-pass` как раньше.

#### Ручной production deploy из Azure DevOps

Отдельный [`azure-deploy-hermes.yml`](../azure-ci/azure-deploy-hermes.yml) запускает тот
же playbook вручную из Azure DevOps. Он не заменяет локальный запуск выше:
`--ask-vault-pass`, `--ask-pass` и локальный `group_vars/all/vault.yml`
продолжают работать без изменений. Существующий `azure-ai-code-review.yml` также
остаётся отдельным launcher для AI code review и VPS не изменяет.

Перед первым запуском создайте новый Azure pipeline из существующего YAML-файла
`azure-ci/azure-deploy-hermes.yml` в ветке `main`. Затем в **Pipelines → Library →
Secure files** загрузите:

| Secure file | Содержимое |
|---|---|
| `vault.yml` | Текущий зашифрованный `ansible/group_vars/all/vault.yml`, загруженный напрямую |
| `hermes-vps-known-hosts` | Проверенная запись SSH host key VPS |

В **Library → Variable groups** создайте `hermes-deploy-secrets` и добавьте
две переменные, включив для каждой **Keep this value secret**:

| Secret variable | Содержимое |
|---|---|
| `HERMES_VAULT_PASSWORD` | Пароль текущего Ansible Vault |
| `HERMES_TAILSCALE_AUTH_KEY` | Отдельный reusable + ephemeral CI auth key с тегом `tag:hermes-deploy` |

Значения вводите одной строкой, без дополнительных кавычек и без Base64.
Группа подключается только к stage `DeployProduction`; этап фиксации SHA
не получает эти секреты. Старые Secure Files с паролем и CI-ключом больше
не используются pipeline; удалять их следует только после проверки, что
другие pipeline от них не зависят.

Готовые безопасные шаблоны и команды подготовки находятся в
[`ansible/azure-secure-files`](ansible/azure-secure-files/README.md). Рабочие
файлы в этом каталоге игнорируются Git; в Secure Files загружаются только два
файла из таблицы без суффикса `.example`. Локальный файл пароля нужен лишь
для подготовки Vault, загружать его в Secure Files не нужно.

Этот pipeline использует **Tailscale SSH**: он должен быть включён на VPS,
а SSH policy должна разрешать CI-тегу вход без browser check. Auth key лишь
подключает CI node к tailnet, сам по себе он не даёт право SSH-входа.
Private SSH key и `.pub` не нужны ни в ADO, ни на VPS; обычный OpenSSH через
tailnet этим pipeline не поддерживается. После обновления YAML в `main` старый
Secure File `hermes-vps-ssh-key` можно удалить, если другие pipelines его не
используют. Локальные ключи автоматически не удаляются.
`known_hosts` создавайте на доверенном компьютере и сверяйте fingerprint
через консоль VPS-провайдера до загрузки. Не получайте и не принимайте новый
host key прямо внутри pipeline.

Для каждого Secure File и группы `hermes-deploy-secrets`:

1. В **Pipeline permissions** разрешите только production deployment pipeline;
   не включайте **Open access**.
2. В **Approvals and checks** добавьте **Branch control** для
   `refs/heads/main` и approval владельца репозитория. Переключайте на
   `refs/heads/*` только после зафиксированного sign-off владельца в change
   request/PR.

Создайте Azure Environment `hermes-vps`. В его **Approvals and checks**
добавьте approval владельца, **Branch control** для `refs/heads/main` и
**Exclusive lock**. У самого pipeline оставьте право **Queue builds** только
владельцу. Эти проверки задаются в Azure UI, а не в YAML. Branch control
проверяет все связанные repository resources. Шаблон `refs/heads/*` разрешает
любую ветку кода `deploySource`, не разрешая теги; сохраните проверку защиты
ветки, approvals и остальные checks. Это разрешает production deploy любого
branch ref, включая ещё не проверенный. Владелец должен зафиксировать принятие
риска в change request/PR до merge и изменения allowlist; пока sign-off не записан,
оставляйте `refs/heads/main`. Только после sign-off переключите Environment
Branch control на `refs/heads/*`. Для каждого run проверяйте SHA в Summary и одобряйте
именно его. Настройте защиту всех deployable веток, если Branch control требует
protected source branches. YAML фиксирует выбранный resource commit и публикует
Summary до production stage; approval и Branch control задаются отдельно в Azure
UI на Environment `hermes-vps` и Secure Files. Код выбранной ветки получит
production credentials только после этих checks и approval.
Перед первым production run с feature ref владелец сверяет в Azure UI, что
Environment и оба Secure Files требуют owner approval и protected source branch;
Queue builds остаётся только у владельца. Не запускайте такой run, пока эти
checks не проверены.

Repository resource `deploySource` использует GitHub service connection
`github.com_YauheniPo`; разрешите его использование deployment pipeline без
**Open access**, если разрешение ещё не выдано.
После попадания этой версии YAML в `main`, в **Run pipeline** оставьте pipeline
на `main` и укажите короткое имя ветки в параметре **Branch to deploy**:

| Поле | Значение |
|---|---|
| Branch/tag (ветка самого pipeline) | `main` — не меняйте на feature-ветку |
| Branch to deploy | `<branch>` (например, `feat/hermes-ai-digest-cron`); по умолчанию `main` |
| Ansible deployment mode (`deployMode`) | `full`, `config-only` или `runtime-only` |

Отдельный чекбокс подтверждения production не требуется: достаточно ручного
**Run pipeline**, затем настроенных approvals в Azure. Автоматические CI/PR
triggers выключены; проверки ветки, доступа, секретов и backup сохраняются.

`full` сохраняет консервативный путь установки/обновления; `config-only`
применяет конфигурацию без обновления upstream Hermes; `runtime-only` ограничивает
изменения runtime/services. Подробности и ограничения — в
[режимах deploy](#режимы-deploy). Это тот же `hermes_deploy_mode`, что при
локальном запуске; политика backup не меняется.

Pipeline:

1. проверит, что definition запущен из `main`;
2. через GitHub connection скачает `self` из `main` и выбранную параметром
   ветку `deploySource` в разные каталоги без сохранения credentials; helper из доверенного `main`
   сверит checkout с SHA resource Azure и сохранит архив этого commit,
   не вычисляя вершину ветки заново;
3. до approvals опубликует в Summary ветку, SHA и режим. Проверьте их перед
   согласованием environment, Secure Files и группы переменных;
4. после approvals возьмёт архив **из этого же запуска**, проверит защищённые
   файлы, выполнит syntax-check и Ansible deploy выбранного режима с проверкой
   SSH host key. Push в выбранную ветку во время ожидания не меняет этот deploy.

Несуществующая/некорректная ветка останавливает запуск до получения Secure Files.
Ветка должна находиться в том же репозитории и содержать Hermes playbook;
выбирайте в picker ветку, а не тег. Helper принимает только ref
`refs/heads/...` и SHA выбранного resource; смена репозитория на fork не поддерживается.
Для нового SHA запускайте новый pipeline; повтор deploy job использует прежний
артефакт. GitHub credentials, `.git` и незакоммиченные локальные файлы в него не
попадают. Доступ к артефакту исходников ограничьте доверенными пользователями.

Pipeline теперь подключает одноразовый hosted agent к tailnet и до deploy
проверяет SSH/sudo с общим таймаутом 75 секунд, без ожидания browser login.
После job (включая сбой/отмену) выполняется logout. Однократно настройте теги,
ограниченные сетевые/SSH правила и secret variable `HERMES_TAILSCALE_AUTH_KEY` по
[инструкции Tailscale для ADO](../azure-ci/tailscale-deploy.md).
Не назначайте тег VPS до сохранения личного доступа: `autogroup:self` не
покрывает tagged node. Личные browser checks не отключаются.

Зашифрованный Vault передаётся как защищённый extra-vars файл и не копируется в
checkout. Пароль Vault и CI-ключ временно записываются на agent в файлы `0600`
в каталоге `0700`, без вывода значений в лог или аргументы команд. Отдельный
шаг `always()` удаляет их при успехе, ошибке и отмене. При аварийной потере
agent этот шаг не гарантирован; используется одноразовая hosted VM.
Azure удаляет скачанные Secure Files после job. Доступ к логам
pipeline тоже должен оставаться только у доверенных пользователей: SSH-ошибка
может содержать адрес конечного host, даже если адрес отсутствует в Git.

`vps_hermes.config.managed_overlay` — selective authoritative overlay: каждый явно указанный
вложенный ключ заменяет соответствующее значение в существующем `config.yaml`,
а остальные ключи, в том числе в том же разделе, сохраняются. Списки
заменяются целиком. API key храните только в `hermes_secret_env`; не используйте
inline `api_key` в `config.yaml`.

State разделён по владельцам. Vault полностью управляет `.env`, repository —
только указанными config-ключами, отдельными marked blocks в workspace
`AGENTS.md`/`SOUL.md` и обязательными локальными source patches. Hermes сохраняет
остальную часть `AGENTS.md`, `SOUL.md`, memory, sessions, profiles и custom
skills. Поэтому новый deploy применяет исправленные настройки из кода, но не
стирает накопленную персонализацию агента.

`workspace/AGENTS.md` в Memory-редакторе Workspace — тот же живой файл, а не
отдельная копия настроек. Общий блок берётся из `instructions/common.md`,
окружение VPS — из `ansible/AGENTS.md` с явным текущим статусом host administration;
GitHub — из `ansible/tasks/github.yml`, DevOps, SearXNG и delegation — из
соответствующих `ansible/templates/*-*.md.j2`, имена Vault variables — из
`ansible/playbook.yml`.
Карта исходников есть в разделах **Instruction ownership** и **Integration sources and wiki**.
Общие правила меняйте только в `instructions/common.md`, особенности окружения —
в `ansible/AGENTS.md` или `docker/AGENTS.md`, интеграции — в их исходниках;
не копируйте весь live-файл обратно в исходный фрагмент, иначе появятся вложенные
managed-блоки и дубли. Правки через UI внутри маркеров следующий deploy заменит,
правки снаружи сохранит. При пустом `vps_web.searxng_url` инструкции явно сообщают,
что endpoint не настроен, и не предлагают нерабочую команду с адресом `/search`.

Оба способа установки используют `runtime/manage-workspace-agents.py`: общий
блок `HERMES MANAGED COMMON` и блок окружения `HERMES MANAGED ENVIRONMENT`
обновляются без дублирования. Общие правила остаются при `host_admin: false`;
блок окружения явно запрещает администрирование хоста. Это инструкции, не замена
системным ограничениям прав. Docker обновляет блоки при каждом старте контейнера
из файлов image; для новых исходников нужен rebuild. Старый VPS managed-блок
заменяется; старый Docker-текст мигрирует только при точном совпадении с известным
префиксом (`runtime/legacy/container-instructions.md` — замороженные данные миграции,
не источник активной политики). Неузнанный изменённый текст сохраняется для ручной
проверки; старый marker-файл не разрешает перезапись личных инструкций.

Устаревшие ключи удаляются только через явный `vps_runtime.unset`; deploy не
угадывает, что неизвестный ключ можно безопасно стереть. После всех изменений
обязательный `hermes config check` выполняется до запуска gateway. Если остался
невалидный ключ, deploy остановится: сначала классифицируйте его как managed
значение или добавьте точный путь в `unset`. Memory, skills и identity при этом
не меняются.

По умолчанию `vps_deploy.features.host_admin: true`, поэтому playbook создаёт
`/etc/sudoers.d/hermes-host-admin`, устанавливает Docker Engine и добавляет
Hermes в группу `docker`. Поэтому Hermes сможет устанавливать пакеты, запускать
контейнеры и системные services по вашему запросу без отдельного SSH-вмешательства.
И `sudo`, и Docker group дают полный root-equivalent доступ; правило в его
`AGENTS.md` запрещает удаление данных, но не может технически ограничить root.
Задайте `false`, если нужен ограниченный VPS.

В текущем профиле `vps_deploy.secret_environment.managed: true`: Vault — источник истины для
всего Hermes `.env`: ручные изменения на VPS будут заменены следующим запуском
playbook. Сохраняйте в Vault все используемые ключи; пустой `hermes_secret_env`
останавливает deploy до перезаписи `.env`. Добавление и ротация ключей описаны в
[`ansible/group_vars/all/VAULT.md`](ansible/group_vars/all/VAULT.md).

Для dashboard с Nous OAuth добавьте в тот же `hermes_secret_env` два значения:
`HERMES_DASHBOARD_OAUTH_CLIENT_ID` из вывода `hermes dashboard register` и
`HERMES_DASHBOARD_PUBLIC_URL` с полным адресом вида
`https://your-hermes-host.tailnet-example.ts.net`. Это не API tokens, но Ansible
управляет ими вместе с `.env`, поэтому они не исчезнут при следующем deploy.
URL должен совпадать с зарегистрированным redirect URI. Для входа используйте
только полный HTTPS hostname tailnet с доменом `.ts.net`.

Если Vault содержит оба значения `TELEGRAM_BOT_TOKEN` и
`TELEGRAM_ALLOWED_USERS`, playbook автоматически поднимет gateway уже с
обновлёнными ключами. Если хотите продолжать вводить credentials вручную через
`hermes model`, задайте `vps_deploy.secret_environment.managed: false`.

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

Перед изменением уже установленного Hermes playbook останавливает gateway,
создаёт полный backup и проверяет ZIP CRC, наличие каждого живого файла,
целостность всех Kanban SQLite DB и количество задач по статусам. Это выполняется
и при смене commit, и при config-only deploy того же commit; неуспешный backup
останавливает deploy до изменения runtime state.

Playbook устанавливает Hermes, Tailscale, operations-слой и systemd units. Для
полного восстановления с конфигурацией, OAuth/API credentials, memory,
сессиями, profiles и skills передайте полный архив `hermes backup`:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory.ini \
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

Для Git автоматически включаются настройки из `vps_github.git_defaults`:
ветка `main` по умолчанию, prune веток/tags, fast-forward-only pull и
автоматическая привязка новой ветки при первом push. Ansible берёт commit
identity, GitHub owner, workspace и write boundaries из `vps_github` в едином файле
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

Для Firecrawl `web_extract` добавьте `FIRECRAWL_API_KEY` в `hermes_secret_env`
Vault и прогоните playbook: непустой ключ включает capability
`firecrawl_extract` (`ansible/tasks/runtime.yml`), а она задаёт
`web.extract_backend: firecrawl` (`config/vps-defaults.yml`). Отдельной ручной
настройки не требуется, без ключа остаётся `auto`. Порядок работы — поиск,
извлечение, браузер — репозиторий ставит в managed-блок `workspace/AGENTS.md`
разделом «Skills and research» из `instructions/common.md`; личные правила под свои задачи
дописывайте в том же файле вне managed-маркеров, deploy их сохраняет.

Для собственного SearXNG задайте `vps_web.searxng_url` в
`config/vps-defaults.yml` (например, `http://127.0.0.1:8888` для контейнера на
том же VPS). Непустое значение пишет `SEARXNG_URL` в управляемый `.env` и
включает capability `searxng_search`, то есть `web.search_backend: searxng`.
Приоритет выше Brave: пока endpoint задан, `BRAVE_SEARCH_API_KEY` остаётся в
Vault, но backend поиска не занимает. Очистите значение — и deploy вернёт
Brave. Hermes держит один search backend одновременно и сам между ними не
переключается: при недоступном SearXNG поиск не перейдёт на Brave
автоматически. SearXNG — search-only, извлечение страниц остаётся за Firecrawl
или локальным браузером.

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
Версия пакета закреплена в `vps_browser.agent_browser_version`, поэтому новый
deploy не получает другой browser runtime при неизменном commit проекта.

Для этого VPS playbook задаёт `browser.backend: off`: это отключает Browser Use
CLI и оставляет встроенные `browser_*` инструменты Hermes поверх проверенного
`agent-browser`. Причина — Browser Use CLI на headless VPS мог быть установлен,
но не запускать собственный Chrome harness. Не меняйте это значение, пока не
появится успешная live-проверка именно `browser_exec`.

На Ubuntu 23.10+ некоторые VPS-образы блокируют sandbox Chromium через
AppArmor. Поэтому `vps_browser.launch_args` задаёт для отдельного
непривилегированного пользователя `hermes` параметры
`--no-sandbox,--disable-dev-shm-usage`. Они позволяют
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

Hermes использует встроенный terminal и bundled skill `github` (auth, PR
review, issues, workflow, repo management — с v0.21.0 объединены в один
скилл вместо прежних шести отдельных). Отдельный GitHub MCP не нужен.
`git` выполняет локальные операции; `gh` — PR, unresolved review threads,
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

Git defaults, identity и credential helper в блоке `HERMES MANAGED GIT DEFAULTS`
принадлежат Ansible. Shell-установщик сохраняет этот блок; без него применяет
defaults через `git config --replace-all`, чтобы существующие дубли не прерывали
установку. Остальные личные Git-настройки сохраняются.

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

Перед полным deploy (`full`) уже установленного Hermes полный backup обязателен;
быстрые `config-only` и `runtime-only` не создают такой архив перед каждым запуском. Проверка
требует ZIP CRC, присутствия всех файлов, которые штатный full backup обязан
сохранить (включая `SOUL.md`, custom skills, sessions, profiles и зеркальную
копию workspace `AGENTS.md` и дополнительных инструкций), а для всех Kanban DB — SQLite integrity и
неизменные counts по статусам. При source update дополнительно сравниваются
личные файлы и Kanban до/после установки.

Проверяемый список учитывает штатные исключения закреплённого Hermes:
`node/`, `models/`, `runtimes/` и `browser_profiles/` исключаются только в корне
Hermes и `profiles/<name>/`. В `cache/` на этих уровнях обязательны `images/`,
`audio/`, `videos/`, `documents/`, `screenshots/` и `citations/`; прочие временные
данные не требуются. Одноимённые вложенные каталоги skills остаются личными
данными. `browser-profile/` и `browser-profiles/` исключены штатным backup как
runtime-профили браузера. Отсутствие обязательных файлов по-прежнему прерывает
deploy; сообщение ограничено количеством и первыми 20 отсутствующими путями.

При source update установщик сначала скачивается по закреплённому commit и
проверяется по SHA-256, пока gateway продолжает работать. Загрузка допускает
до четырёх попыток при временных ошибках (включая HTTP 429), учитывает
`Retry-After`: таймаут соединения — 10 секунд, одной передачи — 30 секунд,
бюджет повторов — 120 секунд (последняя передача может добавить до 30 секунд).
В логах видны повторы, итоговый HTTP status и код ошибки curl, без содержимого
ответа. Ошибка загрузки или checksum прерывает deploy без остановки gateway.
Только после успешной проверки установщика gateway останавливается для
согласованного полного backup; установка начинается после проверки backup.

Временный `gateway.lock` не входит ни в архив, ни в проверяемый список файлов:
он может исчезнуть при остановке процесса между обходом каталогов и чтением.
До обязательного backup (config-only и source update) deploy применяет только
backup-патч через `apply-hermes-patches.py --backup-only`; остальные runtime-патчи
остаются после backup. Исключение также действует для последующих штатных full
backups, включая scheduled backup, в основном home и профилях. Патч идемпотентен.
Pre-backup проход работает с *установленной* версией Hermes: если её
commit отличается от закреплённого и архиватор не совпадает с анкором под новую
версию, deploy пишет warning и делает backup без исключения; патч применяется
после установки. При config-only deploy или повторной установке того же commit
несовпадение анкора прерывает backup и возобновляет ранее работавший gateway.
Отсутствие
самого файла архиватора по-прежнему прерывает deploy. В полном проходе после
установки несовпадение анкора остаётся ошибкой.
Исчезновение личных файлов по-прежнему останавливает deploy; остальные
`*.lock` не исключаются автоматически.

Git metadata `.git` также исключается из проверки и как каталог, и как
служебный файл worktree (в том числе у Swarm workers). Рабочие файлы,
незакоммиченные изменения и `.gitignore` остаются в проверяемом списке.
Hermes backup не заменяет резервную копию Git history и настройку worktrees.

Systemd создаёт ещё и scheduled backup:

```text
/home/hermes/hermes-backups
```

Первый scheduled backup полный. Затем ежедневный запуск создаёт quick snapshot
в обычные дни и full snapshot раз в неделю. Quick snapshots хранятся 14 дней,
из full сохраняются последние 5, а из обязательных `pre-deploy` и
`pre-config-deploy` архивов — последние 10 групп вместе с их state manifests.
Health check отдельно сообщает, если любой backup старше 26 часов или полный
старше 8 дней. Пороги, день недели и retention меняются в `vps_ops` файла
[`config/vps-defaults.yml`](config/vps-defaults.yml) и применяются deploy.

Retention deploy-архивов ограничен числом групп, а scheduled quick snapshots —
возрастом, не количеством: несколько запусков в сутки могут оставить больше
14 снимков за 14 дней. Ручные snapshots и записи с неизвестным/повреждённым
manifest автоматически не удаляются. Очистка выполняется после успешного backup
или на этапе retention deploy; прерванные прогоны могут временно превысить лимит.
Ограничение числа архивов не ограничивает их общий размер в байтах.

Расширенный **scheduled quick backup** дополнительно сохраняет `SOUL.md`,
`AGENTS.md`/`AGENTS.*.md`, `memories/**`, `external_memory_providers.json`,
`swarm/swarm.yaml`, а также личные инструкции, memory, `config.yaml`, `.env`
и `auth.json` каждого существующего профиля, включая маркер `.managed-swarm`.
Для managed Swarm ссылки `.env`/`auth.json` на глобальные credentials не копируются:
сохраняется глобальный файл, а ссылки восстанавливает Ansible Swarm deployment.
Тяжёлые profile sessions, исходники, skills и caches не добавляются в quick;
для них нужен full backup с учётом штатных исключений Hermes.
Ручной `hermes backup --quick` остаётся штатным,
без нашего расширения; для расширенного снимка используйте backup service.

Перед scheduled/full-deploy backup все `AGENTS.md` и `AGENTS.*.md` под
`hermes_workspace` (включая вложенные проекты) зеркалируются в приватный
`operator-state/workspace-instructions.json`. `.git`, dependencies, caches,
backup-каталоги и symlink-каталоги не обходятся; ссылки вместо самих инструкций
отклоняются. Удалённые инструкции исчезают из нового зеркала, но restore
не удаляет другие файлы в целевом workspace. Произвольные пути вне workspace,
внешние skills и данные других сервисов этим зеркалом **не покрываются**.

Scheduled full проверяет ZIP CRC и наличие ожидаемых личных файлов/профилей
до публикации архива. Quick проверяет manifest и наличие/размер файлов до
публикации; при ошибке прежние снимки не удаляются. Формат quick остаётся
штатным, совместимым с `/snapshot restore <ID>` в интерактивном Hermes CLI;
для внешних workspace-инструкций после такого restore дополнительно выполните
от имени `hermes`:

```bash
python3 /usr/local/lib/hermes-ops/backup-personal-state.py restore \
  --hermes-home /home/hermes/.hermes --workspace /home/hermes/workspace
```

Выполняйте restore при остановленных gateway/Workspace/workers, предварительно
сохранив текущие данные. При восстановлении полного ZIP через Ansible дополнительные
workspace-инструкции восстанавливаются автоматически из выбранного архива.
Пути выше — стандартные; при кастомном home/workspace используйте свои значения.

Backup не меняет правила redeploy: Vault перезаписывает managed `.env`, Ansible
обновляет managed ключи конфигурации и блоки инструкций. Личные записи вне этих
блоков сохраняются. Browser localStorage и внешние Docker volumes не входят в
Hermes backup. Архивы содержат секреты: права `0600` — не шифрование; храните
выгруженные копии в зашифрованном хранилище.

Также включается `updates.pre_update_backup: full`: перед будущими обновлениями
Hermes создаёт полный архив `HERMES_HOME` с настройками, авторизацией, сессиями,
памятью и skills. Для постоянного перехода на `quick` измените это значение в
`vps_runtime.set` файла `config/vps-defaults.yml`. Ручная команда ниже действует
только до следующего managed deploy:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes \
  config set updates.pre_update_backup quick
```

Важно: local backup на том же VPS не спасает при удалении VPS или отказе диска.
Сейчас система хранит только локальные архивы; шифрованный offsite backup остаётся
отдельной задачей и требует выбранного вами хранилища/credentials. До этого
периодически переносите полный архив на Ansible controller и проверяйте restore.

[Обновления и backups](https://hermes-agent.nousresearch.com/docs/getting-started/updating)

### Автоматическая диагностика

Прямой installer в конце запускает `hermes config check` и `hermes doctor` и
показывает их замечания как warnings, потому что часть функций может ожидать
ещё не настроенную интеграцию. Ansible deploy строже: финальный
`hermes config check` обязан пройти до запуска gateway.

### Проверочный запрос к модели с повторами

После деплоя отдельный helper можно запустить на VPS:

```bash
sudo /usr/local/lib/hermes-ops/api-retry-loop.sh
```

Он читает `vps_ops.api_retry` через `/etc/hermes-ops.conf` и запускает
`hermes chat` от сервисного пользователя с заданными `provider` и `model`.
Ключи загружает сам Hermes из своего окружения; Dashboard для этого не нужен.
Основная модель и настройки cron при таком запросе не меняются.
Каждое переключение на fallback-маршрут записывается в `metrics.db`
(таблица `route_fallbacks`) и видно в Grafana как `hermes_api_fallback_total`.

`max_attempts` ограничивает число запусков CLI, `wait_seconds` задаёт паузу
между неудачами, `timeout_seconds` ограничивает каждый запуск. Внутри одного
запуска Hermes может выполнять собственные повторы API. После успеха скрипт
завершается сразу, после исчерпания попыток возвращает ошибку.
Успех здесь означает код выхода `0` и непустой текст ответа. Пустой ответ или
одни пробелы вызывают повтор: частичный результат Hermes может завершиться с
кодом `0`, не дав ответа. Helper пишет об этом отдельно: `returned no answer
(exit 0)`, сохраняя фактический код выхода CLI в диагностике.
Ошибки использования CLI с кодом `2`, а также коды `126` и `127` не повторяются:
helper передаёт фиксированный набор аргументов, поэтому повтор не может изменить
такую ошибку.

Для одного запуска можно передать другую модель и текст:

```bash
sudo /usr/local/lib/hermes-ops/api-retry-loop.sh '<model-id>' 'Ответь одним предложением'
```

Текст после имени модели — один shell-аргумент. Для обычного текста используйте
одинарные кавычки; если внутри есть одинарная кавычка, экранируйте её по правилам
shell. Для многострочного запроса безопаснее сначала собрать значение heredoc-ом,
а затем передать его в двойных кавычках:

```bash
message=$(cat <<'EOF'
Первая строка
Текст с 'кавычками' и символами $HOME
EOF
)
sudo /usr/local/lib/hermes-ops/api-retry-loop.sh '<model-id>' "$message"
```

Если аргументы не указаны, helper использует `HERMES_API_RETRY_MODEL` и
`HERMES_API_RETRY_MESSAGE` из управляемого `/etc/hermes-ops.conf`; stdin оставлен
для передачи запроса самому Hermes и не заменяет эти настройки.

Провайдер остаётся указанным в конфиге. Helper запускает запрос без toolsets
и передаёт текст через stdin, сохраняя кавычки и переводы строк буквально.

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

Deploy применяет модельную политику из `vps_hermes.config.managed_overlay` в
[`config/vps-defaults.yml`](config/vps-defaults.yml): основной provider и модель,
вспомогательные модели, настройки cron и `fallback_policy.default_routes`. Для каждого
используемого provider нужны его credentials; наличие записи fallback не
заменяет авторизацию. Следующий deploy снова применит модельную политику, но
сохранит существующий `fallback_providers`, включая пустой список. Hermes также
поддерживает built-in providers с API key/OAuth, named custom providers и
локальные OpenAI-compatible endpoints.
Для Ansible укажите нужные ENV keys в `hermes_secret_env`, а non-secret
provider/fallback policy — в `vps_hermes.config.managed_overlay`. Добавление
нового custom endpoint не требует изменения playbook.

Без Ansible либо для OAuth provider запустите официальный мастер один раз для
каждого нужного provider:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes model
```

При Vault workflow API keys уже находятся в закрытом `.env`, а named custom
providers могут быть описаны в `vps_hermes.config.managed_overlay`; выбранная
модель сохраняется до следующего deploy. Мастер автоматически увидит built-in credentials; повторно
вставлять их не нужно. OAuth flows по-прежнему выполняются через `hermes model`,
поскольку их нельзя безопасно заменить статическим API key в Vault.

Для основной работы выбирайте tool-capable модель с достаточным context window.
Model IDs задаются в `config/vps-defaults.yml`; каталоги и доступность меняются. Не
присылайте ключи в Telegram или в чат агенту.

#### Ollama Cloud

Закреплённая версия Hermes уже поддерживает provider `ollama-cloud` с endpoint
`https://ollama.com/v1`. Для него нужен `OLLAMA_API_KEY`; отдельный Ollama
server и настройка custom provider не требуются.

1. В своём terminal из корня repository откройте существующий Vault:
   ```bash
   EDITOR=nano ansible-vault edit hermes/ansible/group_vars/all/vault.yml
   ```
2. Добавьте `OLLAMA_API_KEY` в существующий `hermes_secret_env`, сохранив
   остальные ключи. Значение берётся из [Ollama Keys](https://ollama.com/settings/keys).
   Доставка `.env` включена в `vps_deploy.secret_environment.managed`; если
   старый Vault задаёт `hermes_manage_secret_env: false`, удалите это
   переопределение, чтобы использовалась политика repository.
3. Примените обычный Ansible deploy. Для Azure сначала загрузите обновлённый
   **зашифрованный** `vault.yml` в **Pipelines → Library → Secure files** и
   запустите deployment pipeline из `main` и пройдите настроенные approvals.
   Изменения repository должны быть доступны в
   `main`; изменение только локального Vault не обновляет Azure Secure File.
   Deploy сам перезапустит gateway.
4. Откройте `/model` в Hermes/Telegram и выберите **Ollama Cloud**. Hermes
   получает каталог моделей автоматически. Для явного выбора используйте
   `/model ollama-cloud:<model-id>` с точным ID из каталога.

Для постоянного выбора через Ansible задайте нужные model IDs в
`vps_hermes.config.managed_overlay.model` (`provider` и `default`). Deploy
применяет эту пару к главному агенту, делегированию, cron по умолчанию и
Swarm-профилям. Compression и fallback сохраняют отдельные маршруты.
Подробнее: [единая модель агентов](workspace-ui/README.md#единая-модель-агентов-и-swarm).

### Надёжные cron-задачи

Для задач вида «собрать данные → проверить источники → подготовить отчёт →
доставить результат» используйте сохранённую cron-задачу Hermes с прикреплённым
skill. Время, имя задачи и адрес доставки выбираются при создании задачи через
бота; Ansible их не записывает и не меняет. Ручной запуск той же задачи
выполняется через `/cron run <job_id>` или `hermes cron run <job_id>`. Ответ
приходит в настроенный у задачи канал после завершения, а историю и ошибки
можно посмотреть через `hermes cron runs <job_id>` и `hermes cron doctor`.
После создания проверьте следующее время запуска через `/cron list`: в
закреплённой здесь версии Hermes часовой пояс расписания общий для инстанса,
а не отдельный параметр задачи. Этот deploy его не меняет.

Репозиторий устанавливает пример этого подхода — skill `ai_digest` для новостей
IT/AI. Попросите бота создать cron-задачу с этим skill, нужным расписанием и
доставкой в нужный Telegram-топик. Prompt выбирает режим: `daily` по умолчанию
(релизы/новости, 24 часа, до 5 материалов) или `--mode weekly` (исследования,
подкасты и бенчмарки, 168 часов, до 7 материалов). Тему, окно и число
материалов можно уточнить в prompt. Дополнительная команда `/ai_digest` не
нужна. Для ручной проверки запускайте сохранённую задачу через `/cron run`;
сам skill не отправляет сообщения напрямую, а возвращает краткую сводку и
Markdown-вложение штатному механизму доставки Hermes.

Сборщик skill читает [список источников и лимиты](skills/ai_digest/scripts/sources.json),
сохраняет сырой JSON и журнал сбора в `~/.hermes/ops/news/`, а проверенный
отчёт — в `~/workspace/digests/`. Недоступный источник отмечается в отчёте;
при отсутствии пригодных материалов задача завершается ошибкой. Для новых
подобных задач используйте тот же контракт: ограниченный сбор и явная
атрибуция данных, skill для анализа, проверка файла перед выдачей и штатный
`deliver` cron. Отдельный scheduler, поисковый сервис или LLM-ключ для
дайджеста не требуются. Для недельного выпуска подкасты анализируются по
описанию, а результаты SWE-bench — по опубликованному JSON; изменение ранга
без предыдущего снимка не утверждается. Детали и локальная проверка описаны в
[документации skill](skills/ai_digest/README.md).

Для cron deploy формирует `cron.model_provider` и `cron.model` из общей
модели `vps_hermes.config.managed_overlay.model`. Задачи без собственного pin используют
эти значения, поэтому не зависят от временного переключения модели чата и не
получают `drift_skip` при
изменении интерактивного provider. Личный pin конкретной задачи имеет
приоритет над этой конфигурацией. Если выбранный provider недоступен или не
авторизован, preflight переведёт задачу в `blocked_config` без скрытого запуска
на другой модели.

Для другой общей модели измените `provider/default` в
`vps_hermes.config.managed_overlay.model` и примените Ansible-деплой. Для разовой
задачи задайте pin явно, заменив placeholders на provider и model ID из каталога:

```text
cronjob(action="create", schedule="every 2h", prompt="Check server status",
        provider="<provider>", model="<model-id>", deliver="origin")
```

Проверить существующие задачи можно командами:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes cron list
sudo -u hermes -H /home/hermes/.local/bin/hermes cron status
```

Не добавляйте пустые entries в `fallback_policy.default_routes`: deploy отклоняет
записи без `provider` или `model`. Provider без credentials не проходит preflight.

Переключение внутри Hermes или Telegram не требует перезапуска и не теряет
историю диалога:

```text
/model
/model <built-in-provider>:<model-id>
/model <built-in-provider>/<model-id>
/model_global <built-in-provider>/<model-id>
/model custom:<provider-name>:<model-id>
```

В формате `provider/model` префикс известного встроенного провайдера выбирает
именно его, а оставшаяся часть передаётся как model ID. Например,
`/model nous/meituan/longcat-2.0:free` выбирает provider `nous` и модель
`meituan/longcat-2.0:free`; `/model_global` делает тот же выбор глобальным.
Буквальная команда `/global-model` не существует: используйте `/model_global`
или `/model <provider>/<model> --global`. Если нужна модель агрегатора, чей
vendor-префикс совпадает с именем встроенного провайдера, явно укажите
`--provider openrouter` (или другой нужный агрегатор).

После каждого завершённого текстового ответа агента в Telegram показывается
фактически выбранный для этого хода `provider/model` в отдельном копируемом
code block, включая переключение через fallback. Для потокового ответа блок
отправляется последним сообщением; служебные команды, промежуточный прогресс
и намеренно пустые ответы им не дополняются. Блок не попадает в историю
диалога и не меняет модель в конфигурации. Как и штатный runtime footer
Hermes, в непотоковом ответе он приклеен к тексту, который читают judge
`/loop --until` и проверка `/goal`; в потоковом ответе они видят чистый текст.

После первоначальной настройки проверьте конфигурацию:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes config check
sudo -u hermes -H /home/hermes/.local/bin/hermes doctor
sudo -u hermes -H /home/hermes/.local/bin/hermes status
```

`provider_routing` имеет смысл только для aggregators, которые его поддерживают
(например, Nous Portal), поэтому deploy больше не включает его
глобально. Если выбран такой provider, нужные `sort`, allow/deny lists,
`require_parameters` и `data_collection` задайте в `vps_hermes.config.managed_overlay`.
Provider-specific cache также включается только явно.

Задайте API keys отдельные spending limits и следите за расходом через
Grafana либо `hermes-ops-report --period 7d`. Если provider не сообщает стоимость,
заполните `model-prices.json` актуальными ценами.

Официальные справочники: [providers в Hermes](https://hermes-agent.nousresearch.com/docs/integrations/providers),
[routing aggregators](https://hermes-agent.nousresearch.com/docs/user-guide/features/provider-routing),
[fallback providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers).

#### Резервные модели и провайдеры в Telegram

Managed Hermes использует нативную цепочку fallback без повторного запуска
задачи или выполненных tools. Начальный упорядоченный список задаётся в
`vps_hermes.config.managed_overlay.fallback_policy.default_routes` в
[`config/vps-defaults.yml`](config/vps-defaults.yml). В
`fallback_policy.allowed_providers` того же overlay задаются провайдеры,
которые можно выбирать через чат. Ключи остаются в Vault/штатной авторизации;
chat-команда не принимает credentials или произвольные endpoint URL.

Каждый элемент `default_routes` содержит **оба** поля: `provider` и `model`.
Один provider может встречаться несколько раз с разными моделями; запрещён
только повтор одинаковой пары. `allowed_providers` — разрешения chat-команды,
не список моделей для автоматического выбора. Deploy формирует нативный
`fallback_providers` из `default_routes` при первой установке, а затем сохраняет
активный список пользователя. Старый deploy-ключ `fallback_providers` читается
для совместимости, только если `fallback_policy.default_routes` не задан.

Базовый список проверен по публичным каталогам **23 сентября 2026**:

|| Порядок | Provider | Model ID | Основание выбора |
|| --- | --- | --- | --- |
|| 1 | `openrouter` | *см. текущий дефолт в `vps-defaults.yml`* | Быстрее всего (3.3s avg), 99.8% success rate в метриках Grafana |
|| 2 | `ollama-cloud` | *см. текущий дефолт в `vps-defaults.yml`* | Workhorse для объёмных задач (9s avg, 99.1% success) |
|| 3 | `nvidia` | `nvidia/nemotron-3-super-120b-a12b` | Уже настроен для compression Hermes; agentic reasoning/coding/tools |
|| 4 | `openrouter` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Уже настроен резервом Direct Review, есть в curated-каталоге Hermes |
|| 5 | `nous` | `inclusionai/ling-3.0-flash-sante:free` | Validated для PR review (<15s, 0 retries, 88 runs) |

*Актуальный список маршрутов — в `vps-defaults.yml` (`fallback_policy.default_routes`). README содержит устаревший снимок; при смене маршрутов обновляйте эту таблицу.*

Источники: [каталог OpenRouter](https://openrouter.ai/api/v1/models),
[бесплатные рекомендации Nous](https://portal.nousresearch.com/api/nous/recommended-models),
[каталог API Nous](https://inference-api.nousresearch.com/v1/models),
[curated-каталог Hermes](https://hermes-agent.nousresearch.com/docs/api/model-catalog.json),
[NVIDIA Super endpoint](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b).
У выбранных OpenRouter/Nous routes на дату проверки нулевые input/output цены
и есть `tools` в supported parameters. NVIDIA предоставляет бесплатный endpoint
для прототипирования с ограничениями [Developer Program](https://docs.api.nvidia.com/nim/docs/product),
а не гарантированный бесплатный production SLA. Для Nous требуется существующий
OAuth login, для остальных — их API keys; ключи этой правкой не добавляются.

Это обоснованный стартовый набор, **не результат сравнительного live-теста**
на ваших задачах. Настройка модели в review не доказывает качество её вердиктов.
Проверялись публичные каталоги, не inference с вашими credentials. Free-квоты,
модели и доступность могут меняться; для чувствительных данных учитывайте условия
free endpoint (в частности, NVIDIA предупреждает о логировании запросов).
Разные API providers также могут использовать общий upstream: запасной Ultra
через OpenRouter не гарантирует независимость от сбоя NVIDIA. При quota
переключение идёт между providers, а повторные модели того же provider
пригодятся при других ошибках, не для обхода его общей квоты.

В Telegram у авторизованного пользователя доступны команды (placeholders
замените точными provider/model IDs):

```text
/fallback
/fallback set <provider-1> <model-1>; <provider-2> <model-2>
/fallback add <provider> <model>
/fallback remove 2
/fallback off
/fallback reset
```

`set` заменяет список, `add` дополняет, `remove` удаляет по номеру,
`off` отключает дальнейшие резервные переключения. `reset` восстанавливает
базовый список из последней раскатки. Максимум — 8 маршрутов; дубликаты
запрещены. Для OpenRouter разрешены только IDs с `:free`; фактическая
доступность модели зависит от провайдера и не гарантируется суффиксом.

Список записывается атомарно в `config.yaml` текущего routed Hermes home/profile,
а не только одной беседы: другие беседы этого профиля используют тот же список.
Изменения учитываются со следующего сообщения без рестарта; в занятой беседе
команда отклоняется. Уже выполняющиеся задачи не прерываются. Если агент уже
работает на резервной модели, `off` сам по себе не возвращает его на основную —
возврат остаётся под управлением штатного cooldown Hermes.

При quota/429/billing обход пропускает **все модели провайдера, уже отказавшего
по квоте**, до проверки его credentials/client и пробует следующий другой
провайдер в заданном порядке, без случайного выбора. Если он тоже исчерпал
квоту, его остальные модели также пропускаются в этом обходе. Проверка
credentials и реальный API-вызов выполняются нативным fallback при использовании;
`/fallback` не делает платных/пробных запросов и не выдаёт сохранение списка за
успешную проверку API. При исчерпании списка сохраняется штатная ошибка Hermes,
без бесконечного перебора. При других ошибках штатные критерии fallback
сохраняются, но платные OpenRouter-маршруты также пропускаются.

Повторный Ansible deploy сохраняет выбранный список даже без Workspace UI и
обновляет только baseline `fallback_policy.default_routes` для `/fallback reset`.
Чтобы заменить активный список новым deploy-default, выполните `reset` после
раскатки. Для отдельного routed profile нужны собственные
`fallback_policy.allowed_providers` и `fallback_policy.default_routes` в его
конфигурации. Команды изменения списка также убирают legacy `fallback_model`,
чтобы он не включил скрытый резерв после `off`.

Официальный CLI-мастер по-прежнему доступен:

```bash
sudo -u hermes -H /home/hermes/.local/bin/hermes fallback
```

Авторизацию дополнительных провайдеров настройте через Vault либо мастер моделей:

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

Deploy задаёт бюджеты основного агента и `/goal` через `agent.max_turns` и
`goals.max_turns` в [`config/vps-defaults.yml`](config/vps-defaults.yml).
Loop guardrails остаются активны; их значения и остальные runtime-лимиты
определяются конфигурацией и закреплённой версией Hermes. Для Telegram включён
подробный tool-progress, о background process приходит только итог, а сессия
автоматически сбрасывается после настроенного периода простоя. Метрики, health
checks, backups и `hermes-ops-report` работают без LLM; автоматический анализ
запускается только по вашему запросу.

#### Активность в `/status`

`Agent Running` показывает только основной агент, обрабатывающий сообщение.
Значение `No` не означает, что завершились запущенные им фоновые команды.
Дополнительные поля разделяют эти состояния:

```text
Agent Running: No
Work: background active (main agent idle)
Subagents: 0 active
Background processes: 1 running
• proc_5f83c682dac3 — 7m 43s; agent notification on exit: enabled
```

`Work` различает ответ агента, его запуск, фоновую работу и простой. Если
получить данные не удалось, отображается `unavailable` / `unknown`, а не ноль.
Показываются только процессы этого чата/топика; процессы с известной привязкой
к предыдущей сессии после `/new` исключаются. Для старых записей без ID
родительской сессии используется привязка к чату. Список ограничен пятью
процессами с указанием общего количества. Команды, логи и секреты не выводятся.

`agent notification on exit: enabled` означает установленный у процесса
`notify_on_complete=true`: Hermes должен уведомить агента о завершении.
Это не подтверждение доставки будущего ответа. Для поллера, после которого
нужен анализ результата, агенту следует задавать `terminal(background=true,
notify_on_complete=true)`. Настройка `display.background_process_notifications:
result` сама по себе не включает продолжение агентом. Статус не меняет эти
настройки и не потребляет результат процесса. Время в строке — возраст процесса,
а не время последнего успешного запроса: `running` не доказывает прогресс поллера.

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
`vps_deploy.features.host_admin: false`.

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
`vps_deploy.features.host_admin: true` Ansible делает это намеренно, потому что
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
(`--without-host-admin` или `vps_deploy.features.host_admin: false`) этого `sudo`
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

### Azure DevOps и SonarQube Cloud

Ansible доставляет токены через существующий `hermes_secret_env` и публикует
инструкции по работе с API в отдельном блоке `workspace/AGENTS.md`.
Личные заметки сохраняются, повторный deploy обновляет тот же блок, а его
копия входит в Hermes backup. Python и terminal уже доступны; установка
Azure CLI, Sonar scanner или MCP для чтения API не требуется.

Адреса и проекты заданы в `config/vps-defaults.yml` → `vps_integrations`:

| Сервис | URL | Организация | Проект |
|---|---|---|---|
| Azure DevOps | `https://dev.azure.com/YauheniPo` | `YauheniPo` | `popot-bot-2.0` |
| SonarQube Cloud | `https://sonarcloud.io` | `yauhenipo` | `YauheniPo_popot-bot-2.0` |

Для другого сервера или проекта измените этот раздел. Токены туда не добавляйте.

1. В личном terminal из корня repository откройте Vault:
   ```bash
   EDITOR=nano ansible-vault edit hermes/ansible/group_vars/all/vault.yml
   ```
2. Добавьте `AZURE_DEVOPS_EXT_PAT` и `SONAR_TOKEN` в существующий
   `hermes_secret_env`, сохранив все остальные ключи. Для SonarQube Cloud
   используйте personal access token с доступом к нужному проекту.
   Секрет `SONAR_TOKEN` в GitHub Actions не доставляется на VPS автоматически.
3. Примените Ansible deploy. Для Azure pipeline обновите зашифрованный
   `vault.yml` в **Pipelines → Library → Secure files**; изменения repository
   должны быть в `main`. Запустите pipeline и пройдите настроенные approvals.
   Gateway перезапустится автоматически.
4. Проверьте из нового диалога Hermes: «Покажи последние пять сборок Azure
   DevOps проекта popot-bot-2.0» и «Покажи Quality Gate и открытые issues
   SonarQube проекта YauheniPo_popot-bot-2.0». Проверка должна вернуть данные
   проекта; наличие имени переменной само по себе не подтверждает доступ.

Оба токена опциональны для deploy. Если токен не добавлен, Hermes сообщит
имя недостающей переменной; значения токенов не нужно передавать в чат.
Инструкции используют Azure PAT через HTTP Basic, Sonar token через Bearer
и читают значения из окружения внутри Python-процесса.

Для чтения Azure builds/logs выдайте Build: Read; для запуска pipeline —
Build: Read & execute; для чтения репозиториев — Code: Read. В Sonar права
зависят от владельца токена: Browse project для результатов и See Source Code
для исходников. Изменения и запуски выполняются по запросу владельца с
сохранением существующих approvals.

Официальные справочники: [Azure PAT](https://learn.microsoft.com/en-us/azure/devops/cli/log-in-via-pat?view=azure-devops),
[Azure REST API](https://learn.microsoft.com/en-us/rest/api/azure/devops/),
[Sonar tokens](https://docs.sonarsource.com/sonarqube-cloud/managing-your-account/managing-tokens),
[Sonar Web API](https://docs.sonarsource.com/sonarqube-cloud/appendices/web-api).

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

Файловый менеджер официального Dashboard может создавать, редактировать и
удалять файлы в настроенном `vps_deploy.identity.workspace` от пользователя
Hermes. Systemd разрешает запись только в Hermes home и эту рабочую папку;
`ProtectSystem=strict` и `ProtectHome=read-only` остаются включёнными.
Это не даёт запись во весь домашний каталог или произвольные пути, выбранные
в UI, и не отменяет обычные права файлов. Для старой установки с ошибкой
`Read-only file system` примените deploy с обновлённым unit: изменение вступает
в силу после перезапуска `hermes-dashboard.service`. Удаление — реальное,
не способ просто скрыть файл из списка; заранее сохраняйте нужные данные.

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
