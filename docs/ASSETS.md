# 表情素材怎么放（本仓库不附带第三方图片）

代码里的表情机制是完整的，但**图片不随仓库分发**。原因见下，放法也见下。

## 为什么不在仓库里

表情功能上线时用的是社区整理的"鲸鱼娘"二创图。那个[资产库](https://github.com/TreapGoGo/deepseek-whale-girl)自己写得很清楚：

> 收录不把图片变成 MIT 素材。网站代码许可与投稿作品许可分别处理。
> 本页仅作个人学习与欣赏的整理。

也就是说：

- 代码是 MIT，**图片不是**
- 部分投稿明确标注 CC BY-NC-SA 4.0，部分资料缺失、授权状况不明
- 把几十张授权不统一的二创图放进 MIT 仓库再分发，超出了"个人欣赏"的范围

所以本仓库只带机制，不带图。你的自用副本里有图没关系——那是你自己的使用。

## 怎么加自己的表情

### 1. 准备图片

任意来源（自己画的、买的、明确授权的）都行。要求：

- JPG 格式
- 建议 320px 宽左右、单张 20–50 KB
- 方形或接近方形，主体在中间

### 2. 放文件

```
android/assets/stickers/st01.jpg
android/assets/stickers/st02.jpg
...
```

### 3. 写映射表

`android/assets/stickers.json`，键是**情绪名**，值是文件名列表（一个情绪可以多张，随机选）：

```json
{
  "开心": ["st01", "st02"],
  "难过": ["st03"],
  "生气": ["st04"]
}
```

情绪名要和 `server.py` 里的 `STICKERS` 列表对得上（大小写、用词完全一致），否则模型发出来也取不到图。

### 4. 服务端也要能取到

`/api/sticker/<情绪>` 优先读 `data/stickers/stickers.json` + `data/stickers/*.jpg`，
找不到才回落到仓库里的 `android/assets/stickers/`。

所以两种放法都行：

- 只放 `android/assets/`：App 内置图能用；服务端兜底也能取到
- 再复制一份到 `data/stickers/`：服务端优先用这份（适合经常换图）

```bash
# 可选：复制一份到服务端目录
mkdir -p data/stickers
cp android/assets/stickers/*.jpg data/stickers/
cp android/assets/stickers.json data/stickers/stickers.json
```

### 5. 重新打包 App

```powershell
.\android\build.ps1 -VersionCode 1 -VersionName "1.0"
```

`build.ps1` 会把 `android/assets/` 打进 APK（`-A assets`）。装完就有图了。

## 头像

联系人头像不是本地资源，是 URL。数据库里 `agents.avatar` 存路径，比如：

```sql
UPDATE agents SET avatar='/static/avatars/xxx.jpg' WHERE id=5;
```

图片放到 `static/avatars/` 下即可（`/static` 是公开可读的静态目录，不需要口令）。

不设 `avatar` 就退回原来的字形图标，不影响使用。
