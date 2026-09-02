# 受管 Skill 在线新建与编辑设计

## 背景

管理界面当前只能注册服务端已有 Skill 目录，或从电脑导入完整 Skill 文件夹。对于只有一个 `SKILL.md` 的轻量 Skill，用户仍需先在文件系统创建目录和文件，无法在 Teamwork 中直接完成配置。

## 目标

- 在 Skill 页面直接新建受管 Skill。
- 在线查看和编辑 Teamwork 管理的 `./skills/<directory>/SKILL.md`。
- 保留导入完整 Skill 文件夹的能力，以及 `scripts`、`references`、`assets` 等配套资源。
- 明确 `description` 与正文的运行时作用：frontmatter 中的 `description` 同时用于 Codex 技能发现和 UI 展示，正文是 Skill 被选中或触发后读取的完整操作说明。

## 存储与权限边界

在线新建的 Skill 写入配置文件同级的 `skills` 目录。目录名由 Skill `name` 规范化生成，配置中继续保存 `./skills/<directory>` 相对路径。

在线编辑只允许作用于 `skills` 目录下的直接子目录，并拒绝符号链接、目录穿越和任意服务端绝对路径。配置为外部路径的 Skill 仍可读取元数据并由 Agent 使用，但在 UI 中保持只读；用户需要在原始位置维护其内容。

新建和编辑只写入根部 `SKILL.md`，不会删除或覆盖同目录中的其他资源。移除配置引用也不删除磁盘目录，避免误删可复用资源。

## 文档格式

新建表单包含：

- 配置 ID：Teamwork 配置和 Agent 引用使用的稳定标识。
- Skill 名称：写入 `SKILL.md` frontmatter 的 `name`，用于 Codex 识别 Skill。
- 描述：写入 frontmatter 的 `description`，用于 Codex 技能发现和 UI 展示。
- 操作说明：写入 frontmatter 后的 Markdown 正文。

编辑已有 Skill 时，后端读取完整 frontmatter，并在保存时只替换 `name` 和 `description`；其他 frontmatter 字段原样保留。正文按用户提交的 Markdown 更新。生成结果必须是 UTF-8，且继续遵守现有 1 MiB 大小限制。

## 交互

Skill 页面在编辑配置模式下提供三个入口：

1. “新建 Skill”：打开右侧编辑抽屉，填写字段后创建受管目录，并把相对路径加入当前配置草稿。
2. “配置已有目录”：保留手工注册服务端路径的能力。
3. “从电脑导入文件夹”：保留完整目录导入能力。

受管 Skill 卡片显示“编辑内容”操作；外部 Skill 显示“外部目录只读”。创建文件成功后，配置引用仍遵循页面现有的配置草稿语义，需要使用页面右上角“保存配置”持久化。即使用户取消配置草稿，新建目录也会保留在“已导入但未配置”区域，避免静默删除内容。

## API

- `POST /api/skill-directories`：新建受管 Skill。
- `GET /api/skill-directories/{directory}/document`：读取受管 Skill 的可编辑文档。
- `PUT /api/skill-directories/{directory}/document`：更新受管 Skill 的根 `SKILL.md`。
- 现有列表和检查响应增加 `managed`、`editable` 与 `directory`，供 UI 区分受管目录和外部目录。

## 非目标

- 不在 UI 中创建或编辑 `scripts`、`references`、`assets` 等多文件资源。
- 不支持在线重命名受管目录。
- 不自动修改 Agent 的 Skill 选择。
- 不在移除配置时删除 Skill 文件。
