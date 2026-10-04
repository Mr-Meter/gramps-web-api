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

## Сборка и запуск

    cd gramps-web-api/deploy
    cp .env.example .env          # отредактируйте, как минимум GRAMPSWEB_BASE_URL
    docker compose up -d --build

Поднимаются четыре контейнера: `grampsweb` (сайт и API, порт `GRAMPSWEB_PORT`,
по умолчанию 80), `grampsweb_celery` (фоновые задачи), `grampsweb_redis` и
необязательный `grampsweb_succession` (см. ниже).

Журналы: `docker compose logs -f grampsweb`. Остановка: `docker compose down`
(данные в томах сохраняются; `down -v` их удалит).

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
