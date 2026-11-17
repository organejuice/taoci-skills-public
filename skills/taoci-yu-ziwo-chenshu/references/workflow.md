# 执行说明

## 环境与私有配置

复用用户已有且依赖兼容的 Python 环境。脚本使用 Python 3.10 或以上；运行依赖 `lxml`、`openpyxl`、`pdfplumber`。默认 Word 渲染还需要 Windows、已安装 Microsoft Word、可发现的 `pwsh` 或 `powershell`、`pypdfium2` 及 Pillow。合成测试另需 `python-docx` 和 `reportlab`。本包不安装任何依赖。

将 `references/project.json` 复制到公开包以外的私有工作区，例如 `private-workspace/config/materials.json`。示例 `project_root: ".."` 相对配置文件所在目录，其他输入输出路径相对 `project_root`。示例对应：

```text
private-workspace/
  config/materials.json
  data/roster.xlsx
  templates/letter.docx
  templates/statement.docx
  templates/statement.pdf
  work/materials/
  outputs/materials/
```

修改 `baseline_advisor`、`baseline_direction` 以准确对应自备模板红字；需要精确删除特定开头时才配置 `statement_opening_deletions`。150–200 字仅为可调容量目标，不是科学阈值。不得直接以匿名示例身份或来源生产正式申请材料。

名录为 XLSX，首行为列名，至少包含：

| 工作表 | 必需列 |
| --- | --- |
| 导师联系表 | 学院、院系、导师、邮箱、研究方向、是否发送邮件、回复状态 |
| 方向与招生核验 | 编号、学校、院系、导师、官方及原始来源 |

“导师联系表”的“学院”与核验表的“学校”共同作为机构字段，须一致；联合院系及导师姓名关联。核验表编号使用正整数 M 编号。脚本拒绝重复身份或等价数字编号，不把缺少联系记录解释为“未发送”。

## 命令

以下命令在公开包根目录运行，`python` 指当前兼容环境。先确认目标范围：

```powershell
$scriptPath = '.\skills\taoci-yu-ziwo-chenshu\scripts\workflow.py'
$configPath = '..\private-workspace\config\materials.json'
python -B $scriptPath --config $configPath list --id M002
python -B $scriptPath --config $configPath inspect
```

也支持 `list --name 示例导师 --school 示例机构` 或 `list --start 2 --end 5`；只在用户明确要求全表时使用 `list --all`。复制到其他安装目录时相应更新 `$scriptPath`。

核实证据并完成私有 `content.json` 后：

```powershell
python -B $scriptPath --config $configPath build --content '..\private-workspace\work\materials\content.json'
python -B $scriptPath --config $configPath render --manifest '<build 返回的暂存目录>\manifest.json'
# 打开 render 返回的每张页图，完成真实视觉检查后填写 visual-review.json。
python -B $scriptPath --config $configPath qa --manifest '<暂存目录>\manifest.json' --receipt '<暂存目录>\visual-review.json'
python -B $scriptPath --config $configPath publish --manifest '<暂存目录>\manifest.json'
```

`--config` 是全局参数，放在子命令前。`build` 输出给出本次暂存目录与成品路径；不要把尖括号中的说明直接当路径运行。`render` 返回所有页图及回执字段，真实看图后填写 reviewer、实际 reviewed_at、passed、完整 images 哈希和观察 notes。任何文件或渲染变更都要重新检查。

`publish` 重新核对验收、源文件及全部成品，不覆盖原模板或不同的既有结果。完全相同的结果跳过，缺失项补齐。修改文案应建立新构建，不手工改暂存 DOCX 绕过 build 哈希。失败时读取本次 `render-error.json`，检查字体、依赖及转换问题；不能缺少渲染或验收便交付。

## 渲染后端

默认 `runtime: {"render_backend": "word"}` 使用随包 `word_export.ps1` 创建独立隐藏 Word 实例，只读打开 DOCX，导出 PDF，关闭而不回写源件，再生成全部页图。须使用用户自己已验证的 Office 与字体环境。

可选 `render_backend: "external"` 由用户提供兼容的 `runtime.render_docx` 脚本路径，`runtime.python` 可选，默认当前解释器；可选 `runtime.office` 指向已有 LibreOffice 可执行程序。外部脚本须接受 `input.docx --output_dir DIR --emit_pdf` 并产生一个 PDF 与完整的 `page-1.png` 等页图。本包不附带外部 renderer，也不依赖某个私有插件安装路径。首次切换后端时重新对照真实模板，避免分页漂移。

当前流程复用 OOXML、openpyxl、pdfplumber 与 Word/PDFium 等现有组件，没有复制第三方源码。合成测试验证机制，实际论文核验、视觉检查和本机 Office 可用性仍需每位使用者完成。
