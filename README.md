# 角落 · 一个可以一直待在角落里的 AI 同伴

> 自托管、可加密、异步的 AI 陪伴系统。你和它各自有一部"手机"，它有自己的时间和沉默。

---
<p align="center">
  <img src="docs/screenshot.png" alt="角落 · 界面预览" width="760">
</p>

## 这是什么

市面上大多数 AI 聊天产品是**你问它答、关掉即断**。这个项目反过来：

- **它有自己的节奏**：不是秒回。它按自己的内在状态决定什么时候说话、说什么
- **它会主动找你**：觉得该说话了就发消息，不需要你先开口
- **它有记忆**：原始层只增不删，情节层异步抽取，几个月后还认得你
- **它有人设**：人设是它自己的，可以自己起名、自己写设定
- **你可以有多个它**：每个 AI 独立人设、独立记忆，互不共享

名字取"角落"：它就在你生活的角落里，一直亮着。

## 功能

| 模块 | 作用 |
|---|---|
| `server.py` | HTTP 服务（仅标准库）：对话、鉴权、长轮询、静态文件 |
| `memory.py` | 两层记忆：原始事件（只追加）+ 情节（异步抽取） |
| `inner.py` | 内在数值状态（精力/心情等）+ 自主权分级 + 审计日志 |
| `proactive.py` | 判断"现在要不要说话"，含额度、间隔、被冷落退让 |
| `research.py` | 自研究：按课程表研究"人类哪些能力值得模拟" |
| `curiosity.py` | 自主阅读（RSS/HN 等只读源），有每日预算帽 |
| `persona.py` | 25 字段人设，版本化，支持"自己起名" |
| `agents.py` | 多 AI 管理，每人独立记忆 |
| `android/` | 安卓壳（WebView + 前台服务 + 长轮询通知） |

## 架构

```
手机 App（WebView + 前台服务）
   │  HTTPS（自签证书 + 指纹锁定）
   ▼
服务器 :8443  ← 对话 / 记忆 / 主动消息 / 静态文件
   │
   └─ 明文 :8787 只放 /static（下载安装包），其余 302 到 HTTPS
```

主动消息链路：服务器写入消息表 → 唤醒挂起的长轮询 → App 立刻弹通知（实测 < 2 秒）。

## 快速开始

### 1. 服务器

```bash
git clone <this-repo> companion && cd companion
cp .env.example .env
# 编辑 .env：填 COMPANION_API_KEY（DeepSeek 或其他 OpenAI 兼容接口）
python3 server.py
```

启动后会打印访问地址和口令（口令存在 `data/token.txt`）。

### 2. 加密（强烈建议）

```bash
mkdir -p tls
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout tls/key.pem -out tls/cert.pem \
  -subj "/CN=$(curl -s ifconfig.me)" \
  -addext "subjectAltName=IP:$(curl -s ifconfig.me)"
openssl x509 -in tls/cert.pem -noout -fingerprint -sha256
```

把指纹（去掉冒号、转小写）填进
`android/src/com/companion/app/TlsPin.java` 的 `SHA256` 常量，再打包 App。

设 `COMPANION_FORCE_HTTPS=1` 后，明文端口只放静态文件，其余自动跳加密端口。

### 3. 安卓 App

没有 Gradle，用 SDK build-tools 直接打包（见 `android/build.ps1`）：

```powershell
# 需要：JDK 17、Android SDK build-tools、一个签名密钥
.\android\build.ps1 -VersionCode 1 -VersionName "1.0"
```

首次打开 App 输入完整地址（含 `?t=口令`）。之后地址和口令存在本地。

## 安全模型

| 项 | 做法 |
|---|---|
| 传输 | 自签证书 HTTPS（TLS 1.2+） |
| 防中间人 | App 里锁死证书 SHA-256，只信任这一张 |
| 鉴权 | 首次 `?t=` 换 HttpOnly+Secure Cookie，之后口令不进 URL |
| 明文端口 | 只允许 `/static`，POST 直接拒绝 |
| 签名 | Android 会校验安装包签名，伪造包装不上 |

**注意**：安装包下载走明文端口（为了不弹证书警告），靠 Android 签名校验防替换。
更严格的做法是在 App 内用锁定指纹的加密连接下载（本项目已实现）

## 已知限制（诚实说明）

1. **不接厂商推送**：国内推送（小米/OPPO/华为）需要企业资质，个人开发者接不了。
   本项目用前台服务 + 长轮询，App 被杀掉就收不到。已做保活（开机自启、被划掉后拉回、看门狗），
   但系统强行停止或省电策略会终止一切 App——微信能收到是因为系统推送通道，不是它自己强
2. **证书要续**：自签证书到期或更换后，要重打包 App（指纹变了）
3. **单机 SQLite**：适合一个人用，不适合多用户并发
4. **成本**：主动评估会持续消耗 token，可用 `proactive_per_day` / `judge_interval_min` 调节

## 设计取舍

- **宁可少说，不要骚扰**：主动消息有额度、最小间隔、被冷落退让；默认夜间安静
- **记忆不共享**：多个 AI 之间不串味
- **不假装是人**：人设可以很丰富，但被认真追问"你是 AI 吗"时应当承认

## 目录

```
server.py            主服务
memory.py            原始层 + 情节层
inner.py             内在状态
proactive.py         主动判断
research.py          自研究
curiosity.py         自主阅读
persona.py           人设
agents.py            多 AI
static/              网页界面（微信风格）
android/             安卓壳
deploy/              部署脚本
```

## License

MIT（见 LICENSE）。你可以自由使用、修改、商用；不提供任何担保。
