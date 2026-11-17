# SMTP/IMAP操作与恢复

## 本地配置

本包只提供通用代码和合成测试，不含联系人表、正文模板、真实邮件或授权秘密。先在自己的私有目录准备输入，将随包 `project.json` 复制到包外 `../private-workspace/config/mail.json`，填写自己的配置后以 `--config` 指定。不要把私有配置写回公开包。

`project_root` 相对配置文件所在目录解析；`roster`、`materials_dir`、`work_dir`、`roster_helper` 相对解析后的project_root。示例按包根与 `private-workspace` 为相邻目录设计；配置复制到私有config目录后，根目录指向private-workspace。roster_helper示例指向相邻公开包内的材料skill，必须按实际安装位置修改。不要直接把随包示例当作已配置环境。

- `sender`：使用者自己的完整发件邮箱。
- `expected_subject`：与套磁信首段一致，末尾 `-` 后为申请人落款姓名。
- `statement_pdf_pages`：审核通过PDF的预期页数，示例为1；按实际模板修改。
- `attachment_overrides`：同名导师使用学校后缀时，按M编号明确指定附件文件名。
- `transport.smtp_host/smtp_port`、`imap_host/imap_port`：邮件服务商公开文档所列的隐式TLS端点，默认465/993；保留证书和主机名校验。当前实现不支持STARTTLS、OAuth或提供商专属认证。

示例sender及主机均为 `example.invalid`。真实连接构造器会拒绝这些占位值；离线测试注入内存传输工厂，不会连接。

## 离线准备

以下命令从包根运行，`python` 应指向已具备依赖的本地环境；配置参数在子命令之前。

```powershell
$mail = './skills/taoci-xin-fasong/scripts/mail_workflow.py'
$config = '../private-workspace/config/mail.json'
python -X utf8 -B $mail --config $config prepare --ids M001 --out '../private-workspace/work/mail/batch-unique'
python -X utf8 -B $mail --config $config check --bundle '../private-workspace/work/mail/batch-unique/M001/bundle.json'
```

`prepare`写入新目录中的plan.json、body.txt、body.html、message.eml及bundle.json，已自动check。源件未变时无需重复运行相同校验。单独 `send_preflight.py` 的prepare只生成plain计划；常规使用完整 `mail_workflow.py`。

新包schema为2。独立原始字节校验器核对CRLF、头部、MIME边界、嵌套结构和base64负载，再与正文、HTML及PDF哈希比对。SMTP与副本补存复用相同邮件字节；不编辑清单来升级旧包。

## 凭据

CLI凭据存储仅支持Windows，使用当前Windows用户的Credential Manager目标 `Codex/Outreach-Public/<完整发件邮箱>`；不同发件账号有不同目标。同账号若需不同项目凭据，需自行规划隔离，不声称进程专属访问。

用户在自己的交互终端运行下列命令，两次隐藏输入服务商授权秘密。不要将秘密发到聊天、命令参数、环境变量、管道或文件。长度检查只做基础输入检查，不证明服务商支持该认证方式。

```powershell
python -X utf8 -B $mail --config $config auth set
python -X utf8 -B $mail --config $config auth status
python -X utf8 -B $mail --config $config probe
```

`auth set --replace`明确更新，`auth remove`仅删除该账号目标。`credential_present`不证明认证成功。probe连接服务商，仅显示稳定错误码和可用SMTP数字码，不记录原始认证回复。不要为导出或离线测试运行这些命令。

## 发送授权

授权记录放在work_dir内，以下仅为结构说明，不能当作现成授权。必须根据当前真实用户指令填写，地址及哈希必须与刚审核的bundle一致。

```json
{
  "action": "send",
  "sender": "sender@example.invalid",
  "user_instruction": "填写真实用户发送指令原文",
  "instruction_reference": "填写该指令的实际定位",
  "allow_resend": false,
  "save_sent_copy": false,
  "allowed": [
    {"advisor": "M001", "to": "advisor@example.invalid", "bundle_sha256": "填写已审核bundle的SHA256"}
  ]
}
```

完成真实配置、材料审核及明确授权后才可执行：

```powershell
python -X utf8 -B $mail --config $config send --bundles '../private-workspace/work/mail/batch-unique/M001/bundle.json' --authorization '../private-workspace/work/mail/batch-unique/authorization.json'
```

默认protocol模式不读取显示审阅文件，仍检查完整源件、MIME、地址、唯一PDF、授权及防重。`--reviews`只能与client模式组合。

只有用户明确要求显示核验时，才添加 `--review-mode client` 和真实审阅记录路径；见 [显示约定](workflow.md)。两种模式均不代替用户发送授权。

## 状态与恢复

| 状态 | 事实与处理 |
|---|---|
| offline_bundle_verified | 仅本地源件和MIME一致；未发送 |
| duplicate | 发现本地接受记录或实时相同目的邮件；跳过，不计新发送 |
| accepted_confirmed | SMTP最终250且Sent解码正文、HTML及唯一PDF一致；非送达或已读回执 |
| accepted_unconfirmed / copy_failed | 有接受事实，副本未完整确认；先核对，不重发 |
| rejected / failed_before_data | 明确拒绝或尚未进入DATA，修复后可在仍有效的原授权内重试 |
| unknown | DATA响应不明，停止该批；不可自动重试 |
| unknown_copy_verified | 未知attempt找到匹配副本，仍缺SMTP接受证据 |
| blocked | 材料、身份、配置、授权或持久化检查失败，停止 |

`work_dir/transport-state/attempts.jsonl`追加并fsync；send.lock防并发。崩溃后核对原进程、事件和邮箱，不直接删锁重发；残缺日志保留原件。

```powershell
python -X utf8 -B $mail --config $config reconcile --bundle '../private-workspace/work/mail/batch-unique/M001/bundle.json'
```

reconcile不发送、不APPEND；已有250且内容匹配才可确认，没有250的未知仍保留未知。明确重发须新用户指令、`allow_resend=true`、新prepare和新Message-ID。

默认copy-mode=verify。只有用户明确授权保存副本且确认服务器不自动留存，才使用save_sent_copy=true与append_if_missing。不要通过追加副本把unknown改为成功。

Sent先识别服务器 `\Sent`，次选实际存在且唯一的常见名称；歧义时指定 `--sent-folder`，不创建新夹。BODY.PEEK避免标记其他邮件已读。连接/非DATA默认30秒，DATA默认600秒；长等待不构成再次发送的理由。

退出码0为命令完成（可含跳过），2为校验/配置/连接错误，3为发送批次未确认/失败/未知而停止，130为中断。
