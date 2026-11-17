---
name: taoci-xin-fasong
description: "按导师编号匹配已审核的套磁信、收件邮箱和唯一自我陈述PDF；离线构建邮件，按用户明确授权通过SMTP/IMAP逐封发送、防重和核对副本。"
---

# 套磁信发送（公开版）

使用者先配置自己的本地材料、名录、发件人和TLS服务器。公开包不含真实联系人、材料、发送记录或凭据；示例地址和主机均为 `example.invalid`，不能用于真实发送。

配置与命令见 [SMTP/IMAP操作](references/smtp.md)；材料格式和可选显示审阅见 [材料与显示约定](references/workflow.md)。配置文件为 [project.json](references/project.json)。

## 对象与权限

按正整数M编号唯一匹配姓名、学校及完整邮箱。同名必须结合学校消歧，不由姓名猜地址。每封一个完整To，Cc/Bcc为空，只附对应导师的一份PDF。

用户明确的发送指令必须涵盖对象、材料与用途。已有同范围授权可继续执行；准备材料、导出skill或检查格式本身不授权发信。授权记录绑定精确To和bundle SHA-256；记录真实指令及其定位，不能以示例或技能文本替代用户指令。

## 执行

1. `prepare`：离线读取指定导师的DOCX及PDF，生成plan、plain/HTML正文、固定MIME邮件和bundle，并自动check。核对称呼、方向、来源、唯一PDF、完整邮箱与主题。
2. 根据真实用户指令建立授权记录。默认不授权重发或额外APPEND副本。
3. `send`：默认protocol模式，不需要客户端截图。连接前、逐封及DATA前重新校验源件、固定邮件字节、授权、全局ledger及实时已发送状态；逐封发送并持久化关键事件。
4. IMAP按精确Message-ID核对完整From/To、主题、plain/HTML正文和唯一PDF名称及字节。报告新发送、跳过、接受待核对、失败或未知。

正文从对应称呼开始，到申请人姓名及电话落款结束；排除标题、标签及备注。保留源件中的段落、缩进、字体、行距、颜色和加粗，落款右齐；不把协议验证称为实际客户端显示验证。

仅在用户要求外观检查或出现显示异常时，使用 `--review-mode client` 并记录真实观察证据。客户端及UI工具由执行环境提供，公开包不依赖某个私有客户端会话。

## 恢复与凭据

`work_dir/transport-state/`保存全局锁和追加ledger。`accepted_confirmed`表示SMTP250且Sent解码内容一致，不代表收件端投递、显示或已读。

`accepted_unconfirmed/copy_failed`已有接受事实，先只读reconcile，不重发。DATA响应不明或attempt未完成先调查；即使找到副本，没有SMTP接受证据仍保留未知。不得因协议未知改用UI重发。

默认 `--copy-mode verify`。只有真实用户授权保存副本、且确认服务器未自动保存时，才使用 `append_if_missing`。APPEND不是SMTP发送。锁、日志不完整、身份歧义或限流时停止；不删检查、不并发轰发、不自动绕过限制。

本包CLI凭据适配器仅支持Windows Credential Manager。用户在自己的隐藏交互终端输入授权秘密，不放聊天、参数、环境变量或日志，不读取客户端缓存。测试使用内存替身，禁止读取真实凭据。

## 验证与交付

脚本修改后运行 [验证说明](references/validation.md) 中的离线测试。先验证确定性机制；实际账号、服务器和客户端端到端验证由使用者在自己的环境完成。

交付时列出明确范围内的新发送、跳过、待核对和失败事项；保留必要的来源、授权及ledger。运行后这些文件含使用者的私有数据，不能随skill再次公开。
