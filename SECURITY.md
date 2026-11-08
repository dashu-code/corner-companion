# 安全说明

## 部署前必做

1. **生成自己的证书**，不要把别人的 `tls/` 拷过来
2. **把证书指纹填进 App**（`TlsPin.java` 的 `SHA256`），否则 App 拒绝连接
3. **口令由服务端生成**（`data/token.txt`），不要用仓库里的示例值
4. **`COMPANION_FORCE_HTTPS=1`**，让明文端口只服务静态文件
5. 只对外开放两个端口：`8443`（加密）和 `8787`（下载安装包用）

## 绝对不要提交

- `tls/key.pem`（私钥）
- `android/keystore/`（签名密钥，泄露就能伪造更新包）
- `data/companion.db`（聊天记录）
- `data/token.txt`（访问口令）
- `.env`（API key）

`git status` 里看到以上任何文件，先加进 `.gitignore` 再提交。
