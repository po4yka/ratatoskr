#!/bin/sh
set -eu

# The rendered config contains receiver credentials. Keep it private even when
# the container gains additional processes in a future image.
umask 077

default_webhook_url="http://127.0.0.1:9/alertmanager-unconfigured"
environment="${RATATOSKR_ENV:-development}"
webhook_url="${ALERT_WEBHOOK_URL:-$default_webhook_url}"
slack_api_url="${ALERT_SLACK_API_URL:-}"
telegram_webhook_url="${ALERT_TELEGRAM_WEBHOOK_URL:-}"
pagerduty_routing_key="${ALERT_PAGERDUTY_ROUTING_KEY:-}"

if [ "$environment" = "production" ] \
  && [ "$webhook_url" = "$default_webhook_url" ] \
  && [ -z "$slack_api_url" ] \
  && [ -z "$telegram_webhook_url" ] \
  && [ -z "$pagerduty_routing_key" ]; then
  echo "ERROR: Alertmanager has no production receiver configured. Set ALERT_WEBHOOK_URL, ALERT_SLACK_API_URL, ALERT_TELEGRAM_WEBHOOK_URL, or ALERT_PAGERDUTY_ROUTING_KEY." >&2
  exit 1
fi

fail_invalid() {
  echo "ERROR: Alertmanager receiver in $1 is invalid; refusing to start." >&2
  exit 1
}

validate_http_url() {
  variable_name="$1"
  value="$2"
  case "$value" in
    http://?* | https://?*) ;;
    *) fail_invalid "$variable_name" ;;
  esac
  case "$value" in
    *[[:space:]]*) fail_invalid "$variable_name" ;;
  esac
}

validate_https_url() {
  variable_name="$1"
  value="$2"
  case "$value" in
    https://?*) ;;
    *) fail_invalid "$variable_name" ;;
  esac
  case "$value" in
    *[[:space:]]*) fail_invalid "$variable_name" ;;
  esac
}

yaml_quote() {
  # YAML single-quoted scalars escape a quote by doubling it. Values are never
  # printed to logs or command output.
  printf '%s' "$1" | sed "s/'/''/g"
}

template="${ALERTMANAGER_CONFIG_TEMPLATE:-/etc/alertmanager/alertmanager.yml}"
rendered="${ALERTMANAGER_CONFIG_RENDERED:-/tmp/alertmanager.yml}"
alertmanager_bin="${ALERTMANAGER_BIN:-/bin/alertmanager}"

marker="$(sed -n '/# RECEIVER_CONFIGS/p' "$template")"
if [ -z "$marker" ]; then
  echo "ERROR: Alertmanager config template is missing the receiver marker." >&2
  exit 1
fi

sed -n '1,/# RECEIVER_CONFIGS/p' "$template" > "$rendered"

configured=0

if [ "$webhook_url" != "$default_webhook_url" ]; then
  validate_http_url "ALERT_WEBHOOK_URL" "$webhook_url"
fi
if [ -n "$telegram_webhook_url" ]; then
  validate_https_url "ALERT_TELEGRAM_WEBHOOK_URL" "$telegram_webhook_url"
fi
if [ "$webhook_url" != "$default_webhook_url" ] || [ -n "$telegram_webhook_url" ]; then
  {
    printf '%s\n' '    webhook_configs:'
    if [ "$webhook_url" != "$default_webhook_url" ]; then
      escaped="$(yaml_quote "$webhook_url")"
      printf "      - url: '%s'\n" "$escaped"
      printf '%s\n' '        send_resolved: true'
    fi
    if [ -n "$telegram_webhook_url" ]; then
      escaped="$(yaml_quote "$telegram_webhook_url")"
      printf "      - url: '%s'\n" "$escaped"
      printf '%s\n' '        send_resolved: true'
    fi
  } >> "$rendered"
  configured=1
fi

if [ -n "$slack_api_url" ]; then
  validate_https_url "ALERT_SLACK_API_URL" "$slack_api_url"
  escaped="$(yaml_quote "$slack_api_url")"
  {
    printf '%s\n' '    slack_configs:'
    printf "      - api_url: '%s'\n" "$escaped"
    printf '%s\n' "        title: '{{ template \"slack.default.title\" . }}'"
    printf '%s\n' "        text: '{{ template \"slack.default.text\" . }}'"
    printf '%s\n' '        send_resolved: true'
  } >> "$rendered"
  configured=1
fi

if [ -n "$pagerduty_routing_key" ]; then
  case "$pagerduty_routing_key" in
    *[!A-Za-z0-9_-]*) fail_invalid "ALERT_PAGERDUTY_ROUTING_KEY" ;;
  esac
  escaped="$(yaml_quote "$pagerduty_routing_key")"
  {
    printf '%s\n' '    pagerduty_configs:'
    printf "      - routing_key: '%s'\n" "$escaped"
    printf '%s\n' "        severity: '{{ if eq .CommonLabels.severity \"critical\" }}critical{{ else }}warning{{ end }}'"
    printf '%s\n' '        send_resolved: true'
  } >> "$rendered"
  configured=1
fi

if [ "$configured" -eq 0 ]; then
  # Development keeps the historical discard receiver so a local monitoring
  # stack remains optional. Production has already failed closed above.
  escaped="$(yaml_quote "$default_webhook_url")"
  {
    printf '%s\n' '    webhook_configs:'
    printf "      - url: '%s'\n" "$escaped"
    printf '%s\n' '        send_resolved: true'
  } >> "$rendered"
fi

chmod 600 "$rendered"

exec "$alertmanager_bin" \
  --config.file="$rendered" \
  --storage.path=/alertmanager \
  --web.listen-address=:9093
