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
значения в `hermes_secret_env` и при необходимости в `hermes_llm_config`, затем
сохраните файл и выйдите из редактора.

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

Минимальный набор для LLM — `OPENROUTER_API_KEY` в `hermes_secret_env` и
модель в `hermes_llm_config`. Дополнительно можно добавить:

- `FIRECRAWL_API_KEY` — чтение HTML/PDF и веб-страниц;
- `BRAVE_SEARCH_API_KEY` — поиск через Brave;
- `TELEGRAM_BOT_TOKEN` вместе с `TELEGRAM_ALLOWED_USERS` — запуск Telegram
  gateway;
- `GITHUB_TOKEN` — private repositories, PR/reviews/issues и GitHub Actions
  через managed `gh`; используйте fine-grained PAT с selected repositories;
- `hermes_grafana_admin_password` — пароль администратора Grafana;
- `hermes_code_server_password` — пароль браузерного IDE code-server
  (порт 3000 доступен только через SSH-туннель);
- `tailscale_auth_key` — временный ключ подключения Tailscale.

Для совместимости deployment также принимает `GH_TOKEN` или
`GITHUB_PERSONAL_ACCESS_TOKEN`, но в managed Vault предпочтительно единое имя
`GITHUB_TOKEN`, которое напрямую понимают bundled Hermes GitHub skills.

При применении playbook Hermes получает в своём `workspace/AGENTS.md` только
отсортированные **названия** ключей из `hermes_secret_env`. Это помогает ему
выбрать нужный инструмент (например, GitHub или Firecrawl), но значения не
попадают в этот документ: они остаются в `.hermes/.env` с правами `0600`.
Название ключа не подтверждает, что токен ещё действителен или имеет нужные
права — Hermes должен проверить это безопасной операцией, не читая и не
печатая `.env`.

Это правило поведения, а не техническая изоляция: terminal Hermes работает
тем же Unix-пользователем `hermes`, поэтому при нарушении инструкции он может
прочитать собственный `.env`. Ограничение `0600` защищает ключи только от
других непривилегированных пользователей VPS. Для строгой границы нужны
отдельный runtime без terminal/sudo и внешний secret manager или egress proxy;
подробное ограничение модели описано в
[`../../../SECRETS-CHECKLIST.md`](../../../SECRETS-CHECKLIST.md).

Не добавляйте `AGENT_BROWSER_ARGS`: безопасное для данного Ubuntu VPS значение
управляется playbook автоматически и не является секретом.

Минимальный пример для редактирования (замените placeholders внутри
`ansible-vault edit`, но не публикуйте реальные значения):

```yaml
hermes_manage_secret_env: true
hermes_secret_env:
  OPENROUTER_API_KEY: "replace-inside-ansible-vault"
  FIRECRAWL_API_KEY: "replace-inside-ansible-vault"
  TELEGRAM_BOT_TOKEN: "replace-inside-ansible-vault"
  TELEGRAM_ALLOWED_USERS: "123456789"
  GITHUB_TOKEN: "replace-inside-ansible-vault"

hermes_llm_config:
  model:
    provider: "openrouter"
    default: "openrouter/free"
    max_tokens: 4096
  agent:
    reasoning_effort: "medium"
  display:
    show_reasoning: false
```

## Применить изменения

После каждого изменения Vault вернитесь в каталог `hermes` и запустите:

```bash
ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml \
  --ask-vault-pass \
  --ask-pass
```

Сначала будет запрошен текущий SSH/root-пароль VPS, затем пароль Ansible Vault.
При `hermes_manage_secret_env: true` файл
`/home/hermes/.hermes/.env` на VPS полностью управляется Vault и его ручные
изменения будут перезаписаны при следующем запуске playbook.
