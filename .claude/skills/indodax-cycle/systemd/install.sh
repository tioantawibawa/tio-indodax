#!/usr/bin/env bash
# Install/remove the indodax-skills systemd USER timers (no sudo needed).
#   install.sh [--pairs btc_idr,eth_idr,sol_idr] [--dry-run]
#   install.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/indodax-skills"
STATE="${INDODAX_STATE_DIR:-$HOME/.indodax-skills}"
PYTHON="$(command -v python3)"
PAIRS="btc_idr,eth_idr,sol_idr"
UNITS=(indodax-skills-cycle indodax-skills-alerts indodax-skills-report)
DRY=0
ME="${USER:-$(id -un)}"
MODE=install

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pairs) PAIRS="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    *) echo "argumen tidak dikenal: $1" >&2; exit 2 ;;
  esac
done
[[ "$PAIRS" =~ ^[a-z0-9_,]+$ ]] || { echo "--pairs tidak valid" >&2; exit 2; }
run() { if [[ $DRY == 1 ]]; then echo "+ $*"; else "$@"; fi; }

if [[ $MODE == uninstall ]]; then
  for u in "${UNITS[@]}"; do
    run systemctl --user disable --now "$u.timer" 2>/dev/null || true
    run rm -f "$UNIT_DIR/$u.service" "$UNIT_DIR/$u.timer"
  done
  run systemctl --user daemon-reload
  echo "Timer dihapus. State ($STATE) dan $CONF_DIR/env TIDAK dihapus."
  exit 0
fi

if [[ "${INDODAX_LIVE:-}" == "true" ]]; then
  echo "INDODAX_LIVE=true di shell ini — timer otomatis hanya untuk PAPER. Unset dulu." >&2; exit 1
fi

run mkdir -p "$UNIT_DIR" "$CONF_DIR" "$STATE"
if [[ ! -f "$CONF_DIR/env" ]]; then
  if [[ $DRY == 1 ]]; then echo "+ create $CONF_DIR/env (600)"; else
    umask 077; sed "s#%STATE%#$STATE#" "$HERE/env.example" > "$CONF_DIR/env"; chmod 600 "$CONF_DIR/env"; fi
  echo "Dibuat $CONF_DIR/env — isi TELEGRAM_BOT_TOKEN/CHAT_ID bila ingin notifikasi."
fi
for u in "${UNITS[@]}"; do
  for ext in service timer; do
    if [[ $DRY == 1 ]]; then echo "+ write $UNIT_DIR/$u.$ext"; else
      sed -e "s#@PYTHON@#$PYTHON#g" -e "s#@SKILLS@#$SKILLS#g" -e "s#@PAIRS@#$PAIRS#g" \
        "$HERE/$u.$ext" > "$UNIT_DIR/$u.$ext"; fi
  done
done
run systemctl --user daemon-reload
for u in "${UNITS[@]}"; do run systemctl --user enable --now "$u.timer"; done

if [[ ! -f "$STATE/paper_balance.json" ]]; then
  echo "PERHATIAN: saldo paper belum ada. Jalankan sekali:"
  echo "  INDODAX_STATE_DIR=$STATE $PYTHON $SKILLS/indodax-broker/scripts/broker.py paper-init --idr 300000"
fi
if command -v loginctl >/dev/null && [[ "$(loginctl show-user "$ME" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo "CATATAN: tanpa 'linger', timer user hanya jalan saat Anda login. Agar jalan 24/7 (sekali, butuh izin admin):"
  echo "  sudo loginctl enable-linger $ME"
fi
echo "Selesai. Cek: systemctl --user list-timers 'indodax-skills-*'"
