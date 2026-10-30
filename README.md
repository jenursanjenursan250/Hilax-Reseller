# 3x-ui Telegram Reseller Bot (multi-port)

The admin gives resellers GB credit. A reseller sends `/create`, picks a port from an inline menu, and a
client is created on the matching 3x-ui inbound. Pure Python (telebot + requests + SQLite), so it
installs cleanly on Termux and on any Linux server.

## 1. Project structure

```
xui-reseller-bot/
├── bot.py            # Telegram commands + inline port menu (entry point)
├── service.py        # credit rules + panel operations
├── xui.py            # 3x-ui API client (login, add/update/delete client, usage)
├── db.py             # SQLite: resellers, clients, credit ledger
├── config.py         # reads .env
├── .env              # YOUR settings (already filled in; keep private)
├── .env.example      # template
├── requirements.txt
└── tests/test_flow.py  # offline self-test against a fake panel
```

## 2. Configuration (`.env`, no code changes needed)

| Setting | Meaning |
|---|---|
| `BOT_TOKEN`, `ADMIN_IDS` | Telegram bot token and admin numeric ID(s) |
| `XUI_URL` | Panel URL incl. port and base path, no trailing slash |
| `XUI_USERNAME`, `XUI_PASSWORD` | Panel login |
| `INBOUNDS` | Port menu as `port:inbound_id`, e.g. `8443:1,443:2,119:3,80:11` |
| `XUI_VERIFY_SSL` | `false` only for self-signed certificates |
| `XUI_FLOW` | Only for VLESS+Reality inbounds (e.g. `xtls-rprx-vision`) |
| `SUB_URL_BASE` | Optional subscription base URL; enables subscription links |
| `DEFAULT_DAYS`, `MAX_GB_PER_CLIENT`, `LIMIT_IP`, `REFUND_UNUSED_ON_DELETE` | Reseller rules |

To add/remove/rename ports later, just edit `INBOUNDS` and restart.

**Security:** `.env` holds your bot token and panel password. Run `chmod 600 .env`, never share the
file, and never commit it (`.gitignore` already excludes it).

## 3. Credit system

| Who | Command | What it does |
|---|---|---|
| Admin | `/addreseller <tg_id> [username]` | Register / re-activate a reseller |
| Admin | `/addgb <tg_id> <gb>` / `/subgb <tg_id> <gb>` | Add / remove credit |
| Admin | `/resellers`, `/removereseller <tg_id>`, `/panel` | List, deactivate, test panel + every inbound |
| Reseller | `/balance` | Remaining GB |
| Reseller | `/create <name> <gb> [days]` | Shows the **port menu**; creating deducts GB |
| Reseller | `/topup <name> <gb>` | Add data to an existing client (deducted) |
| Reseller | `/myclients`, `/link <name>` | Usage per client and port, subscription link |
| Reseller | `/delete <name>` | Remove a client (refunds unused GB if enabled) |

How `/create` charges:
1. Input and credit are validated before the menu appears (resellers with no credit get no menu).
2. After the port is chosen, the client is created on the panel.
3. **GB is deducted only after the panel confirms success.** If the panel call fails, nothing is charged.
   If saving locally fails, the panel client is removed again and nothing is charged.
4. Each reseller's operations are serialised, so quick double requests can't overspend. The port
   menu is single-use and expires after 5 minutes (`PENDING_TTL`).

Every credit change is recorded in the `ledger` table of `reseller.db`.

## 4. Run and test

### Termux (Android)
```bash
pkg update && pkg upgrade -y
pkg install python unzip -y
unzip xui-reseller-bot.zip && cd xui-reseller-bot
pip install -r requirements.txt
python tests/test_flow.py     # optional self-test -> ALL TESTS PASSED
python bot.py
```
Keep it alive with `termux-wake-lock` and `nohup python bot.py > out.log 2>&1 &` (or `tmux`), and turn
off battery optimisation for Termux. Android can still kill it, so a small VPS is more reliable.

### Linux server
```bash
sudo apt install -y python3 python3-venv unzip
unzip xui-reseller-bot.zip && cd xui-reseller-bot
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
chmod 600 .env
python bot.py
```
As a service (`/etc/systemd/system/xui-reseller.service`, adjust paths/user):
```ini
[Unit]
Description=3x-ui reseller bot
After=network-online.target

[Service]
WorkingDirectory=/opt/xui-reseller-bot
ExecStart=/opt/xui-reseller-bot/venv/bin/python bot.py
Restart=always
RestartSec=5
User=botuser

[Install]
WantedBy=multi-user.target
```
`sudo systemctl daemon-reload && sudo systemctl enable --now xui-reseller`

### First test in Telegram
1. As admin: `/panel` → login OK and all four inbounds listed as OK.
2. `/addreseller <id>` then `/addgb <id> 20`.
3. From the reseller account: `/create test1 1 7` → tap a port → check the client in the panel.
4. `/delete test1`.

## 5. Troubleshooting

- **"non-JSON reply"** – wrong `XUI_URL`, port or base path.
- **"Panel login failed"** – wrong credentials (or 2FA on the panel).
- **SSL error** – set `XUI_VERIFY_SSL=false` for self-signed certificates.
- **`/panel` shows an inbound as ⚠️** – wrong inbound ID in `INBOUNDS`.
- **Client created but won't connect** – check the inbound's protocol / `XUI_FLOW`.
- Logs: `bot.log`.

## Notes

- Targets current 3x-ui (v2.x, `/panel/api/inbounds/...`) and VLESS/VMess clients. Trojan/Shadowsocks
  inbounds need a different client payload.
- Resellers are authenticated only by Telegram ID, so only add people you trust.
- Back up `reseller.db` regularly; it holds balances and the client list.
