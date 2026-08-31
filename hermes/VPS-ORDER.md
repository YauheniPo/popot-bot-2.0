# Заказ VPS для Hermes в AlphaVPS

Эта инструкция фиксирует рекомендуемую конфигурацию VPS для текущей сборки
Hermes: Docker, Telegram Gateway (если заданы оба Telegram credentials),
Dashboard, Prometheus и Grafana. Модели вызываются через OpenRouter, поэтому
GPU для этого сервера не требуется.

Откройте [страницу High-Performance VPS AlphaVPS](https://alphavps.com/high-performance-vps)
и выберите тариф **P12G**:

| Ресурс | Значение |
| --- | --- |
| CPU | 4 vCPU |
| RAM | 12 GB |
| Диск | 80 GB NVMe |

Это рекомендуемый баланс цены и запаса ресурсов для одного пользователя. Если
планируются несколько одновременных задач Hermes, частая работа Chromium или
крупные репозитории, выбирайте P16G. P8G годится только для лёгкой нагрузки и
не является рекомендуемым вариантом для полной установки.

## Опции в форме заказа

Выберите следующие значения.

| Поле | Выбор |
| --- | --- |
| Operating System | **Linux Ubuntu 24.04 X86 64 Minimal Latest** |
| Additional IPv4 Addresses | **0** |
| Location | **London, UK (10Gbit/s)** |

Дополнительные IPv4-адреса Hermes не нужны: достаточно основного адреса VPS.
Nuremberg может быть удобнее географически, но если форма помечает его как
`Out of stock`, выбирайте London. В показанной форме London также отмечен как
ближайшая доступная локация с задержкой около 40 ms; доступность и задержка
могут измениться до момента заказа.

## После оплаты и первый деплой

Дождитесь, пока в клиентской панели AlphaVPS статус станет **Active** и сервер
будет **Online**. Сохраните публичный IP и первоначальный root password. Если
провайдер установил не Ubuntu 24.04, на новом пустом сервере выберите
**Rebuild OS** и установите `Ubuntu 24.04 64-bit Minimal`; это стирает данные
на VPS.

### 1. Проверить первоначальный SSH-доступ

На Mac выполните:

```bash
ssh root@YOUR_VPS_IP
```

При первом подключении подтвердите fingerprint словом `yes`. При вводе пароля
символы не отображаются — это нормальное поведение SSH. После успешного входа
выйдите командой `exit`: Ansible запускается с Mac, а не внутри VPS.

> Встроенная **noVNC Console** в панели AlphaVPS — это экран самой Ubuntu.
> На приглашении `login:` введите `root`, а затем текущий root password VPS.
> Email и пароль личного кабинета AlphaVPS нужны только для входа в панель,
> не для этого консольного приглашения.

### 2. Заполнить локальные файлы

В репозитории на Mac:

- в `hermes/ansible/inventory.ini` укажите `ansible_host=YOUR_VPS_IP` и
  `ansible_user=root`;
- заполните `hermes/ansible/group_vars/all/vault.yml` необходимыми секретами:
  OpenRouter, Telegram (обе переменные), Brave и Grafana.

`tailscale_auth_key` можно оставить пустым: Tailscale можно подключить позднее
вручную. Не добавляйте `inventory.ini` или `vault.yml` в Git.

### 3. Установить Ansible и подготовить Vault

Выполните из корня репозитория на Mac:

```bash
brew install ansible
```

Дальше выполните пошаговую подготовку, шифрование и безопасное редактирование
секретов по [Vault-инструкции](ansible/group_vars/all/VAULT.md). Пароль Vault
отдельный от root password VPS и пароля личного кабинета провайдера.

### 4. Запустить полный деплой

Перейдите в каталог `hermes` внутри репозитория и выполните:

```bash
cd hermes
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i ansible/inventory.ini \
  ansible/playbook.yml \
  --ask-vault-pass \
  --ask-pass
```

Сначала Ansible спросит текущий root password VPS, затем отдельный пароль
Vault. Команда установит Hermes, настроит Gateway при наличии обеих
Telegram-переменных и поднимет Dashboard, Prometheus и Grafana. Она также
задаст бесплатный русский Edge TTS (`ru-RU-SvetlanaNeural`) и добавит до двух
повторных попыток только для временного ответа Edge `NoAudioReceived`.

Успешный запуск заканчивается строкой вида `failed=0` в `PLAY RECAP` и
`Hermes deploy completed at … (VPS time)`. Повторный запуск этой же команды
безопасен: playbook повторно применяет конфигурацию и восстанавливает
прерванные шаги.

### 5. Быстро проверить службы на VPS

При необходимости войдите по SSH или noVNC как `root` и выполните:

```bash
systemctl is-active hermes-gateway.service
systemctl is-active hermes-dashboard.service
systemctl is-active grafana-server.service
systemctl is-enabled hermes-startup-notify.service
systemctl list-timers 'hermes-*'
ls -lah /home/hermes/hermes-backups
```

Все три `is-active` должны вернуть `active`, а `is-enabled` — `enabled`.
После каждого запуска gateway `hermes-startup-notify` отправляет в настроенный
Telegram alert target имя VPS, default model и время; недоставка сообщения не
останавливает gateway. Первый backup создаётся во время deploy; последующие
quick backups выполняются ежедневно, а полный — раз в неделю.

Если `tailscale_auth_key` оставлен пустым, Tailscale устанавливается, но не
входит в tailnet. В таком случае playbook намеренно не включает UFW-lockdown,
и первоначальный публичный SSH-доступ остаётся доступен до отдельного
подключения Tailscale.

### 6. Открыть Hermes GUI и Grafana с Mac

После успешного deploy создайте один SSH-туннель и оставьте этот терминал
открытым:

```bash
ssh -N \
  -L 9119:127.0.0.1:9119 \
  -L 3000:127.0.0.1:3000 \
  root@YOUR_VPS_IP
```

Затем на Mac откройте:

| Сервис | Локальный адрес | Доступ |
| --- | --- | --- |
| Hermes GUI | `http://127.0.0.1:9119` | Доступен только через SSH-туннель |
| Grafana | `http://127.0.0.1:3000` | Логин `hermes`, пароль — `hermes_grafana_admin_password` из Vault |

Не открывайте эти порты в панели VPS или firewall: на сервере они намеренно
слушают только `127.0.0.1`.

Не добавляйте на этапе заказа публичные порты для Grafana или Hermes Dashboard.
В нашей установке они слушают только loopback-интерфейс VPS и открываются
безопасно через SSH-туннель или Tailscale.
