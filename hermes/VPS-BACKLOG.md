# Backlog улучшений Hermes VPS

Единый список будущих улучшений production VPS. Этот файл не содержит
секретов, IP-адресов, токенов и команд, которые изменяют production без
отдельного решения владельца.

## Как вести backlog

- Добавляйте одну задачу по шаблону ниже; не смешивайте несколько независимых
  изменений в одной записи.
- `idea` означает, что работа не авторизована. До перехода в `ready` нужно
  согласовать scope, стоимость, внешние сервисы и rollback.
- `done` ставится только после проверки на local Docker и на VPS (если задача
  затрагивает VPS).
- Секреты указываются только названием переменной. Значения хранятся в
  `ansible/group_vars/all/vault.yml`, а не в этом файле.

### Шаблон задачи

```md
## [P?] Краткое название

- Status: idea | ready | in_progress | blocked | done
- Owner: 
- Ценность: 
- Scope: 
- Не входит в scope: 
- Зависимости / стоимость: 
- Риски и security: 
- Критерии готовности:
  - [ ]
- Проверка и rollback: 
- Решение владельца / дата: 
```

## Очередь

### [P0] Проверить Telegram gateway, alerts и recovery

- Status: idea
- Ценность: подтвердить, что 24/7 gateway обнаруживает сбой и восстанавливается.
- Scope: test Telegram allowlist, безопасная остановка gateway, проверка
  systemd restart, health signal и alert/recovery.
- Не входит в scope: отправка реальных аварийных сообщений посторонним людям.
- Риски и security: не ослаблять Telegram allowlist; не тестировать disk alert
  заполнением диска.
- Критерии готовности:
  - [ ] Gateway принимает сообщения только от разрешённого Telegram user ID.
  - [ ] После контролируемой остановки service восстанавливается.
  - [ ] Alert и recovery зафиксированы без личных данных в метриках.
- Проверка и rollback: временно снизить monitoring threshold; вернуть его
  после теста.

### [P1] Закрыть public SSH после проверки Tailscale

- Status: idea
- Ценность: оставить доступ к VPS только через private tailnet.
- Scope: проверить вход по Tailscale SSH с Mac и iPhone, затем включить
  `hermes_lock_public_ssh=true` и аналогичное правило cloud firewall.
- Не входит в scope: блокировка public SSH до успешной второй SSH-сессии.
- Риски и security: риск потерять доступ к VPS при неверном firewall rule.
- Критерии готовности:
  - [ ] Tailscale SSH проверен с двух устройств.
  - [ ] Public port 22 недоступен, Tailscale SSH доступен.
  - [ ] Есть проверенная emergency-консоль VPS provider.
- Проверка и rollback: cloud console → временно восстановить SSH rule.

### [P1] Добавить OpenRouter account metrics в Grafana

- Status: idea
- Ценность: видеть баланс, расходы, requests и токены по моделям рядом с
  локальными метриками Hermes.
- Scope: отдельный collector → Prometheus textfile → Grafana dashboard.
  Получать баланс из `/api/v1/credits`, а расходы/токены/requests по моделям —
  из `/api/v1/activity` или `/api/v1/analytics/query`.
- Зависимости / стоимость: отдельный OpenRouter management key. Текущий
  inference key уже позволяет получить баланс, но API activity ответил `403`;
  management key нужен для детальной account analytics.
- Риски и security: ключ хранить только как отдельную secret ENV variable в
  `local.env` / Ansible Vault; не отдавать его в чат Hermes или Grafana UI.
  Не использовать текущий inference key как management key.
- Критерии готовности:
  - [ ] Никакие секреты не попадают в Prometheus, Grafana, logs или audit.
  - [ ] Grafana показывает остаток кредитов, лимит ключа и дневные расходы
    по моделям.
  - [ ] Для детализации видны requests, prompt/completion/reasoning tokens и
    стоимость без model prompt/response content.
  - [ ] При недоступности API collector сохраняет последние значения и health
    отражает ошибку без ответа OpenRouter целиком.
- Проверка и rollback: тест с management key; отключение collector не влияет
  на вызовы моделей.

### [P1] Шифрованный offsite backup в object storage

