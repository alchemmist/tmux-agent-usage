#!/usr/bin/env bash

: "${providers:=}"
: "${now:=$(date +%s)}"
: "${separator:=  }"
: "${prefix:=}"

refresh_interval="$(get_tmux_option @agent_usage_refresh_interval 60)"
[[ "$refresh_interval" =~ ^[0-9]+$ ]] || refresh_interval=60
((refresh_interval < 15)) && refresh_interval=15
cursor_resolve="$(get_tmux_option @agent_usage_cursor_resolve '')"
python="$(command -v python3 2>/dev/null || true)"
if [ -z "$python" ]; then
	for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
		[ -x "$candidate" ] && { python="$candidate"; break; }
	done
fi
if [ -n "$python" ]; then
	("$python" "$DIR/subscriptions.py" --providers "$providers" --ttl "$refresh_interval" --cursor-resolve "$cursor_resolve" >/dev/null 2>&1 &) 2>/dev/null
fi

subscription_segment() {
	local provider="$1" label pct="" reset="" updated=0 status=loading key value window
	local current_pct="" current_reset="" week_pct="" week_reset=""
	label="$(get_tmux_option "@agent_usage_${provider}_label" "$provider")"
	if [ -f "$HOME/.cache/claude-usage/subscriptions/$provider" ]; then
		while IFS='=' read -r key value; do
			case "$key" in
			PCT) pct="$value" ;;
			RESET) reset="$value" ;;
			CURRENT_PCT) current_pct="$value" ;;
			CURRENT_RESET) current_reset="$value" ;;
			WEEK_PCT) week_pct="$value" ;;
			WEEK_RESET) week_reset="$value" ;;
			UPDATED_AT) updated="$value" ;;
			STATUS) status="$value" ;;
			esac
		done <"$HOME/.cache/claude-usage/subscriptions/$provider"
	fi
	if [ "$provider" = chatgpt ]; then
		window="$(get_tmux_option @agent_usage_chatgpt_window weekly)"
		if [ "$window" = current ]; then
			pct="$current_pct"; reset="$current_reset"
		else
			pct="$week_pct"; reset="$week_reset"
		fi
	fi
	if ! [[ "$pct" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
		printf '#[fg=%s]%s ?#[default]' "${label_color:-default}" "$label"
		return
	fi
	if [[ "$reset" =~ ^[0-9]+$ ]] && ((now >= reset)); then
		printf '#[fg=%s]%s ?#[default]' "${label_color:-default}" "$label"
		return
	fi
	window_segment "$pct" "$reset" "" "$label"
	if ! [[ "$updated" =~ ^[0-9]+$ ]] || ((now - updated > refresh_interval * 2)) || [ "$status" != ok ]; then
		printf '#[fg=%s] ~#[default]' "${label_color:-default}"
	fi
}

output=""
IFS=',' read -r -a provider_list <<<"$providers"
for provider in "${provider_list[@]}"; do
	case "$provider" in chatgpt|cursor) ;; *) continue ;; esac
	[ -n "$output" ] && output+="$separator"
	output+="$(subscription_segment "$provider")"
done
printf '%s%s' "$prefix" "$output"
