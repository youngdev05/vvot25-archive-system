# Система архивирования

Контрольная работа № 1. API принимает URL файла, ставит задание в очередь YMQ и возвращает `202`. Функция сохраняет ZIP в закрытый бакет, а метаданные записывает в YDB. Архивы доступны через API Gateway.

[Видео с развёртыванием, тестами и удалением ресурсов (4:57)](https://yadi.sk/i/g37bXrF5CNaCJQ)

- `openapi.yaml` — спецификация API.
- `function/` — код функции архивирования.
- `main.tf` — ресурсы и роли Yandex Cloud.

## Развёртывание

Нужны Terraform 1.6+, Python 3.12+ и Yandex Cloud CLI. В PowerShell из корня репозитория выполните команды и выберите учебный каталог при входе.

```powershell
yc init
$env:YC_TOKEN = (yc iam create-token).Trim()
$env:TF_VAR_cloud_id = (yc config get cloud-id).Trim()
$env:TF_VAR_folder_id = (yc config get folder-id).Trim()
$env:TF_VAR_bucket_name = "vvot25-archives-$([guid]::NewGuid().ToString('N').Substring(0, 12))"
terraform init
terraform apply
```

## Проверка и удаление

```powershell
python -B -m scripts.smoke (terraform output -raw api_url)
terraform destroy
terraform state list
```

Для проверки DLQ добавьте к команде `smoke` флаг `--enqueue-failure`. Сообщение появится в очереди после таймаута видимости в 900 секунд. Выполняйте `destroy` с теми же переменными и состоянием Terraform.
