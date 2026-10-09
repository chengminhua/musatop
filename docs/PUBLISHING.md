# PyPI 发布维护

发行包名和命令名均为 `musatop`。版本只在 `musatop/__init__.py` 定义，`pyproject.toml` 动态读取它。软件版本与 JSON 的 `schema_version=1` 无关。

## 首次配置

PyPI 账号完成邮箱验证及双重认证后，在账户的 Publishing 页面添加 GitHub 待发布者：项目 `musatop`、Owner `chengminhua`、Repository `musatop`、Workflow `publish.yml`、Environment `pypi`。首次成功上传会创建项目。后续沿用项目中的可信发布者，不需要长期 API Token 或仓库 Secret。

工作流使用 GitHub 的 `pypi` 环境，首次引用时可自动创建。若仓库管理员为此环境设置了保护规则，发布需要满足这些规则。

本项目直接发布正式 PyPI，不使用 TestPyPI；构建、安装及回归测试必须在上传前通过。

## 发布步骤

1. 在 `main` 完成修改、独立审查和真机验证，更新 README、版本以及 `docs/VALIDATION.md`。修正 README 中指向版本标签的截图和文档链接。
2. 在干净目录构建本次发行包：`python -m build`，再执行 `python -m twine check --strict dist/*`。不要混用历史 `dist/` 中的包。检查 wheel 和源码包内容，在源码目录之外的独立环境中安装验证。
3. 运行 Python 3.10／3.12 完整回归。真机仅使用独立环境进行验证，不替换驱动或操作已有业务进程。
4. 检查远端变化，整合后推送 `main`，确认常规 CI 成功。
5. 为该提交建立与软件版本一致的标签，例如 `git tag -a v0.2.1 -m 'musatop v0.2.1'`，再执行 `git push origin v0.2.1`。**推送 `v*` 标签会触发正式发布。**
6. 查看 GitHub Actions 的 `publish` 工作流。确认 PyPI 页面、GitHub Release 和安装后的版本一致；在干净环境中执行 `python -m pip install --index-url https://pypi.org/simple musatop==0.2.1` 并验证命令、采集和 TUI。

以上 `0.2.1` 是首次发布示例，后续使用新的版本号。正式站不能覆盖已上传的同名发行文件，删除后也不能重用；修复已发布内容必须递增版本，不移动已经发布的标签。

## 自动化关卡

`.github/workflows/publish.yml` 在 `v*` 标签推送时依次执行：

- 校验标签与运行时版本一致；从源码包构建 wheel，并以 `twine check --strict` 检查元数据。
- 下载同一份构建产物，在 Python 3.10／3.12 上从 wheel 安装，在源码目录之外运行完整测试和命令入口检查；另外检查源码包安装。
- 仅在全部检查通过后，使用 `pypi` 环境的 Trusted Publishing 上传这份产物。只有上传 job 获得 `id-token: write`，该 job 不检出或执行项目源码。
- 上传成功后创建 GitHub Release，附上同一份 wheel 和源码包。只有此 job 获得仓库内容写权限。

普通分支推送不会发布。手动运行此工作流仅构建和测试，即使选择标签也不会上传或创建 Release。

## 失败处理

- 标签和版本不一致：检查目标提交；在上传前修正版本与标签，不能靠关闭校验绕过。
- `invalid-publisher`：核对 PyPI 绑定的 Owner、仓库、工作流文件名和环境名，尤其不能把 Workflow 填成完整路径或工作流显示名称。
- 环境等待审核：按仓库已有环境规则操作，不移除保护规则来绕过审核。
- 上传中断：先检查 PyPI 实际已接收的文件。不要直接重跑整个发布或开启 `skip-existing` 掩盖差异；核对文件哈希后再决定如何补齐，已发布内容有误时使用新版本。
- PyPI 成功但 GitHub Release 创建失败：包已经公开，不重复上传；在 Actions 中只重跑失败的 Release job，或用相同标签和已发布文件补建 Release。
- pip 找不到版本：先确认 PyPI 项目页。第三方镜像同步可能延迟，可显式使用 `--index-url https://pypi.org/simple` 检查。

参考：[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)、[首次创建项目](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)、[PyPA 打包指南](https://packaging.python.org/en/latest/tutorials/packaging-projects/)。
