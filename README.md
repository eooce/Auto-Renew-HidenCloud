## HidenCloud自动续期
使用Github Actions 自动给HidenCloud服务续期,HidenCloud容易封多账号，隔离好环境,使用独享节点(仅自己一人使用的)


温馨提示： 需要过cloudflare验证，尽量使用干净点的节点，否则无法过验证。

**⚠️ 2026.11.9 更新**：HidenCloud 免费服务续期现在**必须使用 Discord 登录**。remember_web Cookie 和账号密码登录的会话均已失效（点击 Create Invoice 会被踢回登录页），**两种登录方式已弃用**，脚本仅支持 `DISCORD_TOKEN` Discord OAuth 授权登录；续期时若会话被踢回登录页，会自动用 Discord 重新登录并重试。

## 配置

在仓库 `Settings → Secrets and variables → Actions` 中添加以下 Secrets：

| Secret 名称 | 是否必填 | 说明 | 示例 |
|---|---|---|---|
| `DISCORD_TOKEN`  | ✅必填 | Discord User Token（Discord OAuth 登录用） | `MTE4OTYw...` |
| `EMAIL`         | ⭕选填 | HidenCloud 邮箱（仅 TG 通知展示账号用） |
| `REGION`         | ⭕选填 | 服务器地区（仅 TG 通知展示账号用） |
| ~~`PASSWORD`~~     | ❌已弃用 | 账号密码登录已失效，无需配置 |
| ~~`COOKIE_VALUE`~~  | ❌已弃用 | remember_web cookie 登录已失效，无需配置 |
| `NODE_LINK`     | ❌可选 | 代理节点地址,例如:vless:// vmess:// trojan:// hysteria2:// anytls://|
| `TG_BOT_TOKEN`  | ❌可选 | Telegram Bot Token | 
| `TG_CHAT_ID`    | ❌可选 | Telegram Chat ID |


`DISCORD_TOKEN` 获取方式：浏览器登录 [discord.com](https://discord.com) → F12 打开开发者工具 → 网络(Network) → 刷新页面 → 点任意请求 → 请求头里 `Authorization` 字段的值（切勿泄露给他人）。
<img width="600" height="400" alt="image" src="https://github.com/user-attachments/assets/dd772127-f261-49d2-9bcc-91e73a3b14f2" />



### 代理格式（确认在v2rayN里使用正常的节点,使用注册时使用的代理节点）

`NODE_LINK` 支持以下任意一种代理协议的完整分享链接（不配置则直连）：

- **VLESS**：`vless://uuid@server:port?security=reality&sni=...&type=ws&...`
- **VMess**：`vmess://base64encoded...`
- **Trojan**：`trojan://password@server:port?sni=...&type=ws&...`
- **tuic**：`tuic://uuid:password@server:port...`
- **anytls**：`anytls://uuid@server:port...`
- **hysteria2**：`hysteria2://base64@server:port...`
- **SOCKS5**：`socks5://user:pass@server:port` 或 `socks://user:pass@server:port`

## 使用

### GitHub Actions 运行步骤

1. Fork 本仓库  
2. 在仓库 Secrets 中配置必填的环境变量,（可选）配置 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`NODE_LINK`  
3. Actions菜单里手动触发 `workflow_dispatch`  
4. 根据服务到期时间来修改cron运行时间,比如你的服务是25号到期,计算后是星期二,则设置cron为每周一运行


---

**⚠️ 免责声明**：本脚本仅供学习交流使用，使用者需遵守 [HidenCloud](https://hidencloud.com) 的服务条款。因使用本脚本造成的任何问题，作者不承担任何责任。
