# TrezaReg - 注册自动化工具

> Treza 注册自动化工具，支持 Cloudflare 临时邮箱和代理池，自动转换为 API 格式

---

## 📖 目录

- [功能特性](#-功能特性)
- [快速开始](#-快速开始)
- [配置说明](#-配置说明)
- [API 接口](#-api-接口)
- [项目结构](#-项目结构)

---

## ✨ 功能特性

- 🤖 **全自动注册** - 支持自动化创建账号
- 📧 **Cloudflare 临时邮箱** - 内置临时邮箱支持
- 🌐 **代理池支持** - 内置代理池管理
- 🔄 **API 格式转换** - 自动转换为 sub2api 等格式
- 📊 **Web 管理界面** - 提供可视化操作面板
- ⚙️ **配置灵活** - YAML 配置文件支持

---

## 🚀 快速开始

### 安装依赖

```bash
cd TrezaReg/trezaRegAnd2Api
uv sync
```

### 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml 配置您的参数
```

### 运行

```bash
# 使用 uv 运行
uv run treza-reg

# 或安装后运行
uv pip install .
treza-reg
```

### Web 界面

启动后访问 `http://localhost:8000` 查看管理面板。

---

## ⚙️ 配置说明

### config.yaml 示例

```yaml
email:
  type: "cloudflare"  # cloudflare 或其他
  api_url: "https://your-cloudflare-email-api.com"

proxy:
  type: "pool"  # pool 或 static
  pool_size: 10

register:
  batch_size: 5
  retry_times: 3

api:
  output_format: "sub2api"  # sub2api, cpa, json
  webhook_url: ""
```

---

## 🌐 API 接口

| 接口 | 方法 | 描述 |
| :--- | :--- | :--- |
| `/` | GET | Web 管理界面 |
| `/register` | POST | 发起注册请求 |
| `/generate` | POST | 生成账号 |
| `/accounts` | GET | 获取账号列表 |

---

## 📁 项目结构

```
trezaRegAnd2Api/
├── src/treza_reg/
│   ├── main.py           # 入口文件
│   ├── runner.py         # 运行器
│   ├── register.py       # 注册逻辑
│   ├── register_service.py
│   ├── account_manager.py # 账号管理
│   ├── cloudflare_email.py # 临时邮箱
│   ├── proxy_pool.py     # 代理池
│   ├── generate_service.py
│   ├── web_server.py     # Web 服务
│   ├── config.py         # 配置加载
│   ├── state.py          # 状态管理
│   ├── storage.py        # 存储
│   └── templates/        # HTML 模板
├── config.yaml.example   # 配置示例
├── pyproject.toml       # 项目配置
└── README.md
```

---

## 📝 许可证

MIT License

---

**最后更新**: 2026-07-08
