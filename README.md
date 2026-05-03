# CityClinic Feedback Bot — Telegram admin + WhatsApp через Umnico

Проект работает так:

1. Администратор в Telegram пишет боту `/new`.
2. Администратор отправляет данные пациента:

```text
Иван Иванович, +79991234567
```

3. Бот отправляет пациенту сообщение в WhatsApp через Umnico API.
4. Пациент отвечает в WhatsApp:
   - сначала оценкой `1`, `2`, `3`, `4` или `5`;
   - затем текстовым или голосовым отзывом.
5. Ответы пациента приходят в проект через Umnico Webhook.
6. Бот продолжает сценарий в WhatsApp.
7. Вся переписка отправляется в Telegram-группу клиники.
8. После получения отзыва телефон заносится в базу `reviewed_patients`, чтобы повторно не просить отзыв у того же пациента.

## Важное про WhatsApp

Если вы используете официальный WhatsApp Business API / WABA, первое сообщение клиенту обычно должно быть шаблонным сообщением, одобренным WhatsApp. В этом проекте реализована отправка обычного текста через метод Umnico `messaging/post`. Если ваш канал Umnico/WABA требует template-сообщение, нужно будет заменить первичную отправку в функции `receive_patient_data()` на отправку template.

## Файлы проекта

```text
bot.py
config.py
database.py
umnico_client.py
requirements.txt
Procfile
runtime.txt
.env.example
README.md
```

## Railway Variables

Добавьте в Railway → Variables:

```env
BOT_TOKEN=токен_telegram_бота
ADMIN_GROUP_ID=-1001234567890
CLINIC_NAME=СитиКлиник
YANDEX_MAPS_REVIEW_URL=https://yandex.ru/maps/...
OPENAI_API_KEY=ключ_OpenAI_если_нужно_распознавание_голоса

PUBLIC_BASE_URL=https://your-project.up.railway.app

UMNICO_API_TOKEN=ваш_токен_Umnico
UMNICO_API_BASE_URL=https://api.umnico.com/v1.3
UMNICO_SA_ID=75
UMNICO_SOURCE_ID=
UMNICO_USER_ID=
```

### Где взять `UMNICO_SA_ID`

После деплоя напишите Telegram-боту:

```text
/umnico_integrations
```

Бот покажет список интеграций Umnico. Для WhatsApp/WABA возьмите `id` нужной интеграции и вставьте его в Railway как `UMNICO_SA_ID`.

### Где взять `UMNICO_SOURCE_ID` и `UMNICO_USER_ID`

Для первичной отправки достаточно `UMNICO_SA_ID`.

Для ответов в существующий Umnico lead желательно указать:

```env
UMNICO_SOURCE_ID=...
UMNICO_USER_ID=...
```

Если их не указать, бот будет пытаться отправлять ответы через `messaging/post` по телефону пациента.

## Настройка webhook Umnico

1. В Railway создайте домен.
2. В Railway Variables добавьте:

```env
PUBLIC_BASE_URL=https://your-project.up.railway.app
```

3. Сделайте Redeploy.
4. В Telegram напишите боту:

```text
/setup_umnico_webhook
```

Бот создаст webhook в Umnico на адрес:

```text
https://your-project.up.railway.app/umnico/webhook
```

Проверить список webhook:

```text
/umnico_webhooks
```

Проверить, что веб-сервис работает:

```text
https://your-project.up.railway.app/health
```

## Telegram-команды администратора

```text
/start
/new
/id
/reviewed
/delete_reviewed +79991234567
/umnico_integrations
/setup_umnico_webhook
/umnico_webhooks
```

## Логика сценария WhatsApp

### Первое сообщение пациенту

```text
Иван Иванович, спасибо Вам, что посетили СитиКлиник!
Мы надеемся, что Вы остались довольны нашей работой.
Оцените, пожалуйста, Ваш визит от 1 до 5.

Ответьте одной цифрой: 1, 2, 3, 4 или 5.
```

### Если оценка 5

Бот просит отзыв. После получения текста/голоса отправляет пациенту:

```text
Спасибо! Ниже текст Вашего отзыва.

<текст отзыва>

Нажмите и удерживайте текст отзыва выше, затем выберите «Скопировать».
После этого откройте Яндекс.Карты по ссылке ниже.

Пожалуйста, выберите 5 ⭐⭐⭐⭐⭐,
в поле «опишите минусы и плюсы» нажмите «вставить».
Текст Вашего отзыва будет вставлен.
Опубликуйте отзыв 😊

<ссылка на Яндекс.Карты>
```

### Если оценка ниже 5

Бот просит подробнее описать замечания, не ведет пациента на Яндекс.Карты и отправляет переписку в Telegram-группу.

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python bot.py
```

Для локального webhook нужен публичный HTTPS-адрес, например через ngrok. На Railway HTTPS-домен создается автоматически.

## Что делать, если сообщения не приходят в WhatsApp

1. Проверьте `UMNICO_API_TOKEN`.
2. Выполните `/umnico_integrations` и убедитесь, что `UMNICO_SA_ID` соответствует WhatsApp/WABA-интеграции.
3. Проверьте формат номера: лучше `+79991234567`.
4. Откройте Railway Logs и найдите ошибку Umnico API.
5. Если у вас WABA, уточните в Umnico, разрешена ли отправка первого обычного текстового сообщения или нужно использовать approved template.

## Что делать, если ответы пациента не обрабатываются

1. Проверьте `PUBLIC_BASE_URL`.
2. Проверьте `/health` в браузере.
3. Выполните `/umnico_webhooks`.
4. Если webhook не создан — выполните `/setup_umnico_webhook`.
5. Посмотрите Railway Logs.
