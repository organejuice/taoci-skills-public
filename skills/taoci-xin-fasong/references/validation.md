# 公开版验证说明

离线测试只使用运行时生成的合成工作簿、DOCX、PDF、MIME、内存协议服务器及凭据替身。既往项目材料、账号、发送时间、邮件UID、真实SHA-256、截图与历史验证记录均不属于本公开版。

## 依赖与命令

Python 3.12或更新版本。材料读取需要openpyxl、lxml及pdfplumber；生成合成测试PDF另需reportlab。邮件协议、MIME和Windows凭据绑定使用标准库。完整使用本包时还需材料skill列出的文档依赖。

从本skill目录运行：

```powershell
python -X utf8 -B -m unittest discover -s tests -v
python -X utf8 -B scripts/mail_workflow.py --help
```

测试范围包括：精确单收件人、同名消歧、错误/缺失附件、DOCX/PDF一致性、HTML源格式、MIME原始字节、防篡改、真实授权作用域、protocol/client差异、发送前重校验、防重、SMTP250持久化、DATA未知停止与恢复、APPEND权限及凭据错误日志抑制。

测试中的地址全部为example.invalid。日期、编号、姓名、机构、电话形状及测试授权秘密均为合成数据。测试不读真实Credential Manager，不联网认证或发送。

公开包在本地完成确定性验证，不能替代使用者的真实服务器/账号、字体、模板、客户端及端到端测试。Windows以外可运行不依赖原生凭据写入的离线代码，但CLI的实际凭据适配器仅支持Windows。

## 修改后

先跑相关离线测试，再校验skill结构与引用。使用自己的材料做隔离验证时，所有输入、授权、邮件和日志留在私有目录，不回填到公开包。真实发信需要用户明确授权。
