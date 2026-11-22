terraform {
  required_version = ">= 1.6, < 2.0"

  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = ">= 0.217, < 1.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
}

variable "cloud_id" {
  type        = string
  description = "Идентификатор облака Yandex Cloud."
}

variable "folder_id" {
  type        = string
  description = "Идентификатор каталога Yandex Cloud."
}

variable "bucket_name" {
  type        = string
  description = "Глобально уникальное имя бакета с префиксом vvotNN-."

  validation {
    condition     = can(regex("^vvot[0-9]{2}-[a-z0-9][a-z0-9-]{0,54}[a-z0-9]$|^vvot[0-9]{2}-[a-z0-9]$", var.bucket_name))
    error_message = "Имя должно начинаться с vvotNN- и содержать до 63 строчных букв, цифр и дефисов."
  }
}

locals {
  prefix     = substr(var.bucket_name, 0, 7)
  table_name = "archives"
}

resource "yandex_iam_service_account" "system" {
  for_each = toset(["gateway", "worker", "trigger", "queue-admin"])
  name     = "${local.prefix}${each.key}"
}

locals {
  queue_roles = {
    gateway     = "ymq.writer"
    trigger     = "ymq.reader"
    queue-admin = "ymq.admin"
  }
}

resource "yandex_resourcemanager_folder_iam_member" "queue" {
  for_each  = local.queue_roles
  folder_id = var.folder_id
  role      = each.value
  member    = "serviceAccount:${yandex_iam_service_account.system[each.key].id}"
}

resource "yandex_iam_service_account_static_access_key" "queue_admin" {
  service_account_id = yandex_iam_service_account.system["queue-admin"].id
  description        = "${local.prefix}ymq-terraform"

  depends_on = [yandex_resourcemanager_folder_iam_member.queue["queue-admin"]]
}

resource "yandex_message_queue" "dead_letters" {
  name                      = "${local.prefix}dead-letters"
  access_key                = yandex_iam_service_account_static_access_key.queue_admin.access_key
  secret_key                = yandex_iam_service_account_static_access_key.queue_admin.secret_key
  message_retention_seconds = 1209600
}

resource "yandex_message_queue" "tasks" {
  name                       = "${local.prefix}tasks"
  access_key                 = yandex_iam_service_account_static_access_key.queue_admin.access_key
  secret_key                 = yandex_iam_service_account_static_access_key.queue_admin.secret_key
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = yandex_message_queue.dead_letters.arn
    maxReceiveCount     = 1
  })
}

resource "yandex_storage_bucket" "archives" {
  bucket        = var.bucket_name
  folder_id     = var.folder_id
  max_size      = 10 * 1024 * 1024 * 1024
  force_destroy = true

  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }
}

resource "yandex_storage_bucket_iam_binding" "archives" {
  for_each = {
    gateway = "storage.viewer"
    worker  = "storage.uploader"
  }
  bucket  = yandex_storage_bucket.archives.bucket
  role    = each.value
  members = ["serviceAccount:${yandex_iam_service_account.system[each.key].id}"]
}

resource "yandex_ydb_database_serverless" "archives" {
  name                = "${local.prefix}archives-db"
  deletion_protection = false

  serverless_database {
    enable_throttling_rcu_limit = true
    throttling_rcu_limit        = 10
    storage_size_limit          = 1
  }
}

resource "yandex_ydb_database_iam_binding" "archives" {
  for_each = {
    gateway = "ydb.viewer"
    worker  = "ydb.editor"
  }
  database_id = yandex_ydb_database_serverless.archives.id
  role        = each.value
  members     = ["serviceAccount:${yandex_iam_service_account.system[each.key].id}"]
}

# The row-table resource cannot create a Document API table. Use the same
# YC_TOKEN for both Terraform's provider and these Document API requests.
resource "terraform_data" "archives_table" {
  input = {
    endpoint = yandex_ydb_database_serverless.archives.document_api_endpoint
    name     = local.table_name
  }

  triggers_replace = [yandex_ydb_database_serverless.archives.id, local.table_name]

  provisioner "local-exec" {
    command = "python -B -m scripts.document_table create"
    environment = {
      YDB_DOCUMENT_ENDPOINT = self.input.endpoint
      ARCHIVES_TABLE        = self.input.name
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "python -B -m scripts.document_table delete"
    environment = {
      YDB_DOCUMENT_ENDPOINT = self.input.endpoint
      ARCHIVES_TABLE        = self.input.name
    }
  }
}

data "archive_file" "worker" {
  type        = "zip"
  output_path = "${path.module}/archive-function.zip"

  dynamic "source" {
    for_each = fileset("${path.module}/function", "*.py")
    content {
      filename = source.value
      content  = file("${path.module}/function/${source.value}")
    }
  }
}

resource "yandex_function" "worker" {
  name               = "${local.prefix}archiver"
  runtime            = "python312"
  entrypoint         = "index.handler"
  memory             = 256
  execution_timeout  = 600
  user_hash          = data.archive_file.worker.output_sha256
  service_account_id = yandex_iam_service_account.system["worker"].id

  content {
    zip_filename = data.archive_file.worker.output_path
  }

  environment = {
    ARCHIVE_BUCKET        = yandex_storage_bucket.archives.bucket
    YDB_DOCUMENT_ENDPOINT = terraform_data.archives_table.output.endpoint
    ARCHIVES_TABLE        = terraform_data.archives_table.output.name
  }

  depends_on = [
    yandex_storage_bucket_iam_binding.archives["worker"],
    yandex_ydb_database_iam_binding.archives["worker"],
  ]
}

resource "yandex_function_iam_binding" "invoke" {
  function_id = yandex_function.worker.id
  role        = "serverless.functions.invoker"
  members     = ["serviceAccount:${yandex_iam_service_account.system["trigger"].id}"]
}

resource "yandex_function_trigger" "tasks" {
  name = "${local.prefix}task-trigger"

  message_queue {
    queue_id           = yandex_message_queue.tasks.arn
    service_account_id = yandex_iam_service_account.system["trigger"].id
    batch_cutoff       = "0"
    batch_size         = "1"
    visibility_timeout = "900"
  }

  function {
    id                 = yandex_function.worker.id
    service_account_id = yandex_iam_service_account.system["trigger"].id
    tag                = "$latest"
  }

  depends_on = [
    yandex_function_iam_binding.invoke,
    yandex_resourcemanager_folder_iam_member.queue["trigger"],
  ]
}

resource "yandex_api_gateway" "archives" {
  name = "${local.prefix}archive-api"
  spec = templatefile("${path.module}/openapi.yaml", {
    queue_url          = yandex_message_queue.tasks.id
    folder_id          = var.folder_id
    gateway_account_id = yandex_iam_service_account.system["gateway"].id
    database_path      = replace(yandex_ydb_database_serverless.archives.document_api_endpoint, "/^https://[^/]+/", "")
    table_name         = terraform_data.archives_table.output.name
    bucket_name        = yandex_storage_bucket.archives.bucket
  })

  depends_on = [
    yandex_storage_bucket_iam_binding.archives["gateway"],
    yandex_ydb_database_iam_binding.archives["gateway"],
    yandex_resourcemanager_folder_iam_member.queue["gateway"],
    yandex_function_trigger.tasks,
  ]
}

output "api_url" {
  description = "Служебный HTTPS-адрес API Gateway."
  value       = "https://${yandex_api_gateway.archives.domain}"
}

output "dead_letter_queue_url" {
  description = "Адрес очереди необработанных заданий."
  value       = yandex_message_queue.dead_letters.id
}
