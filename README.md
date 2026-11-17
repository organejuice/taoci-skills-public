# 套磁材料与邮件发送 Skills

两套可配合使用的 Codex skill：依据自己的导师名录和模板定制博士申请材料，完成文案与版式审核后，再准备邮件并按明确授权发送。

| Skill | 功能 |
| --- | --- |
| [taoci-yu-ziwo-chenshu](skills/taoci-yu-ziwo-chenshu/SKILL.md) | 核验导师及研究证据，保留模板版式，生成套磁信 DOCX、自我陈述 DOCX 和 PDF，逐页审阅后交付 |
| [taoci-xin-fasong](skills/taoci-xin-fasong/SKILL.md) | 按 M 编号匹配收件人、正文和唯一 PDF，离线构建邮件，通过 SMTP/IMAP 逐封发送、防重及核对副本 |

[下载原始匿名版 ZIP](downloads/taoci-skills-public.zip) · [ZIP 的 SHA-256](downloads/taoci-skills-public.zip.sha256) · [使用指南](使用指南.md) · [公开检查报告](公开检查报告.md)

## 安装

将 `skills/` 内两个完整文件夹并列复制到自己的项目 `.agents/skills/`，保留脚本与参考文档：

```text
你的项目/
  .agents/skills/
    taoci-yu-ziwo-chenshu/
    taoci-xin-fasong/
```

重新打开项目后调用 skill。已有同名安装时，先保存旧版，避免混用配置。

## 使用流程

1. 自备导师名录、套磁信与自我陈述模板，将两份示例配置复制到私有工作目录后填写。串联两个 skill 时使用 `M001` 至 `M999` 编号，并按[使用指南](使用指南.md)对齐材料目录、模板格式和附件名称。
2. 核实导师身份、研究方向和论文证据，完成 `list → inspect → build → render → 逐页审阅 → qa → publish`，交付每位导师的三份材料。
3. 使用 `prepare` 离线生成邮件，核对完整收件地址、主题、正文和唯一 PDF。
4. 用户明确授权对象、材料与用途后，建立授权记录并发送；状态不明时先核对和恢复，不自动重发。

个人模板、名录、凭据和运行记录保存在私有目录。包内配置与 `.invalid` 地址用于说明格式，使用前替换为自己的资料。

## 示例调用

```text
使用 $taoci-yu-ziwo-chenshu，为我名录中的 M002 准备套磁信和自我陈述。先检查我的配置与模板，仅起草材料。
```

```text
使用 $taoci-xin-fasong，为 M002 离线准备邮件，展示收件人、主题、正文和唯一 PDF，暂不发送。
```

详细配置、命令参数、模板合同及恢复操作见[使用指南](使用指南.md)和各 skill 的 `references/`。

## 本地依赖

- 完整工具包使用 Python 3.12 或以上；材料处理使用 `lxml`、`openpyxl`、`pdfplumber`。
- 默认 DOCX 导出使用 Windows、PowerShell 和已安装的 Microsoft Word；PDF 页图使用 `pypdfium2`、Pillow。其他渲染器需自行配置并逐页验证。
- 邮件发送需要服务商支持的隐式 TLS SMTP/IMAP 端点；凭据 CLI 使用 Windows 凭据管理器。
- 合成文档测试另需 `python-docx`、`reportlab`。检查现有环境后复用兼容依赖，验证版本与范围见[公开检查报告](公开检查报告.md)。

默认协议模式核验邮件内容、附件及服务器副本；客户端实际显示需通过可选的 `client` 模式观察。真实发送始终以用户的明确授权为前提。

## 赞助支持

如果这两个 skill 帮助你减少了材料整理和邮件核对的工作，欢迎通过微信自愿赞助，支持后续维护与改进。

<a href="assets/sponsor/wechat-pay.jpg"><img src="assets/sponsor/wechat-pay.jpg" alt="微信赞助收款码" width="320"></a>

可使用微信扫描或识别图片；点击图片可查看完整尺寸。感谢支持。