- Status: idea
- Ценность: восстановить Hermes после потери VPS, диска или ошибочного
  провижининга, а не полагаться только на локальные `/home/hermes/hermes-backups`.
- Scope: регулярно выгружать зашифрованный полный Hermes backup в отдельный
  private S3-compatible storage; задать retention, health signal и понятную
  команду восстановления.
- Не входит в scope: выбор или оплата storage provider без решения владельца;
  публичный bucket; backup secrets в Git.
- Зависимости / стоимость: выбранный provider, регион, стоимость хранения и
  отдельные credentials с доступом только к одному backup bucket.
- Риски и security: client-side encryption до upload, private bucket,
  минимальные bucket permissions; ключ шифрования и storage credentials —
  только в Ansible Vault.
- Критерии готовности:
  - [ ] Backup зашифрован до передачи и не содержит plaintext secrets в logs.
  - [ ] Хранятся минимум несколько точек восстановления по согласованному
    retention policy.
  - [ ] Есть тестовое восстановление на отдельной машине/временном VPS.
  - [ ] Failure upload и устаревший backup видны в Grafana/alert.
- Проверка и rollback: restore drill из последнего архива; выключение remote
  timer не влияет на основной Hermes gateway.

### [P1] CI-проверки инфраструктуры Hermes

- Status: done
- Ценность: ловить ошибки Docker, Ansible и provisioning до VPS deploy.
- Scope: Bash syntax/shellcheck, Python compile/tests, Ansible syntax и
  whitespace checks в GitHub Actions.
- Риски и security: CI не получает production Vault, Docker `local.env` или
  токены интеграций.
- Критерии готовности:
  - [x] Проверка запускается для изменений в `hermes/**`.
  - [x] Ошибки не печатают env values.
  - [x] Локальная команда проверки совпадает с CI.
- Проверка и rollback: pull request с намеренной синтаксической ошибкой.

### [P2] Автоматическая маршрутизация запросов между free и paid model

- Status: idea
- Ценность: обычные запросы остаются на `openrouter/free`, а сложные задачи
  получают более сильную модель без ручного переключения.
- Scope: сначала исследовать поддерживаемый Hermes механизм и определить
  budget/правила маршрутизации; затем сделать отдельный контролируемый router
  или delegation workflow.
- Не входит в scope: неявное переключение основной модели без лимитов,
  прозрачности и возможности выключить функцию.
- Зависимости / стоимость: платная модель, лимит на запрос и месячный budget.
- Риски и security: неверная классификация может неожиданно тратить credits;
  prompt и персональные данные нельзя отправлять дополнительному classifier
  без отдельного согласования.
- Критерии готовности:
  - [ ] Правила выбора, модель и лимиты согласованы владельцем.
  - [ ] Каждый paid route отражается в Grafana с причиной и стоимостью.
  - [ ] Есть switch для мгновенного возврата к free-only режиму.
  - [ ] Проверены простые, сложные и пограничные запросы.
- Проверка и rollback: local Docker с малым budget; удалить router config и
  оставить `openrouter/free`.

### [P2] Изолировать работу с недоверенными репозиториями

- Status: idea
- Ценность: не давать коду стороннего проекта доступ к VPS host.
- Scope: rootless Podman/Docker terminal backend для явно помеченных
  репозиториев, с ограниченными mounts, сетью и ресурсами.
- Риски и security: не смешивать sandbox и host workspace; не монтировать
  Docker socket.
- Критерии готовности:
  - [ ] Непроверенный проект не может читать host secrets или workspace Hermes.
  - [ ] Есть отдельная проверка network/filesystem isolation.
  - [ ] Host backend остаётся только для явно разрешённых VPS-задач.

### [P2] Бюджет и anomaly review LLM-расходов

- Status: idea
- Ценность: обнаруживать неожиданный рост затрат без постоянных дорогих
  LLM-проверок.
- Scope: редкий scheduled script/SQL review с жёстким monthly budget;
  уведомление только при аномалии.
- Риски и security: сначала использовать детерминированные метрики, а LLM —
  только для краткой интерпретации агрегатов.
