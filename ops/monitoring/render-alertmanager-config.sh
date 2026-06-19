#!/bin/sh
set -eu

default_webhook_url="http://127.0.0.1:9/alertmanager-unconfigured"

if [ "${RATATOSKR_ENV:-development}" = "production" ] \
  && [ "${ALERT_WEBHOOK_URL:-$default_webhook_url}" = "$default_webhook_url" ] \
  && [ -z "${ALERT_SLACK_API_URL:-}" ] \
  && [ -z "${ALERT_TELEGRAM_WEBHOOK_URL:-}" ] \
  && [ -z "${ALERT_PAGERDUTY_ROUTING_KEY:-}" ]; then
  echo "ERROR: Alertmanager has no production receiver configured. Set ALERT_WEBHOOK_URL, ALERT_SLACK_API_URL, ALERT_TELEGRAM_WEBHOOK_URL, or ALERT_PAGERDUTY_ROUTING_KEY." >&2
fi

template="/etc/alertmanager/alertmanager.yml"
rendered="/tmp/alertmanager.yml"
tmp="${rendered}.tmp"

cp "$template" "$rendered"

for name in ALERT_WEBHOOK_URL ALERT_SLACK_API_URL ALERT_TELEGRAM_WEBHOOK_URL ALERT_PAGERDUTY_ROUTING_KEY; do
  value="$(printenv "$name" || true)"
  escaped="$(printf '%s' "$value" | sed -e 's/[\/&|]/\\&/g')"
  sed "s|\${$name}|$escaped|g" "$rendered" > "$tmp"
  mv "$tmp" "$rendered"
done

exec /bin/alertmanager \
  --config.file="$rendered" \
  --storage.path=/alertmanager \
  --web.listen-address=:9093
