# Ansible Vault: секреты Hermes

Этот каталог содержит единственный рабочий файл секретов для VPS:
`vault.yml`. Он не попадает в Git и должен быть зашифрован Ansible Vault.
Шаблон структуры и названий полей находится в
[`vault.yml.example`](vault.yml.example).

Все команды ниже запускаются прямо из этого каталога:
`hermes/ansible/group_vars/all`.

Пароль Ansible Vault — отдельный секрет. Он **не совпадает** с root-паролем
VPS, паролем панели провайдера или паролем SSH. Если этот пароль утрачен,
расшифровать существующий `vault.yml` невозможно: создайте новый Vault и
заново внесите ключи.

## Первое создание

Выполните на Mac из текущего каталога:

```bash
cp vault.yml.example vault.yml
chmod 600 vault.yml
ansible-vault encrypt vault.yml
```

Сразу сохраните пароль Vault в password manager. Проверить, что файл
зашифрован, можно без чтения его содержимого:

```bash
head -n 1 vault.yml
```

Ожидаемый результат начинается с `$ANSIBLE_VAULT;`.

## Безопасное редактирование

Редактируйте только через `ansible-vault edit`: команда создаёт временный
файл, а после сохранения автоматически записывает `vault.yml` обратно в
зашифрованном виде.

```bash
EDITOR=nano ansible-vault edit vault.yml
```

На macOS можно заменить `nano` на предпочитаемый текстовый редактор. Внесите
значения в `hermes_secret_env`, затем сохраните файл и выйдите из редактора.
Non-secret модельную и runtime-политику меняйте в
`config/vps-defaults.yml`, а не в Vault.

## Приватный просмотр

Чтобы один раз посмотреть расшифрованное содержимое без изменения файла,
выполните в локальном приватном terminal:

```bash
ansible-vault view vault.yml
```

Команда выводит все secrets в открытом виде. Не перенаправляйте вывод в файл,
не запускайте её в логируемом terminal/чате и не копируйте результат в Git,
issue или сообщение. Для изменения значений используйте `ansible-vault edit`,
а не `view`.

Не используйте для обычного редактирования `ansible-vault decrypt`: он оставит
секреты в открытом виде на диске. Если файл уже был расшифрован осознанно,
зашифруйте его до следующего deploy:

```bash
ansible-vault encrypt vault.yml
```

Никогда не отправляйте содержимое `vault.yml`, вывод `ansible-vault view`,
Vault password или API keys в чат, Git, issue либо shell history.

## Типичная конфигурация

Для LLM добавьте в `hermes_secret_env` credentials выбранных providers.
Основной provider, модели и fallback задаются в
[`config/vps-defaults.yml`](../../../config/vps-defaults.yml) →
`vps_hermes.config.managed_overlay`. Добавляйте ключи только нужных интеграций:

- `OLLAMA_API_KEY` — Ollama Cloud, provider `ollama-cloud` в меню `/model`;
- `OPENROUTER_API_KEY` — OpenRouter для выбранной модели или настроенного fallback;
- `NVIDIA_API_KEY` — NVIDIA NIM, provider `nvidia` в меню `/model`;
- `FIRECRAWL_API_KEY` — чтение HTML/PDF и веб-страниц;
- `BRAVE_SEARCH_API_KEY` — поиск через Brave;
- `TELEGRAM_BOT_TOKEN` вместе с `TELEGRAM_ALLOWED_USERS` — запуск Telegram
  gateway;
- `GITHUB_TOKEN` — private repositories, PR/reviews/issues и GitHub Actions
  через managed `gh`; используйте fine-grained PAT с selected repositories;
- `AZURE_DEVOPS_EXT_PAT` — Azure DevOps REST API: builds, logs, repositories
  и другие операции в пределах выданных прав;
