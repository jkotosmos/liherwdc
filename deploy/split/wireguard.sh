#!/bin/sh
# Приватный канал WireGuard между сервером баз (10.8.0.1) и сервером приложения (10.8.0.2).
# Секретные ключи создаются на каждом сервере и никуда не передаются —
# серверы обмениваются только публичными ключами.
#
#   1. Сервер баз:        sh wireguard.sh db   <публичный IP сервера приложения>
#                         → напечатает КЛЮЧ_БАЗ
#   2. Сервер приложения: sh wireguard.sh app  <публичный IP сервера баз> <КЛЮЧ_БАЗ>
#                         → напечатает КЛЮЧ_ПРИЛОЖЕНИЯ
#   3. Сервер баз:        sh wireguard.sh peer <КЛЮЧ_ПРИЛОЖЕНИЯ>
#   Проверка с сервера приложения: ping -c 3 10.8.0.1
#
# Запускать от root. Повторный запуск безопасен: ключи не пересоздаются.
set -e

PORT=51820
DB_IP=10.8.0.1
APP_IP=10.8.0.2
CONF=/etc/wireguard/wg0.conf

usage() {
    sed -n '2,13p' "$0"
    exit 2
}

need_root() {
    [ "$(id -u)" = 0 ] || { echo "ОШИБКА: запустите от root"; exit 1; }
}

install_wg() {
    if ! command -v wg >/dev/null 2>&1; then
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard-tools
    fi
}

make_keys() {
    umask 077
    mkdir -p /etc/wireguard
    [ -s /etc/wireguard/private.key ] || wg genkey > /etc/wireguard/private.key
    wg pubkey < /etc/wireguard/private.key > /etc/wireguard/public.key
}

check_key() {
    # Публичный ключ WireGuard: 44 символа base64, оканчивается на «=».
    echo "$1" | grep -Eq '^[A-Za-z0-9+/]{43}=$' || { echo "ОШИБКА: «$1» не похоже на ключ WireGuard"; exit 1; }
}

check_ip() {
    echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || { echo "ОШИБКА: «$1» не похоже на IP-адрес"; exit 1; }
}

# Указан адрес этого же сервера — значит, команду запустили не на том сервере.
not_self() {
    if ip -4 addr show 2>/dev/null | grep -q "inet $1/"; then
        echo "ОШИБКА: $1 — это адрес ЭТОГО сервера ($(hostname))."
        echo "Похоже, команда запущена не на том сервере: «db» и «peer» — на сервере баз,"
        echo "«app» — на сервере приложения."
        exit 1
    fi
}

start() {
    systemctl enable wg-quick@wg0 >/dev/null 2>&1
    systemctl restart wg-quick@wg0
}

# Docker на сервере баз занимает порты на адресе 10.8.0.1 — после перезагрузки
# он должен стартовать уже после WireGuard, иначе адреса ещё нет.
docker_after_wg() {
    mkdir -p /etc/systemd/system/docker.service.d
    cat > /etc/systemd/system/docker.service.d/after-wireguard.conf <<EOF
[Unit]
After=wg-quick@wg0.service
Wants=wg-quick@wg0.service
EOF
    systemctl daemon-reload
}

firewall_db() {
    # Порт WireGuard — только для сервера приложения. SSH не закрываем.
    command -v ufw >/dev/null 2>&1 || return 0
    ufw allow 22/tcp >/dev/null
    ufw allow from "$1" to any port "$PORT" proto udp >/dev/null
    ufw --force enable >/dev/null
    echo "Фаервол: SSH открыт, $PORT/udp — только для $1"
}

need_root
case "${1:-}" in
db)
    [ -n "${2:-}" ] || usage
    check_ip "$2"
    not_self "$2"
    install_wg
    make_keys
    cat > "$CONF" <<EOF
[Interface]
Address = $DB_IP/24
ListenPort = $PORT
PrivateKey = $(cat /etc/wireguard/private.key)
EOF
    chmod 600 "$CONF"
    start
    docker_after_wg
    firewall_db "$2"
    echo
    echo "Сервер баз: $DB_IP. Шаг 2 — на сервере приложения:"
    echo "  sh wireguard.sh app <публичный IP этого сервера> $(cat /etc/wireguard/public.key)"
    ;;
app)
    [ -n "${2:-}" ] && [ -n "${3:-}" ] || usage
    check_ip "$2"
    not_self "$2"
    check_key "$3"
    install_wg
    make_keys
    cat > "$CONF" <<EOF
[Interface]
Address = $APP_IP/24
PrivateKey = $(cat /etc/wireguard/private.key)

[Peer]
PublicKey = $3
Endpoint = $2:$PORT
AllowedIPs = $DB_IP/32
PersistentKeepalive = 25
EOF
    chmod 600 "$CONF"
    start
    echo
    echo "Сервер приложения: $APP_IP. Шаг 3 — на сервере баз:"
    echo "  sh wireguard.sh peer $(cat /etc/wireguard/public.key)"
    ;;
peer)
    [ -n "${2:-}" ] || usage
    check_key "$2"
    [ -f "$CONF" ] || { echo "ОШИБКА: сначала шаг 1 (sh wireguard.sh db ...)"; exit 1; }
    # Один сервер приложения: прежний [Peer], если был, заменяем.
    sed -i '/^\[Peer\]/,$d' "$CONF"
    sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$CONF"
    cat >> "$CONF" <<EOF

[Peer]
PublicKey = $2
AllowedIPs = $APP_IP/32
EOF
    if ip link show wg0 >/dev/null 2>&1; then
        # Канал уже поднят — применяем без перезапуска интерфейса: базы,
        # занявшие порты на 10.8.0.1, ничего не заметят.
        umask 077
        wg-quick strip wg0 > /etc/wireguard/wg0.stripped
        wg syncconf wg0 /etc/wireguard/wg0.stripped
        rm -f /etc/wireguard/wg0.stripped
    else
        start
    fi
    echo
    echo "Канал настроен. Проверка с сервера приложения: ping -c 3 $DB_IP"
    ;;
*)
    usage
    ;;
esac
