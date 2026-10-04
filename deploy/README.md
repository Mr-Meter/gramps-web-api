# Gramps Web «Tree» в Docker

Форк Gramps Web с администраторами ветвей и преемственностью. Публичные образы
апстрима не подходят (в них нет ни новых API, ни новых экранов), поэтому образ
собирается локально из двух репозиториев: `gramps-web` (фронтенд) и
`gramps-web-api` (бэкенд). Файлы в этом каталоге: `Dockerfile`,
`Dockerfile.dockerignore`, `docker-compose.yml`, `.env.example`.

## Требования

- Docker >= 23 с плагином Compose v2 (команда `docker compose`, не
  `docker-compose`). Сборка использует BuildKit, он включён по умолчанию.
- Оба репозитория склонированы рядом, именно под этими именами:

      Tree/
      ├── gramps-web/        # фронтенд
      └── gramps-web-api/    # бэкенд; этот каталог: gramps-web-api/deploy/

- Доступ к ghcr.io (базовый образ Gramps Web) и Docker Hub (node, redis).
  Сборка фронтенда занимает несколько минут и требует около 2 ГБ свободной
  памяти; при нехватке памяти собирайте образ на другой машине.

## Rootless Docker

Если Docker работает от обычного пользователя без sudo (rootless), учтите три
отличия:

- Порты ниже 1024 недоступны. В `.env` укажите, например,
  `GRAMPSWEB_PORT=127.0.0.1:5055`, а наружу сайт отдавайте через прокси.
- Плагина buildx может не быть (`docker buildx version`). Он ставится без sudo:
  файл `buildx-<версия>.linux-amd64` со страницы релизов docker/buildx
  сохраняется как `~/.docker/cli-plugins/docker-buildx` и делается исполняемым.
- Контейнеры работают, пока работает пользовательский демон. Чтобы сайт не
  останавливался после выхода из SSH, для пользователя должен быть включён
  linger: проверка `loginctl show-user $USER --property=Linger`, включает
  администратор машины командой `loginctl enable-linger <пользователь>`.

Предупреждения про io.max в журнале демона для rootless нормальны. На общей
машине имеет смысл уменьшить `GUNICORN_NUM_WORKERS` до 2.

## Сборка и запуск

    cd gramps-web-api/deploy
    cp .env.example .env          # отредактируйте, как минимум GRAMPSWEB_BASE_URL
    docker compose up -d --build

Поднимаются четыре контейнера: `grampsweb` (сайт и API, порт `GRAMPSWEB_PORT`,
по умолчанию 80), `grampsweb_celery` (фоновые задачи), `grampsweb_redis` и
необязательный `grampsweb_succession` (см. ниже).

Журналы: `docker compose logs -f grampsweb`. Остановка: `docker compose down`
(данные в томах сохраняются; `down -v` их удалит).

## Без домена и белого IP: Tailscale

