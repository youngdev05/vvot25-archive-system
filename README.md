# Контрольная работа № 1. Система архивирования

`POST /tasks` проверяет JSON и передаёт задание в Yandex Message Queue. Триггер передаёт функции одно сообщение. Функция скачивает файл, записывает ZIP в закрытый бакет и сохраняет метаданные в документной таблице YDB. Клиент читает метаданные и скачивает архив через служебный домен API Gateway.

| Запрос | Интеграция | Результат |
| --- | --- | --- |
| `POST /tasks` | `cloud_ymq / SendMessage` | `202` для принятого задания, `400` для некорректного запроса |
| `GET /archives` | `cloud_ydb / Scan` | `200` и JSON-массив записей |
| `GET /archives/{key}` | `cloud_ydb / GetItem` | `200` и запись либо `404` |
| `GET /download/{key}` | `object_storage` | `200` и ZIP либо `404` |

## Файлы

- `openapi.yaml` описывает четыре маршрута, схемы, проверку запроса и интеграции API Gateway. Terraform подставляет параметры шлюза перед развёртыванием.
- `function/index.py` принимает событие YMQ, формирует ключ UUIDv7, создаёт ZIP и сохраняет объект и метаданные. При повторной доставке одного сообщения функция вычисляет тот же ключ из `SentTimestamp` и идентификатора сообщения.
- `function/source.py` скачивает файлы по публичным HTTP(S) URL и проверяет адрес при каждом перенаправлении. Лимит 2 400 000 байт оставляет место для заголовков ZIP при лимите ответа API Gateway 2,5 МБ. [Лимиты API Gateway](https://yandex.cloud/ru/docs/api-gateway/concepts/limits)
- `main.tf` объявляет инфраструктуру и IAM-роли. `scripts/document_table.py` создаёт и удаляет документную таблицу `archives` через Document API с IAM-токеном из `YC_TOKEN`. [Работа с Document API через HTTP](https://yandex.cloud/ru/docs/ydb/docapi/tools/aws-http)

## Развёртывание в Yandex Cloud

Нужны Terraform 1.6+, Python 3.12+ с командой `python`, Yandex Cloud CLI и доступ к выбранному каталогу для создания ресурсов и назначения IAM-ролей. Выполняйте команды из корня репозитория.

### PowerShell

```powershell
# При первом входе выберите учебное облако и каталог, например vvot25.
yc init
$env:YC_TOKEN = (yc iam create-token).Trim()
$env:TF_VAR_cloud_id = (yc config get cloud-id).Trim()
$env:TF_VAR_folder_id = (yc config get folder-id).Trim()
$env:TF_VAR_bucket_name = "vvot25-archives-$([guid]::NewGuid().ToString('N').Substring(0, 12))"

terraform init
terraform fmt -check
terraform validate
terraform plan
terraform apply
```

### Bash

```bash
yc init
export YC_TOKEN=$(yc iam create-token)
export TF_VAR_cloud_id=$(yc config get cloud-id)
export TF_VAR_folder_id=$(yc config get folder-id)
export TF_VAR_bucket_name="vvot25-archives-$(python -c 'import uuid; print(uuid.uuid4().hex[:12])')"

terraform init
terraform fmt -check
terraform validate
terraform plan
terraform apply
```

Бакет должен иметь глобально уникальное имя. Первые семь символов `bucket_name` задают префикс облачных ресурсов. Имя таблицы `archives` фиксируется заданием. У Terraform три входные переменные. Токен передаётся окружением провайдеру Yandex Cloud и скрипту таблицы. Для управления YMQ Terraform создаёт отдельный статический ключ сервисного аккаунта с ролью `ymq.admin`. Храните `terraform.tfstate`, файлы плана и любые локальные учётные данные вне публичного репозитория.

## Проверка

```text
python -B -m unittest scripts.test_worker -v
```

### Проверка после `apply` в PowerShell

```powershell
$api = terraform output -raw api_url
python -B -m scripts.smoke $api --enqueue-failure
terraform output -raw dead_letter_queue_url
```

### Проверка после `apply` в Bash

```bash
python -B -m scripts.smoke "$(terraform output -raw api_url)" --enqueue-failure
terraform output -raw dead_letter_queue_url
```

Сценарий проверяет `400` на некорректный JSON, `202` на корректное задание, `404` для отсутствующего архива, список, запись, UUIDv7, тип ответа и содержимое ZIP. Если тестовый хост недоступен из облака, передайте через `--source-url` доступный с обеих сторон небольшой файл. Список на маршруте `/archives` отражает одну операцию `Scan`. Document API по умолчанию возвращает не более 100 записей или 1 МБ за вызов. [Операция Scan](https://yandex.cloud/ru/docs/ydb/docapi/api-ref/actions/scan)

С `--enqueue-failure` сценарий дополнительно отправляет задачу с недоступным источником и печатает её имя. В Cloud Functions проверьте ошибку вызова. В Message Queue проверьте, что после истечения таймаута видимости задание появилось в очереди `vvot25-dead-letters`, а запись в YDB для него отсутствует. Таймаут видимости задан в 900 секунд, поэтому проверка очереди может занять около 15 минут.

## Удаление ресурсов

Выполните команды в том же каталоге, с тем же состоянием и теми же значениями переменных.

```text
terraform plan -destroy
terraform destroy
terraform state list
```

При необходимости обновите `YC_TOKEN` через `yc iam create-token`. `destroy` удаляет в том числе тестовые ZIP-объекты, так как у бакета `force_destroy = true`. После удаления `terraform state list` должен быть пустым.

## Сценарий скринкаста на 10–12 минут

| Время записи | Действие и голосовое пояснение |
| --- | --- |
| 0:00–1:45 | Открыть `openapi.yaml`. Назвать четыре маршрута. Показать обязательные поля и `x-yc-status-mapping`, затем операции `Scan`, `GetItem` и чтение объекта. |
| 1:45–3:30 | Открыть `function/index.py`. Показать путь `details.message.body`, формирование UUIDv7 и порядок `PutObject` → `PutItem`. Объяснить, почему ошибка выходит из обработчика. |
| 3:30–5:30 | Открыть `main.tf`. Показать три переменные, роли сервисных аккаунтов, `redrive_policy`, настройки триггера, YDB и закрытого бакета. Показать вызов скрипта создания таблицы. |
| 5:30–7:30 | Запустить `terraform apply`. Если есть ожидание, вырезать его при монтаже. Показать результат и `terraform output -raw api_url`. |
| 7:30–10:00 | Запустить `scripts.smoke`. Показать коды HTTP и подтверждение проверки ZIP. После паузы показать ошибку функции и поступление неуспешного задания в DLQ. |
| 10:00–12:00 | Выполнить `terraform destroy`. Показать завершение и пустой `terraform state list`. |

Перед записью войдите в CLI и подготовьте переменные окружения. В кадре достаточно команд и результатов без вывода токена, состояния Terraform и содержимого статического ключа. Для сдачи опубликуйте исходники в одном публичном Git-репозитории, разместите скринкаст на Яндекс Диске, проверьте доступность обеих ссылок и отправьте их через [форму сдачи](https://forms.yandex.ru/u/6af821c9de8ec32ea31e0b79).