- Критерии готовности:
  - [ ] Есть budget и порог аномалии.
  - [ ] Анализ не содержит prompts, ответы и tokens.
  - [ ] Его стоимость отображается отдельно.

### [P2] Управляемое обновление Hermes и проверка совместимости

- Status: idea
- Ценность: получать security fixes и новые возможности Hermes без неожиданной
  поломки gateway, Dashboard, plugins или metrics.
- Scope: проверка новой версии на local Docker, автоматический backup перед
  VPS update, smoke tests и фиксированный rollback на предыдущий image/revision.
- Риски и security: не запускать `hermes update` вручную внутри production
  container; обновлять только через versioned Docker/Ansible workflow.
- Критерии готовности:
  - [ ] Новая версия проходит local Docker smoke tests до VPS rollout.
  - [ ] VPS update имеет backup и записанную предыдущую revision.
  - [ ] Проверяются gateway, Dashboard, Telegram, Brave, GitHub и Grafana.
  - [ ] Rollback не требует повторного ввода secrets.

### [P3] Голосовой режим и wake word после ручного теста

- Status: idea
- Ценность: голосовой доступ к Hermes без ущерба private окружению.
- Scope: проверить STT/TTS на local Docker, выбрать provider и только затем
  решить, нужен ли wake word на VPS.
- Риски и security: микрофон, аудио и внешние STT providers требуют отдельного
  согласования по privacy и стоимости.
- Критерии готовности:
  - [ ] Подтверждено, где обрабатывается audio.
  - [ ] Работает выключение голосового режима одной командой.
  - [ ] Нет автоматической записи аудио в backup/metrics.

### [P2] Полный hermes/check.sh на VPS после установки (dev-тулинг)

- Status: in_progress
- Owner: YauheniPo
- Ценность: на свежеустановленном VPS одна команда проверяет всё — bash,
  shellcheck, Python, pytest, Ansible — без ручной доустановки пакетов.
- Scope: в dev-CLI уже добавлены `ansible-core`, `python3-pytest`,
  `python3-yaml` (коммит в PR #3) и `check.sh` получил pytest-smok-запуск.
  Осталось: после ближайшего VPS deploy прогнать
  `bash hermes/check.sh --require-tools` и убедиться, что все секции
  выполняются, включая pytest и `ansible-playbook --syntax-check`.
- Не входит в scope: ставить на VPS что-то сверх dev-CLI по умолчанию.
- Риски и security: новые системные пакеты устанавливаются как root через
  apt — только из официального репозитория, никаких сторонних источников.
- Критерии готовности:
  - [ ] `check.sh --require-tools` на VPS выполняется без ошибок.
  - [ ] pytest и ansible-playbook реально запускаются (не пропущены).
- Проверка и rollback: команда read-only; при проблеме пакеты удаляются как
  обычные apt packages.

### [P3] Отследить фикс anthropics/claude-code-action для накопленных PR

- Status: idea
- Owner: YauheniPo
- Ценность: `claude-code-plugin-review` в workflow падает на ветках с
  символами `(`, `)` в имени (например `feat(hermes)-...`) из-за бага
  Action: validate допустимые символы в имени branch. Владелец уже создал
  fix и issue в репозитории плагина; пока мёрдж не вышел — job остаётся
  красным на PR с такими именами веток.
- Scope: после выхода фикса в `anthropics/claude-code-action` обновить пин
  в `.github/workflows/claude-review.yml` (сейчас закреплён на
  `5ee796a5`) и убедиться, что review-пайплайн снова без потребности
  в переименовании ветки.
- Не входит в scope: переименование ветки существующего PR, чтобы обойти
  баг.
- Критерии готовности:
  - [ ] Новый pin включает валидные символы `(`/`)` в имени ветки.
  - [ ] claude-code-plugin-review завершается success на PR #3.
- Проверка и rollback: откатить пин на прежний commit при регрессе.

## История решений

Записывайте сюда только принятые решения, чтобы не возвращаться к уже
отклонённым вариантам.

| Дата | Решение | Причина |
| --- | --- | --- |
| 2026-08-18 | Не добавлять ручный профиль или alias платной модели. | Цель — автоматическая маршрутизация по типу задачи, а не ручное переключение. |