- `SONAR_TOKEN` — SonarQube API: issues, metrics и Quality Gate;
- `hermes_grafana_admin_password` — пароль администратора Grafana;
- `hermes_code_server_password` — пароль браузерного IDE code-server
  (`vps_vscode.host_port` доступен только через SSH-туннель); допускаются любые
  непустые символы, перед записью в Compose окружение пароль JSON-экранируется;
- `tailscale_auth_key` — временный ключ подключения Tailscale.

Для совместимости deployment также принимает `GH_TOKEN` или
`GITHUB_PERSONAL_ACCESS_TOKEN`, но в managed Vault предпочтительно единое имя
`GITHUB_TOKEN`, которое напрямую понимают bundled Hermes GitHub skills.

При применении playbook Vault-managed блок в `workspace/AGENTS.md` получает
только отсортированные **названия** ключей из `hermes_secret_env`; личная часть
файла сохраняется отдельно от этого блока. Это помогает Hermes выбрать нужный
инструмент (например, GitHub или Firecrawl), но значения не попадают в этот
документ: они остаются в `.hermes/.env` с правами `0600`.
Название ключа не подтверждает, что токен ещё действителен или имеет нужные
права — Hermes должен проверить это безопасной операцией, не читая и не
печатая `.env`.

Для Azure DevOps и SonarQube deploy также добавляет постоянные инструкции
по API в отдельный managed block `workspace/AGENTS.md`. Адреса и проекты
задаются в `config/vps-defaults.yml` → `vps_integrations`; значения токенов
в инструкции не попадают. Токены опциональны: их отсутствие не блокирует
deploy, но соответствующие авторизованные API-запросы будут недоступны.
Настройка и проверка описаны в
[инструкции Azure DevOps и SonarQube](../../../README.md#azure-devops-и-sonarqube-cloud).

Это правило поведения, а не техническая изоляция: terminal Hermes работает
тем же Unix-пользователем `hermes`, поэтому при нарушении инструкции он может
прочитать собственный `.env`. Ограничение `0600` защищает ключи только от
других непривилегированных пользователей VPS. Для строгой границы нужны
отдельный runtime без terminal/sudo и внешний secret manager или egress proxy;
подробное ограничение модели описано в
[`../../../SECRETS-CHECKLIST.md`](../../../SECRETS-CHECKLIST.md).

Не добавляйте `AGENT_BROWSER_ARGS` и `AGENT_BROWSER_CONFIG`: эти non-secret
значения управляются через `config/vps-defaults.yml` и playbook автоматически.

Минимальный пример для редактирования (замените placeholders внутри
`ansible-vault edit`, но не публикуйте реальные значения):

```yaml
hermes_secret_env:
  OLLAMA_API_KEY: "replace-inside-ansible-vault"
  OPENROUTER_API_KEY: "replace-inside-ansible-vault"  # optional alternative
  FIRECRAWL_API_KEY: "replace-inside-ansible-vault"
  TELEGRAM_BOT_TOKEN: "replace-inside-ansible-vault"
  TELEGRAM_ALLOWED_USERS: "123456789"
  GITHUB_TOKEN: "replace-inside-ansible-vault"
```

## Применить изменения

После каждого изменения Vault вернитесь в каталог `hermes` и запустите:

```bash
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml \
  --ask-vault-pass \
  --ask-pass
```

Сначала будет запрошен текущий SSH/root-пароль VPS, затем пароль Ansible Vault.
В текущем профиле `vps_deploy.secret_environment.managed: true`: файл
`/home/hermes/.hermes/.env` на VPS полностью управляется Vault и его ручные
изменения будут перезаписаны при следующем запуске playbook.
Сохраняйте все используемые ключи в `hermes_secret_env`; пустая карта
останавливает deploy до перезаписи `.env`.

При deploy через Azure обновите также зашифрованный `vault.yml` в
**Pipelines → Library → Secure files**: pipeline читает эту копию, а не
локальный ignored-файл. Подключение и выбор моделей описаны в
[инструкции Ollama Cloud](../../../README.md#ollama-cloud).