Если у сервера нет статического адреса и домена, сайт можно открыть через
[Tailscale](https://tailscale.com): он даёт имя вида
`имя-сервера.имя-сети.ts.net` с настоящим HTTPS-сертификатом и работает из-за
NAT провайдера без проброса портов.

1. На сервере: `curl -fsSL https://tailscale.com/install.sh | sh` (Arch:
   `pacman -S tailscale && systemctl enable --now tailscaled`), затем
   `sudo tailscale up` и вход по ссылке из вывода.
2. В админ-консоли Tailscale включите **DNS → MagicDNS** и
   **DNS → HTTPS Certificates**.
3. В `.env` не публикуйте порт наружу: `GRAMPSWEB_PORT=127.0.0.1:8080`
   (шаблон `${GRAMPSWEB_PORT}:5000` даёт `127.0.0.1:8080:5000`), затем
   `docker compose up -d`.
4. Выберите режим доступа:
   - только для устройств вашей сети Tailscale (семье нужно поставить
     Tailscale и войти в вашу сеть; бесплатный план: 3 пользователя):

         sudo tailscale serve --bg 8080

   - публично, для всех по ссылке (вход по паролю Gramps Web остаётся):

         sudo tailscale funnel --bg 8080

     При первом вызове Funnel команда выведет ссылку, по которой нужно
     разрешить Funnel для этого узла в политике сети.
5. `tailscale serve status` покажет адрес вида
   `https://имя-сервера.имя-сети.ts.net`; впишите его в
   `GRAMPSWEB_BASE_URL` и выполните `docker compose up -d`, чтобы ссылки в
   письмах вели на него.

Сертификат продлевается автоматически. Ограничения Funnel: доступны только
порты 443, 8443 и 10000, пропускная способность общая и невысокая, адрес
длинный. Когда появится свой домен, достаточно поставить Caddy или другой
обратный прокси перед портом 8080 и сменить `GRAMPSWEB_BASE_URL`.

## Публичный доступ с домашнего белого IP: Caddy и DuckDNS

Подходит, если сервер стоит дома за роутером с белым IP, пусть и
динамическим. Caddy принимает посетителей и сам получает сертификат
Let's Encrypt, а маленький контейнер раз в пять минут обновляет бесплатное имя
на duckdns.org, если провайдер сменил адрес. Лимита на размер загрузки нет.

1. Зарегистрируйтесь на duckdns.org, создайте поддомен (например, `tree`) и
   скопируйте token со страницы аккаунта.
2. В `.env`:

       COMPOSE_PROFILES=caddy,duckdns
       SITE_ADDRESS=tree.duckdns.org
       DUCKDNS_SUBDOMAIN=tree
       DUCKDNS_TOKEN=<token>
       GRAMPSWEB_BASE_URL=https://tree.duckdns.org
       GRAMPSWEB_PORT=127.0.0.1:5055

3. На роутере закрепите за сервером его локальный адрес (резервирование DHCP
   по MAC-адресу) и пробросьте TCP 80 на `<адрес сервера>:5080` и TCP 443 на
   `<адрес сервера>:5443`. С обычным, не rootless Docker можно задать
   `CADDY_HTTP_PORT=80` и `CADDY_HTTPS_PORT=443` и пробрасывать порты как есть.
4. `docker compose up -d`. Проверка из домашней сети:
   `curl -sI http://<адрес сервера>:5080 | head -1` должен вывести строку
   `HTTP/1.1 ...`; если команда висит, порт закрыт файрволом сервера.
5. `docker compose logs -f caddy duckdns`: DuckDNS пишет `OK`, а сертификат
   получен, когда Caddy сообщит `certificate obtained successfully`. Сайт
   проверяйте с телефона через мобильный интернет: изнутри домашней сети
   внешнее имя открывается, только если роутер умеет NAT loopback.

Когда появится свой домен, впишите его в `SITE_ADDRESS` и `GRAMPSWEB_BASE_URL`,
а в DNS домена заведите CNAME на имя DuckDNS.

## Первый запуск

Откройте сайт в браузере. Пока пользователей нет, сайт предложит создать
первого: он становится владельцем (owner). Дерево с именем `GRAMPSWEB_TREE`
создаётся пустым; существующую базу можно импортировать прямо в мастере первого
запуска или позже в настройках (файл `.gramps` или GEDCOM), медиафайлы
загружаются отдельно.

## Обновление

    cd Tree/gramps-web && git pull
    cd ../gramps-web-api && git pull
    cd deploy && docker compose up -d --build

Миграции базы пользователей выполняются автоматически при старте контейнеров
(`user migrate` в entrypoint). Чтобы заодно обновить базовый образ апстрима,
соберите с `--pull`: `docker compose build --pull && docker compose up -d`.

## Где лежат данные

Именованные тома; проект называется `tree`, поэтому имена начинаются с `tree_`:

| Том                                                              | Содержимое                                               |
|------------------------------------------------------------------|----------------------------------------------------------|
| `tree_gramps_db`                                                 | база данных Gramps (само дерево)                         |
| `tree_gramps_users`                                              | пользователи, администраторы ветвей, планы преемственности |
| `tree_gramps_media`                                              | медиафайлы                                               |
| `tree_gramps_secret`                                             | секретный ключ Flask (если не задан в `.env`)            |
| `tree_gramps_index`                                              | поисковый индекс (восстанавливается переиндексацией)     |
| `tree_gramps_cache`, `tree_gramps_thumb_cache`, `tree_gramps_tmp` | кэши, потеря безболезненна                               |

Резервная копия: остановить контейнеры и заархивировать тома, например

    docker compose stop
    for v in gramps_db gramps_users gramps_media gramps_secret; do
      docker run --rm -v tree_$v:/data -v "$PWD":/backup alpine \
        tar czf /backup/$v.tgz -C /data .
    done
    docker compose start

Дополнительно полезно регулярно делать экспорт `.gramps` из интерфейса.

## Преемственность

- Веб-приложение проверяет планы раз в `GRAMPSWEB_SUCCESSION_CHECK_INTERVAL`
  секунд (по умолчанию 600), но только когда к сайту кто-то обращается.
- Сервис `grampsweb_succession` — страховка: раз в `SUCCESSION_CHECK_EVERY`
  секунд (по умолчанию 3600) выполняет проверку независимо от посещений.
  Не нужен — `docker compose stop grampsweb_succession` или удалите его из
  `docker-compose.yml`.
- Вручную (`--tree <ID>` ограничивает проверку одним деревом):

      docker compose exec grampsweb python3 -m gramps_webapi \
        --config /app/config/config.cfg succession check

## Особенности форка: на что обратить внимание

- **`GRAMPSWEB_BASE_URL` должен быть публичным адресом сайта.** Письма
  (подтверждение e-mail, сброс пароля, уведомления о преемственности) содержат
  ссылки вида `<BASE_URL>/settings/succession`; без этой переменной compose
  откажется запускаться.
- **Почта должна быть настроена** (`GRAMPSWEB_EMAIL_*` и
  `GRAMPSWEB_DEFAULT_FROM_EMAIL`), иначе подтверждения и уведомления не
  отправляются: в журнале появится предупреждение, а план всё равно продвинется
  по таймеру. Проверить отправку тестовым письмом:

      docker compose exec grampsweb python3 -m gramps_webapi \
        --config /app/config/config.cfg email confirm-email you@example.org test

- Фронтенд собран для того же origin, что и API: сайт и `/api` должны отдаваться
  с одного адреса. За обратным прокси проксируйте всё на `GRAMPSWEB_PORT` и
  разрешите большие загрузки (например, `client_max_body_size 500m` в nginx).
- Образ носит тег `tree-grampsweb:local` и никуда не публикуется.
