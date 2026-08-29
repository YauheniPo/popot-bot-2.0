#!/bin/bash
# api-retry-loop.sh — повторює запит до моделі кожні 30 сек, максимум 3 хвилини (6 спроб) при rate-limit (429)
# Використання: ./api-retry-loop.sh [MODEL_NAME] ["USER_MESSAGE"]
# Приклад: ./api-retry-loop.sh "poolside/laguna-s-2.1:free" "напиши короткий опис"

API_URL="http://127.0.0.1:9119/api/execute"

# Параметри за замовчуванням або з аргументів
MODEL_NAME="${1:-poolside/laguna-s-2.1:free}"
USER_MESSAGE="${2:-Hello}"

MAX_ATTEMPTS=6
WAIT_SECONDS=30  # 30 секунд, максимум 3 хвилини (6 спроб)

echo "🔄 Запуск retry-циклу для моделі: $MODEL_NAME"
echo "📡 API endpoint: $API_URL"
echo "⏱ Интервал повторів: кожні $WAIT_SECONDS секунд"
echo "========================================"

for i in $(seq 1 $MAX_ATTEMPTS); do
    # Надсилаємо запит
    RESPONSE=$(curl -s -w "\n%{http_code}" "$API_URL" \
        -H "Content-Type: application/json" \
        -d "{"model":"$MODEL_NAME","messages":[{"role":"user","content":"$USER_MESSAGE"}]}")

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -n -1)

    if [ "$HTTP_CODE" -eq 200 ]; then
        echo "✅ [$(date '+%H:%M:%S')] Успішно! Відповідь:"
        echo "$BODY"
        exit 0
    elif [ "$HTTP_CODE" -eq 429 ]; then
        REMAINING_MIN=$(( (MAX_ATTEMPTS - i) * WAIT_SECONDS / 60 ))
        echo "🕐 [$(date '+%H:%M:%S')] Rate limited (429). Чекаю $WAIT_SECONDS сек... (спроба $i/$MAX_ATTEMPTS, залишилось ~${REMAINING_MIN} хв)"
        sleep $WAIT_SECONDS
    else
        echo "❌ [$(date '+%H:%M:%S')] HTTP $HTTP_CODE: $BODY"
        if [ $i -lt $MAX_ATTEMPTS ]; then
            echo "🔄 Чекаємо $WAIT_SECONDS сек і повторюємо..."
            sleep $WAIT_SECONDS
        fi
    fi
done

echo "🔴 [$(date '+%H:%M:%S')] Вичерпано спроби ($MAX_ATTEMPTS). Не вдалося виконати запрос."
exit 1
