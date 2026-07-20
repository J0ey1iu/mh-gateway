# mh-gateway 打包分发指导

本文档面向 **World of Agents 内部开发人员**，说明如何将 `mh-gateway` 打包为 `.whl` 并提供给客户企业。

---

## 依赖关系

```
mh-gateway ──depends on──▶ minimal-harness
```

`minimal-harness` **未发布到 PyPI**，打包时必须**同时构建两个包**，一并交付给客户。

---

## 打包步骤

### 1. 构建 `minimal-harness`

```bash
cd packages/minimal-harness
uv build
```

产出位于项目根目录 `dist/`：

- `minimal_harness-<version>-py3-none-any.whl`
- `minimal_harness-<version>.tar.gz`

### 2. 构建 `mh-gateway`

```bash
cd packages/mh-gateway
uv build
```

产出同样位于项目根目录 `dist/`：

- `mh_gateway-<version>-py3-none-any.whl`
- `mh_gateway-<version>.tar.gz`

### 3. 准备交付包

将两个 `.whl` 文件一并提供给客户：

```
delivery-package/
├── minimal_harness-<version>-py3-none-any.whl
├── mh_gateway-<version>-py3-none-any.whl
└── customer-adaptation-guide.md   # 见 customer-adaptation-guide.md
```

> **为什么不能只发 `mh-gateway`？**
>
> `mh_gateway` 的代码大量 `import` `minimal_harness.*`，
> pip 安装时会尝试从 PyPI 下载 `minimal-harness`，但该包未在 PyPI 上发布，
> 因此必须提供 `.whl` 或搭建私有 PyPI。

---

## 版本号管理

| 包 | 版本号位置 |
|---|---|
| `mh-gateway` | `pyproject.toml` → `[project].version` |
| `minimal-harness` | `packages/minimal-harness/pyproject.toml` → `[project].version` |

发布前请确认：
- 两个包的版本号已更新（遵循 [SemVer](https://semver.org/)）
- 如 `mh-gateway` 有破坏性变更，可能也需要 `minimal-harness` 发版

---

## 构建验证

每次构建后运行：

```bash
# 验证 wheel 包含所有必要文件
unzip -l dist/mh_gateway-*.whl

# 在临时虚拟环境中测试安装
uv venv /tmp/test-venv
source /tmp/test-venv/bin/activate
pip install dist/mh_gateway-*.whl dist/minimal_harness-*.whl
python -c "from mh_gateway import create_app; print('OK')"
```

---

## 高阶：私有 PyPI

如果客户数量多，可以考虑搭建私有 PyPI（如 [devpi](https://www.devpi.net/)、[twine](https://pypi.org/project/twine/) + 私有仓库），
将两个包发布到私有源上，客户只需：

```bash
pip install --index-url https://pypi.internal.company.com mh-gateway
```

---

## 常见问题

**Q: `uv build` 失败，报 workspace 相关错误？**

A: 确保在包自己的目录（`packages/mh-gateway/`）下运行 `uv build`，而不是项目根目录。

**Q: 客户安装时报 `ModuleNotFoundError: No module named 'minimal_harness'`？**

A: 客户只安装了 `mh_gateway`，没有安装 `minimal-harness`。参考 [打包步骤 3](#3-准备交付包) 将两个 `.whl` 都发给客户。

**Q: 客户使用 uv，install 时报源不可达？**

A: `pyproject.toml` 中已配置 Aliyun 镜像为默认索引，若客户不在中国内地，可删除 `[[tool.uv.index]]` 配置或覆盖 `UV_INDEX_URL` 环境变量。
